"""Offline tests for core/email_checks.py (login check + verification code), the robots.txt
guard in bots/leadgen_pipeline.py, the SMTP-only sender in bots/email_marketing.py and the
check_email_login.py wrapper. Fake SMTP/IMAP factories and injected fetchers: no network,
no email, no real .env. The TLS context and the real connection code live in test_email_tls.py."""

import contextlib
import imaplib
import io
import os
import re
import smtplib
import socket
import ssl
import tempfile
import types
import unittest
from unittest import mock

import core.db as db
from core import email_checks
from core.email_checks import (
    HINTS,
    PROVIDERS,
    EmailCheckError,
    check_login,
    clean_app_password,
    code_matches,
    guess_provider,
    is_personal_microsoft,
    make_code,
    provider_settings,
    send_verification_code,
    verification_message,
)

PASSWORD = "hunter2-secret"


class FakeConnection:
    """Stands in for smtplib.SMTP / imaplib.IMAP4_SSL after connecting."""

    def __init__(self, login_error: BaseException | None = None, close_error: BaseException | None = None):
        self.login_error = login_error
        self.close_error = close_error
        self.logins = []
        self.closed = False

    def login(self, user, password):
        self.logins.append((user, password))
        if self.login_error:
            raise self.login_error

    def _close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error

    quit = _close
    logout = _close


def factory_for(connection: FakeConnection | None = None, connect_error: BaseException | None = None):
    """A factory(host, port) that records calls and returns `connection` or raises."""
    calls = []

    def factory(host, port):
        calls.append((host, port))
        if connect_error:
            raise connect_error
        return connection

    factory.calls = calls
    return factory


def run_check(smtp_conn=None, imap_conn=None, smtp_error=None, imap_error=None, smtp_host="smtp.hostinger.com",
              imap_host="imap.hostinger.com", smtp_port=465, imap_port=993, password=PASSWORD,
              address="me@acme.test"):
    smtp_factory = factory_for(smtp_conn or FakeConnection(), smtp_error)
    imap_factory = factory_for(imap_conn or FakeConnection(), imap_error)
    result = check_login(address, password, smtp_host, smtp_port, imap_host, imap_port,
                         smtp_factory=smtp_factory, imap_factory=imap_factory)
    return result, smtp_factory, imap_factory


class TestProviders(unittest.TestCase):
    def test_every_provider_has_the_fields_the_wizard_needs(self):
        self.assertEqual(set(PROVIDERS), {"gmail", "google_workspace", "outlook", "hostinger", "zoho", "other"})
        for key, provider in PROVIDERS.items():
            for field in ("label", "smtp_host", "smtp_port", "imap_host", "imap_port", "help", "password_label"):
                self.assertIn(field, provider, f"{key} is missing {field}")
            self.assertIsInstance(provider["smtp_port"], int)
            self.assertIsInstance(provider["imap_port"], int)
            self.assertTrue(provider["help"])
            if key != "other":
                self.assertTrue(provider["smtp_host"] and provider["imap_host"], key)

    def test_gmail_points_to_app_passwords(self):
        self.assertEqual((PROVIDERS["gmail"]["smtp_host"], PROVIDERS["gmail"]["smtp_port"]), ("smtp.gmail.com", 465))
        self.assertIn("myaccount.google.com/apppasswords", PROVIDERS["gmail"]["help"])
        self.assertEqual(PROVIDERS["outlook"]["smtp_port"], 587)

    def test_provider_settings_applies_overrides_for_other(self):
        settings = provider_settings("other", smtp_host=" mail.acme.test ", smtp_port="587",
                                     imap_host="imap.acme.test", imap_port=None)
        self.assertEqual(settings["smtp_host"], "mail.acme.test")
        self.assertEqual(settings["smtp_port"], 587)
        self.assertEqual(settings["imap_host"], "imap.acme.test")
        self.assertEqual(settings["imap_port"], 993)
        self.assertEqual(PROVIDERS["other"]["smtp_host"], "", "the shared table must not be mutated")

    def test_provider_settings_keeps_defaults_when_no_overrides(self):
        self.assertEqual(provider_settings("zoho")["smtp_host"], "smtp.zoho.com")

    def test_unknown_provider_fails_loudly(self):
        with self.assertRaises(ValueError) as ctx:
            provider_settings("yahooo")
        self.assertIn("gmail", str(ctx.exception))


class TestGuessProvider(unittest.TestCase):
    def test_personal_domains_need_no_dns(self):
        lookup = mock.Mock(side_effect=AssertionError("no DNS for known domains"))
        self.assertEqual(guess_provider("me@gmail.com", lookup), "gmail")
        self.assertEqual(guess_provider("Bob@Hotmail.com", lookup), "outlook")
        self.assertEqual(guess_provider("x@zoho.com", lookup), "zoho")

    def test_business_domains_from_mx_records(self):
        cases = {
            "aspmx.l.google.com": "google_workspace",
            "acme-test.mail.protection.outlook.com": "outlook",
            "mx1.hostinger.com": "hostinger",
            "mx.zoho.eu": "zoho",
            "mx.some-isp.test": "other",
        }
        for mx_host, expected in cases.items():
            self.assertEqual(guess_provider("sam@acme.test", lambda domain, h=mx_host: [h]), expected, mx_host)

    def test_lookup_gets_the_domain(self):
        lookup = mock.Mock(return_value=[])
        self.assertEqual(guess_provider("sam@Acme.Test", lookup), "other")
        lookup.assert_called_once_with("acme.test")

    def test_never_raises(self):
        self.assertEqual(guess_provider("sam@acme.test", mock.Mock(side_effect=OSError("dns down"))), "other")
        self.assertEqual(guess_provider("not-an-email", mock.Mock()), "other")
        self.assertEqual(guess_provider("", mock.Mock()), "other")


class TestCleanAppPassword(unittest.TestCase):
    def test_strips_spaces_from_google_app_passwords_only(self):
        self.assertEqual(clean_app_password("gmail", "abcd efgh ijkl mnop"), "abcdefghijklmnop")
        self.assertEqual(clean_app_password("google_workspace", " abcdefghijklmnop "), "abcdefghijklmnop")
        self.assertEqual(clean_app_password("outlook", "abcd efgh ijkl mnop"), "abcd efgh ijkl mnop")
        self.assertEqual(clean_app_password("gmail", "My Real Pass 1"), "My Real Pass 1")


class TestCheckLogin(unittest.TestCase):
    def test_success_logs_in_to_both_and_closes(self):
        smtp_conn, imap_conn = FakeConnection(), FakeConnection()
        result, smtp_factory, imap_factory = run_check(smtp_conn, imap_conn)
        self.assertEqual(result, {"smtp": True, "imap": True, "errors": []})
        self.assertEqual(smtp_factory.calls, [("smtp.hostinger.com", 465)])
        self.assertEqual(imap_factory.calls, [("imap.hostinger.com", 993)])
        self.assertEqual(smtp_conn.logins, [("me@acme.test", PASSWORD)])
        self.assertTrue(smtp_conn.closed and imap_conn.closed)

    def test_missing_credentials_checks_nothing(self):
        smtp_factory, imap_factory = factory_for(FakeConnection()), factory_for(FakeConnection())
        result = check_login("me@acme.test", "", "smtp.x.test", 465, "imap.x.test", 993,
                             smtp_factory=smtp_factory, imap_factory=imap_factory)
        self.assertEqual((result["smtp"], result["imap"]), (False, False))
        self.assertIn("password", result["errors"][0])
        self.assertEqual(smtp_factory.calls + imap_factory.calls, [])

    def test_gmail_app_password_hint_given_once(self):
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required."))
        imap = FakeConnection(imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)"))
        result, _, _ = run_check(smtp, imap, smtp_host="smtp.gmail.com", imap_host="imap.gmail.com")
        self.assertEqual((result["smtp"], result["imap"]), (False, False))
        self.assertTrue(result["errors"][0].startswith("Sending (SMTP): smtp.gmail.com didn't accept"))
        self.assertTrue(result["errors"][1].startswith("Inbox (IMAP): imap.gmail.com didn't accept"))
        hints = [e for e in result["errors"] if "apppasswords" in e]
        self.assertEqual(len(hints), 1)
        self.assertEqual(len(result["errors"]), 3)

    def test_gmail_hint_from_message_even_on_other_host(self):
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(534, b"Application-specific password required"))
        result, _, _ = run_check(smtp, smtp_host="mail.relay.test")
        self.assertTrue(any("apppasswords" in e for e in result["errors"]))
        self.assertTrue(result["imap"])

    def test_microsoft_basic_auth_disabled_hint(self):
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(
            535, b"5.7.139 Authentication unsuccessful, SmtpClientAuthentication is disabled for the Tenant."))
        result, _, _ = run_check(smtp, smtp_host="smtp.office365.com", smtp_port=587)
        self.assertFalse(result["smtp"])
        self.assertTrue(any("Authenticated SMTP" in e for e in result["errors"]))
        self.assertFalse(any("hosting-panel" in e for e in result["errors"]))
        self.assertFalse(any("Personal Outlook.com" in e for e in result["errors"]),
                         "a work (Microsoft 365) address still gets the admin advice")

    def test_personal_outlook_or_hotmail_gets_told_it_cannot_work(self):
        """A sole trader on hotmail.com has no admin to ask: Microsoft removed password sign-in
        for personal mailboxes, so the only honest advice is to send from another mailbox."""
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(
            535, b"5.7.139 Authentication unsuccessful, basic authentication is disabled."))
        imap = FakeConnection(imaplib.IMAP4.error("LOGIN failed."))
        result, _, _ = run_check(smtp, imap, smtp_host="smtp.office365.com", smtp_port=587,
                                 imap_host="outlook.office365.com", address="sam@hotmail.com")
        self.assertEqual((result["smtp"], result["imap"]), (False, False))
        personal = [e for e in result["errors"] if "Personal Outlook.com" in e]
        self.assertEqual(len(personal), 1, result["errors"])
        self.assertIn("can't be used", personal[0])
        self.assertIn("setup --section email", personal[0])
        self.assertFalse(any("admin" in e for e in result["errors"]), "no admin to ask on hotmail.com")
        self.assertFalse(any("hosting-panel" in e for e in result["errors"]))
        self.assertEqual(len(result["errors"]), 3)

    def test_personal_microsoft_address_beats_a_wrongly_chosen_gmail_server(self):
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"))
        result, _, _ = run_check(smtp, smtp_host="smtp.gmail.com", address="sam@live.com")
        self.assertTrue(any("Personal Outlook.com" in e for e in result["errors"]))
        self.assertFalse(any("apppasswords" in e for e in result["errors"]))

    def test_personal_microsoft_domains(self):
        for address in ("sam@hotmail.com", "Sam@Outlook.com", "x@live.com", "x@msn.com", "x@hotmail.co.uk",
                        "x@hotmail.fr", "x@live.co.uk", "x@outlook.de", "x@hotmail.com.br", "x@windowslive.com"):
            self.assertTrue(is_personal_microsoft(address), address)
        for address in ("sam@acme.test", "sam@acme.onmicrosoft.com", "not-an-email", "", None):
            self.assertFalse(is_personal_microsoft(address), address)
        self.assertIn("microsoft_personal", HINTS)

    def test_outlook_provider_help_warns_personal_accounts_up_front(self):
        help_text = PROVIDERS["outlook"]["help"]
        self.assertIn("Hotmail", help_text)
        self.assertIn("can't be used", help_text)
        self.assertIn("Authenticated SMTP", help_text, "Microsoft 365 work mailboxes still get the admin route")

    def test_zoho_hint(self):
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(535, b"Authentication Failed"))
        result, _, _ = run_check(smtp, smtp_host="smtp.zoho.com")
        self.assertTrue(any(e.startswith("Zoho:") for e in result["errors"]))

    def test_generic_auth_hint_for_other_providers(self):
        smtp = FakeConnection(smtplib.SMTPAuthenticationError(535, b"5.7.8 Error: authentication failed"))
        imap = FakeConnection(imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Authentication failed.'"))
        result, _, _ = run_check(smtp, imap)
        generic = [e for e in result["errors"] if "hosting-panel" in e]
        self.assertEqual(len(generic), 1)

    def test_dns_failure(self):
        result, _, _ = run_check(smtp_error=socket.gaierror(8, "nodename nor servname provided"),
                                 smtp_host="smtp.acme.tset")
        self.assertFalse(result["smtp"])
        self.assertTrue(result["imap"])
        self.assertIn('can\'t find the server "smtp.acme.tset"', result["errors"][0])
        self.assertEqual(len(result["errors"]), 1, "a DNS problem is not a password problem: no hint")

    def test_timeout(self):
        result, _, _ = run_check(imap_error=TimeoutError("timed out"))
        self.assertTrue(result["smtp"])
        self.assertIn("didn't answer in time", result["errors"][0])
        self.assertTrue(result["errors"][0].startswith("Inbox (IMAP):"))

    def test_socket_timeout_alias(self):
        result, _, _ = run_check(smtp_error=socket.timeout("timed out"))
        self.assertIn("didn't answer in time", result["errors"][0])

    def test_connection_refused(self):
        result, _, _ = run_check(smtp_error=ConnectionRefusedError(61, "Connection refused"), smtp_port=2525)
        self.assertIn("refused the connection on port 2525", result["errors"][0])

    def test_ssl_errors(self):
        result, _, _ = run_check(smtp_error=ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"),
                                 smtp_port=587)
        self.assertIn("Port 465 uses SSL and 587 uses STARTTLS", result["errors"][0])
        cert_error = ssl.SSLCertVerificationError(1, "certificate verify failed: Hostname mismatch")
        result, _, _ = run_check(smtp_error=cert_error, smtp_host="mail.mysite.test")
        self.assertIn("security certificate", result["errors"][0])
        self.assertIn("own website", result["errors"][0])

    def test_empty_trust_store_points_at_install_certificates(self):
        """python.org's macOS Python trusts nothing until 'Install Certificates.command' runs; the
        old message blamed the server name, which sends people the wrong way."""
        error = ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")
        with mock.patch.object(email_checks, "trust_store_is_empty", return_value=True):
            result, _, _ = run_check(smtp_error=error, smtp_host="smtp.gmail.com")
        self.assertIn("Install Certificates.command", result["errors"][0])
        self.assertIn("certifi", result["errors"][0])
        self.assertNotIn("own website", result["errors"][0])
        self.assertEqual(len(result["errors"]), 1, "not a password problem: no login hint")

    def test_untrusted_issuer_with_a_working_store_is_explained_differently(self):
        error = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
        with mock.patch.object(email_checks, "trust_store_is_empty", return_value=False):
            result, _, _ = run_check(imap_error=error, imap_host="mail.mysite.test")
        self.assertIn("isn't trusted", result["errors"][0])
        self.assertNotIn("Install Certificates.command", result["errors"][0])

    def test_server_disconnect(self):
        result, _, _ = run_check(smtp_error=smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
        self.assertIn("closed the connection", result["errors"][0])

    def test_imap_disabled(self):
        imap = FakeConnection(imaplib.IMAP4.error("[ALERT] Your account is not enabled for IMAP use."))
        result, _, _ = run_check(imap_conn=imap)
        self.assertIn("IMAP (inbox access) is turned off", result["errors"][0])

    def test_non_ascii_password(self):
        error = UnicodeEncodeError("ascii", "pässword", 1, 2, "ordinal not in range(128)")
        result, _, _ = run_check(FakeConnection(error), password="pässword")
        self.assertIn("characters", result["errors"][0])
        self.assertNotIn("pässword", " ".join(result["errors"]))

    def test_other_smtp_error_shows_server_text_without_password(self):
        smtp = FakeConnection(smtplib.SMTPResponseException(550, f"policy rejected {PASSWORD}".encode()))
        result, _, _ = run_check(smtp)
        self.assertIn("the server said: 550 policy rejected ***", result["errors"][0])
        self.assertNotIn(PASSWORD, " ".join(result["errors"]))

    def test_password_never_in_any_error(self):
        for error in (smtplib.SMTPAuthenticationError(535, f"bad {PASSWORD}".encode()),
                      OSError(f"weird {PASSWORD}"), RuntimeError(PASSWORD)):
            result, _, _ = run_check(FakeConnection(error))
            self.assertNotIn(PASSWORD, " ".join(result["errors"]), repr(error))

    def test_unexpected_error_is_reported_not_raised(self):
        result, _, _ = run_check(smtp_error=RuntimeError("boom"))
        self.assertIn("unexpected error (RuntimeError: boom)", result["errors"][0])

    def test_bad_port_and_missing_host_skip_the_connection(self):
        smtp_factory, imap_factory = factory_for(FakeConnection()), factory_for(FakeConnection())
        result = check_login("me@acme.test", PASSWORD, "smtp.acme.test", "abc", "", 993,
                             smtp_factory=smtp_factory, imap_factory=imap_factory)
        self.assertEqual((result["smtp"], result["imap"]), (False, False))
        self.assertIn("port must be a number", result["errors"][0])
        self.assertIn("no server name set", result["errors"][1])
        self.assertEqual(smtp_factory.calls + imap_factory.calls, [])

    def test_string_ports_are_accepted(self):
        result, smtp_factory, _ = run_check(smtp_port="587")
        self.assertTrue(result["smtp"])
        self.assertEqual(smtp_factory.calls, [("smtp.hostinger.com", 587)])

    def test_failed_goodbye_does_not_fail_a_working_login(self):
        smtp = FakeConnection(close_error=smtplib.SMTPServerDisconnected("gone"))
        result, _, _ = run_check(smtp)
        self.assertTrue(result["smtp"])
        self.assertEqual(result["errors"], [])


class FakeEngine:
    from_email = "hello@acme.test"

    def __init__(self, result=True):
        self.result = result
        self.sent = []

    def send_real_email(self, to, subject, html_body, text_body, extra_headers=None):
        self.sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body,
                          "headers": extra_headers or {}})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class TestVerificationCode(unittest.TestCase):
    def test_make_code_is_six_digits(self):
        codes = {make_code() for _ in range(50)}
        self.assertTrue(all(re.fullmatch(r"\d{6}", c) for c in codes))
        self.assertGreater(len(codes), 1)

    def test_make_code_uses_secrets_and_zero_pads(self):
        with mock.patch.object(email_checks.secrets, "randbelow", return_value=42) as randbelow:
            self.assertEqual(make_code(), "000042")
        randbelow.assert_called_once_with(1_000_000)

    def test_code_matches_is_forgiving_but_exact(self):
        self.assertTrue(code_matches("123456", "123456"))
        self.assertTrue(code_matches("123456", " 123 456 "))
        self.assertTrue(code_matches("123456", "123-456"))
        self.assertFalse(code_matches("123456", "123457"))
        self.assertFalse(code_matches("123456", "12345"))
        self.assertFalse(code_matches("123456", ""))
        self.assertFalse(code_matches("123456", None))
        self.assertFalse(code_matches("", ""))

    def test_message_contains_code_everywhere(self):
        subject, text, html_body = verification_message("048213", "Automated SDR by Fred")
        self.assertEqual(subject, "048213 is your Automated SDR by Fred verification code")
        self.assertIn("048213", text)
        self.assertIn("048213", html_body)
        self.assertIn("nothing else will be sent", text)
        self.assertIn("Automated SDR by Fred setup", text)

    def test_message_escapes_html(self):
        _subject, _text, html_body = verification_message("123456", "<b>Evil</b> & Co")
        self.assertNotIn("<b>Evil</b>", html_body)
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; &amp; Co", html_body)

    def test_default_product_name(self):
        subject, _text, _html = verification_message("123456")
        self.assertIn("Automated SDR by Fred", subject)

    def test_send_returns_the_code_it_sent(self):
        engine = FakeEngine()
        code = send_verification_code(engine, " me@acme.test ")
        self.assertRegex(code, r"^\d{6}$")
        sent = engine.sent[0]
        self.assertEqual(sent["to"], "me@acme.test")
        self.assertTrue(sent["subject"].startswith(code))
        self.assertIn(code, sent["text"])
        self.assertIn(code, sent["html"])
        self.assertTrue(sent["headers"]["Message-ID"].endswith("@acme.test>"))

    def test_failed_send_raises(self):
        with self.assertRaises(EmailCheckError):
            send_verification_code(FakeEngine(result=False), "me@acme.test")

    def test_engine_exception_becomes_email_check_error(self):
        with self.assertRaises(EmailCheckError) as ctx:
            send_verification_code(FakeEngine(result=OSError(f"boom {PASSWORD}")), "me@acme.test")
        self.assertNotIn(PASSWORD, str(ctx.exception))

    def test_invalid_recipient_sends_nothing(self):
        engine = FakeEngine()
        with self.assertRaises(EmailCheckError):
            send_verification_code(engine, "not-an-email")
        self.assertEqual(engine.sent, [])


class TestVerificationThroughRealEngine(unittest.TestCase):
    """The integration path setup uses: a minimal profile + SMTP login in the environment,
    sent through EmailMarketingEngine.send_real_email with smtplib mocked."""

    def setUp(self):
        from bots import email_marketing
        self.email_marketing = email_marketing
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(email_marketing, "load_env_file", lambda: None),
            mock.patch.dict(os.environ, {"SMTP_USER": "me@acme.test", "SMTP_PASS": PASSWORD,
                                         "SMTP_HOST": "smtp.acme.test", "SMTP_PORT": "465", "SENDER_EMAIL": ""}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_code_goes_out_over_smtp(self):
        server = mock.MagicMock()
        smtp_ssl = mock.MagicMock()
        smtp_ssl.return_value.__enter__.return_value = server
        profile = {"sender": {"from_name": "Fred", "product_name": "Acme"}, "outreach": {}}
        with mock.patch.object(self.email_marketing.smtplib, "SMTP_SSL", smtp_ssl), \
                contextlib.redirect_stdout(io.StringIO()):
            engine = self.email_marketing.EmailMarketingEngine(profile)
            code = send_verification_code(engine, "me@acme.test")
        self.assertEqual(smtp_ssl.call_args.args[:2], ("smtp.acme.test", 465))
        server.login.assert_called_once_with("me@acme.test", PASSWORD)
        envelope_from, to, message = server.sendmail.call_args.args
        self.assertEqual((envelope_from, to), ("me@acme.test", "me@acme.test"))
        self.assertIn(f"Subject: {code} is your", message)


class TestSmtpOnlySender(unittest.TestCase):
    """bots/email_marketing.py sends over SMTP only — the Hostinger HTTP API path is gone."""

    def setUp(self):
        from bots import email_marketing
        self.email_marketing = email_marketing
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(email_marketing, "load_env_file", lambda: None),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP sending")),
        ]
        for p in self.patches:
            p.start()
        self.profile = {"sender": {"from_name": "Fred", "product_name": "Acme"}, "outreach": {}}

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def engine(self, env: dict):
        keys = ("SMTP_USER", "SMTP_PASS", "SMTP_HOST", "SENDER_EMAIL", "HOSTINGER_MAIL_API_TOKEN",
                "HOSTINGER_SENDER_EMAIL")
        clean = {k: v for k, v in os.environ.items() if k not in keys}
        if env.get("SMTP_USER") and env.get("SMTP_PASS"):
            env = {"SMTP_HOST": "smtp.acme.test", **env}  # a login without its server is refused (see TestSequence)
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            return self.email_marketing.EmailMarketingEngine(self.profile)

    def test_hostinger_api_is_gone(self):
        self.assertFalse(hasattr(self.email_marketing.EmailMarketingEngine, "send_via_hostinger_api"))
        engine = self.engine({"HOSTINGER_MAIL_API_TOKEN": "tok", "HOSTINGER_SENDER_EMAIL": "api@acme.test"})
        self.assertFalse(hasattr(engine, "hostinger_api_token"))
        self.assertIsNone(engine.smtp_user, "HOSTINGER_SENDER_EMAIL is no longer a login fallback")

    def test_without_login_nothing_is_sent(self):
        engine = self.engine({"HOSTINGER_MAIL_API_TOKEN": "tok"})
        engine.send_via_smtp = mock.Mock(side_effect=AssertionError("no SMTP without login"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(engine.send_real_email("lead@x.test", "Hi", "<p>Hi</p>", "Hi"))
        self.assertIn("nothing was sent", out.getvalue())

    def test_smtp_result_is_final(self):
        engine = self.engine({"SMTP_USER": "me@acme.test", "SMTP_PASS": "x", "HOSTINGER_MAIL_API_TOKEN": "tok"})
        for outcome in (True, False):
            engine.send_via_smtp = mock.Mock(return_value=outcome)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIs(engine.send_real_email("lead@x.test", "Hi", "<p>Hi</p>", "Hi", {"X": "1"}), outcome)
            engine.send_via_smtp.assert_called_once_with("lead@x.test", "Hi", "<p>Hi</p>", "Hi", {"X": "1"})

    def test_from_address_falls_back_to_login(self):
        engine = self.engine({"SMTP_USER": "me@acme.test", "SMTP_PASS": "x"})
        self.assertEqual(engine.from_email, "me@acme.test")


class TestRobotsTxt(unittest.TestCase):
    def setUp(self):
        from bots import leadgen_pipeline
        self.lp = leadgen_pipeline
        self.lp.clear_robots_cache()
        self.logged = []
        self.log_patch = mock.patch.object(leadgen_pipeline, "log_event",
                                           lambda *args: self.logged.append(args))
        self.log_patch.start()

    def tearDown(self):
        self.log_patch.stop()
        self.lp.clear_robots_cache()

    def fetcher(self, responses: dict):
        """fetch(robots_url) from a {robots_url: (status, text) | Exception} table, recording calls."""
        calls = []

        def fetch(url):
            calls.append(url)
            response = responses.get(url, (404, ""))
            if isinstance(response, BaseException):
                raise response
            return response

        fetch.calls = calls
        return fetch

    def test_disallowed_paths_are_blocked_and_others_allowed(self):
        fetch = self.fetcher({"https://acme.test/robots.txt": (200, "User-agent: *\nDisallow: /private\n")})
        self.assertFalse(self.lp.robots_allowed("https://acme.test/private/team", fetch))
        self.assertTrue(self.lp.robots_allowed("https://acme.test/about", fetch))
        self.assertTrue(self.lp.robots_allowed("https://acme.test", fetch))

    def test_one_download_per_host(self):
        fetch = self.fetcher({"https://acme.test/robots.txt": (200, "User-agent: *\nDisallow:\n")})
        for path in ("", "/about", "/contact", "/contact-us"):
            self.lp.robots_allowed("https://acme.test" + path, fetch)
        self.lp.robots_allowed("https://other.test/", fetch)
        self.lp.robots_allowed("http://acme.test/", fetch)  # different scheme = different robots.txt
        self.assertEqual(fetch.calls, ["https://acme.test/robots.txt", "https://other.test/robots.txt",
                                       "http://acme.test/robots.txt"])

    def test_host_case_does_not_split_the_cache(self):
        fetch = self.fetcher({"https://acme.test/robots.txt": (200, "User-agent: *\nDisallow: /\n")})
        self.assertFalse(self.lp.robots_allowed("https://ACME.test/a", fetch))
        self.assertFalse(self.lp.robots_allowed("https://acme.test/b", fetch))
        self.assertEqual(len(fetch.calls), 1)

    def test_fetch_failures_allow(self):
        cases = {
            "https://down.test/robots.txt": OSError("connection reset"),
            "https://slow.test/robots.txt": TimeoutError("timed out"),
            "https://broken.test/robots.txt": (503, "User-agent: *\nDisallow: /\n"),
            "https://missing.test/robots.txt": (404, "User-agent: *\nDisallow: /\n"),
            "https://private.test/robots.txt": (403, ""),
        }
        fetch = self.fetcher(cases)
        for robots_url in cases:
            page = robots_url.replace("/robots.txt", "/contact")
            self.assertTrue(self.lp.robots_allowed(page, fetch), robots_url)

    def test_rules_for_our_user_agent_token(self):
        # Our UA is a desktop-browser string, so the matching group is "Mozilla" or "*".
        fetch = self.fetcher({
            "https://a.test/robots.txt": (200, "User-agent: Mozilla\nDisallow: /\n"),
            "https://b.test/robots.txt": (200, "User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"),
        })
        self.assertFalse(self.lp.robots_allowed("https://a.test/contact", fetch))
        self.assertTrue(self.lp.robots_allowed("https://b.test/contact", fetch))

    def test_byte_order_mark_is_ignored(self):
        fetch = self.fetcher({"https://bom.test/robots.txt": (200, "﻿User-agent: *\nDisallow: /\n")})
        self.assertFalse(self.lp.robots_allowed("https://bom.test/about", fetch))

    def test_non_http_urls_are_not_checked(self):
        fetch = self.fetcher({})
        self.assertTrue(self.lp.robots_allowed("mailto:hi@acme.test", fetch))
        self.assertTrue(self.lp.robots_allowed("not a url", fetch))
        self.assertEqual(fetch.calls, [])

    def test_cache_expires_after_a_day(self):
        clock = [1000.0]
        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None)
        fetch = self.fetcher({"https://acme.test/robots.txt": (200, "User-agent: *\nDisallow:\n")})
        with mock.patch.object(self.lp, "time", fake_time):
            self.lp.robots_allowed("https://acme.test/", fetch)
            clock[0] += self.lp.ROBOTS_CACHE_SECONDS - 1
            self.lp.robots_allowed("https://acme.test/", fetch)
            self.assertEqual(len(fetch.calls), 1)
            clock[0] += 2
            self.lp.robots_allowed("https://acme.test/", fetch)
        self.assertEqual(len(fetch.calls), 2)


class TestSafeGetRespectsRobots(unittest.TestCase):
    def setUp(self):
        from bots import leadgen_pipeline
        self.lp = leadgen_pipeline
        if not leadgen_pipeline.REQUESTS_AVAILABLE:
            self.skipTest("requests is not installed")
        self.lp.clear_robots_cache()
        self.logged = []
        robots = {"https://acme.test/robots.txt": (200, "User-agent: *\nDisallow: /private\n"),
                  "https://studio.test/robots.txt": (200, "User-agent: *\nDisallow: /about\n")}
        self.response = mock.Mock(status_code=200, text="<html></html>")
        self.patches = [
            mock.patch.object(leadgen_pipeline, "log_event", lambda *args: self.logged.append(args)),
            mock.patch.object(leadgen_pipeline, "_fetch_robots_txt",
                              lambda url: robots.get(url, (404, ""))),
            mock.patch.object(leadgen_pipeline.requests, "get", return_value=self.response),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.get = self.mocks[2]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.lp.clear_robots_cache()

    def test_disallowed_url_is_never_fetched(self):
        self.assertIsNone(self.lp.safe_get("https://acme.test/private/staff"))
        self.get.assert_not_called()
        self.assertEqual(len(self.logged), 1)
        self.assertIn("acme.test", self.logged[0][3])

    def test_skip_is_logged_once_per_host(self):
        self.lp.safe_get("https://acme.test/private/a")
        self.lp.safe_get("https://acme.test/private/b")
        self.assertEqual(len(self.logged), 1)

    def test_allowed_url_is_fetched_with_the_existing_user_agent(self):
        self.assertIs(self.lp.safe_get("https://acme.test/contact", timeout=8), self.response)
        self.get.assert_called_once_with("https://acme.test/contact", headers=self.lp.SCRAPER_HEADERS, timeout=8)
        self.assertIn("Chrome/120", self.lp.SCRAPER_HEADERS["User-Agent"])

    def test_scrape_site_skips_disallowed_pages(self):
        with mock.patch.object(self.lp, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                      monotonic=lambda: 0.0)):
            self.lp.scrape_site("https://studio.test/")
        fetched = [c.args[0] for c in self.get.call_args_list]
        self.assertEqual(fetched, ["https://studio.test", "https://studio.test/contact",
                                   "https://studio.test/contact-us"])


class TestCheckEmailLoginScript(unittest.TestCase):
    def setUp(self):
        import check_email_login
        self.script = check_email_login
        self.env_patch = mock.patch.object(check_email_login, "load_env_file", lambda: None)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def run_main(self, env: dict, result: dict | None = None):
        keys = ("SMTP_USER", "SMTP_PASS", "SMTP_HOST", "SMTP_PORT", "IMAP_HOST", "IMAP_PORT")
        clean = {k: v for k, v in os.environ.items() if k not in keys}
        check = mock.Mock(return_value=result or {"smtp": True, "imap": True, "errors": []})
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok = self.script.main(check=check)
        return ok, check, out.getvalue()

    def test_checks_the_env_login_without_printing_the_password(self):
        ok, check, output = self.run_main({"SMTP_USER": "me@acme.test", "SMTP_PASS": PASSWORD,
                                           "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "465",
                                           "IMAP_HOST": "imap.gmail.com"})
        self.assertTrue(ok)
        check.assert_called_once_with(address="me@acme.test", password=PASSWORD, smtp_host="smtp.gmail.com",
                                      smtp_port=465, imap_host="imap.gmail.com", imap_port=993)
        self.assertIn("Sending (SMTP) : OK", output)
        self.assertNotIn(PASSWORD, output)

    def test_missing_login(self):
        ok, check, output = self.run_main({})
        self.assertFalse(ok)
        check.assert_not_called()
        self.assertIn("[NOT SET]", output)
        self.assertIn("sdr setup", output)

    def test_failure_prints_friendly_errors(self):
        ok, _check, output = self.run_main(
            {"SMTP_USER": "me@acme.test", "SMTP_PASS": PASSWORD,
             "SMTP_HOST": "smtp.acme.test", "IMAP_HOST": "imap.acme.test"},
            {"smtp": False, "imap": True, "errors": ["Sending (SMTP): nope."]})
        self.assertFalse(ok)
        self.assertIn("Sending (SMTP) : FAILED", output)
        self.assertIn("Sending (SMTP): nope.", output)
        self.assertIn("smtp.acme.test:465", output)

    def test_login_without_a_host_is_never_sent_to_a_guessed_server(self):
        ok, check, output = self.run_main({"SMTP_USER": "me@gmail.com", "SMTP_PASS": PASSWORD})
        self.assertFalse(ok)
        check.assert_not_called()
        self.assertIn("SMTP_HOST is missing", output)
        self.assertIn("sdr setup --section email", output)
        self.assertNotIn("hostinger", output.lower())
        self.assertNotIn(PASSWORD, output)


if __name__ == "__main__":
    unittest.main()
