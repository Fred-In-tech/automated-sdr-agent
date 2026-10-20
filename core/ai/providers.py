"""Which AI to talk to, and whether that is allowed.

Owners bring their own AI: a hosted provider with their own key, or a model running on their own
computer (Ollama, LM Studio...). Most of them speak the OpenAI Chat Completions format, so three
adapters cover everything (see core/ai/connectors.py). This module holds the presets, reads the
[ai] settings (including profiles from before v1.3, which only knew Claude), and decides whether a
URL may be used: "local only" never lets data leave this computer or the home network, and
anything on the internet must use https.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

MODES = ("off", "local_only", "hosted")
JOB_NAMES = ("openers", "reply_intent", "answer_questions", "lead_fit")
# Which settings each job accepts. Labelling replies and explaining fit never send anything,
# so they have nothing to review.
JOB_CHOICES = {
    "openers": ("off", "review", "auto"),
    "reply_intent": ("off", "auto"),
    "answer_questions": ("off", "review", "auto"),
    "lead_fit": ("off", "auto"),
}
JOB_DEFAULTS = {"openers": "auto", "reply_intent": "auto", "answer_questions": "review", "lead_fit": "auto"}
DEFAULT_TIMEOUT = 60.0
DEFAULT_KEY_ENV = "AI_API_KEY"
LEGACY_KEY_ENV = "ANTHROPIC_API_KEY"


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    adapter: str        # "openai" | "anthropic" | "ollama"
    base_url: str
    auth: str           # "bearer" | "api-key" | "x-api-key" | "none"
    local: bool
    needs_key: bool


PRESETS: dict[str, Preset] = {p.name: p for p in (
    Preset("openai", "OpenAI", "openai", "https://api.openai.com/v1", "bearer", False, True),
    Preset("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com/v1", "x-api-key", False, True),
    Preset("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
           "bearer", False, True),
    Preset("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "bearer", False, True),
    Preset("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "bearer", False, True),
    Preset("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "bearer", False, True),
    Preset("azure", "Azure OpenAI", "openai", "", "api-key", False, True),
    Preset("ollama", "Ollama", "ollama", "http://localhost:11434", "none", True, False),
    Preset("lmstudio", "LM Studio", "openai", "http://localhost:1234/v1", "none", True, False),
    Preset("llamacpp", "llama.cpp server", "openai", "http://localhost:8080/v1", "none", True, False),
    Preset("vllm", "vLLM", "openai", "http://localhost:8000/v1", "bearer", True, False),
    Preset("jan", "Jan", "openai", "http://localhost:1337/v1", "bearer", True, False),
    Preset("custom", "Custom (OpenAI-compatible)", "openai", "", "bearer", False, False),
)}


@dataclass(frozen=True)
class AIConfig:
    mode: str = "off"
    provider: str = "ollama"
    adapter: str = "ollama"
    base_url: str = ""
    model: str = ""
    api_key: str | None = field(default=None, repr=False)   # never printed, never logged
    auth: str = "none"
    timeout: float = DEFAULT_TIMEOUT
    monthly_budget_usd: float = 0.0
    jobs: Mapping[str, str] = field(default_factory=lambda: dict(JOB_DEFAULTS))

    @property
    def label(self) -> str:
        return PRESETS[self.provider].label if self.provider in PRESETS else self.provider

    @property
    def is_local(self) -> bool:
        return is_private_host(self.base_url)


def _number(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _jobs(raw: Mapping | None) -> dict[str, str]:
    raw = raw if isinstance(raw, Mapping) else {}
    jobs = {}
    for name in JOB_NAMES:
        value = str(raw.get(name, JOB_DEFAULTS[name])).strip().lower()
        jobs[name] = value if value in JOB_CHOICES[name] else JOB_DEFAULTS[name]
    return jobs


def _legacy(ai: Mapping, environ: Mapping[str, str]) -> AIConfig:
    """Profiles from before v1.3: [ai] enabled = true meant Claude with ANTHROPIC_API_KEY."""
    key = environ.get(LEGACY_KEY_ENV) or None
    if not ai.get("enabled") or not key:
        return AIConfig()
    preset = PRESETS["anthropic"]
    return AIConfig(mode="hosted", provider="anthropic", adapter=preset.adapter, base_url=preset.base_url,
                    model=str(ai.get("model") or ""), api_key=key, auth=preset.auth, jobs=_jobs(ai.get("jobs")))


def load_ai_config(profile: Mapping, environ: Mapping[str, str] | None = None) -> AIConfig:
    """The [ai] settings as an AIConfig. Anything unknown or incomplete means AI is off."""
    environ = os.environ if environ is None else environ
    ai = profile.get("ai") or {}
    if not isinstance(ai, Mapping):
        return AIConfig()
    if "mode" not in ai and "enabled" in ai:
        return _legacy(ai, environ)
    mode = str(ai.get("mode", "off")).strip().lower()
    provider = str(ai.get("provider", "ollama")).strip().lower()
    if mode not in MODES or mode == "off" or provider not in PRESETS:
        return AIConfig()
    preset = PRESETS[provider]
    base_url = str(ai.get("base_url") or preset.base_url).strip().rstrip("/")
    key_env = str(ai.get("api_key_env") or DEFAULT_KEY_ENV).strip()
    key = environ.get(key_env) or (environ.get(LEGACY_KEY_ENV) if provider == "anthropic" else None) or None
    return AIConfig(mode=mode, provider=provider, adapter=preset.adapter, base_url=base_url,
                    model=str(ai.get("model") or "").strip(), api_key=key, auth=preset.auth,
                    timeout=_number(ai.get("timeout_seconds"), DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT,
                    monthly_budget_usd=_number(ai.get("monthly_budget_usd"), 0.0), jobs=_jobs(ai.get("jobs")))


def is_private_host(url: str) -> bool:
    """True for this computer and the home/office network: loopback, private ranges, link-local,
    `localhost` and `*.local` names."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def url_problem(cfg: AIConfig) -> str | None:
    """Why this AI address may not be used, in plain words, or None when it's fine."""
    if cfg.mode == "off":
        return "AI is off."
    if not cfg.base_url:
        return f"{cfg.label} needs an address (base URL). Add it in Settings > AI."
    scheme = urlsplit(cfg.base_url).scheme.lower()
    if scheme not in ("http", "https"):
        return "The AI address must start with http:// or https://."
    private = is_private_host(cfg.base_url)
    if cfg.mode == "local_only" and not private:
        return (f"'Local only' keeps everything on this computer or your own network, but {cfg.base_url} is on "
                "the internet. Choose a local AI (like Ollama or LM Studio) or switch the mode to 'Hosted'.")
    if scheme == "http" and not private:
        return "AI providers on the internet must use https:// so your data and key are encrypted."
    return None


def data_notice(cfg: AIConfig) -> str:
    """One plain line about what leaves this computer, shown wherever AI is configured."""
    if cfg.mode == "off":
        return "AI is off. Built-in rules are used and nothing is sent to any AI."
    if cfg.is_local:
        return f"{cfg.label} runs on your computer or network: nothing leaves this computer."
    return (f"Lead names, companies, a snippet of their website and their replies are sent to {cfg.label} "
            "to write and sort emails.")
