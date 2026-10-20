"""Dashboard routes for the Follow-ups tab. Mixed into DashboardHandler, so login, CSRF and the
local-only checks apply exactly as elsewhere. Nothing here sends email."""

from __future__ import annotations

import json
import os

from core import followups
from core.db import log_event

FOLLOWUPS_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "followups.js")


class FollowupRoutesMixin:
    FOLLOWUP_GET_ROUTES = {"/api/followups": "_api_followups", "/followups.js": "_followups_script"}
    FOLLOWUP_POST_ROUTES = {"/api/followups": "_api_followup_action"}

    def _followups_script(self) -> None:
        try:
            with open(FOLLOWUPS_SCRIPT_PATH, "rb") as f:
                self._send_body(f.read(), "text/javascript; charset=utf-8")
        except OSError:
            self.send_error(404, "File Not Found")

    def _api_followups(self) -> None:
        self._send_json({
            "today": followups.today_list(),
            "deals": followups.all_deals(),
            "summary": followups.summary(),
            "stages": [{"value": s, "label": followups.STAGE_LABELS[s]} for s in followups.STAGES],
        })

    def _api_followup_action(self) -> None:
        try:
            payload = json.loads(self.body.decode("utf-8"))
            deal_id, action = int(payload["id"]), str(payload["action"])
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            self._send_json({"success": False, "message": "Choose a follow-up and what to do."}, 400)
            return
        try:
            if action == "done":
                deal = followups.complete(deal_id, next_step=payload.get("next_step"), due=payload.get("due") or None,
                                          outcome=payload.get("outcome") or None, stage=payload.get("stage") or None,
                                          **({"value": payload["value"]} if "value" in payload else {}))
            elif action == "snooze":
                deal = followups.snooze(deal_id, str(payload.get("when")))
            elif action == "update":
                fields = {k: payload[k] for k in ("stage", "value", "notes", "next_step", "due") if k in payload}
                deal = followups.update(deal_id, **fields)
            else:
                self._send_json({"success": False, "message": "Choose Done, Snooze or a change."}, 400)
                return
        except LookupError as exc:
            self._send_json({"success": False, "message": str(exc)}, 404)
            return
        except ValueError as exc:
            self._send_json({"success": False, "message": str(exc)}, 400)
            return
        log_event("Dashboard", "FollowUp", "success", f"Follow-up {deal_id}: {action}")
        self._send_json({"success": True, "deal": deal})
