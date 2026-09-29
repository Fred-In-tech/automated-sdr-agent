"""The dashboard's Settings page: reading settings, saving a section, and keeping secrets in."""

import json
import os
import tempfile
import unittest
from unittest import mock

from core import settings_api
from core.setup_profile import build_profile_from_answers
from core.setup_questions import BY_KEY, SECTIONS
from core.setup_steps import Deps
from core.setup_toml import read_env_file

SECRET = "abcd efgh ijkl mnop"
DASH_SECRET = "correct horse battery"
FAST_HASH = "pbkdf2_sha256$1000$c2FsdHNhbHQ=$aGFzaGhhc2g="
HANDOFF = {   # what `sdr setup` saves when someone chooses to finish in the browser
    "business.website": "https://acme.example", "business.name": "Acme", "business.offer": "free consultation",
    "business.pitch": "book more weddings", "business.postal_address": "1 Main St, Austin, TX",
    "audience.ideal_client": "wedding photographer", "audience.job_titles": ["wedding photographer"],
    "audience.cities": ["Austin, TX"], "audience.search_engine": "auto",
    "email.sign_off": "Acme", "email.from_name": "Acme",
}


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile_path = os.path.join(self.tmp.name, "profile.toml")
        self.env_path = os.path.join(self.tmp.name, ".env")
        with open(self.profile_path, "w", encoding="utf-8") as f:
            f.write(build_profile_from_answers(HANDOFF))
        self.calls = []
        self.login = {"smtp": True, "imap": True, "errors": []}
        self.schedule_on = False
        self.deps = Deps(
            detect_brand=self.record("detect_brand", lambda *a, **k: {"url": "https://acme.example", "name": "Acme",
                                     "tagline": "", "description": "", "logo_url": "", "brand_color": "#FF5500",
                                     "heading_color": "#111111", "text_color": "#222222", "background": "#FFF5EE",
                                     "found": {"name": True}, "error": ""}),
            guess_provider=self.record("guess_provider", lambda *a, **k: "gmail"),
            check_login=self.record("check_login", lambda *a, **k: self.login),
            hash_password=self.record("hash_password", lambda *a, **k: FAST_HASH),
            install_schedule=self.record("install_schedule", lambda *a, **k: ["Full run at 10:00"]),
            schedule_status=self.record("schedule_status", lambda *a, **k: {"installed": self.schedule_on}),
        )
        patcher = mock.patch.dict(os.environ, {}, clear=False)   # setup exports new .env values
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def record(self, name, result):
        def fn(*args, **kwargs):
            self.calls.append(name)
            return result(*args, **kwargs)
        return fn

    def read(self) -> dict:
        return settings_api.read_settings(self.profile_path, self.env_path, self.deps)

    def save(self, section: str, values: dict) -> dict:
        return settings_api.save_section(section, values, self.profile_path, self.env_path,
                                         os.path.join(self.tmp.name, "backups"), self.deps)

    def field(self, key: str) -> dict:
        return next(f for s in self.read()["sections"] for f in s["fields"] if f["key"] == key)


class TestReading(SettingsCase):
    def test_every_section_and_known_question_is_shown(self):
        data = self.read()
        self.assertEqual([s["name"] for s in data["sections"]], list(SECTIONS))
        for keys in settings_api.SECTION_FIELDS.values():
            for key in keys:
                self.assertIn(key, BY_KEY)
        self.assertEqual(self.field("audience.cities")["value"], ["Austin, TX"])
        self.assertEqual(data["reopen_command"], "sdr dashboard")

    def test_checklist_after_finishing_in_the_browser(self):
        data = self.read()
        self.assertFalse(data["setup_complete"])
        self.assertEqual([item["done"] for item in data["checklist"]], [True, False, False, False, False])

    def test_no_profile_yet(self):
        os.remove(self.profile_path)
        data = self.read()
        self.assertFalse(data["configured"])
        self.assertFalse(data["checklist"][0]["done"])


class TestSaving(SettingsCase):
    EMAIL = {"email.address": "sam@gmail.com", "email.provider": "gmail", "email.password_env": SECRET,
             "email.sign_off": "Sam", "email.from_name": "Sam at Acme"}

    def test_mailbox_is_checked_then_saved_and_never_sent_back(self):
        result = self.save("email", self.EMAIL)
        self.assertTrue(result["success"], result)
        self.assertIn("Your login works: sending and inbox.", result["messages"])
        self.assertIn("check_login", self.calls)
        env = read_env_file(self.env_path)
        self.assertEqual((env["SMTP_USER"], env["SMTP_HOST"]), ("sam@gmail.com", "smtp.gmail.com"))
        everything = json.dumps([self.read(), result])
        for secret in (SECRET, SECRET.replace(" ", ""), "SDR_FORM_SECRET"):
            self.assertNotIn(secret, everything)
        self.assertTrue(self.field("email.password_env")["is_set"])
        self.assertEqual([item["done"] for item in self.read()["checklist"]][:3], [True, True, True])

    def test_a_refused_login_is_not_saved(self):
        self.login = {"smtp": False, "imap": False, "errors": ["Sending (SMTP): the password was refused."]}
        result = self.save("email", self.EMAIL)
        self.assertFalse(result["success"])
        self.assertIn("Sending (SMTP): the password was refused.", result["messages"])
        self.assertFalse(read_env_file(self.env_path).get("SMTP_PASS"))

    def test_an_empty_password_box_keeps_the_saved_password(self):
        self.save("email", self.EMAIL)
        saved = read_env_file(self.env_path)["SMTP_PASS"]
        result = self.save("email", {**self.EMAIL, "email.password_env": "", "email.sign_off": "Sammy"})
        self.assertTrue(result["success"], result)
        self.assertEqual(read_env_file(self.env_path)["SMTP_PASS"], saved)
        self.assertEqual(self.field("email.sign_off")["value"], "Sammy")

    def test_dashboard_password(self):
        short = self.save("security", {"security.dashboard_password_env": "short"})
        self.assertFalse(short["success"])
        self.assertTrue(any("at least 8" in m for m in short["messages"]), short)
        good = self.save("security", {"security.dashboard_password_env": DASH_SECRET})
        self.assertTrue(good["success"] and good["password_changed"], good)
        self.assertEqual(read_env_file(self.env_path)["DASHBOARD_PASSWORD_HASH"], FAST_HASH)
        self.assertNotIn(DASH_SECRET, json.dumps(good))

    def test_other_sections_only_change_their_own_settings(self):
        before = self.field("business.offer")["value"]
        for section, values, key, expected in (
                ("audience", {"audience.ideal_client": "florist", "business.pitch": "sell more",
                              "audience.job_titles": ["florist"], "audience.cities": ["Leeds", "Austin, TX"],
                              "audience.search_engine": "duckduckgo"}, "audience.cities", ["Leeds", "Austin, TX"]),
                ("style", {"style.kind": "marketing", "replies.branded_welcome": True}, "style.kind", "marketing"),
                ("replies", {"replies.enabled": False}, "replies.enabled", False),
                ("updates", {"updates.mode": "auto"}, "updates.mode", "auto")):
            with self.subTest(section=section):
                result = self.save(section, values)
                self.assertTrue(result["success"], result)
                self.assertEqual(self.field(key)["value"], expected)
        self.assertEqual(self.field("business.offer")["value"], before)

    def test_schedule_is_only_turned_on_when_asked(self):
        times = {"schedule.run_times": ["10:00"], "schedule.send_days": ["Mon", "Tue"],
                 "schedule.daily_send_limit": 8, "schedule.reply_check_minutes": 30, "audience.leads_per_run": 3}
        with mock.patch.object(settings_api, "_turn_schedule_off", lambda: ["off"]) as _:
            self.assertTrue(self.save("schedule", {**times, "schedule.install": False})["success"])
        self.assertNotIn("install_schedule", self.calls)
        self.assertTrue(self.save("schedule", {**times, "schedule.install": True})["success"])
        self.assertIn("install_schedule", self.calls)
        self.assertEqual(self.field("schedule.daily_send_limit")["value"], 8)

    def test_turning_the_schedule_off_removes_it(self):
        self.schedule_on = True
        removed = []
        with mock.patch.object(settings_api, "_turn_schedule_off", lambda: removed.append(1) or ["The schedule is off."]):
            result = self.save("schedule", {"schedule.run_times": ["10:00"], "schedule.install": False})
        self.assertTrue(result["success"], result)
        self.assertEqual(removed, [1])
        self.assertIn("The schedule is off.", result["messages"])

    def test_bad_requests(self):
        for section, values in (("nope", {}), ("email", "x"), ("email", None)):
            self.assertFalse(self.save(section, values)["success"])
        result = self.save("schedule", {"schedule.daily_send_limit": "lots", "rm -rf": 1})
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
