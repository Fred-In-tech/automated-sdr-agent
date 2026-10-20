"""`sdr ai test`, `sdr drafts`, and the AI line of `sdr doctor`.

The connection test is shared with the dashboard's "Test connection" button: list the models,
ask one tiny question that needs a JSON answer, and report in plain words what works and what
to fix. It never shows the API key.
"""

from __future__ import annotations

import time
from typing import Callable, Mapping

from core.ai import drafts
from core.ai.connectors import AIError, chat, extract_json, list_models
from core.ai.jobs import ANSWER_ROOM, TRIAGE_SCHEMA, TRIAGE_SYSTEM, is_valid_answer, is_valid_opener
from core.ai.providers import AIConfig, data_notice, load_ai_config, url_problem
from core.intent import INTENTS
from core.product import CLI_NAME
from core.tui import UI

TEST_REPLY = "<reply>\nHow much does it cost after the trial?\n</reply>"
DRAFT_ACTIONS = (("approve", "Approve", "send it as written"), ("edit", "Edit", "change the text, then approve"),
                 ("skip", "Skip", "use the standard wording instead"), ("later", "Decide later", ""))
KIND_LABELS = {"opener": "First line of the first email", "answer": "Answer to their question"}


def _model_installed(model: str, models: list[str]) -> bool:
    base = model.split(":")[0]
    return any(m == model or m.split(":")[0] == base for m in models)


def check_connection(cfg: AIConfig, *, list_fn: Callable = list_models, chat_fn: Callable = chat,
                    clock: Callable[[], float] = time.monotonic) -> dict:
    """{"ok", "json_ok", "message", "notice", "models", "latency_ms", "provider", "model"}."""
    result = {"ok": False, "json_ok": False, "message": "", "notice": data_notice(cfg), "models": [],
              "latency_ms": None, "provider": cfg.label, "model": cfg.model}
    if cfg.mode == "off":
        result["message"] = f"AI is off. Turn it on in Settings > AI, or run `{CLI_NAME} setup --section ai`."
        return result
    problem = url_problem(cfg)
    if problem:
        result["message"] = problem
        return result
    try:
        result["models"] = list_fn(cfg)
    except AIError:
        result["models"] = []          # some providers don't list models; the real question decides
    if result["models"] and cfg.model and not _model_installed(cfg.model, result["models"]):
        fix = f" Run: ollama pull {cfg.model}" if cfg.adapter == "ollama" else ""
        result["message"] = (f"Model '{cfg.model}' isn't available. Available: {', '.join(result['models'][:8])}.{fix}")
        return result
    start = clock()
    try:
        reply = chat_fn(cfg, TRIAGE_SYSTEM, TEST_REPLY, schema=TRIAGE_SCHEMA, max_tokens=ANSWER_ROOM)
    except AIError as exc:
        result["message"] = str(exc)
        return result
    result["latency_ms"] = int(round((clock() - start) * 1000))
    intent = (extract_json(reply.text) or {}).get("intent")
    result["json_ok"] = intent in INTENTS
    result["ok"] = True
    result["message"] = (f"Working: {cfg.label} ({cfg.model}) answered in {result['latency_ms']} ms."
                         if result["json_ok"] else
                         f"Connected to {cfg.label}, but its answer wasn't in the expected format. It still works: "
                         "the built-in rules step in whenever that happens. A larger model usually fixes it.")
    return result


def cmd_ai_test(ui: UI, profile: Mapping, env: Mapping[str, str] | None = None, **testers) -> int:
    cfg = load_ai_config(profile, environ=env)
    result = check_connection(cfg, **testers)
    ui.info(result["notice"])
    if result["models"]:
        ui.info("Models: " + ", ".join(result["models"][:10]))
    (ui.success if result["ok"] else ui.error)(result["message"])
    return 0 if result["ok"] else 1


def ai_doctor_checks(profile: Mapping | None, env: Mapping[str, str], *, online: bool,
                     tester: Callable[[AIConfig], dict] | None = None):
    from core.cli_doctor import Check
    cfg = load_ai_config(profile or {}, environ=env)
    if cfg.mode == "off":
        return [Check("AI", "info", f"off (optional). Add one with `{CLI_NAME} setup --section ai`.")]
    problem = url_problem(cfg)
    if problem:
        return [Check("AI", "fail", problem)]
    if not online:
        return [Check("AI", "ok", f"{cfg.label} ({cfg.model}). {data_notice(cfg)} Test it with `{CLI_NAME} ai test`.")]
    result = (tester or check_connection)(cfg)
    return [Check("AI", "ok" if result.get("ok") else "fail", result.get("message", ""))]


def _validator(kind: str, profile: Mapping) -> Callable[[str], bool | str]:
    def check(text: str) -> bool | str:
        valid = is_valid_opener(text) if kind == "opener" else is_valid_answer(text, profile)
        return True if valid else ("Keep it to one specific sentence, no links." if kind == "opener"
                                   else "Keep it short, with no links, placeholders or prices not in your facts.")
    return check


def cmd_drafts(ui: UI, profile: Mapping) -> int:
    """Review AI drafts in the terminal (the dashboard's Needs-you card does the same)."""
    waiting = drafts.pending()
    if not waiting:
        ui.info("Nothing is waiting for your review.")
        return 0
    for item in waiting:
        who = f"{item.get('first_name') or item.get('lead_name') or ''} at {item.get('company')} <{item.get('email')}>"
        ui.panel(item["body"], title=f"{KIND_LABELS.get(item['kind'], item['kind'])} · {who}"
                 + (" · waiting over 2 hours" if item.get("stale") else ""))
        if not ui.interactive:
            continue
        action = ui.select("drafts.action", "What should happen?", DRAFT_ACTIONS, default="approve")
        if action == "later":
            continue
        if action == "edit":
            body = ui.text("drafts.text", "New text", default=item["body"], validate=_validator(item["kind"], profile))
            drafts.decide(item["id"], "edit", body)
        else:
            drafts.decide(item["id"], action)
        ui.success({"approve": "Approved: it goes out with the next run.", "edit": "Saved and approved.",
                    "skip": "Skipped: the standard wording is used."}[action])
    if not ui.interactive:
        ui.info(f"Approve, edit or skip them on the dashboard (`{CLI_NAME} dashboard`, Overview > Needs you), "
                f"or run `{CLI_NAME} drafts` in a terminal.")
    return 0
