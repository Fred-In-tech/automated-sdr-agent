"""Follow-ups: one open deal per replying lead, always with one dated next step."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from core import db, followups

TODAY = date(2026, 10, 21)          # a Wednesday


class FollowupsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "t.db")),
                        mock.patch.object(db, "DB_DIR", self.tmp.name)):
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def lead(self, email="ivy@lanternhall.test", first="Ivy", company="Lantern Hall") -> int:
        conn = db.get_connection()
        cur = conn.execute("INSERT INTO leads (name, company, email, first_name, created_at) VALUES (?,?,?,?,?)",
                           (company, company, email, first, "2026-10-01"))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def deal(self, deal_id):
        return next(d for d in followups.all_deals(today=TODAY) if d["id"] == deal_id)


class TestCreate(FollowupsCase):
    def test_reply_opens_a_deal_due_tomorrow(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        d = self.deal(deal_id)
        self.assertEqual((d["stage"], d["due"], d["company"]), ("replied", "2026-10-22", "Lantern Hall"))
        self.assertIn("Ivy", d["next_step"])

    def test_never_duplicated(self):
        lead = self.lead()
        first = followups.ensure_deal(lead, today=TODAY)
        self.assertEqual(followups.ensure_deal(lead, today=TODAY), first)
        self.assertEqual(len(followups.all_deals(today=TODAY)), 1)

    def test_closed_deal_is_not_reopened(self):
        lead = self.lead()
        deal_id = followups.ensure_deal(lead, today=TODAY)
        followups.complete(deal_id, outcome="lost", today=TODAY)
        self.assertEqual(followups.ensure_deal(lead, today=TODAY), deal_id)
        self.assertEqual(self.deal(deal_id)["stage"], "lost")

    def test_inside_a_callers_transaction(self):
        lead = self.lead()
        conn = db.get_connection()
        deal_id = followups.ensure_deal(lead, cursor=conn.cursor(), today=TODAY)
        conn.commit()
        conn.close()
        self.assertEqual(self.deal(deal_id)["lead_id"], lead)

    def test_name_falls_back_to_company(self):
        deal_id = followups.ensure_deal(self.lead(first=None), today=TODAY)
        self.assertIn("Lantern Hall", self.deal(deal_id)["next_step"])


class TestToday(FollowupsCase):
    def test_order_overdue_today_none_upcoming(self):
        ids = {}
        for key, due in (("upcoming", "2026-10-25"), ("today", "2026-10-21"), ("overdue", "2026-10-19"), ("none", None)):
            ids[key] = followups.ensure_deal(self.lead(f"{key}@x.test", key.title()), today=TODAY)
            followups.update(ids[key], due=due, next_step=None if due is None else "Call", today=TODAY)
        listed = followups.today_list(today=TODAY)
        self.assertEqual([d["id"] for d in listed], [ids["overdue"], ids["today"], ids["none"], ids["upcoming"]])
        self.assertEqual([d["bucket"] for d in listed], ["overdue", "today", "none", "upcoming"])

    def test_closed_deals_are_not_listed(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        followups.complete(deal_id, outcome="won", value=1200, today=TODAY)
        self.assertEqual(followups.today_list(today=TODAY), [])

    def test_stale_after_five_days_in_a_stage(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        self.assertFalse(self.deal(deal_id)["stale"])
        later = TODAY + timedelta(days=5)
        self.assertTrue(next(d for d in followups.today_list(today=later) if d["id"] == deal_id)["stale"])


class TestActions(FollowupsCase):
    def test_done_asks_for_the_next_step(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        with self.assertRaises(ValueError):
            followups.complete(deal_id, today=TODAY)
        followups.complete(deal_id, next_step="Send proposal", due="2026-10-24", stage="proposal", today=TODAY)
        d = self.deal(deal_id)
        self.assertEqual((d["next_step"], d["due"], d["stage"]), ("Send proposal", "2026-10-24", "proposal"))

    def test_won_and_lost_close_the_deal(self):
        won = followups.ensure_deal(self.lead("a@x.test"), today=TODAY)
        followups.complete(won, outcome="won", value="1,200", today=TODAY)
        d = self.deal(won)
        self.assertEqual((d["stage"], d["value"], d["next_step"], d["due"]), ("won", 1200.0, None, None))
        self.assertIsNotNone(d["closed_at"])

    def test_snooze(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        self.assertEqual(followups.snooze(deal_id, "tomorrow", today=TODAY)["due"], "2026-10-22")
        self.assertEqual(followups.snooze(deal_id, "3days", today=TODAY)["due"], "2026-10-24")
        self.assertEqual(followups.snooze(deal_id, "monday", today=TODAY)["due"], "2026-10-26")
        self.assertEqual(followups.snooze(deal_id, "monday", today=date(2026, 10, 26))["due"], "2026-11-02")
        with self.assertRaises(ValueError):
            followups.snooze(deal_id, "someday", today=TODAY)

    def test_update_validation(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        for bad in ({"stage": "party"}, {"value": "-5"}, {"value": "lots"}, {"due": "next week"},
                    {"next_step": "x" * 201}, {"notes": "x" * 2001}):
            with self.assertRaises(ValueError, msg=bad):
                followups.update(deal_id, today=TODAY, **bad)
        followups.update(deal_id, stage="meeting", value="2500", notes="Wants the premium plan", today=TODAY)
        d = self.deal(deal_id)
        self.assertEqual((d["stage"], d["value"], d["notes"]), ("meeting", 2500.0, "Wants the premium plan"))

    def test_unknown_deal(self):
        with self.assertRaises(LookupError):
            followups.update(999, stage="meeting", today=TODAY)


class TestNumbers(FollowupsCase):
    def test_summary_and_revenue_per_100_emails(self):
        a = followups.ensure_deal(self.lead("a@x.test"), today=TODAY)
        b = followups.ensure_deal(self.lead("b@x.test"), today=TODAY)
        followups.ensure_deal(self.lead("c@x.test"), today=TODAY)
        followups.update(b, value=500, today=TODAY)
        followups.complete(a, outcome="won", value=1200, today=TODAY)
        conn = db.get_connection()
        for i in range(40):
            conn.execute("INSERT INTO email_logs (lead_id, lead_email, subject, body, status, sent_at, step) "
                         "VALUES (?, ?, 's', 'b', 'sent', '2026-10-01', 1)", (i + 100, f"l{i}@x.test"))
        conn.execute("INSERT INTO email_logs (lead_id, lead_email, subject, body, status, sent_at, step) "
                     "VALUES (1, 'a@x.test', 's', 'b', 'sent', '2026-10-05', 2)")
        conn.commit()
        conn.close()
        s = followups.summary(today=TODAY)
        self.assertEqual((s["open"], s["won"], s["won_value"], s["pipeline_value"]), (2, 1, 1200.0, 500.0))
        self.assertEqual(s["revenue_per_100_emails"], 3000.0)       # $1,200 from 40 first emails

    def test_digest_lines(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        followups.update(deal_id, due="2026-10-20", next_step="Call Ivy", today=TODAY)
        lines = followups.digest_lines(today=TODAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("Call Ivy", lines[0])
        self.assertIn("overdue", lines[0])
        self.assertEqual(followups.digest_lines(today=date(2026, 10, 1)), [])


if __name__ == "__main__":
    unittest.main()
