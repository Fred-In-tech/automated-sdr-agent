"""Bring your own AI: hosted providers or a local model, for four optional jobs.

    providers.py   presets, the local-only / https rules, reading [ai] (and pre-1.3 profiles)
    connectors.py  plain-HTTP adapters: OpenAI-compatible, Anthropic, Ollama
    jobs.py        openers, reply intent, answers to questions, lead fit; validation + fallbacks
    drafts.py      the review queue for drafts that wait for the owner

Everything degrades to the built-in rules: AI never stops outreach.
"""

from __future__ import annotations

from typing import Mapping

from core.ai.connectors import AIError, Reply, chat, list_models
from core.ai.jobs import (AIRun, ai_phrase_problem, answer_question, classify_reply, explain_fit, is_valid_answer,
                          is_valid_opener, write_opener)
from core.ai.providers import PRESETS, AIConfig, data_notice, load_ai_config, url_problem

__all__ = ["AIConfig", "AIError", "AIRun", "PRESETS", "Reply", "ai_enabled", "ai_phrase_problem", "answer_question",
           "chat", "classify_reply", "classify_reply_with_ai", "data_notice", "explain_fit", "is_valid_answer",
           "is_valid_opener", "list_models", "load_ai_config", "url_problem", "write_opener"]


def ai_enabled(profile: Mapping) -> bool:
    """True when any AI job could run for this profile (pre-1.3 name, kept for callers and plugins)."""
    run = AIRun(profile)
    return any(run.enabled(job) for job in ("openers", "reply_intent", "answer_questions", "lead_fit"))


def classify_reply_with_ai(profile: Mapping, text: str, opt_out_words) -> str:
    """Pre-1.3 name: the reply's intent using the profile's AI, or the built-in rules."""
    return classify_reply(AIRun(profile), text, opt_out_words)
