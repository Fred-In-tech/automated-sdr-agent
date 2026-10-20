"""Dashboard: deliverability card + check again, results funnel, and borderline lead reviews."""

import json
from unittest import mock

from core import db
from core import deliverability as dv
from test_dashboard import DashboardServerCase

RESULT = {"status": "problem", "domain": "brightline.example", "checked_at": "2026-10-20T09:00:00+00:00",
          "checks": [{"key": "dkim", "label": "DKIM", "status": "problem", "detail": "No DKIM.", "fix": "Turn it on.",
                      "record": ""}]}


class InsightsCase(DashboardServerCase):
    password = None

    def review_lead(self) -> int:
        conn = db.get_connection()
        cur = conn.execute("INSERT INTO leads (name, company, email, fit_score, fit_reasons, status, created_at) VALUES "
                           "('Bloom', 'Bloom', 'info@bloom.test', 55, '[\"shared inbox\"]', 'review', '2099-01-01')")
        conn.commit()
        conn.close()
        return cur.lastrowid


class TestInsights(InsightsCase):
    def test_overview_data(self):
        with mock.patch.object(dv, "load", return_value=RESULT):
            status, data = self.get_json("/api/insights")
        self.assertEqual(status, 200)
        self.assertEqual(data["deliverability"]["status"], "problem")
        self.assertIn("limited to 5", data["deliverability"]["cap_note"])
        self.assertEqual([s["key"] for s in data["funnel"]["steps"]][0], "sent")

    def test_check_again(self):
        with mock.patch.object(dv, "refresh_if_stale", return_value=RESULT) as refresh:
            status, _h, body = self.post_json("/api/deliverability/check", {})
        self.assertEqual((status, json.loads(body)["status"]), (200, "problem"))
        self.assertTrue(refresh.call_args.kwargs["force"])

    def test_lead_review_list_and_decide(self):
        lead_id = self.review_lead()
        status, data = self.get_json("/api/lead-review")
        self.assertEqual((status, data["leads"][0]["id"], data["leads"][0]["reasons"]), (200, lead_id, ["shared inbox"]))
        status, _h, body = self.post_json("/api/lead-review", {"id": lead_id, "action": "approve"})
        self.assertEqual((status, json.loads(body)["status"]), (200, "new"))
        status, _h, _b = self.post_json("/api/lead-review", {"id": lead_id, "action": "approve"})
        self.assertEqual(status, 404)
        status, _h, _b = self.post_json("/api/lead-review", {"id": "x", "action": "approve"})
        self.assertEqual(status, 400)

    def test_script_and_containers(self):
        status, headers, body = self.get("/insights.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"innerHTML", body)
        page = self.get("/")[2].decode()
        self.assertIn('id="insights-root"', page)


class TestProtected(DashboardServerCase):
    def test_needs_login_and_csrf(self):
        self.assertEqual(self.get_json("/api/insights")[0], 401)
        token = self.login()
        self.assertEqual(self.post_json("/api/deliverability/check", {}, self.cookie(token))[0], 403)
        self.assertEqual(self.post_json("/api/lead-review", {"id": 1, "action": "approve"}, self.cookie(token))[0], 403)
