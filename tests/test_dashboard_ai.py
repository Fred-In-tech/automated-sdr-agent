"""Dashboard AI endpoints: the Needs-you review queue and the Test connection button."""

import json
from unittest import mock

from core import db
from core.ai import drafts
from test_dashboard import DashboardServerCase


class AICase(DashboardServerCase):
    password = None

    def add_lead(self) -> int:
        conn = db.get_connection()
        cur = conn.execute("INSERT INTO leads (name, company, email, first_name, created_at) VALUES "
                           "('Ivy', 'Lantern Hall', 'ivy@lanternhall.test', 'Ivy', '2026-10-01')")
        conn.commit()
        conn.close()
        return cur.lastrowid


class TestDraftsAPI(AICase):
    def test_lists_pending_drafts_with_the_lead(self):
        lead = self.add_lead()
        drafts.add_draft(lead, "answer", "Plans start at $29 a month.")
        status, data = self.get_json("/api/ai/drafts")
        self.assertEqual(status, 200)
        item = data["drafts"][0]
        self.assertEqual((item["kind"], item["company"], item["email"], item["body"]),
                         ("answer", "Lantern Hall", "ivy@lanternhall.test", "Plans start at $29 a month."))
        self.assertIn("notice", data["ai"])

    def test_approve_skip_and_edit(self):
        lead = self.add_lead()
        a = drafts.add_draft(lead, "answer", "Plans start at $29 a month.")
        status, _h, body = self.post_json("/api/ai/drafts", {"id": a, "action": "approve"})
        self.assertEqual((status, json.loads(body)["status"]), (200, "approved"))
        b = drafts.add_draft(lead, "opener", "Your rooftop venue in East Austin looks lovely.")
        status, _h, body = self.post_json("/api/ai/drafts", {"id": b, "action": "edit",
                                                             "body": "Your rooftop ceremonies in East Austin look calm."})
        self.assertEqual((status, json.loads(body)["status"]), (200, "edited"))
        c = drafts.add_draft(lead, "answer", "x")
        status, _h, body = self.post_json("/api/ai/drafts", {"id": c, "action": "skip"})
        self.assertEqual(json.loads(body)["status"], "skipped")
        self.assertEqual(self.get_json("/api/ai/drafts")[1]["drafts"], [])

    def test_edited_text_is_checked(self):
        lead = self.add_lead()
        a = drafts.add_draft(lead, "opener", "Your rooftop venue in East Austin looks lovely.")
        status, _h, body = self.post_json("/api/ai/drafts", {"id": a, "action": "edit",
                                                             "body": "I hope this finds you well, see https://x.test"})
        self.assertEqual(status, 400)
        self.assertIn("link", json.loads(body)["message"])
        self.assertTrue(drafts.has_pending(lead, "opener"))

    def test_bad_requests(self):
        for payload, code in (({"id": 999, "action": "approve"}, 404), ({"id": "x", "action": "approve"}, 400),
                              ({"action": "approve"}, 400)):
            status, _h, _b = self.post_json("/api/ai/drafts", payload)
            self.assertEqual(status, code, payload)

    def test_script_is_served(self):
        status, headers, body = self.get("/ai.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertNotIn(b"innerHTML", body)


class TestConnectionAPI(AICase):
    def test_runs_the_shared_check(self):
        fake = {"ok": True, "json_ok": True, "message": "Working", "notice": "nothing leaves this computer",
                "models": ["llama3.2"], "latency_ms": 12, "provider": "Ollama", "model": "llama3.2"}
        with mock.patch("dashboard.ai_routes.check_connection", return_value=fake) as check:
            status, _h, body = self.post_json("/api/ai/test", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["message"], "Working")
        check.assert_called_once()

    def test_ai_off(self):
        status, _h, body = self.post_json("/api/ai/test", {})
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("setup --section ai", data["message"])


class TestProtectedDashboard(DashboardServerCase):
    def test_needs_login_and_csrf(self):
        status, _h, _b = self.post_json("/api/ai/test", {})
        self.assertEqual(status, 401)
        token = self.login()
        status, _h, _b = self.post_json("/api/ai/test", {}, self.cookie(token))
        self.assertEqual(status, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()
