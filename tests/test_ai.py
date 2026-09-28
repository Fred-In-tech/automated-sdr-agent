"""Offline tests for the optional Claude layer: a fake client stands in for the API."""

import sys
import types
import unittest
from unittest import mock

from core import ai

fake_sdk = types.SimpleNamespace(
    APIStatusError=type("APIStatusError", (Exception,), {}),
    APIConnectionError=type("APIConnectionError", (Exception,), {}),
)
PROFILE = {"ai": {"enabled": True, "model": "claude-opus-5"}}


def fake_client(text: str | None, stop_reason: str = "end_turn"):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        content = [types.SimpleNamespace(type="text", text=text)] if text is not None else []
        return types.SimpleNamespace(stop_reason=stop_reason, content=content)

    client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create)))
    return client, calls


class TestAI(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"anthropic": fake_sdk})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opener_is_used_when_valid(self):
        client, calls = fake_client("Your barn wedding film at Prairie Loft had a lovely, unhurried pace.")
        opener = ai.write_opener(PROFILE, "Prairie Films", "wedding videographer", "barn weddings...", client)
        self.assertTrue(opener.startswith("Your barn wedding film"))
        self.assertEqual(calls[0]["model"], "claude-opus-5")
        self.assertEqual(calls[0]["fallbacks"], "default")
        self.assertIn("<website_text>", calls[0]["messages"][0]["content"])

    def test_unsafe_or_empty_openers_fall_back(self):
        for bad in ("NONE", "Visit https://evil.test now.", "Line one\nLine two is here", "Hi"):
            client, _ = fake_client(bad)
            self.assertIsNone(ai.write_opener(PROFILE, "X", "y", "text", client), bad)

    def test_refusal_falls_back(self):
        client, _ = fake_client(None, stop_reason="refusal")
        self.assertIsNone(ai.write_opener(PROFILE, "X", "y", "text", client))

    def test_ai_triage_uses_schema(self):
        client, calls = fake_client('{"intent": "not_now"}')
        intent = ai.classify_reply_with_ai(PROFILE, "Ask me again after October", ["unsubscribe"], client)
        self.assertEqual(intent, "not_now")
        self.assertEqual(calls[0]["output_config"]["format"]["schema"]["properties"]["intent"]["enum"],
                         list(ai.INTENTS))

    def test_opt_out_never_goes_to_the_model(self):
        client, calls = fake_client('{"intent": "interested"}')
        self.assertEqual(ai.classify_reply_with_ai(PROFILE, "please unsubscribe me", ["unsubscribe"], client),
                         "unsubscribe")
        self.assertEqual(calls, [])

    def test_bad_ai_answer_falls_back_to_rules(self):
        client, _ = fake_client("not json")
        self.assertEqual(ai.classify_reply_with_ai(PROFILE, "Sounds good!", ["unsubscribe"], client), "interested")

    def test_disabled_without_key(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(ai.ai_enabled(PROFILE))
        self.assertFalse(ai.ai_enabled({"ai": {"enabled": False}}))
