"""Offline tests for `sdr setup`: answers mode, --section re-runs, --print-questions, the
interactive flow in plain mode, and the TOML/.env writers.

Nothing here touches the network, a mailbox, the real crontab or the real config: every run
uses a temp profile/.env/backup folder, fake `Deps`, and a patched os.environ.
"""

import io
import json
import os
import stat
import tempfile
import tomllib
import unittest
from unittest import mock

from bots.email_marketing import build_outreach_email, sequence_length
from bots.inbox_listener import build_reply
from core.config import load_profile
from core.setup_profile import (DEFAULT_DAILY_LIMIT, build_profile_from_answers, managed_values, pitch_sentence,
                                plural, state_from_profile)
from core.setup_questions import QUESTIONS, known_answer_keys, normalize_section, questions_json
from core.setup_steps import Deps
from core.setup_toml import (REMOVE, parse_env_value, read_env_file, toml_multiline, update_env_file,
                             update_profile_text)
from core.setup_wizard import run_setup
from core import tui
from core.tui import UI, flatten_answers

LOGO = "https://acme.example/logo.png"
BRAND = {"url": "https://acme.example", "name": "Acme", "tagline": "Less admin, more shooting.", "description": "",
         "logo_url": LOGO, "brand_color": "#FF5500", "heading_color": "#111111", "text_color": "#222222",
         "background": "#FFF5EE", "found": {"name": True, "logo_url": True, "brand_color": True}, "error": ""}
FAST_HASH = "pbkdf2_sha256$1000$c2FsdHNhbHQ=$aGFzaGhhc2g="
SECRET = "abcd efgh ijkl mnop"   # a Gmail-style app password (spaces are removed when saved)
DASH_SECRET = "correct horse battery"

ANSWERS = {
    "business": {"website": "acme.example", "offer": "free 14-day trial", "pitch": "book more weddings with less admin",
                 "postal_address": "1 Main St, Austin, TX 78701"},
    "audience": {"ideal_client": "wedding photographer",
                 "job_titles": ["wedding photographer", "elopement photographer"],
                 "cities": ["Austin, TX", "Denver, CO"]},
    "style": {"kind": "sales"},
    "email": {"provider": "gmail", "address": "jamie@acme.example", "password_env": "SDR_EMAIL_PASSWORD",
              "from_name": "Jamie at Acme", "sign_off": "Jamie"},
    "schedule": {"run_times": ["08:30", "13:00"], "install": True},
    "security": {"dashboard_password_env": "SDR_DASHBOARD_PASSWORD"},
    "updates": {"mode": "auto"},
}
ENVIRON = {"SDR_EMAIL_PASSWORD": SECRET, "SDR_DASHBOARD_PASSWORD": DASH_SECRET}

# An older profile (before [schedule], [updates] and [sender] website existed), with hand-edited
# copy and comments that a `--section` re-run must leave exactly as they are.
LEGACY_PROFILE = '''# My own notes at the top.
[sender]
product_name   = "Snap Studio"
product_url    = "https://snap.example/signup"   # where "try it" goes
from_name      = "Sam at Snap"
sign_off       = "Sam\\nSnap Studio"
offer          = "30-day free trial"
postal_address = ""
require_postal_address = false

[targeting]
ideal_client = "florist"
professions = [
  "florist",
  "wedding florist",
]
cities = ["Leeds", "Austin, TX"]
leads_per_run = 4

[outreach]
emails_per_run = 10
daily_send_limit = 20              # raise slowly
send_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
subject = "flowers at {{company}}"
body = """Hi {{first_name}},

[Custom] my own hand-written opener about {{company}}.
key = "this line is copy, not a setting"

{{sign_off}}"""

[[outreach.follow_ups]]
days_after_previous = 3
body = """Hi {{first_name}}, a custom follow-up.

{{sign_off}}"""

[email_design]
style = "personal"
logo_url = "https://snap.example/icon.png"
signature_logo_url = "https://snap.example/icon.png"
signature_logo_test = true

[auto_reply]
enabled = true
branded_intents = ["interested"]

[auto_reply.replies]
interested = """Hi {{first_name}}, here's my custom welcome."""

[sdr]
alert_email = ""

[social]
enabled = true
posts = ["""[1/3] a thread that starts with a bracket"""]
'''


def fake_deps(**overrides) -> tuple[Deps, list]:
    """Deps that record calls and never touch the outside world."""
    calls: list = []

    def record(name, result):
        def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result(*args, **kwargs) if callable(result) else result
        return fn

    defaults = {
        "detect_brand": record("detect_brand", lambda url: dict(BRAND, found=dict(BRAND["found"]))),
        "guess_provider": record("guess_provider", "gmail"),
        "check_login": record("check_login", {"smtp": True, "imap": True, "errors": []}),
        "send_code": record("send_code", "123456"),
        "hash_password": record("hash_password", FAST_HASH),
        "install_schedule": record("install_schedule", ["Full run at 08:30 and 13:00"]),
        "schedule_status": record("schedule_status", {"installed": False}),
        "send_sample": record("send_sample", True),
        "start_dashboard": record("start_dashboard", None),
        "find_leads": record("find_leads", {"new_leads_count": 2}),
    }
    defaults.update({name: record(name, value) for name, value in overrides.items()})
    return Deps(**defaults), calls


def called(calls: list, name: str) -> list:
    return [c for c in calls if c[0] == name]


class WizardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_path = os.path.join(self.tmp.name, "config", "profile.toml")
        self.env_path = os.path.join(self.tmp.name, ".env")
        self.backups = os.path.join(self.tmp.name, "backups")
        patcher = mock.patch.dict(os.environ, {}, clear=False)  # the wizard exports new .env values
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_profile(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.profile_path), exist_ok=True)
        with open(self.profile_path, "w", encoding="utf-8") as f:
            f.write(text)

    def run_answers(self, answers: dict, environ: dict | None = None, section: str | None = None, deps=None):
        deps, calls = deps or fake_deps()
        out = io.StringIO()
        ui = UI(answers=flatten_answers(answers), plain=True, interactive=False, collect_missing=True, out=out,
                environ=ENVIRON if environ is None else environ)
        code = run_setup(section=section, ui=ui, profile_path=self.profile_path, env_path=self.env_path,
                         backup_dir=self.backups, deps=deps)
        return code, out.getvalue(), calls

    def profile(self) -> dict:
        return load_profile(self.profile_path)

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()


class TestAnswersMode(WizardCase):
    def test_sales_setup_writes_a_valid_profile_and_private_env(self):
        code, out, calls = self.run_answers(ANSWERS)
        self.assertEqual(code, 0, out)
        profile = self.profile()
        self.assertEqual(profile["sender"]["product_name"], "Acme")          # from the website
        self.assertEqual(profile["sender"]["website"], "https://acme.example")
        self.assertEqual(profile["sender"]["product_url"], "https://acme.example")
        self.assertEqual(profile["email_design"]["style"], "personal")
        self.assertEqual(profile["email_design"]["signature_logo_url"], LOGO)
        self.assertEqual(profile["targeting"]["professions"], ["wedding photographer", "elopement photographer"])
        self.assertEqual(profile["targeting"]["search_engine"], "auto")
        self.assertEqual(profile["schedule"], {"run_times": ["08:30", "13:00"], "reply_check_minutes": 45})
        self.assertEqual(profile["updates"]["mode"], "auto")
        self.assertEqual(profile["outreach"]["emails_per_run"], 10)
        self.assertEqual(profile["auto_reply"]["branded_intents"], ["interested"])

        lead = {"first_name": "Ana", "company": "Silva Photo", "email": "ana@silva.test"}
        subject, _html, text = build_outreach_email(profile, lead)
        self.assertEqual(subject, "question about Silva Photo")
        self.assertIn("Acme helps wedding photographers book more weddings with less admin.", text)
        self.assertIn("Just reply and I'll send you the link.", text)
        for step in range(1, sequence_length(profile) + 1):
            _subject, html_part, text_part = build_outreach_email(profile, lead, step)
            # sales emails carry no link: the link goes out in the welcome reply, to people who asked
            self.assertNotIn("http", text_part, f"step {step}")
            self.assertNotIn("href", html_part, f"step {step}")
            rendered = "".join(build_outreach_email(profile, lead, step))
            self.assertNotIn("{{", rendered)

        env = read_env_file(self.env_path)
        self.assertEqual(env["SMTP_USER"], "jamie@acme.example")
        self.assertEqual(env["SMTP_PASS"], SECRET.replace(" ", ""))
        self.assertEqual(env["SMTP_HOST"], "smtp.gmail.com")
        self.assertEqual(env["DASHBOARD_PASSWORD_HASH"], FAST_HASH)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)
        for secret in (SECRET, SECRET.replace(" ", ""), DASH_SECRET):
            self.assertNotIn(secret, self.read(self.profile_path))
            self.assertNotIn(secret, out)

        self.assertEqual(called(calls, "check_login")[0][2]["smtp_host"], "smtp.gmail.com")
        self.assertEqual(called(calls, "hash_password")[0][1], (DASH_SECRET,))
        self.assertEqual(len(called(calls, "install_schedule")), 1)
        for action in ("send_sample", "start_dashboard", "find_leads", "send_code"):
            self.assertEqual(called(calls, action), [], f"{action} must not run in answers mode")

    def test_marketing_setup_uses_the_branded_template(self):
        answers = {**ANSWERS, "style": {"kind": "marketing"}, "schedule": {"install": False},
                   "finish": {"send_sample": True}}
        code, out, calls = self.run_answers(answers)
        self.assertEqual(code, 0, out)
        profile = self.profile()
        self.assertEqual(profile["email_design"]["style"], "branded")
        self.assertEqual(profile["email_design"]["signature_logo_url"], "")
        self.assertEqual(profile["email_design"]["highlights"], ["Free 14-day trial", "Made for wedding photographers"])
        self.assertEqual(profile["email_design"]["brand_color"], "#FF5500")
        _subject, html, text = build_outreach_email(profile, {"first_name": "Ana", "company": "Silva Photo"})
        self.assertIn("- Less admin, more shooting", text)
        self.assertIn(">See how it works &rarr;</a>", html)
        self.assertIn(LOGO, html)
        welcome, welcome_html = build_reply(profile, "interested", {"first_name": "Ana"})
        self.assertIn("1. Create your account", welcome)
        self.assertIn("Start your free 14-day trial", welcome_html)
        self.assertEqual(called(calls, "install_schedule"), [])
        self.assertEqual(called(calls, "send_sample")[0][1][1], "jamie@acme.example")  # to themselves only

    def test_every_missing_answer_is_reported_at_once_and_nothing_is_saved(self):
        answers = {"business": {"website": "acme.example", "pitch": "x"}, "audience": {"ideal_client": "florist"},
                   "style": {"kind": "sales"}, "email": {"provider": "gmail", "address": "jamie@acme.example",
                                                         "from_name": "Jamie", "sign_off": "Jamie"}}
        code, out, calls = self.run_answers(answers, environ={})
        self.assertEqual(code, 2)
        for key in ("business.offer", "business.postal_address", "audience.cities", "email.password_env"):
            self.assertIn(key, out)
        self.assertFalse(os.path.exists(self.profile_path))
        self.assertFalse(os.path.exists(self.env_path))
        self.assertEqual(called(calls, "check_login"), [])

    def test_unset_password_variable_is_named(self):
        code, out, _calls = self.run_answers(ANSWERS, environ={"SDR_DASHBOARD_PASSWORD": DASH_SECRET})
        self.assertEqual(code, 2)
        self.assertIn("email.password_env", out)
        self.assertIn("SDR_EMAIL_PASSWORD", out)

    def test_answers_file_with_a_password_is_refused(self):
        path = os.path.join(self.tmp.name, "setup-answers.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('[email]\naddress = "a@b.example"\npassword = "hunter22"\n')
        out = io.StringIO()
        ui = UI(plain=True, interactive=False, out=out, environ={})
        code = run_setup(answers_path=path, ui=ui, profile_path=self.profile_path, env_path=self.env_path,
                         deps=fake_deps()[0])
        self.assertEqual(code, 2)
        self.assertIn("password_env", out.getvalue())
        self.assertNotIn("hunter22", out.getvalue())

    def write_answers_file(self, text: str) -> str:
        path = os.path.join(self.tmp.name, "setup-answers.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_unknown_or_misspelled_keys_are_reported_with_a_suggestion(self):
        """A typo like skip_login_chek would otherwise silently trigger a live login attempt."""
        path = self.write_answers_file('[email]\naddress = "a@b.example"\nskip_login_chek = true\n'
                                       '[brand]\nwebsite = "acme.example"\n[schedule]\ninstal = true\n')
        out = io.StringIO()
        ui = UI(plain=True, interactive=False, out=out, environ={})
        deps, calls = fake_deps()
        code = run_setup(answers_path=path, ui=ui, profile_path=self.profile_path, env_path=self.env_path, deps=deps)
        self.assertEqual(code, 2)
        text = out.getvalue()
        for wrong, right in (("email.skip_login_chek", "email.skip_login_check"), ("brand.website", "business.website"),
                             ("schedule.instal", "schedule.install")):
            self.assertIn(wrong, text)
            self.assertIn(right, text)
        self.assertIn("did you mean", text.lower())
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.profile_path))

    def test_failed_login_saves_nothing(self):
        deps, calls = fake_deps(check_login={"smtp": False, "imap": False,
                                             "errors": ["Sending (SMTP): the password was refused."]})
        code, out, _ = self.run_answers(ANSWERS, deps=(deps, calls))
        self.assertEqual(code, 1)
        self.assertIn("the password was refused", out)
        self.assertIn("skip_login_check", out)
        self.assertFalse(os.path.exists(self.profile_path))

    def test_login_check_can_be_skipped(self):
        answers = {**ANSWERS, "email": {**ANSWERS["email"], "skip_login_check": True}}
        code, out, calls = self.run_answers(answers)
        self.assertEqual(code, 0, out)
        self.assertEqual(called(calls, "check_login"), [])

    def test_other_provider_needs_server_names(self):
        email = {**ANSWERS["email"], "provider": "other"}
        code, out, _ = self.run_answers({**ANSWERS, "email": email})
        self.assertEqual(code, 2)
        self.assertIn("email.smtp_host", out)
        self.assertIn("email.imap_host", out)
        email.update(smtp_host="mail.acme.example", smtp_port=587, imap_host="mail.acme.example")
        code, out, calls = self.run_answers({**ANSWERS, "email": email})
        self.assertEqual(code, 0, out)
        self.assertEqual(called(calls, "check_login")[-1][2]["smtp_port"], 587)
        self.assertEqual(read_env_file(self.env_path)["SMTP_PORT"], "587")

    def test_no_mailing_address_needs_explicit_permission(self):
        business = {k: v for k, v in ANSWERS["business"].items() if k != "postal_address"}
        code, out, _ = self.run_answers({**ANSWERS, "business": business})
        self.assertEqual(code, 2)
        self.assertIn("business.postal_address", out)
        code, out, _ = self.run_answers({**ANSWERS, "business": {**business, "allow_no_address": True}})
        self.assertEqual(code, 0, out)
        self.assertIs(self.profile()["sender"]["require_postal_address"], False)

    def test_brave_search_needs_a_key_variable(self):
        audience = {**ANSWERS["audience"], "search_engine": "brave"}
        code, out, _ = self.run_answers({**ANSWERS, "audience": audience})
        self.assertEqual(code, 2)
        self.assertIn("audience.brave_api_key_env", out)
        audience["brave_api_key_env"] = "MY_BRAVE_KEY"
        code, out, _ = self.run_answers({**ANSWERS, "audience": audience}, environ={**ENVIRON, "MY_BRAVE_KEY": "bk-1"})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.profile()["targeting"]["search_engine"], "brave")
        self.assertEqual(read_env_file(self.env_path)["BRAVE_API_KEY"], "bk-1")

    def test_short_dashboard_password_is_rejected(self):
        code, out, _ = self.run_answers(ANSWERS, environ={**ENVIRON, "SDR_DASHBOARD_PASSWORD": "short"})
        self.assertEqual(code, 2)
        self.assertIn("security.dashboard_password_env", out)
        self.assertIn("8 characters", out)

    def test_rerun_backs_up_the_old_profile(self):
        self.write_profile(LEGACY_PROFILE)
        code, out, _ = self.run_answers(ANSWERS)
        self.assertEqual(code, 0, out)
        backups = os.listdir(self.backups)
        self.assertEqual(len(backups), 1)
        self.assertIn("Snap Studio", self.read(os.path.join(self.backups, backups[0])))
        self.assertEqual(self.profile()["sender"]["product_name"], "Acme")

    def test_answers_file_never_prompts_even_on_a_tty(self):
        """AGENTS.md: "With --answers, setup never prompts". Agent shells and the user's own
        terminal are TTYs; a missing answer must exit 2 there too, never block on a question."""
        path = self.write_answers_file('[business]\nwebsite = "acme.example"\n')
        deps, calls = fake_deps()
        with mock.patch.object(tui, "_isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=AssertionError("prompted")), \
                mock.patch("getpass.getpass", side_effect=AssertionError("prompted")), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = run_setup(answers_path=path, profile_path=self.profile_path, env_path=self.env_path,
                             backup_dir=self.backups, deps=deps)
        self.assertEqual(code, 2)
        self.assertIn("business.offer", out.getvalue())
        self.assertIn("audience.cities", out.getvalue())
        self.assertFalse(os.path.exists(self.profile_path))

    def test_full_rerun_updates_a_running_schedule_even_with_install_false(self):
        """`install = false` means "don't start one", not "leave the running one on stale times"."""
        self.write_profile(LEGACY_PROFILE)
        answers = {**ANSWERS, "schedule": {"run_times": ["08:30"], "install": False}}
        code, out, calls = self.run_answers(answers, deps=fake_deps(schedule_status={"installed": True}))
        self.assertEqual(code, 0, out)
        installs = called(calls, "install_schedule")
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][1][0]["schedule"]["run_times"], ["08:30"])
        self.assertIn("already on", out)
        self.assertNotIn("Off for now", out)
        self.assertNotIn("off for now", out)

    def test_website_that_cant_be_read_falls_back_to_answers(self):
        failed = {"url": "https://acme.example", "name": "Acme", "tagline": "", "logo_url": "",
                  "brand_color": "#3B82F6", "found": {}, "error": "Couldn't reach acme.example: timed out."}
        answers = {**ANSWERS, "business": {**ANSWERS["business"], "name": "Acme Weddings"}}
        code, out, _ = self.run_answers(answers, deps=fake_deps(detect_brand=failed))
        self.assertEqual(code, 0, out)
        self.assertIn("Couldn't reach acme.example", out)
        profile = self.profile()
        self.assertEqual(profile["sender"]["product_name"], "Acme Weddings")
        self.assertEqual(profile["email_design"]["signature_logo_url"], "")


class TestSections(WizardCase):
    def test_schedule_section_keeps_custom_copy_comments_and_unknown_tables(self):
        self.write_profile(LEGACY_PROFILE)
        before = tomllib.loads(LEGACY_PROFILE)
        answers = {"schedule": {"run_times": ["07:45"], "reply_check_minutes": 30, "daily_send_limit": 8,
                                "send_days": ["Mon", "Wed"]}}
        code, out, calls = self.run_answers(answers, section="schedule")
        self.assertEqual(code, 0, out)
        text = self.read(self.profile_path)
        after = tomllib.loads(text)
        for table in ("auto_reply", "social", "email_design", "sdr"):
            self.assertEqual(after[table], before[table])
        self.assertEqual(after["outreach"]["body"], before["outreach"]["body"])
        self.assertEqual(after["outreach"]["follow_ups"], before["outreach"]["follow_ups"])
        self.assertEqual(after["sender"], before["sender"])
        self.assertEqual(after["schedule"], {"run_times": ["07:45"], "reply_check_minutes": 30})
        self.assertEqual(after["outreach"]["send_days"], ["Mon", "Wed"])
        self.assertEqual(after["outreach"]["daily_send_limit"], 8)
        self.assertEqual(after["outreach"]["emails_per_run"], 8)          # capped at the new daily limit
        self.assertIn("# My own notes at the top.", text)
        self.assertIn("daily_send_limit = 8              # raise slowly", text)
        self.assertNotIn("updates", after)                                  # other sections untouched
        self.assertEqual(called(calls, "install_schedule"), [])            # it wasn't on, and nobody said so

    def test_schedule_section_updates_a_running_schedule(self):
        self.write_profile(LEGACY_PROFILE)
        deps = fake_deps(schedule_status={"installed": True})
        code, out, calls = self.run_answers({"schedule": {"run_times": ["10:00"]}}, section="schedule", deps=deps)
        self.assertEqual(code, 0, out)
        self.assertEqual(called(calls, "install_schedule")[0][1][0]["schedule"]["run_times"], ["10:00"])

    def test_updates_and_security_sections(self):
        self.write_profile(LEGACY_PROFILE)
        code, out, _ = self.run_answers({"updates": {"mode": "off"}}, section="updates")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.profile()["updates"]["mode"], "off")
        code, out, _ = self.run_answers({"security": {"dashboard_password_env": "SDR_DASHBOARD_PASSWORD"}},
                                        section="password")
        self.assertEqual(code, 0, out)
        self.assertEqual(read_env_file(self.env_path)["DASHBOARD_PASSWORD_HASH"], FAST_HASH)
        self.assertIn('body = """Hi {{first_name}},', self.read(self.profile_path))

    def test_email_section_keeps_the_saved_password(self):
        self.write_profile(LEGACY_PROFILE)
        update_env_file({"SMTP_USER": "sam@snap.example", "SMTP_PASS": "saved-secret", "SMTP_HOST": "smtp.zoho.com",
                         "OTHER_KEY": "kept"}, self.env_path)
        answers = {"email": {"address": "sam@snap.example", "provider": "zoho", "from_name": "Sam from Snap",
                             "sign_off": "Sam"}}
        code, out, calls = self.run_answers(answers, environ={}, section="email")
        self.assertEqual(code, 0, out)
        self.assertEqual(called(calls, "check_login")[0][2]["password"], "saved-secret")
        env = read_env_file(self.env_path)
        self.assertEqual((env["SMTP_PASS"], env["OTHER_KEY"]), ("saved-secret", "kept"))
        profile = self.profile()
        self.assertEqual(profile["sender"]["from_name"], "Sam from Snap")
        self.assertEqual(profile["outreach"]["body"], tomllib.loads(LEGACY_PROFILE)["outreach"]["body"])

    def test_audience_section_refreshes_only_the_pitch_line_setup_wrote(self):
        code, out, _ = self.run_answers(ANSWERS)
        self.assertEqual(code, 0, out)
        answers = {"audience": {"ideal_client": "wedding photographer", "cities": ["Austin, TX"]},
                   "business": {"pitch": "get paid faster"}}
        code, out, _ = self.run_answers(answers, section="audience")
        self.assertEqual(code, 0, out)
        profile = self.profile()
        self.assertIn("Acme helps wedding photographers get paid faster.", profile["outreach"]["body"])
        self.assertEqual(profile["targeting"]["cities"], ["Austin, TX"])
        self.assertEqual(profile["sender"]["pitch"], "get paid faster")

    def test_brand_section_reads_a_new_website_and_moves_the_signature_logo(self):
        self.write_profile(LEGACY_PROFILE)
        code, out, calls = self.run_answers({"business": {"website": "acme.example"}}, section="brand")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(called(calls, "detect_brand")), 1)
        profile = self.profile()
        self.assertEqual(profile["sender"]["product_name"], "Acme")
        self.assertEqual(profile["sender"]["offer"], "30-day free trial")          # kept
        self.assertEqual(profile["email_design"]["logo_url"], LOGO)
        self.assertEqual(profile["email_design"]["signature_logo_url"], LOGO)     # followed the logo
        self.assertEqual(profile["outreach"]["body"], tomllib.loads(LEGACY_PROFILE)["outreach"]["body"])

    def test_switching_to_marketing_and_back_keeps_a_hand_set_signature_logo(self):
        design = ('logo_url = "https://snap.example/icon.png"\nsignature_logo_url = "https://snap.example/sig.png"\n'
                  'signature_logo_test = true')
        self.write_profile(LEGACY_PROFILE.replace(
            'logo_url = "https://snap.example/icon.png"\nsignature_logo_url = "https://snap.example/icon.png"\n'
            'signature_logo_test = true', design))
        for kind in ("marketing", "sales"):
            code, out, _ = self.run_answers({"style": {"kind": kind}}, section="style")
            self.assertEqual(code, 0, out)
            self.assertEqual(self.profile()["email_design"]["signature_logo_url"], "https://snap.example/sig.png", kind)

    def test_no_to_the_logo_test_means_no_logo_not_a_logo_for_everyone(self):
        """The signature logo came from the header logo; answering "no" must not leave the URL
        with the test off, which would put the logo on every prospect's email."""
        self.write_profile(LEGACY_PROFILE)          # signature logo == header logo, test on
        code, out, _ = self.run_answers({"style": {"kind": "sales", "signature_logo_test": False}}, section="style")
        self.assertEqual(code, 0, out)
        after = self.profile()["email_design"]
        self.assertEqual(after["signature_logo_url"], "")
        from core.email_design import signature_variant
        self.assertEqual(signature_variant(self.profile(), "lead@x.test"), "plain")

    def test_style_section_keeps_a_saved_signature_logo_and_its_test_flag(self):
        """The signature logo may be set by hand with no header logo; re-running the style step
        must neither wipe it nor flip the A/B flag, and the old profile is backed up first."""
        cases = [
            ('logo_url = ""\nsignature_logo_url = "https://snap.example/sig.png"\nsignature_logo_test = true',
             "https://snap.example/sig.png", True),
            ('logo_url = ""\nsignature_logo_url = "https://snap.example/sig.png"\nsignature_logo_test = false',
             "https://snap.example/sig.png", False),     # false = everyone gets the logo; keep it that way
            ('logo_url = "https://snap.example/icon.png"\nsignature_logo_url = "https://snap.example/sig.png"\n'
             'signature_logo_test = true', "https://snap.example/sig.png", True),   # hand-tuned: not the header logo
        ]
        for design, expected_url, expected_test in cases:
            with self.subTest(design=design):
                text = LEGACY_PROFILE.replace(
                    'logo_url = "https://snap.example/icon.png"\nsignature_logo_url = "https://snap.example/icon.png"\n'
                    'signature_logo_test = true', design)
                self.assertIn(design, text)
                self.write_profile(text)
                code, out, _ = self.run_answers({"style": {"kind": "sales"}}, section="style")
                self.assertEqual(code, 0, out)
                after = self.profile()["email_design"]
                self.assertEqual(after["signature_logo_url"], expected_url)
                self.assertIs(after["signature_logo_test"], expected_test)

    def test_section_run_backs_up_the_profile_before_changing_it(self):
        self.write_profile(LEGACY_PROFILE)
        code, out, _ = self.run_answers({"schedule": {"run_times": ["07:45"]}}, section="schedule")
        self.assertEqual(code, 0, out)
        backups = os.listdir(self.backups)
        self.assertEqual(len(backups), 1)
        self.assertIn("Snap Studio", self.read(os.path.join(self.backups, backups[0])))
        self.assertIn("backed up", out)

    def test_section_needs_a_profile_and_a_known_name(self):
        code, out, _ = self.run_answers({"updates": {"mode": "off"}}, section="schedule")
        self.assertEqual(code, 1)
        self.assertIn("There's no profile yet", out)
        code, out, _ = self.run_answers({}, section="colours")
        self.assertEqual(code, 2)
        self.assertEqual(normalize_section(" Business "), "brand")

    def test_no_terminal_and_no_answers_points_to_the_answers_file(self):
        code, out, calls = self.run_answers({})
        self.assertEqual(code, 2)
        self.assertIn("sdr setup --answers setup-answers.toml", out)
        self.assertEqual(calls, [])

    def test_odd_hand_edited_values_fall_back_to_defaults(self):
        odd = LEGACY_PROFILE.replace('[sdr]', '[updates]\nmode = "weekly"\n\n[schedule]\nrun_times = "09:00"\n\n[sdr]')
        self.write_profile(odd)
        code, out, _ = self.run_answers({"updates": {"mode": "auto"}}, section="updates")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.profile()["updates"]["mode"], "auto")
        code, out, _ = self.run_answers({"schedule": {"daily_send_limit": 5}}, section="schedule")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.profile()["schedule"]["run_times"], ["09:00", "14:00"])


class TestPrintQuestions(unittest.TestCase):
    def test_json_lists_every_answers_file_key(self):
        questions = json.loads(questions_json())
        keys = [item["key"] for item in questions]
        self.assertEqual(len(keys), len(set(keys)))
        for item in questions:
            self.assertEqual(set(item), {"key", "label", "type", "default", "choices", "required", "help"})
            self.assertFalse(item["key"].endswith(("password", "_key")), "secrets are only named by *_env keys")
        contract = {"business": ["website", "name", "offer", "signup_url", "pitch", "postal_address",
                                 "allow_no_address"],
                    "audience": ["ideal_client", "job_titles", "cities", "leads_per_run", "search_engine",
                                 "brave_api_key_env"],
                    "style": ["kind", "signature_logo_test", "brand_color", "logo_url"],
                    "email": ["provider", "address", "password_env", "smtp_host", "smtp_port", "imap_host",
                              "imap_port", "from_name", "sign_off", "alias", "alert_email", "skip_login_check"],
                    "replies": ["enabled", "branded_welcome"],
                    "schedule": ["run_times", "send_days", "daily_send_limit", "reply_check_minutes", "install"],
                    "security": ["dashboard_password_env"], "updates": ["mode"],
                    "finish": ["send_sample", "open_dashboard", "find_leads"]}
        for section, names in contract.items():
            for name in names:
                self.assertIn(f"{section}.{name}", keys)
        kind = next(item for item in questions if item["key"] == "style.kind")
        self.assertEqual([c["value"] for c in kind["choices"]], ["sales", "marketing"])
        self.assertTrue(all(q.label for q in QUESTIONS))

    def test_print_questions_prints_json(self):
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(run_setup(print_questions=True), 0)
        self.assertIsInstance(json.loads(out.getvalue()), list)

    def test_defaults_in_the_json_are_the_ones_setup_really_uses(self):
        """An agent trusts `--print-questions`: a schedule is never started unless asked, and the
        daily limit suggested is the one the README tells a new sender to start with."""
        by_key = {item["key"]: item for item in json.loads(questions_json())}
        self.assertIs(by_key["schedule.install"]["default"], False)
        self.assertEqual(by_key["schedule.daily_send_limit"]["default"], 10)
        self.assertEqual(DEFAULT_DAILY_LIMIT, 10)
        self.assertIsNone(by_key["business.offer"]["default"])
        self.assertTrue(by_key["business.offer"]["required"])

    def test_every_prompt_key_the_wizard_uses_is_a_known_answers_key(self):
        """The unknown-key check must never reject a key some prompt really reads."""
        import re
        source = ""
        for name in ("setup_steps", "setup_email", "setup_wizard"):
            with open(os.path.join(os.path.dirname(__file__), "..", "core", f"{name}.py"), encoding="utf-8") as f:
                source += f.read()
        used = set(re.findall(r'ui\.(?:text|confirm|select|checkbox|list|password)\(\s*"([a-z_.]+)"', source))
        known = known_answer_keys()
        self.assertEqual(sorted(used - known), [])
        self.assertLessEqual({q.key for q in QUESTIONS}, known)


class TestInteractive(WizardCase):
    """A person at a plain terminal, answering with Enter wherever a default is offered."""

    def scripted_ui(self, replies: dict, passwords: list, lines: list | None = None):
        """`replies` maps a fragment of the prompt to the typed answer; a list gives successive
        answers (the last one repeats). `lines` feeds the one-per-line list prompts."""
        prompts: list = []
        lists = iter(["", "Austin, TX; Denver, CO"] if lines is None else lines)  # titles: default; cities

        def answer(prompt: str) -> str:
            prompts.append(prompt)
            if prompt == "  > ":
                return next(lists, "")
            if prompt.startswith("Choose"):  # a numbered menu: its question is the line above "  1) ..."
                menu_lines = out.getvalue().splitlines()
                first_option = max(i for i, line in enumerate(menu_lines) if line.startswith("  1) "))
                prompt = menu_lines[first_option - 1]
            for fragment, reply in replies.items():
                if fragment in prompt:
                    if isinstance(reply, list):
                        return reply.pop(0) if len(reply) > 1 else reply[0]
                    return reply
            return ""  # Enter: accept the suggestion

        secrets = iter(passwords)
        out = io.StringIO()
        ui = UI(plain=True, interactive=True, out=out, environ={}, input_fn=answer, getpass_fn=lambda _p: next(secrets))
        return ui, out, prompts

    def run_interactive(self, replies: dict, passwords: list, deps=None, section: str | None = None,
                        lines: list | None = None):
        ui, out, prompts = self.scripted_ui(replies, passwords, lines)
        deps, calls = deps or fake_deps()
        code = run_setup(section=section, ui=ui, profile_path=self.profile_path, env_path=self.env_path,
                         backup_dir=self.backups, deps=deps)
        return code, out.getvalue(), calls, prompts

    BASE = {"Your website": "acme.example", "offer new clients": "free consultation",
            "Business mailing address": "1 Main St, Austin, TX",
            "ideal client": "wedding photographer", "Finish the sentence": "book more weddings",
            "send from": "jamie@acme.example", "6-digit code": "123 456", "Turn the schedule on": "n",
            "Open your dashboard": "n", "Find your first leads": "y",
            "Where would you like to finish": "2"}        # 2) here in the terminal

    def test_finishing_in_the_browser_saves_a_safe_profile_and_opens_settings(self):
        """After the cities question, Enter picks the browser: the rest is forms on the dashboard."""
        replies = {**self.BASE, "Where would you like to finish": ""}
        code, out, calls, prompts = self.run_interactive(replies, [])
        self.assertEqual(code, 0, out)
        self.assertLessEqual(len(prompts), 13)                    # short: the promise of this path
        self.assertNotIn("[Step 3/7]", out)
        self.assertIn("sdr dashboard", out)                       # how to reopen it
        self.assertEqual(called(calls, "start_dashboard")[0][2], {"background": False, "page": "#settings"})
        self.assertEqual(called(calls, "check_login") + called(calls, "install_schedule"), [])
        profile = self.profile()
        self.assertEqual(profile["targeting"]["ideal_client"], "wedding photographer")
        self.assertEqual(profile["sender"]["sign_off"], "Acme")    # placeholder until Settings asks
        self.assertFalse(os.path.exists(self.env_path) and read_env_file(self.env_path).get("SMTP_PASS"))

    def test_answers_files_are_never_offered_the_browser(self):
        code, out, calls = self.run_answers(ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Where would you like to finish", out)
        self.assertEqual(called(calls, "start_dashboard"), [])

    def test_full_setup_with_defaults(self):
        code, out, calls, prompts = self.run_interactive(self.BASE, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 0, out)
        self.assertIn("[Step 7/7] Security & updates", out)
        self.assertIn("Sending works.", out)                                   # code confirmed
        self.assertIn("(code confirmed)", out)
        self.assertIn('When a lead says "sounds good, send me the link"', out)  # reply preview
        profile = self.profile()
        self.assertEqual(profile["sender"]["sign_off"], "Jamie")               # guessed from jamie@
        self.assertEqual(profile["sender"]["from_name"], "Jamie at Acme")
        self.assertEqual(profile["sender"]["offer"], "free consultation")     # typed: no placeholder offer
        self.assertEqual(profile["targeting"]["cities"], ["Austin, TX", "Denver, CO"])
        self.assertEqual(profile["schedule"]["run_times"], ["09:00", "14:00"])
        self.assertEqual(profile["outreach"]["daily_send_limit"], 10)
        self.assertIn("sends up to 10 emails a day", out)
        self.assertEqual(read_env_file(self.env_path)["DASHBOARD_PASSWORD_HASH"], FAST_HASH)
        self.assertEqual(len(called(calls, "send_sample")), 1)
        self.assertEqual(len(called(calls, "find_leads")), 1)
        self.assertEqual(called(calls, "install_schedule"), [])
        self.assertLessEqual(len(prompts), 31)   # the long way round; the browser path is the short one

    def test_failed_login_can_be_skipped(self):
        deps = fake_deps(check_login={"smtp": False, "imap": False, "errors": ["The password was refused."]})
        replies = {**self.BASE, "What would you like to do?": "3"}
        code, out, calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET], deps=deps)
        self.assertEqual(code, 0, out)
        self.assertIn("The password was refused.", out)
        self.assertEqual(read_env_file(self.env_path)["SMTP_PASS"], "")         # dry-run until fixed
        self.assertEqual(called(calls, "send_sample"), [])                      # nothing to send with
        self.assertEqual(called(calls, "send_code"), [])

    def test_no_address_requires_confirmation(self):
        replies = {**self.BASE, "Business mailing address": "none", "Send without an address": "y"}
        code, out, _calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 0, out)
        self.assertIn("breaks the law", out)
        self.assertIs(self.profile()["sender"]["require_postal_address"], False)

    def test_existing_profile_menu_changes_one_part(self):
        self.write_profile(LEGACY_PROFILE)
        replies = {**self.BASE, "What would you like to do?": "1", "Which part": "6"}  # change one part: schedule
        code, out, calls, _ = self.run_interactive(replies, [])
        self.assertEqual(code, 0, out)
        after = tomllib.loads(self.read(self.profile_path))
        self.assertEqual(after["schedule"], {"run_times": ["09:00", "14:00"], "reply_check_minutes": 45})
        self.assertEqual(after["outreach"]["body"], tomllib.loads(LEGACY_PROFILE)["outreach"]["body"])
        self.assertEqual(len(called(calls, "schedule_status")), 1)
        self.assertEqual(called(calls, "install_schedule"), [])   # "Turn the schedule on now?" -> n

    def test_existing_profile_menu_can_leave_everything(self):
        self.write_profile(LEGACY_PROFILE)
        code, out, _calls, _ = self.run_interactive({"What would you like to do?": "3"}, [])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(self.profile_path), LEGACY_PROFILE)

    def test_declining_to_save_writes_nothing(self):
        replies = {**self.BASE, "Save these settings": "n"}
        code, _out, calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.profile_path))
        self.assertFalse(os.path.exists(self.env_path))

    # ── first-run defaults ──

    def test_first_run_leaves_the_schedule_off_on_enter(self):
        """Nobody has previewed an email yet, so Enter must not start a live cron job."""
        replies = {k: v for k, v in self.BASE.items() if k != "Turn the schedule on"}
        code, out, calls, prompts = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 0, out)
        self.assertIn("Turn the schedule on now? [y/N]: ", prompts)
        self.assertEqual(called(calls, "install_schedule"), [])
        self.assertIn("sdr schedule on", out)

    def test_offer_has_no_placeholder_default_on_a_first_run(self):
        replies = {k: v for k, v in self.BASE.items() if k != "offer new clients"}
        replies["offer new clients"] = ["", "free tasting menu"]      # Enter first: it must be asked again
        code, out, _calls, prompts = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.profile()["sender"]["offer"], "free tasting menu")
        self.assertNotIn("free 14-day trial", self.read(self.profile_path))
        self.assertTrue(any(p.startswith("What do you offer new clients?: ") for p in prompts))  # no [default]

    def test_rerun_with_a_live_schedule_updates_it_without_asking(self):
        self.write_profile(LEGACY_PROFILE)
        deps = fake_deps(schedule_status={"installed": True})
        replies = {**self.BASE, "What would you like to do?": "2"}          # the whole setup again
        code, out, calls, prompts = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET], deps=deps)
        self.assertEqual(code, 0, out)
        self.assertFalse(any("Turn the schedule on now?" in p for p in prompts))
        self.assertEqual(len(called(calls, "install_schedule")), 1)
        self.assertIn("already on", out)

    # ── dead ends ──

    def test_refusing_an_address_keeps_asking_then_stops_in_plain_words(self):
        """Enter on "Send without an address? [y/N]" means no, so the wizard asks again instead of
        giving up after 3 tries; when it finally stops, it talks to a person, not an AI agent."""
        replies = {**self.BASE, "Business mailing address": "none", "Send without an address": "n"}
        code, out, _calls, prompts = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 2)
        self.assertGreater(sum("Business mailing address" in p for p in prompts), 3)
        self.assertIn("Setup stopped", out)
        self.assertIn("sdr setup", out)
        self.assertNotIn("answers file", out)
        self.assertNotIn("--print-questions", out)
        self.assertFalse(os.path.exists(self.profile_path))

    def test_mismatched_dashboard_passwords_do_not_throw_the_setup_away(self):
        never_matching = [f"pw-{n}" for n in range(60)]
        code, out, _calls, _ = self.run_interactive(self.BASE, [SECRET, *never_matching])
        self.assertEqual(code, 0, out)
        self.assertIn("No new password set", out)
        self.assertNotIn("DASHBOARD_PASSWORD_HASH", read_env_file(self.env_path))
        self.assertTrue(os.path.exists(self.profile_path))

    # ── the website that can't be read ──

    def test_unreachable_website_offers_to_retype_the_address(self):
        failed = {"url": "https://acme.exmaple", "name": "Acme", "tagline": "", "logo_url": "",
                  "brand_color": "#3B82F6", "found": {}, "error": "Couldn't reach acme.exmaple: couldn't connect."}
        seen: list = []

        def detect(url):
            seen.append(url)
            return dict(BRAND, found=dict(BRAND["found"])) if url == "https://acme.example" else dict(failed)

        replies = {**self.BASE, "Your website": ["acme.exmaple", "acme.example"]}
        code, out, _calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET],
                                                    deps=fake_deps(detect_brand=detect))
        self.assertEqual(code, 0, out)
        self.assertEqual(seen, ["https://acme.exmaple", "https://acme.example"])
        self.assertIn("Check the address", out)
        self.assertEqual(self.profile()["sender"]["website"], "https://acme.example")
        self.assertEqual(self.profile()["email_design"]["logo_url"], LOGO)

    def test_unreachable_website_kept_on_enter_is_not_called_found(self):
        failed = {"url": "https://acme.exmaple", "name": "Acme", "tagline": "", "logo_url": "",
                  "brand_color": "#3B82F6", "found": {}, "error": "Couldn't reach acme.exmaple: couldn't connect."}
        replies = {**self.BASE, "Your website": "acme.exmaple"}
        code, out, calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET],
                                                   deps=fake_deps(detect_brand=failed))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(called(calls, "detect_brand")), 1)
        self.assertNotIn("Here's what we found", out)
        self.assertIn("couldn't read", out.lower())
        self.assertEqual(self.profile()["sender"]["website"], "https://acme.exmaple")

    # ── cities on one line ──

    def test_a_wrongly_split_city_line_can_be_typed_again(self):
        """"Surrey" isn't a known region, so "Richmond, Surrey" splits in two; the person says no
        and re-types it with a semicolon."""
        replies = {**self.BASE, "Is that right?": ["n", "y"]}
        code, out, _calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET],
                                                    lines=["", "Richmond, Surrey", "", "Richmond, Surrey; Leeds, UK", ""])
        self.assertEqual(code, 0, out)
        self.assertIn("Searching in 2 cities: Richmond; Surrey", out)
        self.assertIn('Put a ";" between cities', out)
        self.assertEqual(self.profile()["targeting"]["cities"], ["Richmond, Surrey", "Leeds, UK"])

    def test_cities_typed_on_one_line_become_separate_cities_and_are_shown(self):
        code, out, _calls, _ = self.run_interactive(self.BASE, [SECRET, DASH_SECRET, DASH_SECRET],
                                                    lines=["", "Austin, Dallas, Houston", ""])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.profile()["targeting"]["cities"], ["Austin", "Dallas", "Houston"])
        self.assertIn("3 cities: Austin; Dallas; Houston", out)

    # ── the dashboard ──

    def test_opening_the_dashboard_interactively_runs_it_in_this_terminal(self):
        replies = {**self.BASE, "Open your dashboard": "y"}
        code, out, calls, _ = self.run_interactive(replies, [SECRET, DASH_SECRET, DASH_SECRET])
        self.assertEqual(code, 0, out)
        self.assertEqual(called(calls, "start_dashboard")[0][2], {"background": False})

    # ── the mailbox login, re-run on a saved password ──

    def saved_mailbox(self, check_login: dict):
        self.write_profile(LEGACY_PROFILE)
        update_env_file({"SMTP_USER": "sam@snap.example", "SMTP_PASS": "saved-secret", "SMTP_HOST": "smtp.zoho.com",
                         "IMAP_HOST": "imap.zoho.com"}, self.env_path)
        return fake_deps(check_login=check_login)

    def test_email_section_keeps_the_saved_password_when_the_check_cannot_run(self):
        """Offline, or Gmail times out: Enter on every prompt must not erase a working login."""
        deps = self.saved_mailbox({"smtp": False, "imap": False,
                                   "errors": ["Sending (SMTP): smtp.zoho.com:465 didn't answer in time."]})
        code, out, calls, _ = self.run_interactive({"Name people see": "Sam from Snap"}, [], deps=deps,
                                                   section="email")
        self.assertEqual(code, 0, out)
        self.assertIn("didn't answer in time", out)
        self.assertIn("Leave the saved password", out)
        self.assertEqual(read_env_file(self.env_path)["SMTP_PASS"], "saved-secret")
        self.assertEqual(self.profile()["sender"]["from_name"], "Sam from Snap")
        self.assertEqual(len(called(calls, "check_login")), 1)

    def test_email_section_only_removes_the_saved_password_when_told_to(self):
        deps = self.saved_mailbox({"smtp": False, "imap": False,
                                   "errors": ["Sending (SMTP): smtp.zoho.com didn't accept the email address and password."]})
        replies = {"What would you like to do?": "4"}                 # 4) Remove the saved password
        code, out, _calls, _ = self.run_interactive(replies, [], deps=deps, section="email")
        self.assertEqual(code, 0, out)
        self.assertIn("Remove the saved password", out)
        self.assertEqual(read_env_file(self.env_path)["SMTP_PASS"], "")
        self.assertIn("until the login is fixed", out)
        self.assertNotIn("We'll save this", out)

    def test_third_wrong_login_still_offers_to_keep_a_working_sender(self):
        """Outlook: sending works, the inbox doesn't. Retrying twice must not silently drop the
        "keep it" option on the last try, and the last try is announced."""
        deps = fake_deps(check_login={"smtp": True, "imap": False, "errors": ["Inbox (IMAP): login failed."]})
        replies = {**self.BASE, "What would you like to do?": ["2", "2", "1"]}   # retry, retry, keep it
        code, out, calls, _ = self.run_interactive(replies, [SECRET, SECRET, SECRET, DASH_SECRET, DASH_SECRET],
                                                   deps=deps)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(called(calls, "check_login")), 3)
        self.assertIn("Last try", out)
        self.assertEqual(out.count("Keep it: sending works"), 3)
        self.assertEqual(read_env_file(self.env_path)["SMTP_PASS"], SECRET.replace(" ", ""))
        self.assertIn("sending works; the inbox login needs fixing", out)


class TestFailedLoginMenu(unittest.TestCase):
    """Which choice Enter picks after a failed login check."""

    def pick(self, address: str, **kwargs) -> tuple[str, list]:
        from core.setup_email import _after_failed_login
        seen = {}
        ui = mock.Mock()
        ui.select.side_effect = lambda key, label, choices, default=None: seen.update(
            choices=[c[0] for c in choices], default=default) or default
        ctx = mock.Mock(ui=ui)
        return _after_failed_login(ctx, {"smtp": False, "imap": False}, address=address, **kwargs), seen["choices"]

    def test_hotmail_preselects_changing_the_provider(self):
        choice, choices = self.pick("sam@hotmail.fr")
        self.assertEqual(choice, "provider")
        self.assertIn("retry", choices)

    def test_other_mailboxes_preselect_typing_the_password_again(self):
        self.assertEqual(self.pick("sam@acme.test")[0], "retry")

    def test_a_saved_password_is_still_left_alone_by_default(self):
        self.assertEqual(self.pick("sam@hotmail.com", kept_saved=True)[0], "keep_saved")


class TestProfileHelpers(unittest.TestCase):
    def test_pitch_sentence_accepts_endings_and_whole_sentences(self):
        self.assertEqual(pitch_sentence("Snap", "send quotes in one click", "florist"),
                         "Snap helps florists send quotes in one click.")
        self.assertEqual(pitch_sentence("Acme", "Acme books your calls for you.", "coach"),
                         "Acme books your calls for you.")
        self.assertEqual(pitch_sentence("Acme", "helps agencies win pitches", "agency"),
                         "Acme helps agencies win pitches.")
        self.assertEqual(pitch_sentence("Acme", "We build websites", "coach"), "We build websites.")
        self.assertEqual(plural("agency"), "agencies")
        self.assertEqual(plural("coach"), "coaches")
        self.assertEqual(plural("dentists"), "dentists")

    def test_legacy_profile_gets_defaults_for_new_sections(self):
        env = {"SMTP_HOST": "smtp.zoho.com", "SMTP_USER": "sam@x.example"}
        state = state_from_profile(tomllib.loads(LEGACY_PROFILE), env)
        self.assertEqual(state["business.website"], "https://snap.example/signup")
        self.assertTrue(state["business.allow_no_address"])
        self.assertEqual(state["email.provider"], "zoho")
        self.assertNotIn("schedule.run_times", state)
        values = managed_values(state)
        self.assertEqual(values["schedule"], {"run_times": ["09:00", "14:00"], "reply_check_minutes": 45})
        self.assertEqual(values["updates"], {"mode": "notify"})
        self.assertFalse(values["sender"]["require_postal_address"])

    def test_generated_profile_parses_for_odd_input(self):
        text = build_profile_from_answers({"business.name": 'Tricky "Quotes" \\ Co', "business.pitch": 'say """hi"""',
                                           "audience.ideal_client": "coach", "audience.cities": ["Paris"],
                                           "email.sign_off": "Kim\nTricky Co", "business.offer": "trial"})
        profile = tomllib.loads(text)
        self.assertEqual(profile["sender"]["product_name"], 'Tricky "Quotes" \\ Co')
        self.assertEqual(profile["sender"]["sign_off"], "Kim\nTricky Co")
        self.assertIn('say """hi"""', profile["outreach"]["body"])


class TestTomlAndEnvWriters(unittest.TestCase):
    def test_update_keeps_everything_else(self):
        text = LEGACY_PROFILE
        new = update_profile_text(text, {"sender": {"offer": "free month", "website": "https://snap.example"},
                                         "outreach": {"subject": REMOVE}, "schedule": {"run_times": ["09:00"]}})
        before, after = tomllib.loads(text), tomllib.loads(new)
        self.assertEqual(after["sender"]["offer"], "free month")
        self.assertEqual(after["sender"]["website"], "https://snap.example")
        self.assertNotIn("subject", after["outreach"])
        self.assertEqual(after["schedule"]["run_times"], ["09:00"])
        self.assertEqual(after["social"], before["social"])
        self.assertEqual(after["outreach"]["body"], before["outreach"]["body"])
        self.assertIn('product_url    = "https://snap.example/signup"   # where "try it" goes', new)
        with self.assertRaises(ValueError):
            update_profile_text("[broken", {"sender": {"offer": "x"}})

    def test_multiline_strings_round_trip(self):
        for value in ["", "\nstarts with a newline", 'ends with a quote"', 'triple """ quotes', "back\\slash",
                      "tab\tand control \x01", "trailing\n"]:
            self.assertEqual(tomllib.loads("k = " + toml_multiline(value))["k"], value)

    def test_env_file_merge_and_line_break_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment\nKEEP=1\nSMTP_PASS=old\n")
            update_env_file({"SMTP_PASS": "new", "ADDED": "x"}, path)
            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "# comment\nKEEP=1\nSMTP_PASS=new\nADDED=x\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with self.assertRaises(ValueError):
                update_env_file({"SMTP_PASS": "a\nINJECTED=1"}, path)

    def test_env_values_with_quote_characters_round_trip(self):
        """A mailbox password may start or end with a quote: the writer quotes such values and
        the reader strips only a matching pair, so what passed the login check is what's read back."""
        awkward = ['ends-with-quote"', '"quoted-start', "'single'", '"both"', "back\\slash\"", " padded ",
                   'plain-value', '', 'mid"quote', "it's"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            for value in awkward:
                update_env_file({"SMTP_PASS": value, "KEEP": "1"}, path)
                self.assertEqual(read_env_file(path)["SMTP_PASS"], value, repr(value))
                self.assertEqual(read_env_file(path)["KEEP"], "1")
                self.assertEqual(parse_env_value(self.read(path).splitlines()[0].split("=", 1)[1]), value)
        self.assertEqual(parse_env_value('"a\\"b"'), 'a"b')
        self.assertEqual(parse_env_value("'raw\\'"), "raw\\")
        self.assertEqual(parse_env_value('  spaced  '), "spaced")
        self.assertEqual(parse_env_value('"unterminated'), '"unterminated')

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()


if __name__ == "__main__":
    unittest.main()



class TestAnswersModeSafety(unittest.TestCase):
    """An AI agent driving setup from a file must never start a live schedule or hang by accident."""

    def _ctx(self, answers: dict, **deps):
        from core.setup_steps import Deps, SetupContext
        from core.tui import UI
        ui = UI(answers=answers, plain=True, interactive=False)
        return SetupContext(ui=ui, deps=Deps(**deps), answers_mode=True, a=dict(answers), env={})

    def test_schedule_install_defaults_off_from_an_answers_file(self):
        from core.setup_steps import step_schedule
        ctx = self._ctx({"schedule.run_times": ["09:00"], "schedule.send_days": ["Mon"],
                         "schedule.daily_send_limit": 20, "schedule.reply_check_minutes": 45,
                         "audience.leads_per_run": 4})
        step_schedule(ctx)
        self.assertFalse(ctx.install_schedule)
        ctx = self._ctx({**ctx.a, "schedule.install": True})
        step_schedule(ctx)
        self.assertTrue(ctx.install_schedule)

    def test_open_dashboard_from_an_answers_file_says_how_to_run_it_instead(self):
        """A background dashboard dies with the setup process, leaving a dead browser tab; from
        an answers file the only lasting option is `sdr dashboard` in the user's own terminal."""
        import io as _io
        from core.setup_wizard import _finish
        from core.tui import UI
        calls = []
        out = _io.StringIO()
        answers = {"finish.open_dashboard": True, "finish.send_sample": False, "finish.find_leads": False}
        ctx = self._ctx(answers, start_dashboard=lambda background=False: calls.append(background) or {"url": "x"})
        ctx.ui = UI(answers=answers, plain=True, interactive=False, out=out)
        _finish(ctx, {"sender": {"product_name": "Acme"}})
        self.assertEqual(calls, [])
        self.assertIn("sdr dashboard", out.getvalue())
