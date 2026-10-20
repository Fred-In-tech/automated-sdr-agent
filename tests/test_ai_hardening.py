"""Fixes from the v1.3 security review: stricter draft checks, local-only that can't be
tricked, no redirects, key variables that can't point at other secrets."""

import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from core.ai import providers
from core.ai.connectors import AIError, chat, default_opener
from core.ai.jobs import UsageLog, is_valid_answer, is_valid_opener
from core.ai.providers import is_private_host, load_ai_config, url_problem

PROFILE = {"ai": {"facts": "Plans start at $29 a month. Free 30-day trial."}, "sender": {"product_name": "Brightline"}}


class TestStricterDrafts(unittest.TestCase):
    def test_phishing_style_openers_are_rejected(self):
        for bad in ("Visit evil.com/pay to confirm your invoice today.",
                    "Your venue is lovely, call 555-0100 to claim your listing.",
                    "Your account will be suspended unless you verify your password.",
                    "Your rooftop ceremonies look great, see brightline.io for more."):
            self.assertFalse(is_valid_opener(bad), bad)
        self.assertTrue(is_valid_opener("Your rooftop ceremonies in East Austin look calm and well planned."))

    def test_risky_answers_are_rejected(self):
        for bad in ("Please pay 50 dollars to evil.example/pay for a refund.",
                    "Call 512-555-0100 and we'll sort it out.",
                    "You get 50% off this week only.",
                    "It's only USD 99 a year.",
                    "Send your password and we'll set it up."):
            self.assertFalse(is_valid_answer(bad, PROFILE), bad)
        self.assertTrue(is_valid_answer("Plans start at $29 a month, with a free 30-day trial.", PROFILE))
        self.assertTrue(is_valid_answer("Yes, the free trial lasts 30 days.", PROFILE))


class TestLocalOnlyCantBeTricked(unittest.TestCase):
    def test_ipv4_mapped_public_address_is_not_private(self):
        self.assertFalse(is_private_host("http://[::ffff:8.8.8.8]:11434"))
        self.assertTrue(is_private_host("http://[::ffff:127.0.0.1]:11434"))

    def setUp(self):
        providers._name_is_private.cache_clear()
        self.addCleanup(providers._name_is_private.cache_clear)

    def test_names_are_resolved(self):
        with mock.patch.object(providers.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            self.assertFalse(is_private_host("http://studio.local:1234"))
        providers._name_is_private.cache_clear()
        with mock.patch.object(providers.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.20", 0))]):
            self.assertTrue(is_private_host("http://studio.local:1234"))
        providers._name_is_private.cache_clear()
        with mock.patch.object(providers.socket, "getaddrinfo", side_effect=OSError("no such host")):
            self.assertFalse(is_private_host("http://nowhere.local"))
        self.assertTrue(is_private_host("http://localhost:11434"))

    def test_userinfo_in_the_address_is_refused(self):
        cfg = load_ai_config({"ai": {"mode": "hosted", "provider": "custom", "model": "m",
                                     "base_url": "https://user:secret@llm.example.com/v1"}}, environ={})
        self.assertIn("password", url_problem(cfg))


class TestNoRedirects(unittest.TestCase):
    def test_redirects_are_refused(self):
        handler = [h for h in default_opener.handlers if h.__class__.__name__ == "_NoRedirect"][0]
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, io.BytesIO(), 302, "Found", {}, "https://elsewhere.example/")

    def test_a_redirect_becomes_a_plain_error(self):
        def redirecting(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 302, "Redirects are not followed", {}, io.BytesIO(b""))
        cfg = load_ai_config({"ai": {"mode": "hosted", "provider": "openai", "model": "m"}}, environ={"AI_API_KEY": "k"})
        with self.assertRaises(AIError) as err:
            chat(cfg, "s", "u", opener=redirecting)
        self.assertIn("redirect", str(err.exception).lower())


class TestKeyVariable(unittest.TestCase):
    def test_key_variable_cannot_point_at_other_secrets(self):
        env = {"SMTP_PASS": "mailbox-password", "AI_API_KEY": "k"}
        for name in ("SMTP_PASS", "DASHBOARD_PASSWORD_HASH", "IMAP_PASSWORD"):
            cfg = load_ai_config({"ai": {"mode": "hosted", "provider": "openai", "model": "m", "api_key_env": name}},
                                 environ=env)
            self.assertNotEqual(cfg.api_key, "mailbox-password")
            self.assertIsNone(cfg.api_key, name)


class TestBudgetFile(unittest.TestCase):
    def test_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ai_usage.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(UsageLog(path).month_cost(), float("inf"))


if __name__ == "__main__":
    unittest.main()
