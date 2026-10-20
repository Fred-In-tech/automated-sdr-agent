"""Dashboard routes for the AI review queue ("Needs you") and the AI "Test connection" button.

Mixed into DashboardHandler, so login, CSRF and the local-only checks in server.py apply to
every route here exactly as to the others. Drafts are edited through the same checks the
pipeline uses, and no response ever contains the AI key.
"""

from __future__ import annotations

import json
import os

from core.ai import drafts
from core.ai.jobs import is_valid_answer, is_valid_opener
from core.ai.providers import data_notice, load_ai_config
from core.cli_ai import check_connection
from core.config import ProfileError, load_env_file, load_profile
from core.db import log_event

AI_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai.js")
EDIT_HELP = {"opener": "Keep it to one specific sentence, with no links.",
             "answer": "Keep it short, with no links, placeholders or prices that aren't in your AI facts."}


def _profile() -> dict:
    try:
        return load_profile()
    except ProfileError:
        return {}


class AIRoutesMixin:
    AI_GET_ROUTES = {"/api/ai/drafts": "_api_ai_drafts", "/ai.js": "_ai_script"}
    AI_POST_ROUTES = {"/api/ai/drafts": "_api_ai_decide", "/api/ai/test": "_api_ai_test"}

    def _ai_script(self) -> None:
        try:
            with open(AI_SCRIPT_PATH, "rb") as f:
                self._send_body(f.read(), "text/javascript; charset=utf-8")
        except OSError:
            self.send_error(404, "File Not Found")

    def _api_ai_drafts(self) -> None:
        load_env_file()
        cfg = load_ai_config(_profile())
        items = [{key: item.get(key) for key in ("id", "kind", "body", "created_at", "stale", "first_name",
                                                  "lead_name", "company", "email")} for item in drafts.pending()]
        self._send_json({"drafts": items, "ai": {"mode": cfg.mode, "provider": cfg.label, "model": cfg.model,
                                                 "notice": data_notice(cfg)}})

    def _api_ai_decide(self) -> None:
        try:
            payload = json.loads(self.body.decode("utf-8"))
            draft_id, action = int(payload["id"]), str(payload["action"])
            body = payload.get("body")
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            self._send_json({"success": False, "message": "Choose a draft and what to do with it."}, 400)
            return
        if action == "edit":
            kind = next((d["kind"] for d in drafts.pending(limit=500) if d["id"] == draft_id), None)
            valid = (is_valid_opener(body or "") if kind == "opener" else is_valid_answer(body or "", _profile()))
            if kind and not valid:
                self._send_json({"success": False, "message": EDIT_HELP[kind]}, 400)
                return
        try:
            draft = drafts.decide(draft_id, action, body)
        except ValueError as exc:
            self._send_json({"success": False, "message": str(exc)}, 400)
            return
        except LookupError as exc:
            self._send_json({"success": False, "message": str(exc)}, 404)
            return
        log_event("Dashboard", "AIDraft", "success", f"Draft {draft_id} {draft['status']}")
        self._send_json({"success": True, "id": draft_id, "status": draft["status"]})

    def _api_ai_test(self) -> None:
        load_env_file()
        self._send_json(check_connection(load_ai_config(_profile())))
