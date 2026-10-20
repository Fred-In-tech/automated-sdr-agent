"""The AI section of setup and the Settings page: what is saved where, secrets kept in .env,
old Claude profiles, and finding a local AI already running on this computer."""

import os
import stat
import tomllib
import unittest

from core import settings_api
from core.ai.providers import load_ai_config
from core.setup_ai import probe_local_ai
from core.setup_profile import build_profile_from_answers, state_from_profile
from core.setup_questions import BY_KEY, SECTIONS, normalize_section
from core.setup_toml import read_env_file
from test_settings_api import SettingsCase

KEY = "sk-test-not-real-123"


class TestAISection(SettingsCase):
    def profile(self) -> dict:
        with open(self.profile_path, "rb") as f:
            return tomllib.load(f)

    def save(self, values):
        return settings_api.save_section("ai", values, self.profile_path, self.env_path, deps=self.deps)

    def test_section_exists(self):
        self.assertIn("ai", SECTIONS)
        self.assertEqual(normalize_section("llm"), "ai")
        for key in ("ai.mode", "ai.provider", "ai.model", "ai.base_url", "ai.facts", "ai.openers",
                    "ai.answer_questions", "ai.monthly_budget_usd"):
            self.assertIn(key, BY_KEY)

    def test_new_profiles_have_ai_off(self):
        ai = tomllib.loads(build_profile_from_answers({"business.website": "https://a.example"}))["ai"]
        self.assertEqual(ai["mode"], "off")
        self.assertEqual(ai["answer_questions"], "review")

    def test_local_ai_saved_without_any_key(self):
        result = self.save({"ai.mode": "local_only", "ai.provider": "ollama", "ai.model": "llama3.2",
                            "ai.facts": "Plans start at $29 a month.", "ai.openers": "review"})
        self.assertTrue(result["success"], result)
        ai = self.profile()["ai"]
        self.assertEqual((ai["mode"], ai["provider"], ai["model"], ai["openers"]), ("local_only", "ollama", "llama3.2", "review"))
        cfg = load_ai_config(self.profile(), environ={})
        self.assertEqual(cfg.jobs["openers"], "review")
        self.assertNotIn("AI_API_KEY", read_env_file(self.env_path))

    def test_hosted_key_goes_to_env_never_the_profile(self):
        result = self.save({"ai.mode": "hosted", "ai.provider": "openai", "ai.model": "gpt-test", "ai.api_key_env": KEY})
        self.assertTrue(result["success"], result)
        self.assertEqual(read_env_file(self.env_path)["AI_API_KEY"], KEY)
        with open(self.profile_path, encoding="utf-8") as f:
            self.assertNotIn(KEY, f.read())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)
        self.assertNotIn(KEY, str(result))
        fields = {f["key"]: f for s in self.read()["sections"] if s["name"] == "ai" for f in s["fields"]}
        self.assertTrue(fields["ai.api_key_env"]["is_set"])
        self.assertNotIn(KEY, str(self.read()))

    def test_saving_again_without_a_key_keeps_it(self):
        self.save({"ai.mode": "hosted", "ai.provider": "openai", "ai.model": "gpt-test", "ai.api_key_env": KEY})
        result = self.save({"ai.mode": "hosted", "ai.provider": "openai", "ai.model": "gpt-other"})
        self.assertTrue(result["success"], result)
        self.assertEqual(read_env_file(self.env_path)["AI_API_KEY"], KEY)
        self.assertEqual(self.profile()["ai"]["model"], "gpt-other")

    def test_local_only_offers_only_local_providers(self):
        result = self.save({"ai.mode": "local_only", "ai.provider": "openai", "ai.model": "gpt"})
        self.assertFalse(result["success"])
        self.assertIn("ollama", " ".join(result["messages"]))

    def test_local_only_custom_address_on_the_internet_is_refused(self):
        result = self.save({"ai.mode": "local_only", "ai.provider": "custom", "ai.model": "m",
                            "ai.base_url": "https://llm.example.com/v1"})
        self.assertFalse(result["success"])
        self.assertIn("this computer", " ".join(result["messages"]))

    def test_hosted_plain_http_on_the_internet_is_refused(self):
        result = self.save({"ai.mode": "hosted", "ai.provider": "custom", "ai.model": "m",
                            "ai.base_url": "http://llm.example.com/v1"})
        self.assertFalse(result["success"])
        self.assertIn("https", " ".join(result["messages"]))

    def test_turning_ai_off(self):
        self.save({"ai.mode": "local_only", "ai.provider": "ollama", "ai.model": "llama3.2"})
        self.assertTrue(self.save({"ai.mode": "off"})["success"])
        self.assertEqual(self.profile()["ai"]["mode"], "off")

    def test_legacy_profile_state(self):
        state = state_from_profile({"ai": {"enabled": True, "model": "claude-x"}}, {"ANTHROPIC_API_KEY": "x"})
        self.assertEqual((state["ai.mode"], state["ai.provider"], state["ai.model"]), ("hosted", "anthropic", "claude-x"))


class TestProbe(unittest.TestCase):
    def test_finds_a_running_ollama(self):
        def fake_list(cfg, **kwargs):
            if cfg.provider == "ollama":
                return ["llama3.2", "qwen3"]
            raise OSError("nothing here")
        found = probe_local_ai(list_fn=fake_list)
        self.assertEqual(found, {"provider": "ollama", "models": ["llama3.2", "qwen3"]})

    def test_nothing_running(self):
        from core.ai.connectors import AIError

        def nothing(cfg, **kwargs):
            raise AIError("not running")
        self.assertEqual(probe_local_ai(list_fn=nothing), {})


if __name__ == "__main__":
    unittest.main()
