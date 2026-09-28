"""Offline tests for the follow-up sequence. Uses a temporary database and a fake sender:
no network, no emails."""

import io
import os
import ssl
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import core.db as db
from core.config import EXAMPLE_PROFILE_PATH, EmailSettingsError, ProfileError, load_profile, mail_server
from core.product import ROOT_DIR
from bots import email_marketing
from bots.email_marketing import EmailMarketingEngine, build_outreach_email, next_touch_at, sequence_length

LOGIN_ENV = {"SMTP_USER": "me@acme.test", "SMTP_PASS": "x", "SMTP_HOST": "smtp.acme.test", "SMTP_PORT": "465",
             "HOSTINGER_MAIL_API_TOKEN": ""}


def sequence_profile() -> dict:
    profile = load_profile(EXAMPLE_PROFILE_PATH)
    profile["outreach"].update({"send_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "send_window": "",
                                "seconds_between_emails": 0, "emails_per_run": 10, "daily_send_limit": 50})
    return profile


class SequenceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(email_marketing, "load_env_file", lambda: None),
            mock.patch.object(email_marketing, "verify_lead_email", lambda e: (True, "ok")),
            mock.patch.dict(os.environ, LOGIN_ENV),
        ]
        for p in self.patches:
            p.start()
        db.init_db()
        self.sent = []
        self.profile = sequence_profile()
        self.engine = EmailMarketingEngine(self.profile)
        self.engine.send_real_email = lambda to, subject, html, text, headers=None: self.sent.append(
            {"to": to, "subject": subject, "text": text, "html": html, "headers": headers or {}}) or True

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def add_lead(self, email: str, fit: int, status: str = "new") -> int:
        conn = db.get_connection()
        cur = conn.execute(
            "INSERT INTO leads (name, company, email, first_name, fit_score, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (email.split("@")[1], email.split("@")[1].title(), email, "Ana", fit, status,
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        lead_id = cur.lastrowid
        conn.close()
        return lead_id

    def lead(self, lead_id: int) -> dict:
        conn = db.get_connection()
        row = dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
        conn.close()
        return row

    def make_due(self, lead_id: int) -> None:
        conn = db.get_connection()
        conn.execute("UPDATE leads SET next_touch_at = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), lead_id))
        conn.commit()
        conn.close()


class TestSequence(SequenceTestCase):
    def test_first_touch_goes_to_best_fit_first_and_schedules_follow_up(self):
        low = self.add_lead("hi@low.test", 55)
        high = self.add_lead("hi@high.test", 90)
        self.engine.run_outreach_campaign(limit=1)
        self.assertEqual(self.sent[0]["to"], "hi@high.test")
        lead = self.lead(high)
        self.assertEqual((lead["status"], lead["sequence_step"]), ("contacted", 1))
        due = datetime.fromisoformat(lead["next_touch_at"])
        self.assertAlmostEqual((due - datetime.now(timezone.utc)).days, 2, delta=1)  # ~3 days
        self.assertEqual(self.lead(low)["status"], "new")

    def test_follow_up_replies_in_the_same_thread(self):
        lead_id = self.add_lead("hi@studio.test", 80)
        self.engine.run_outreach_campaign()
        first = self.sent[-1]
        self.make_due(lead_id)
        self.engine.run_outreach_campaign()
        follow_up = self.sent[-1]
        self.assertEqual(follow_up["subject"], "Re: " + first["subject"])
        self.assertEqual(follow_up["headers"]["In-Reply-To"], first["headers"]["Message-ID"])
        self.assertEqual(self.lead(lead_id)["sequence_step"], 2)

    def test_follow_ups_are_sent_before_new_leads(self):
        old = self.add_lead("hi@old.test", 60)
        self.engine.run_outreach_campaign()
        self.make_due(old)
        self.add_lead("hi@new.test", 99)
        self.sent.clear()
        self.engine.run_outreach_campaign(limit=1)
        self.assertEqual(self.sent[0]["to"], "hi@old.test")

    def test_sequence_ends_after_last_step(self):
        lead_id = self.add_lead("hi@studio.test", 80)
        for _ in range(sequence_length(self.profile)):
            self.make_due(lead_id)
            self.engine.run_outreach_campaign()
        lead = self.lead(lead_id)
        self.assertEqual((lead["status"], lead["next_touch_at"]), ("no_response", None))
        self.assertEqual(len(self.sent), sequence_length(self.profile))
        self.make_due(lead_id)
        self.engine.run_outreach_campaign()
        self.assertEqual(len(self.sent), sequence_length(self.profile))  # nothing more

    def test_replied_leads_get_no_follow_up(self):
        lead_id = self.add_lead("hi@studio.test", 80)
        self.engine.run_outreach_campaign()
        conn = db.get_connection()
        conn.execute("UPDATE leads SET status = 'interested' WHERE id = ?", (lead_id,))
        conn.commit()
        conn.close()
        self.make_due(lead_id)
        self.sent.clear()
        self.engine.run_outreach_campaign()
        self.assertEqual(self.sent, [])

    def test_daily_limit_is_respected_across_runs(self):
        for i in range(5):
            self.add_lead(f"hi@s{i}.test", 70)
        self.engine.daily_limit = 3
        self.engine.run_outreach_campaign()
        self.engine.run_outreach_campaign()
        self.assertEqual(len(self.sent), 3)

    def test_no_sending_on_off_days(self):
        self.add_lead("hi@studio.test", 80)
        self.engine.profile["outreach"]["send_days"] = []
        result = self.engine.run_outreach_campaign()
        self.assertEqual((result["sent_count"], self.sent), (0, []))


class TestSequenceHelpers(unittest.TestCase):
    def test_next_touch_and_length(self):
        profile = sequence_profile()
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertEqual(sequence_length(profile), 4)
        self.assertEqual(next_touch_at(profile, 1, now), now + timedelta(days=3))
        self.assertIsNone(next_touch_at(profile, 4, now))

    def test_follow_up_body_and_footer(self):
        profile = sequence_profile()
        lead = {"first_name": "Ana", "company": "Silva Photo", "thread_subject": "question about Silva Photo"}
        subject, _html, text = build_outreach_email(profile, lead, step=4)
        self.assertEqual(subject, "Re: question about Silva Photo")
        self.assertIn("1 - send me the link", text)
        self.assertIn(profile["sender"]["postal_address"], text)


class TestCompliance(SequenceTestCase):
    def test_no_cold_email_without_postal_address(self):
        self.add_lead("hi@studio.test", 80)
        self.engine.profile["sender"]["postal_address"] = ""
        result = self.engine.run_outreach_campaign()
        self.assertEqual((result["status"], self.sent), ("blocked", []))


class TestRequalify(SequenceTestCase):
    def test_legacy_leads_are_scored_and_restarted_or_disqualified(self):
        from bots import leadgen_pipeline

        good = self.add_lead("hello@goodstudio.test", None, status="contacted")
        junk = self.add_lead("investor@bigbank.test", None, status="contacted")
        sites = {
            "https://goodstudio.test": {"domain": "goodstudio.test", "website": "https://goodstudio.test",
                                        "site_name": "Good Studio", "title": "", "emails": [],
                                        "page_text": "Wedding photographer. Packages, pricing, inquire."},
            "https://bigbank.test": {"domain": "bigbank.test", "website": "https://bigbank.test",
                                     "site_name": "Big Bank", "title": "", "emails": [],
                                     "page_text": "Investor relations. NYSE: BB."},
        }
        with mock.patch.object(leadgen_pipeline, "scrape_site", lambda url, *a: sites.get(url, {})):
            result = leadgen_pipeline.requalify_existing_leads(self.profile)
        self.assertEqual((len(result["qualified"]), len(result["disqualified"])), (1, 1))
        self.assertEqual((self.lead(good)["status"], self.lead(good)["sequence_step"]), ("new", 0))
        self.assertEqual(self.lead(good)["company"], "Good Studio")
        self.assertEqual(self.lead(junk)["status"], "disqualified")

    def test_owner_can_send_without_address_by_choice(self):
        self.add_lead("hi@studio.test", 80)
        self.engine.profile["sender"].update({"postal_address": "", "require_postal_address": False})
        self.engine.run_outreach_campaign()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("\n\n\n", self.sent[0]["text"])  # no blank line where the address would be


class TestSendingRules(SequenceTestCase):
    def test_outside_business_hours_nothing_is_sent(self):
        self.add_lead("hi@studio.test", 80)
        self.engine.profile["outreach"]["send_window"] = "09:00-09:00"
        with mock.patch("core.outreach_rules.datetime") as fake_dt:
            fake_dt.strptime = datetime.strptime
            result = self.engine.run_outreach_campaign()
        self.assertEqual(self.sent, [])
        self.assertIn("Outside sending hours", result["message"])

    def test_bounce_alarm_pauses_sending(self):
        for i in range(10):
            lead_id = self.add_lead(f"hi@s{i}.test", 70)
            conn = db.get_connection()
            conn.execute("INSERT INTO email_logs (lead_id, lead_email, subject, body, sent_at, step) "
                         "VALUES (?, ?, 's', 'b', ?, 1)", (lead_id, f"hi@s{i}.test", "2026-01-01T00:00:00+00:00"))
            conn.execute("UPDATE leads SET status = ? WHERE id = ?", ("bounced" if i < 2 else "contacted", lead_id))
            conn.commit()
            conn.close()
        self.add_lead("hi@fresh.test", 90)
        result = self.engine.run_outreach_campaign()
        self.assertEqual((result["status"], self.sent), ("blocked", []))

    def test_do_not_contact_list_is_respected(self):
        lead_id = self.add_lead("owner@customer.test", 95)
        with mock.patch.object(email_marketing, "load_do_not_contact", lambda: {"customer.test"}):
            self.engine.run_outreach_campaign()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.lead(lead_id)["status"], "disqualified")

    def test_links_to_own_site_are_tracked(self):
        self.add_lead("hi@studio.test", 80)
        self.engine.profile["outreach"]["body"] = "See {{product_url}}. Or https://other.test/page"
        self.engine.run_outreach_campaign()
        text = self.sent[0]["text"]
        self.assertIn("utm_source=outreach&utm_medium=email&utm_campaign=sdr&utm_content=email1.", text)
        self.assertIn("https://other.test/page\n", text)  # other sites untouched

    def test_subject_ab_test_assigns_and_records_a_variant(self):
        ids = [self.add_lead(f"hi@s{i}.test", 70) for i in range(6)]
        self.engine.profile["outreach"]["subject_variants"] = ["alpha {{company}}", "beta {{company}}"]
        self.engine.run_outreach_campaign()
        variants = {self.lead(i)["variant"] for i in ids}
        self.assertEqual(variants, {"A", "B"})
        for message in self.sent:
            self.assertTrue(message["subject"].startswith(("alpha", "beta")))

    def test_not_now_gets_one_check_in_in_the_same_thread(self):
        lead_id = self.add_lead("hi@studio.test", 80)
        self.engine.run_outreach_campaign()
        first = self.sent[-1]
        self.engine.profile["outreach"]["not_now_follow_up"] = {"days": 60, "body": "Checking back, {{first_name}}."}
        conn = db.get_connection()
        conn.execute("UPDATE leads SET status = 'not_now' WHERE id = ?", (lead_id,))
        conn.commit()
        conn.close()
        self.make_due(lead_id)
        self.engine.run_outreach_campaign()
        check_in = self.sent[-1]
        self.assertEqual(check_in["subject"], "Re: " + first["subject"])
        self.assertIn("Checking back, Ana.", check_in["text"])
        self.assertEqual((self.lead(lead_id)["status"], self.lead(lead_id)["next_touch_at"]), ("nurtured", None))
        self.engine.run_outreach_campaign()
        self.assertEqual(len(self.sent), 2)  # only once


class TestRuleEdgeCases(unittest.TestCase):
    def test_bad_send_window_falls_back_instead_of_crashing(self):
        from core.outreach_rules import in_send_window
        noon = datetime.now().astimezone().replace(hour=12, minute=0)
        self.assertTrue(in_send_window({"outreach": {"send_window": "9am-5pm"}}, noon))

    def test_tracking_in_html_uses_escaped_separators(self):
        from core.outreach_rules import add_tracking
        profile = sequence_profile()
        url = profile["sender"]["product_url"]
        tagged = add_tracking(f'<a href="{url}?a=1&amp;b=2">x</a>', profile, "email1", html=True)
        self.assertIn("a=1&amp;b=2&amp;utm_source=outreach&amp;utm_medium=email", tagged)
        self.assertNotIn("&amp;amp;", tagged)
        self.assertEqual(add_tracking(f"{url}?utm_source=x", profile, "email1"), f"{url}?utm_source=x")

    def test_check_ins_are_not_counted_as_sequence_steps(self):
        from core.report import pipeline_stats
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(db, "DB_PATH", os.path.join(tmp, "r.db")), mock.patch.object(db, "DB_DIR", tmp):
            db.init_db()
            conn = db.get_connection()
            for step in (1, 2, 0):
                conn.execute("INSERT INTO email_logs (lead_id, lead_email, subject, body, sent_at, step) "
                             "VALUES (1, 'a@b.test', 's', 'b', '2026-01-01T00:00:00+00:00', ?)", (step,))
            conn.commit()
            conn.close()
            stats = pipeline_stats()
        self.assertEqual((stats["emails_by_step"], stats["check_ins_sent"]), ({1: 1, 2: 1}, 1))


class TestAliasSender(SequenceTestCase):
    def test_alias_is_the_visible_sender_login_stays_the_envelope(self):
        self.engine.profile["sender"]["from_email"] = "freddy@acme.test"
        engine = EmailMarketingEngine(self.engine.profile)
        self.assertEqual((engine.from_email, engine.smtp_user), ("freddy@acme.test", "me@acme.test"))
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, user, password): sent["login"] = user
            def sendmail(self, envelope_from, to, message): sent.update(envelope=envelope_from, message=message)

        with mock.patch.object(email_marketing.smtplib, "SMTP_SSL", FakeSMTP):
            self.assertTrue(engine.send_via_smtp("lead@studio.test", "hi", "<p>hi</p>", "hi"))
        self.assertEqual((sent["login"], sent["envelope"]), ("me@acme.test", "me@acme.test"))
        self.assertIn("From: Jamie from Acme <freddy@acme.test>", sent["message"])


class TestSmtpCertificateVerification(SequenceTestCase):
    """Sending uses the same certifi-backed, verifying TLS context as the login check, so what
    `sdr setup` proved is exactly what the scheduled runs do (and a python.org Python on macOS,
    whose own trust store is empty, still connects)."""

    def test_ssl_port_uses_the_shared_verifying_context(self):
        seen = {}

        class FakeSMTP:
            def __init__(self, host, port, **kwargs):
                seen.update(kwargs)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, user, password): pass
            def sendmail(self, *a): pass

        shared = ssl.create_default_context()
        with mock.patch.object(email_marketing, "tls_context", return_value=shared), \
                mock.patch.object(email_marketing.smtplib, "SMTP_SSL", FakeSMTP), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(self.engine.send_via_smtp("lead@studio.test", "hi", "<p>hi</p>", "hi"))
        self.assertIs(seen["context"], shared)
        self.assertEqual(shared.verify_mode, ssl.CERT_REQUIRED)

    def test_starttls_port_uses_the_shared_verifying_context(self):
        seen = {}

        class FakeSMTP:
            def __init__(self, host, port, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, context=None): seen["context"] = context
            def login(self, user, password): pass
            def sendmail(self, *a): pass

        shared = ssl.create_default_context()
        self.engine.smtp_port = 587
        with mock.patch.object(email_marketing, "tls_context", return_value=shared), \
                mock.patch.object(email_marketing.smtplib, "SMTP", FakeSMTP), mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(self.engine.send_via_smtp("lead@studio.test", "hi", "<p>hi</p>", "hi"))
        self.assertIs(seen["context"], shared)


class TestNoSilentFallbackHost(unittest.TestCase):
    """A login without a server name must stop with a fix-it message, never guess a host: the old
    default sent whatever password was in .env to the author's mail provider."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(email_marketing, "load_env_file", lambda: None),
        ]
        for p in self.patches:
            p.start()
        self.profile = load_profile(EXAMPLE_PROFILE_PATH)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def env(self, **values):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("SMTP_", "IMAP_", "SENDER_"))}
        return mock.patch.dict(os.environ, {**clean, **values}, clear=True)

    def test_login_without_a_server_name_stops_with_a_fix_it_message(self):
        with self.env(SMTP_USER="me@gmail.com", SMTP_PASS="app-password"), self.assertRaises(ProfileError) as ctx:
            EmailMarketingEngine(self.profile)
        message = str(ctx.exception)
        self.assertIn("SMTP_HOST", message)
        self.assertIn("sdr setup --section email", message)
        self.assertNotIn("hostinger", message.lower())
        self.assertNotIn("app-password", message)

    def test_without_a_login_the_engine_stays_in_dry_run(self):
        with self.env(), mock.patch("sys.stdout", io.StringIO()) as out:
            engine = EmailMarketingEngine(self.profile)
            self.assertEqual(engine.smtp_host, "")
            self.assertFalse(engine.send_real_email("lead@studio.test", "hi", "<p>hi</p>", "hi"))
        self.assertIn("DRY-RUN", out.getvalue())

    def test_the_bots_carry_no_default_mail_host(self):
        for name in ("bots/email_marketing.py", "bots/inbox_listener.py"):
            with open(os.path.join(ROOT_DIR, name), encoding="utf-8") as f:
                self.assertNotIn("hostinger.com", f.read(), name)

    def test_mail_server_reads_hosts_and_ports(self):
        env = {"SMTP_USER": "me@acme.test", "SMTP_PASS": "x", "SMTP_HOST": " smtp.acme.test ", "SMTP_PORT": "587",
               "IMAP_HOST": "imap.acme.test"}
        self.assertEqual(mail_server("smtp", env), ("smtp.acme.test", 587))
        self.assertEqual(mail_server("imap", env), ("imap.acme.test", 993))
        self.assertEqual(mail_server("smtp", {}), ("", 465), "no login yet: dry-run, nothing to refuse")
        self.assertEqual(mail_server("imap", {"SMTP_USER": "me@acme.test"}), ("", 993), "half a login is no login")

    def test_mail_server_refuses_a_login_without_its_host(self):
        env = {"SMTP_USER": "me@acme.test", "SMTP_PASS": "x", "SMTP_HOST": "smtp.acme.test"}
        with self.assertRaises(EmailSettingsError) as ctx:
            mail_server("imap", env)
        self.assertIn("IMAP_HOST", str(ctx.exception))
        self.assertTrue(issubclass(EmailSettingsError, ProfileError), "so every entry point prints it as one line")
        with self.assertRaises(EmailSettingsError) as ctx:
            mail_server("smtp", {**env, "SMTP_PORT": "four-six-five"})
        self.assertIn("SMTP_PORT", str(ctx.exception))
        with self.assertRaises(ValueError):
            mail_server("pop3", env)


class TestConsoleEncoding(unittest.TestCase):
    """Windows gives a redirected stdout the ANSI code page with strict errors, so the first emoji a
    bot prints would abort the whole run (and the handler that prints the failure). Both entry
    points switch the streams to errors="replace" before doing anything else."""

    @staticmethod
    def strict_cp1252() -> io.TextIOWrapper:
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)

    def test_cli_main_makes_emoji_survivable(self):
        import cli
        out, err = self.strict_cp1252(), self.strict_cp1252()
        with self.assertRaises(UnicodeEncodeError):
            out.write("🚀")  # the reproduction: this is what Windows does to a pipe
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            self.assertEqual(cli.main(["version"]), 0)
            print("🚀 [Outreach] still running")
        self.assertEqual((out.errors, err.errors), ("replace", "replace"))
        self.assertIn(b"? [Outreach] still running", out.buffer.getvalue())

    def test_runner_main_makes_emoji_survivable(self):
        import runner
        out, err = self.strict_cp1252(), self.strict_cp1252()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(sys, "argv", ["runner.py", "--task", "inbox"]), \
                mock.patch.object(runner, "run_task", lambda task: {"inbox": {"status": "ok"}}):
            runner.main()
            print("❌ [inbox] failed: boom")
        self.assertEqual((out.errors, err.errors), ("replace", "replace"))
        self.assertIn(b"? [inbox] failed: boom", out.buffer.getvalue())

    def test_streams_without_reconfigure_are_left_alone(self):
        import cli
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", None):
            self.assertEqual(cli.main(["version"]), 0)

    def test_windows_launcher_runs_python_in_utf8_mode(self):
        with open(os.path.join(ROOT_DIR, "bin", "sdr.cmd"), encoding="ascii") as f:
            text = f.read()
        self.assertIn('set "PYTHONUTF8=1"', text)
        self.assertIn('set "PYTHONIOENCODING=utf-8"', text)
        self.assertLess(text.index("PYTHONUTF8"), text.index(r'"%SDR_ROOT%\cli.py"'), "set before Python starts")



class TestBulkHeaders(SequenceTestCase):
    def test_no_bulk_mail_header_unless_asked(self):
        self.add_lead("hi@studio.test", 80)
        self.engine.run_outreach_campaign()
        self.assertNotIn("List-Unsubscribe", self.sent[-1]["headers"])
        self.add_lead("hi@other.test", 80)
        self.engine.profile["outreach"]["list_unsubscribe_header"] = True
        self.engine.run_outreach_campaign()
        self.assertIn("List-Unsubscribe", self.sent[-1]["headers"])


class TestSignatureLogoTest(SequenceTestCase):
    def setUp(self):
        super().setUp()
        self.engine.profile["email_design"].update(
            {"style": "personal", "signature_logo_url": "https://acme.test/logo.png", "signature_logo_test": True})

    def test_leads_are_split_and_the_version_is_recorded(self):
        ids = [self.add_lead(f"owner@studio{i}.test", 80) for i in range(10)]  # = emails_per_run
        self.engine.run_outreach_campaign()
        recorded = {self.lead(i)["email"]: self.lead(i)["signature_variant"] for i in ids}
        self.assertEqual(set(recorded.values()), {"logo", "plain"})
        for message in self.sent:
            has_logo = '<img src="https://acme.test/logo.png"' in message.get("html", "")
            self.assertEqual(has_logo, recorded[message["to"]] == "logo")

    def test_logo_version_only_adds_the_logo(self):
        from bots.email_marketing import build_outreach_email
        base = {"first_name": "Ana", "company": "Studio", "email": "a@b.test"}
        _s, plain, text_plain = build_outreach_email(self.engine.profile, {**base, "signature_variant": "plain"})
        _s, logo, text_logo = build_outreach_email(self.engine.profile, {**base, "signature_variant": "logo"})
        self.assertNotIn("<img", plain)
        self.assertEqual(logo.count("<img"), 1)
        self.assertEqual(text_plain, text_logo)  # plain-text version identical

    def test_everyone_gets_the_logo_when_the_test_is_off(self):
        from core.email_design import signature_variant
        self.engine.profile["email_design"]["signature_logo_test"] = False
        self.assertEqual({signature_variant(self.engine.profile, f"x{i}@y.test") for i in range(10)}, {"logo"})


class TestRunLock(unittest.TestCase):
    def test_inbox_check_skips_but_main_run_waits(self):
        import threading
        import time as _time
        import runner
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner, "DB_DIR", tmp):
            held = threading.Event()
            release = threading.Event()

            def hold_lock():
                with runner.run_lock():
                    held.set()
                    release.wait(5)

            worker = threading.Thread(target=hold_lock)
            worker.start()
            held.wait(5)
            with self.assertRaises(runner.AlreadyRunning):          # inbox check: no waiting
                with runner.run_lock(runner.LOCK_WAIT_SECONDS["inbox"]):
                    pass
            threading.Timer(0.5, release.set).start()
            started = _time.monotonic()
            with runner.run_lock(10):                               # main run: waits, then runs
                waited = _time.monotonic() - started
            worker.join()
            self.assertGreater(waited, 0.3)
