"""Follow-ups in the daily digest and in `sdr followups`."""

import io
from datetime import date
from unittest import mock

from bots import digest
from core import followups
from core.cli_followups import cmd_followups
from core.tui import UI
from test_followups import FollowupsCase

TODAY = date(2026, 10, 21)


def plain_ui():
    out = io.StringIO()
    return UI(plain=True, interactive=False, out=out, environ={}), out


class TestCommand(FollowupsCase):
    def test_lists_today(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        followups.update(deal_id, due="2026-10-20", next_step="Call Ivy", today=TODAY)
        ui, out = plain_ui()
        self.assertEqual(cmd_followups(ui, today=TODAY), 0)
        text = out.getvalue()
        self.assertIn("Call Ivy", text)
        self.assertIn("Overdue", text)
        self.assertIn("dashboard", text)

    def test_nothing_yet(self):
        ui, out = plain_ui()
        cmd_followups(ui, today=TODAY)
        self.assertIn("No follow-ups yet", out.getvalue())


class TestDigest(FollowupsCase):
    def test_digest_has_a_follow_ups_section(self):
        deal_id = followups.ensure_deal(self.lead(), today=TODAY)
        followups.update(deal_id, due="2026-10-20", next_step="Call Ivy", today=TODAY)
        sent = {}

        class Engine:
            def __init__(self, profile):
                pass

            def send_real_email(self, to, subject, html, text, headers=None):
                sent.update(subject=subject, text=text)
                return True
        profile = {"sender": {"product_name": "Brightline"}, "sdr": {"alert_email": "me@x.test"}}
        real_lines = followups.digest_lines
        with mock.patch.object(digest, "EmailMarketingEngine", Engine), \
                mock.patch.object(digest, "pipeline_stats", return_value={"hot_leads": [], "sent_today": 0}), \
                mock.patch.object(digest, "render_report", return_value="REPORT"), \
                mock.patch.object(digest, "load_env_file", lambda: None), \
                mock.patch.object(digest.followups, "digest_lines", lambda: real_lines(today=TODAY)):
            digest.send_daily_digest(profile, force=True)
        self.assertIn("FOLLOW-UPS DUE", sent["text"])
        self.assertIn("Call Ivy", sent["text"])
        self.assertIn("1 follow-up due", sent["subject"])
