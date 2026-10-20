"""Profiles from before v1.3 ([ai] enabled = true + ANTHROPIC_API_KEY) keep working on the new
AI layer, and the old function names still behave. The AI call itself is faked."""

import os
import tempfile
import unittest
from unittest import mock

from core import ai
from core.ai.connectors import Reply
from core.ai.jobs import AIRun, UsageLog, write_opener

LEGACY = {"ai": {"enabled": True, "model": "claude-opus-5"}}


class TestLegacyProfiles(unittest.TestCase):
    def test_legacy_profile_with_key_uses_claude(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ak-test"}):
            self.assertTrue(ai.ai_enabled(LEGACY))
            run = AIRun(LEGACY)
        self.assertEqual((run.cfg.provider, run.cfg.model, run.cfg.mode), ("anthropic", "claude-opus-5", "hosted"))

    def test_legacy_profile_without_key_is_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ai.ai_enabled(LEGACY))

    def test_old_triage_name_uses_the_ai_then_rules(self):
        calls = []

        def fake_chat(cfg, system, user, **kwargs):
            calls.append(cfg.provider)
            return Reply('{"intent": "question"}')

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ak-test"}), mock.patch("core.ai.jobs.chat", fake_chat):
            self.assertEqual(ai.classify_reply_with_ai(LEGACY, "how much is it?", ["unsubscribe"]), "question")
            self.assertEqual(ai.classify_reply_with_ai(LEGACY, "unsubscribe me", ["unsubscribe"]), "unsubscribe")
        self.assertEqual(calls, ["anthropic"])          # the opt-out never went to the AI

    def test_opener_through_the_new_api(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ak-test"}), \
                mock.patch("core.ai.jobs.chat", lambda *a, **k: Reply("Your barn wedding film at Prairie Loft had a lovely pace.")):
            self.assertTrue(write_opener(AIRun(LEGACY, usage=MemoryUsage()), "Prairie", "videographer", "barn").startswith("Your barn"))


class MemoryUsage:
    def month_cost(self):
        return 0.0

    def add(self, cost):
        pass


class TestUsageLog(unittest.TestCase):
    def test_spend_is_saved_per_month_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = UsageLog(os.path.join(tmp, "data", "ai_usage.json"))
            self.assertEqual(log.month_cost(), 0.0)
            log.add(0.25)
            log.add(0.5)
            self.assertAlmostEqual(UsageLog(log.path).month_cost(), 0.75)
            if os.name != "nt":
                self.assertEqual(os.stat(log.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
