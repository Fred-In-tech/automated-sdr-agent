"""Offline tests for the TLS side of core/email_checks.py: the shared certificate-verifying
context (system store + certifi, for the python.org macOS build whose own store is empty) and the
real SMTP/IMAP connection code with smtplib/imaplib replaced by mocks. No network, no email."""

import importlib.util
import smtplib
import ssl
import tempfile
import unittest
from unittest import mock

from core import email_checks
from core.email_checks import check_login, tls_context

PASSWORD = "hunter2-secret"
HAS_CERTIFI = importlib.util.find_spec("certifi") is not None


class TestTlsContext(unittest.TestCase):
    """One certificate-verifying context for every SMTP/IMAP connection, with certifi's roots
    added so a python.org Python on macOS (empty system store) still verifies mail servers."""

    def test_verifies_certificates_and_hostnames(self):
        context = tls_context()
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    @unittest.skipUnless(HAS_CERTIFI, "certifi is not installed")
    def test_adds_certifi_roots_when_the_system_store_is_empty(self):
        empty_store = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # what python.org's macOS build starts with
        self.assertEqual(empty_store.cert_store_stats()["x509_ca"], 0)
        with mock.patch.object(email_checks.ssl, "create_default_context", return_value=empty_store):
            context = tls_context()
        self.assertGreater(context.cert_store_stats()["x509_ca"], 100)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_system_store_is_kept_when_certifi_is_missing(self):
        with mock.patch.object(email_checks, "_certifi_bundle", return_value=None):
            context = tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_a_damaged_bundle_never_breaks_the_connection(self):
        with tempfile.NamedTemporaryFile(suffix=".pem") as empty_pem, \
                mock.patch.object(email_checks, "_certifi_bundle", return_value=empty_pem.name):
            context = tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with mock.patch.object(email_checks, "_certifi_bundle", return_value="/nowhere/cacert.pem"):
            self.assertEqual(tls_context().verify_mode, ssl.CERT_REQUIRED)

    def test_trust_store_is_empty_reports_the_real_store(self):
        with mock.patch.object(email_checks, "tls_context", return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)):
            self.assertTrue(email_checks.trust_store_is_empty())
        with mock.patch.object(email_checks, "tls_context", side_effect=RuntimeError("boom")):
            self.assertFalse(email_checks.trust_store_is_empty(), "a failed probe must not invent a diagnosis")

    def test_login_check_factories_share_the_context(self):
        shared = ssl.create_default_context()
        with mock.patch.object(email_checks, "tls_context", return_value=shared), \
                mock.patch.object(email_checks.smtplib, "SMTP_SSL") as smtp_ssl, \
                mock.patch.object(email_checks.smtplib, "SMTP") as smtp, \
                mock.patch.object(email_checks.imaplib, "IMAP4_SSL") as imap_ssl:
            email_checks._default_smtp_factory("smtp.acme.test", 465)
            server = email_checks._default_smtp_factory("smtp.acme.test", 587)
            email_checks._default_imap_factory("imap.acme.test", 993)
        self.assertIs(smtp_ssl.call_args.kwargs["context"], shared)
        self.assertIs(server.starttls.call_args.kwargs["context"], shared)
        self.assertIs(imap_ssl.call_args.kwargs["ssl_context"], shared)
        smtp.assert_called_once_with("smtp.acme.test", 587, timeout=email_checks.LOGIN_TIMEOUT_SECONDS)


class TestDefaultFactories(unittest.TestCase):
    """The real connection code, with smtplib/imaplib replaced by mocks."""

    def test_port_465_uses_ssl(self):
        with mock.patch.object(email_checks.smtplib, "SMTP_SSL") as smtp_ssl:
            email_checks._default_smtp_factory("smtp.acme.test", 465)
        args, kwargs = smtp_ssl.call_args
        self.assertEqual(args, ("smtp.acme.test", 465))
        self.assertIsInstance(kwargs["context"], ssl.SSLContext)
        self.assertEqual(kwargs["timeout"], email_checks.LOGIN_TIMEOUT_SECONDS)

    def test_other_ports_require_starttls(self):
        with mock.patch.object(email_checks.smtplib, "SMTP") as smtp:
            server = email_checks._default_smtp_factory("smtp.acme.test", 587)
        smtp.assert_called_once_with("smtp.acme.test", 587, timeout=email_checks.LOGIN_TIMEOUT_SECONDS)
        server.starttls.assert_called_once()

    def test_no_starttls_means_no_password_sent(self):
        server = mock.Mock()
        server.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        with mock.patch.object(email_checks.smtplib, "SMTP", return_value=server):
            result = check_login("me@acme.test", PASSWORD, "smtp.acme.test", 25, "imap.acme.test", 993,
                                 imap_factory=lambda host, port: mock.MagicMock())
        self.assertFalse(result["smtp"])
        self.assertIn("doesn't offer an encrypted connection", result["errors"][0])
        server.login.assert_not_called()
        server.close.assert_called_once()

    def test_imap_uses_ssl(self):
        with mock.patch.object(email_checks.imaplib, "IMAP4_SSL") as imap_ssl:
            email_checks._default_imap_factory("imap.acme.test", 993)
        args, kwargs = imap_ssl.call_args
        self.assertEqual(args, ("imap.acme.test", 993))
        self.assertIsInstance(kwargs["ssl_context"], ssl.SSLContext)
        self.assertEqual(kwargs["timeout"], email_checks.LOGIN_TIMEOUT_SECONDS)

    def test_check_login_with_default_factories(self):
        smtp_server, imap_server = mock.MagicMock(), mock.MagicMock()
        with mock.patch.object(email_checks.smtplib, "SMTP_SSL", return_value=smtp_server), \
                mock.patch.object(email_checks.imaplib, "IMAP4_SSL", return_value=imap_server):
            result = check_login("me@acme.test", PASSWORD, "smtp.acme.test", 465, "imap.acme.test", 993)
        self.assertEqual(result, {"smtp": True, "imap": True, "errors": []})
        smtp_server.login.assert_called_once_with("me@acme.test", PASSWORD)
        smtp_server.quit.assert_called_once()
        imap_server.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
