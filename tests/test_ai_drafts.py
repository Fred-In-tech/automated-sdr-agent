"""The review queue: AI drafts wait for the owner's Approve / Edit / Skip."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core import db
from core.ai import drafts


class DraftsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "t.db")),
                        mock.patch.object(db, "DB_DIR", self.tmp.name)):
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO leads (name, company, email, first_name, created_at) VALUES "
                     "('Ivy', 'Lantern Hall', 'ivy@lanternhall.example', 'Ivy', '2026-10-01')")
        conn.commit()
        self.lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
        conn.close()

    def test_add_and_list_pending(self):
        draft_id = drafts.add_draft(self.lead_id, "opener", "Your rooftop venue looks great.")
        items = drafts.pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], draft_id)
        self.assertEqual(items[0]["company"], "Lantern Hall")
        self.assertEqual(items[0]["email"], "ivy@lanternhall.example")
        self.assertFalse(items[0]["stale"])
        self.assertTrue(drafts.has_pending(self.lead_id, "opener"))

    def test_new_draft_replaces_older_pending_one_of_the_same_kind(self):
        drafts.add_draft(self.lead_id, "opener", "first")
        drafts.add_draft(self.lead_id, "opener", "second")
        drafts.add_draft(self.lead_id, "answer", "an answer", {"subject": "Re: hi"})
        bodies = sorted(d["body"] for d in drafts.pending())
        self.assertEqual(bodies, ["an answer", "second"])
        self.assertEqual(next(d for d in drafts.pending() if d["kind"] == "answer")["context"], {"subject": "Re: hi"})

    def test_stale_after_two_hours(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with mock.patch.object(drafts, "_now", return_value=old):
            drafts.add_draft(self.lead_id, "answer", "x")
        self.assertTrue(drafts.pending()[0]["stale"])

    def test_approve_edit_skip(self):
        a = drafts.add_draft(self.lead_id, "opener", "original")
        self.assertEqual(drafts.decide(a, "edit", "edited text")["status"], "edited")
        self.assertEqual(drafts.approved_for(self.lead_id, "opener")["body"], "edited text")
        self.assertFalse(drafts.has_pending(self.lead_id, "opener"))
        drafts.mark_sent(a)
        self.assertIsNone(drafts.approved_for(self.lead_id, "opener"))

        b = drafts.add_draft(self.lead_id, "answer", "answer")
        self.assertEqual(drafts.decide(b, "approve")["status"], "approved")
        c = drafts.add_draft(self.lead_id, "opener", "another")
        self.assertEqual(drafts.decide(c, "skip")["status"], "skipped")
        self.assertIsNone(drafts.approved_for(self.lead_id, "opener"))
        self.assertEqual(drafts.pending(), [])

    def test_bad_decisions(self):
        a = drafts.add_draft(self.lead_id, "opener", "x")
        with self.assertRaises(ValueError):
            drafts.decide(a, "explode")
        with self.assertRaises(ValueError):
            drafts.decide(a, "edit", "   ")
        with self.assertRaises(LookupError):
            drafts.decide(9999, "approve")
        drafts.decide(a, "skip")
        with self.assertRaises(LookupError):          # already decided
            drafts.decide(a, "approve")

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            drafts.add_draft(self.lead_id, "poem", "x")

    def test_approved_answers_to_send(self):
        a = drafts.add_draft(self.lead_id, "answer", "Plans start at $29.", {"subject": "Re: q", "message_id": "<m1>"})
        drafts.decide(a, "approve")
        ready = drafts.approved(kind="answer")
        self.assertEqual([(d["id"], d["context"]["message_id"]) for d in ready], [(a, "<m1>")])


if __name__ == "__main__":
    unittest.main()
