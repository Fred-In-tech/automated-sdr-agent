"""Provider presets, the local-only / https safety rules and config loading (incl. old [ai] profiles)."""

import unittest

from core.ai.providers import (JOB_DEFAULTS, PRESETS, data_notice, is_private_host, load_ai_config,
                               url_problem)

ALL = {"openai", "anthropic", "gemini", "groq", "openrouter", "mistral", "azure",
       "ollama", "lmstudio", "llamacpp", "vllm", "jan", "custom"}


def cfg(ai: dict, env: dict | None = None):
    return load_ai_config({"ai": ai}, environ=env or {})


class TestPresets(unittest.TestCase):
    def test_every_provider_has_a_preset(self):
        self.assertEqual(set(PRESETS), ALL)

    def test_local_presets_point_at_this_computer(self):
        for name in ("ollama", "lmstudio", "llamacpp", "vllm", "jan"):
            self.assertTrue(PRESETS[name].local, name)
            self.assertTrue(is_private_host(PRESETS[name].base_url), name)

    def test_adapters(self):
        self.assertEqual(PRESETS["anthropic"].adapter, "anthropic")
        self.assertEqual(PRESETS["ollama"].adapter, "ollama")
        self.assertEqual(PRESETS["gemini"].adapter, "openai")
        self.assertEqual(PRESETS["azure"].auth, "api-key")


class TestLoadConfig(unittest.TestCase):
    def test_no_ai_table_means_off(self):
        self.assertEqual(load_ai_config({}, environ={}).mode, "off")

    def test_new_style_local(self):
        c = cfg({"mode": "local_only", "provider": "ollama", "model": "llama3.2"})
        self.assertEqual((c.mode, c.adapter, c.base_url, c.model), ("local_only", "ollama", "http://localhost:11434", "llama3.2"))
        self.assertIsNone(c.api_key)

    def test_base_url_override_and_key_from_named_env(self):
        c = cfg({"mode": "hosted", "provider": "custom", "base_url": "https://llm.example.com/v1/",
                 "model": "m", "api_key_env": "MY_KEY"}, {"MY_KEY": "sk-123"})
        self.assertEqual(c.base_url, "https://llm.example.com/v1")
        self.assertEqual(c.api_key, "sk-123")

    def test_key_defaults_to_ai_api_key(self):
        self.assertEqual(cfg({"mode": "hosted", "provider": "openai", "model": "m"}, {"AI_API_KEY": "k"}).api_key, "k")

    def test_legacy_enabled_profile_maps_to_hosted_anthropic(self):
        c = cfg({"enabled": True, "model": "claude-x"}, {"ANTHROPIC_API_KEY": "ak"})
        self.assertEqual((c.mode, c.provider, c.model, c.api_key), ("hosted", "anthropic", "claude-x", "ak"))

    def test_legacy_enabled_without_key_is_off(self):
        self.assertEqual(cfg({"enabled": True, "model": "claude-x"}).mode, "off")

    def test_legacy_disabled_is_off(self):
        self.assertEqual(cfg({"enabled": False, "model": "claude-x"}, {"ANTHROPIC_API_KEY": "ak"}).mode, "off")

    def test_unknown_mode_or_provider_is_off(self):
        self.assertEqual(cfg({"mode": "turbo", "provider": "ollama"}).mode, "off")
        self.assertEqual(cfg({"mode": "hosted", "provider": "skynet"}).mode, "off")

    def test_job_modes_and_defaults(self):
        c = cfg({"mode": "local_only", "provider": "ollama", "jobs": {"openers": "review", "lead_fit": "review",
                                                                       "answer_questions": "bogus"}})
        self.assertEqual(c.jobs["openers"], "review")
        self.assertEqual(c.jobs["lead_fit"], JOB_DEFAULTS["lead_fit"])          # review not allowed for display-only job
        self.assertEqual(c.jobs["answer_questions"], JOB_DEFAULTS["answer_questions"])
        self.assertEqual(c.jobs["reply_intent"], "auto")

    def test_timeout_and_budget_are_numbers(self):
        c = cfg({"mode": "hosted", "provider": "openai", "timeout_seconds": "nope", "monthly_budget_usd": 5})
        self.assertEqual((c.timeout, c.monthly_budget_usd), (60.0, 5.0))


class TestSafety(unittest.TestCase):
    def test_private_hosts(self):
        for url in ("http://127.0.0.1:1234/v1", "http://localhost:11434", "http://10.0.0.5/v1", "http://192.168.1.9",
                    "http://172.16.4.2", "http://[::1]:8000", "http://studio-mac.local:1234"):
            self.assertTrue(is_private_host(url), url)
        for url in ("https://api.openai.com/v1", "http://8.8.8.8", "https://ollama.com/v1", "not a url"):
            self.assertFalse(is_private_host(url), url)

    def test_local_only_refuses_internet_hosts(self):
        self.assertIn("this computer", url_problem(cfg({"mode": "local_only", "provider": "openai"})))
        self.assertIsNone(url_problem(cfg({"mode": "local_only", "provider": "lmstudio"})))

    def test_hosted_needs_https_except_on_private_hosts(self):
        bad = cfg({"mode": "hosted", "provider": "custom", "base_url": "http://llm.example.com/v1"})
        self.assertIn("https", url_problem(bad))
        self.assertIsNone(url_problem(cfg({"mode": "hosted", "provider": "ollama"})))
        self.assertIsNone(url_problem(cfg({"mode": "hosted", "provider": "openai"})))

    def test_custom_needs_a_url(self):
        self.assertIsNotNone(url_problem(cfg({"mode": "hosted", "provider": "custom"})))

    def test_data_notice(self):
        self.assertIn("nothing leaves this computer", data_notice(cfg({"mode": "local_only", "provider": "ollama"})))
        self.assertIn("sent to OpenAI", data_notice(cfg({"mode": "hosted", "provider": "openai"})))
        self.assertIn("off", data_notice(load_ai_config({}, environ={})).lower())


if __name__ == "__main__":
    unittest.main()
