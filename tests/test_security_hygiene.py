"""Offline tests for the pre-release security fixes: fixed-string SQL, https-only alerts that never
print their secrets, stable fallback message ids, dependency floors and line-ending rules.
No network, no email."""

import contextlib
import hashlib
import io
import os
import re
import runpy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import core.db as db
from core import notifications
from core.notifications import NotificationManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "123456789:AAH-secret_Token-value"
WEBHOOK = "https://discord.com/api/webhooks/42/very-secret-webhook-token"


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def quiet(fn, *args, **kwargs):
    """Run fn and return (result, everything it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
        ]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def execute(self, sql: str, args=()) -> int:
        conn = db.get_connection()
        cur = conn.execute(sql, args)
        conn.commit()
        conn.close()
        return cur.lastrowid

    def add_lead(self, email: str, status: str = "new", location: str | None = None, fit: int = 50,
                 next_touch_at: str | None = None, variant: str | None = None) -> int:
        return self.execute(
            "INSERT INTO leads (name, company, email, status, location, fit_score, next_touch_at, variant, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (email, email.split("@")[1], email, status, location, fit, next_touch_at, variant,
             datetime.now(timezone.utc).isoformat()))


class TestNotifications(unittest.TestCase):
    def test_telegram_posts_json_over_https_with_a_timeout(self):
        with mock.patch.object(notifications.requests, "post", return_value=FakeResponse(200)) as post:
            ok, _ = quiet(NotificationManager(TOKEN, "42").send_telegram, "hot lead")
        self.assertTrue(ok)
        url = post.call_args.args[0]
        self.assertTrue(url.startswith("https://api.telegram.org/bot"))
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "42")
        self.assertEqual(post.call_args.kwargs["timeout"], notifications.TIMEOUT_SECONDS)

    def test_telegram_errors_never_print_the_bot_token(self):
        boom = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
        with mock.patch.object(notifications.requests, "post", side_effect=boom):
            ok, printed = quiet(NotificationManager(TOKEN, "42").send_telegram, "hot lead")
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, printed)
        self.assertNotIn("AAH-secret", printed)
        self.assertIn("ConnectionError", printed)

    def test_telegram_http_failure_is_reported_by_status_only(self):
        with mock.patch.object(notifications.requests, "post", return_value=FakeResponse(401)):
            ok, printed = quiet(NotificationManager(TOKEN, "42").send_telegram, "hot lead")
        self.assertFalse(ok)
        self.assertIn("401", printed)
        self.assertNotIn(TOKEN, printed)

    def test_malformed_telegram_token_is_refused_without_a_request(self):
        with mock.patch.object(notifications.requests, "post") as post:
            ok, printed = quiet(NotificationManager("12/../evil?x=", "42").send_telegram, "hi")
        self.assertFalse(ok)
        post.assert_not_called()
        self.assertNotIn("evil", printed)

    def test_discord_webhook_must_be_https(self):
        for url in ("file:///etc/passwd", "http://discord.com/api/webhooks/1/x", "ftp://x/y", "discord.com/x"):
            with self.subTest(url=url), mock.patch.object(notifications.requests, "post") as post:
                ok, printed = quiet(NotificationManager(discord_webhook_url=url).send_discord, "t", "d")
                self.assertFalse(ok)
                post.assert_not_called()
                self.assertNotIn(url, printed)

    def test_discord_success_and_errors_never_print_the_webhook(self):
        with mock.patch.object(notifications.requests, "post", return_value=FakeResponse(204)) as post:
            ok, _ = quiet(NotificationManager(discord_webhook_url=WEBHOOK).send_discord, "Hot lead", "Ana")
        self.assertTrue(ok)
        self.assertEqual(post.call_args.args[0], WEBHOOK)
        self.assertEqual(post.call_args.kwargs["json"]["embeds"][0]["title"], "Hot lead")
        self.assertEqual(post.call_args.kwargs["timeout"], notifications.TIMEOUT_SECONDS)

        boom = requests.Timeout(f"HTTPSConnectionPool: Read timed out. url={WEBHOOK}")
        with mock.patch.object(notifications.requests, "post", side_effect=boom):
            ok, printed = quiet(NotificationManager(discord_webhook_url=WEBHOOK).send_discord, "t", "d")
        self.assertFalse(ok)
        self.assertNotIn("very-secret-webhook-token", printed)

    def test_no_settings_is_a_dry_run(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(notifications.requests, "post") as post:
            (_, printed) = quiet(NotificationManager().notify_all, "Title", "Body")
        post.assert_not_called()
        self.assertIn("DRY-RUN", printed)

    def test_running_the_module_as_a_script_sends_nothing(self):
        """There used to be a `__main__` smoke test that posted a pre-rename product name to the real
        chat whenever the env vars were set. The module stays import-and-use only."""
        path = os.path.join(ROOT, "core", "notifications.py")
        secrets = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42", "DISCORD_WEBHOOK_URL": WEBHOOK}
        with mock.patch.dict(os.environ, secrets), mock.patch.object(requests, "post") as post:
            _, printed = quiet(runpy.run_path, path, run_name="__main__")
        post.assert_not_called()
        self.assertEqual(printed, "")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("Antigravity", source)
        self.assertNotIn("__main__", source)


class TestPickLeadsRegionFilter(TempDbCase):
    def engine(self):
        from bots import email_marketing
        with mock.patch.object(email_marketing, "load_env_file", lambda: None), \
             contextlib.redirect_stdout(io.StringIO()):
            return email_marketing.EmailMarketingEngine({"sender": {"from_name": "Fred"}, "outreach": {}})

    def pick(self, budget: int, region: str | None) -> list[str]:
        conn = db.get_connection()
        try:
            return [lead["email"] for lead in self.engine()._pick_leads(conn.cursor(), budget, region)]
        finally:
            conn.close()

    def test_due_follow_ups_first_then_best_fit_new_leads(self):
        due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        self.add_lead("due@a.test", status="contacted", location="Austin, TX", next_touch_at=due)
        self.add_lead("later@a.test", status="contacted", location="Austin, TX", next_touch_at=later)
        self.add_lead("low@a.test", location="Denver, CO", fit=40)
        self.add_lead("high@a.test", location="Austin, TX", fit=90)
        self.assertEqual(self.pick(10, None), ["due@a.test", "high@a.test", "low@a.test"])
        self.assertEqual(self.pick(2, None), ["due@a.test", "high@a.test"])

    def test_region_is_a_bound_parameter_not_sql(self):
        self.add_lead("austin@a.test", location="Austin, TX", fit=90)
        self.add_lead("denver@a.test", location="Denver, CO", fit=80)
        self.add_lead("nowhere@a.test", location=None, fit=70)
        self.assertEqual(self.pick(10, "Austin"), ["austin@a.test"])
        self.assertEqual(self.pick(10, "x' OR '1'='1"), [])
        self.assertEqual(self.pick(10, "Denver') OR 1=1 --"), [])


class TestReportQueries(TempDbCase):
    def test_reply_counts_use_the_intent_constants(self):
        from core.report import pipeline_stats
        a = self.add_lead("a@a.test", status="contacted", variant="A")
        b = self.add_lead("b@b.test", status="contacted", variant="B")
        c = self.add_lead("c@c.test", status="contacted", variant="A")
        for lead_id in (a, b, c):
            self.execute("INSERT INTO email_logs (lead_id, lead_email, subject, body, sent_at, step) "
                         "VALUES (?, 'x@x.test', 's', 'b', '2026-01-01T00:00:00+00:00', 1)", (lead_id,))
        for lead_id, intent in ((a, "interested"), (a, "question"), (b, "not_now"), (c, "auto_reply")):
            self.execute("INSERT INTO inbound_messages (message_id, lead_id, from_email, subject, intent, excerpt, "
                         "received_at) VALUES (?, ?, 'x@x.test', 's', ?, 'hello', '2026-01-02T00:00:00+00:00')",
                         (f"<{lead_id}-{intent}@x>", lead_id, intent))
        stats = pipeline_stats()
        self.assertEqual((stats["contacted"], stats["replied"], stats["positive"]), (3, 2, 1))
        self.assertEqual([h["intent"] for h in stats["hot_leads"]], ["question", "interested"])
        by_variant = {v["variant"]: (v["contacted"], v["replied"], v["positive"]) for v in stats["subject_ab_test"]}
        self.assertEqual(by_variant, {"A": (2, 1, 1), "B": (1, 1, 0)})

    def test_report_sql_is_fixed_text(self):
        with open(os.path.join(ROOT, "core", "report.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertNotRegex(source, r'execute\(f["\']')
        self.assertNotRegex(source, r'q\(f["\']')


class TestSchemaMigrationIdentifiers(TempDbCase):
    def test_unsafe_identifiers_are_refused(self):
        conn = db.get_connection()
        try:
            for table, columns in (("leads; DROP TABLE leads", {"x": "TEXT"}),
                                   ("leads", {"x TEXT, y": "TEXT"}),
                                   ("leads", {"x": "TEXT); DROP TABLE leads; --"})):
                with self.subTest(table=table, columns=columns):
                    with self.assertRaises(ValueError):
                        db._add_missing_columns(conn.cursor(), table, columns)
            db._add_missing_columns(conn.cursor(), "leads", {"extra_note": "TEXT", "extra_n": "INTEGER DEFAULT 0"})
            names = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        finally:
            conn.close()
        self.assertTrue({"extra_note", "extra_n", "variant"} <= names)


class TestFallbackMessageId(unittest.TestCase):
    def test_id_is_unchanged_from_earlier_releases(self):
        """Existing databases stored these ids: changing the hash would re-process old mail."""
        from bots.inbox_listener import fallback_message_id
        legacy = "gen-" + hashlib.sha1("ana@x.test|Re: hi|Mon, 1 Jan 2026".encode(), usedforsecurity=False).hexdigest()
        self.assertEqual(fallback_message_id("ana@x.test", "Re: hi", "Mon, 1 Jan 2026"), legacy)
        self.assertNotEqual(fallback_message_id("ana@x.test", "Re: hi", ""), legacy)


class TestReleaseHygieneFiles(unittest.TestCase):
    def read(self, name: str) -> str:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_gitattributes_pins_script_line_endings(self):
        rules = {tuple(line.split()) for line in self.read(".gitattributes").splitlines()
                 if line.strip() and not line.startswith("#")}
        for rule in (("*.sh", "text", "eol=lf"), ("bin/sdr", "text", "eol=lf"), ("*.cmd", "text", "eol=crlf"),
                     ("*.ps1", "text", "eol=crlf"), ("*.png", "binary")):
            self.assertIn(rule, rules)

    def test_requirement_floors_have_no_known_advisories(self):
        """Floors checked with pip-audit against the minimum versions (2026-09-26)."""
        floors = dict(re.findall(r"^([A-Za-z0-9_.-]+)>=([0-9][0-9.]*)", self.read("requirements.txt"), re.M))
        minimum = {"requests": "2.33", "dnspython": "2.6.1", "urllib3": "2.7", "idna": "3.15",
                   "certifi": "2024.7.4", "soupsieve": "2.9", "pygments": "2.20"}
        as_tuple = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731
        for name, version in minimum.items():
            with self.subTest(package=name):
                self.assertIn(name, floors)
                self.assertGreaterEqual(as_tuple(floors[name]), as_tuple(version))


if __name__ == "__main__":
    unittest.main()
