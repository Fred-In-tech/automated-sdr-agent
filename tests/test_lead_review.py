"""Borderline leads wait for the owner's OK before their first email."""

import io
from datetime import datetime, timedelta, timezone
from unittest import mock

from bots import leadgen_pipeline
from core import db, lead_review
from core.qualify import score_lead
from core.tui import UI
from test_sequence import SequenceTestCase

SITE_TEXT = "wedding planner packages pricing quote wedding planner weddings planner"


class TestBand(SequenceTestCase):
    def profile_with(self, **targeting):
        profile = dict(self.profile)
        profile["targeting"] = {**self.profile["targeting"], "ideal_client": "wedding planner",
                                "professions": ["wedding planner"], **targeting}
        return profile

    def test_score_band(self):
        profile = self.profile_with(min_fit_score=40, auto_send_score=70)
        clear = score_lead(profile, "ana@bloom.test", "bloom.test", SITE_TEXT + " Ana", "Bloom")
        border = score_lead(profile, "info@bloom.test", "bloom.test", "wedding planner", "Bloom")
        self.assertFalse(clear["needs_review"])
        self.assertTrue(border["qualified"])
        self.assertTrue(border["needs_review"])
        self.assertTrue(any("review" in r for r in border["reasons"]))

    def test_borderline_leads_are_saved_for_review_and_never_emailed(self):
        profile = self.profile_with(min_fit_score=30, auto_send_score=95)
        site = {"emails": ["info@bloom.test"], "domain": "bloom.test", "site_name": "Bloom", "title": "Bloom",
                "website": "https://bloom.test", "page_text": SITE_TEXT}
        with mock.patch.object(leadgen_pipeline, "verify_lead_email", lambda e: (True, "ok")):
            leadgen_pipeline.reset_ai_runs()
            lead = leadgen_pipeline.qualify_site(profile, site, source="t", location="Austin, TX",
                                                 category="wedding planner", title="Owner")
        self.assertEqual(lead["status"], "review")
        lead_id = leadgen_pipeline.save_leads([lead], "wedding planner")[0]["id"]
        self.engine.run_outreach_campaign()
        self.assertEqual(self.sent, [])
        self.assertEqual([d["id"] for d in lead_review.pending()], [lead_id])


class TestDecisions(SequenceTestCase):
    def review_lead(self, email="info@bloom.test", days_old=0):
        lead_id = self.add_lead(email, 55)
        created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        conn = db.get_connection()
        conn.execute("UPDATE leads SET status = 'review', fit_reasons = ?, created_at = ? WHERE id = ?",
                     ('["site mentions your ICP 2x", "shared studio inbox (info@)"]', created, lead_id))
        conn.commit()
        conn.close()
        return lead_id

    def test_pending_shows_why(self):
        lead_id = self.review_lead()
        item = lead_review.pending()[0]
        self.assertEqual((item["id"], item["fit_score"]), (lead_id, 55))
        self.assertIn("shared studio inbox (info@)", item["reasons"])

    def test_approve_sends_with_the_next_run(self):
        lead_id = self.review_lead()
        lead_review.decide(lead_id, "approve")
        self.assertEqual(self.lead(lead_id)["status"], "new")
        self.engine.run_outreach_campaign()
        self.assertEqual([m["to"] for m in self.sent], ["info@bloom.test"])

    def test_reject(self):
        lead_id = self.review_lead()
        lead_review.decide(lead_id, "reject")
        self.assertEqual(self.lead(lead_id)["status"], "disqualified")
        self.assertIn("rejected by you", self.lead(lead_id)["fit_reasons"])

    def test_bad_decisions(self):
        lead_id = self.review_lead()
        with self.assertRaises(ValueError):
            lead_review.decide(lead_id, "maybe")
        lead_review.decide(lead_id, "reject")
        with self.assertRaises(LookupError):
            lead_review.decide(lead_id, "approve")

    def test_unreviewed_for_a_week_is_skipped(self):
        old = self.review_lead("old@a.test", days_old=8)
        fresh = self.review_lead("new@b.test", days_old=1)
        self.engine.run_outreach_campaign()          # expiry runs at the start of the email step
        self.assertEqual(self.lead(old)["status"], "disqualified")
        self.assertIn("not reviewed", self.lead(old)["fit_reasons"])
        self.assertEqual(self.lead(fresh)["status"], "review")

    def test_review_command(self):
        from core.cli_review import cmd_review
        self.review_lead()
        out = io.StringIO()
        ui = UI(plain=True, interactive=False, out=out, environ={})
        self.assertEqual(cmd_review(ui), 0)
        self.assertIn("info@bloom.test", out.getvalue())
        self.assertIn("dashboard", out.getvalue())
