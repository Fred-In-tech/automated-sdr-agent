"""The results funnel: first emails -> delivered -> replied -> interested -> meeting -> won."""

from core import db, followups
from core.funnel import compute, funnel_lines
from test_followups import FollowupsCase, TODAY


class TestFunnel(FollowupsCase):
    def sent(self, lead_id, step=1):
        conn = db.get_connection()
        conn.execute("INSERT INTO email_logs (lead_id, lead_email, subject, body, status, sent_at, step) "
                     "VALUES (?, 'x', 's', 'b', 'sent', '2026-10-01', ?)", (lead_id, step))
        conn.commit()
        conn.close()

    def reply(self, lead_id, intent):
        conn = db.get_connection()
        conn.execute("INSERT INTO inbound_messages (message_id, lead_id, from_email, intent, received_at) "
                     "VALUES (?, ?, 'x', ?, '2026-10-02')", (f"<{lead_id}{intent}>", lead_id, intent))
        conn.commit()
        conn.close()

    def status(self, lead_id, status):
        conn = db.get_connection()
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        conn.commit()
        conn.close()

    def test_counts_and_rates(self):
        leads = [self.lead(f"l{i}@x.test", f"L{i}") for i in range(10)]
        for lead in leads:
            self.sent(lead)
        self.sent(leads[0], step=2)                     # follow-ups don't count twice
        self.status(leads[9], "bounced")
        for lead in leads[:4]:
            self.reply(lead, "other")
        self.reply(leads[0], "interested")
        self.reply(leads[1], "question")
        meeting = followups.ensure_deal(leads[0], today=TODAY)
        followups.update(meeting, stage="meeting", today=TODAY)
        won = followups.ensure_deal(leads[1], today=TODAY)
        followups.complete(won, outcome="won", value=900, today=TODAY)
        f = compute()
        counts = [(s["key"], s["count"]) for s in f["steps"]]
        self.assertEqual(counts, [("sent", 10), ("delivered", 9), ("replied", 4), ("interested", 2),
                                  ("meeting", 2), ("won", 1)])
        self.assertEqual(f["steps"][2]["rate"], 44.4)     # 4 of 9 delivered
        self.assertEqual(f["steps"][5]["of_sent"], 10.0)
        self.assertEqual(f["won_value"], 900.0)
        text = "\n".join(funnel_lines(f))
        self.assertIn("Delivered", text)
        self.assertIn("Won", text)

    def test_empty(self):
        f = compute()
        self.assertEqual([s["count"] for s in f["steps"]], [0, 0, 0, 0, 0, 0])
        self.assertIn("No first emails yet", "\n".join(funnel_lines(f)))


class TestInReportAndDigest(FollowupsCase):
    def test_report_has_results(self):
        from core.report import pipeline_stats, render_report
        text = render_report(pipeline_stats({}), "Brightline", {})
        self.assertIn("RESULTS (built on replies, never on opens)", text)

    def test_digest_mentions_a_deliverability_problem(self):
        from unittest import mock
        from bots import digest
        from core import deliverability
        sent = {}

        class Engine:
            def __init__(self, profile):
                pass

            def send_real_email(self, to, subject, html, text, headers=None):
                sent["text"] = text
                return True
        problem = {"status": "problem", "domain": "brightline.example",
                   "checks": [{"key": "dkim", "label": "DKIM", "status": "problem", "detail": "No DKIM record found."}]}
        with mock.patch.object(digest, "EmailMarketingEngine", Engine), \
                mock.patch.object(deliverability, "load", return_value=problem), \
                mock.patch.object(digest, "pipeline_stats", return_value={"hot_leads": [], "sent_today": 0}), \
                mock.patch.object(digest, "render_report", return_value="REPORT"), \
                mock.patch.object(digest, "load_env_file", lambda: None):
            digest.send_daily_digest({"sender": {"product_name": "B"}, "sdr": {"alert_email": "me@x.test"}}, force=True)
        self.assertIn("DELIVERABILITY (brightline.example)", sent["text"])
        self.assertIn("limited to 5 a day", sent["text"])
