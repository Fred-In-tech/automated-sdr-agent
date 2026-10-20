"""Setup's AI step (`sdr setup --section ai`, Settings > AI, or the last step of the wizard).

Starts from "No AI for now". For AI on this computer it looks for a running Ollama or LM Studio
and offers what it finds; for hosted AI it asks for the key, which goes to .env as AI_API_KEY
(never into profile.toml), just like the mailbox password. The job switches are only asked when
someone opens this section on purpose, so the first-run wizard stays short.
"""

from __future__ import annotations

from typing import Callable

from core.ai.connectors import AIError, list_models
from core.ai.providers import PRESETS, AIConfig, load_ai_config, url_problem
from core.setup_questions import AI_PROVIDER_CHOICES, q
from core.tui import InvalidAnswer

KEY_ENV = "AI_API_KEY"
PROBE_TIMEOUT = 2.0
JOB_KEYS = ("ai.openers", "ai.reply_intent", "ai.answer_questions", "ai.lead_fit")
LOCAL_PROBES = ("ollama", "lmstudio")


def probe_local_ai(list_fn: Callable[..., list] = list_models) -> dict:
    """{"provider", "models"} for the first local AI app answering on its usual port, else {}."""
    for name in LOCAL_PROBES:
        preset = PRESETS[name]
        cfg = AIConfig(mode="local_only", provider=name, adapter=preset.adapter, base_url=preset.base_url,
                       auth=preset.auth, timeout=PROBE_TIMEOUT)
        try:
            models = list_fn(cfg)
        except (AIError, OSError):
            continue
        if models:
            return {"provider": name, "models": list(models)}
    return {}


def _whole_dollars(value) -> bool:
    return str(value).strip().isdigit() and int(str(value).strip()) <= 100000


def _choices_for(mode: str) -> tuple:
    if mode == "local_only":
        return tuple(c for c in AI_PROVIDER_CHOICES if PRESETS[c[0]].local or c[0] == "custom")
    return AI_PROVIDER_CHOICES


def _ask_key(ctx, provider: str) -> None:
    ui = ctx.ui
    has_key = bool(ctx.saved_env.get(KEY_ENV) or ui.environ.get(KEY_ENV))
    if has_key and not ui.has_answer("ai.api_key_env"):
        if not ui.interactive or ui.confirm("ai.keep_key", "Keep the AI key you saved before?", default=True):
            return
    key = ui.password("ai.api_key", f"{PRESETS[provider].label} API key", required=True,
                      hint="It's saved on this computer only, in .env.")
    if key:
        ctx.env[KEY_ENV] = key


def _ask_jobs(ctx) -> None:
    ui = ctx.ui
    for key in JOB_KEYS:
        question = q(key)
        if ctx.section == "ai" or ui.has_answer(key):
            ctx.a[key] = ui.select(key, question.label, question.choices, default=ctx.value(key, question.default))
        else:
            ctx.a[key] = ctx.value(key, question.default)


def step_ai(ctx) -> None:
    ui = ctx.ui
    mode_q = q("ai.mode")
    mode = ui.select("ai.mode", mode_q.label, mode_q.choices, default=ctx.value("ai.mode", "off"))
    ctx.a["ai.mode"] = mode
    if mode == "off":
        if ctx.section == "ai":
            _ask_jobs(ctx)
        return

    found = {}
    if mode == "local_only" and ui.interactive and not ui.has_answer("ai.provider"):
        found = ctx.deps.probe_ai()
        if found:
            ui.success(f"Found {PRESETS[found['provider']].label} running on this computer.")
    default_provider = found.get("provider") or ctx.value("ai.provider") or ("ollama" if mode == "local_only" else "openai")
    choices = _choices_for(mode)
    if default_provider not in {c[0] for c in choices}:
        default_provider = choices[0][0]
    provider = ui.select("ai.provider", q("ai.provider").label, choices, default=default_provider)
    ctx.a["ai.provider"] = provider

    preset = PRESETS[provider]
    needs_url = not preset.base_url
    base_url = ui.text("ai.base_url", q("ai.base_url").label, default=ctx.value("ai.base_url") or None,
                       required=needs_url, hint=q("ai.base_url").help) if (needs_url or ui.has_answer("ai.base_url")
                                                                             or ctx.section == "ai") else ""
    ctx.a["ai.base_url"] = base_url or ""

    models = found.get("models") if found.get("provider") == provider else None
    model_default = ctx.value("ai.model") or (models[0] if models else None)
    hint = f"Installed: {', '.join(models[:6])}" if models else q("ai.model").help
    ctx.a["ai.model"] = ui.text("ai.model", q("ai.model").label, default=model_default, required=True, hint=hint)

    cfg = load_ai_config({"ai": {"mode": mode, "provider": provider, "base_url": ctx.a["ai.base_url"],
                                 "model": ctx.a["ai.model"]}}, environ={})
    problem = url_problem(cfg)
    if problem:
        if not ui.interactive:
            raise InvalidAnswer("ai.base_url", q("ai.base_url").label, problem)
        ui.warn(problem)
        ui.info("AI stays off for now. Change it any time on the dashboard's Settings page or with "
                "`sdr setup --section ai`.")
        ctx.a["ai.mode"] = "off"
        return
    if mode == "hosted" and not cfg.is_local and preset.needs_key:
        _ask_key(ctx, provider)

    facts_q = q("ai.facts")
    if ctx.section == "ai" or ui.has_answer("ai.facts"):
        ctx.a["ai.facts"] = ui.text("ai.facts", facts_q.label, default=ctx.value("ai.facts") or None, required=False,
                                    hint=facts_q.help) or ""
    if mode == "hosted" and (ctx.section == "ai" or ui.has_answer("ai.monthly_budget_usd")):
        budget_q = q("ai.monthly_budget_usd")
        ctx.a["ai.monthly_budget_usd"] = int(ui.text("ai.monthly_budget_usd", budget_q.label,
                                                     default=str(ctx.value("ai.monthly_budget_usd", 0)),
                                                     validate=_whole_dollars, hint=budget_q.help))
    _ask_jobs(ctx)
