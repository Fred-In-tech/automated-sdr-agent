"""Dashboard Follow-ups tab: list, Done / Snooze / Update, validation, auth."""

import json

from core import db, followups
from test_dashboard import DashboardServerCase


class FollowupsAPICase(DashboardServerCase):
    password = None

    def deal(self) -> int:
        conn = db.get_connection()
        cur = conn.execute("INSERT INTO leads (name, company, email, first_name, created_at) VALUES "
                           "('Ivy', 'Lantern Hall', 'ivy@lanternhall.test', 'Ivy', '2026-10-01')")
        conn.commit()
        conn.close()
        return followups.ensure_deal(cur.lastrowid)

    def act(self, payload):
        status, _h, body = self.post_json("/api/followups", payload)
        return status, json.loads(body)


class TestFollowupsAPI(FollowupsAPICase):
    def test_list_and_summary(self):
        deal_id = self.deal()
        status, data = self.get_json("/api/followups")
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in data["today"]], [deal_id])
        self.assertEqual(data["today"][0]["email"], "ivy@lanternhall.test")
        self.assertEqual(data["summary"]["open"], 1)
        self.assertEqual([s["value"] for s in data["stages"]], ["replied", "meeting", "proposal", "won", "lost"])

    def test_done_snooze_update_and_close(self):
        deal_id = self.deal()
        status, data = self.act({"id": deal_id, "action": "done", "next_step": "Send the proposal",
                                 "due": "2030-01-02", "stage": "proposal"})
        self.assertEqual((status, data["deal"]["stage"], data["deal"]["due"]), (200, "proposal", "2030-01-02"))
        status, data = self.act({"id": deal_id, "action": "snooze", "when": "3days"})
        self.assertEqual(status, 200)
        status, data = self.act({"id": deal_id, "action": "update", "value": "2500", "notes": "Premium plan"})
        self.assertEqual((data["deal"]["value"], data["deal"]["notes"]), (2500.0, "Premium plan"))
        status, data = self.act({"id": deal_id, "action": "done", "outcome": "won"})
        self.assertEqual(data["deal"]["stage"], "won")
        self.assertEqual(self.get_json("/api/followups")[1]["summary"]["won_value"], 2500.0)

    def test_validation_messages(self):
        deal_id = self.deal()
        for payload, fragment in (({"id": deal_id, "action": "done"}, "next step"),
                                  ({"id": deal_id, "action": "update", "value": "lots"}, "number"),
                                  ({"id": deal_id, "action": "snooze", "when": "someday"}, "Snooze"),
                                  ({"id": deal_id, "action": "explode"}, "Choose"),
                                  ({"action": "done"}, "Choose")):
            status, data = self.act(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn(fragment, data["message"])
        status, _ = self.act({"id": 999, "action": "snooze", "when": "tomorrow"})
        self.assertEqual(status, 404)

    def test_script_and_tab(self):
        status, headers, body = self.get("/followups.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertNotIn(b"innerHTML", body)
        page = self.get("/")[2].decode()
        self.assertIn('data-tab="followups"', page)
        self.assertIn('id="tab-followups"', page)


class TestProtected(DashboardServerCase):
    def test_needs_login_and_csrf(self):
        self.assertEqual(self.get_json("/api/followups")[0], 401)
        token = self.login()
        status, _h, _b = self.post_json("/api/followups", {"id": 1, "action": "snooze", "when": "tomorrow"},
                                        self.cookie(token))
        self.assertEqual(status, 403)
