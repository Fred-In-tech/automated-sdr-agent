"""Dashboard routes for deliverability, the results funnel and borderline lead reviews. Mixed into
DashboardHandler, so login, CSRF and the local-only checks apply as everywhere else."""

from __future__ import annotations

import json
import os
import time

from core import deliverability, lead_review, today
from core.config import ProfileError, load_env_file, load_profile
from core.db import log_event
from core.funnel import compute
from core.report import pipeline_stats

INSIGHTS_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "insights.js")
SCHEDULE_CACHE_SECONDS = 60   # the page refreshes every few seconds; reading the crontab spawns a process
_schedule_cache: dict = {"at": 0.0, "status": None}


def _cached_schedule_status() -> dict:
    """core.scheduler.schedule_status(), re-read at most once a minute."""
    from core.scheduler import schedule_status
    if _schedule_cache["status"] is None or time.monotonic() - _schedule_cache["at"] > SCHEDULE_CACHE_SECONDS:
        _schedule_cache.update(at=time.monotonic(), status=schedule_status())
    return _schedule_cache["status"]


def _with_cap(result: dict) -> dict:
    return {**result, "cap_note": deliverability.capped_limit(10**6, result)[1]}


class InsightsRoutesMixin:
    INSIGHTS_GET_ROUTES = {"/api/insights": "_api_insights", "/api/lead-review": "_api_lead_review",
                           "/api/today": "_api_today", "/insights.js": "_insights_script"}
    INSIGHTS_POST_ROUTES = {"/api/deliverability/check": "_api_deliverability_check",
                            "/api/lead-review": "_api_lead_review_decide"}

    def _insights_script(self) -> None:
        try:
            with open(INSIGHTS_SCRIPT_PATH, "rb") as f:
                self._send_body(f.read(), "text/javascript; charset=utf-8")
        except OSError:
            self.send_error(404, "File Not Found")

    def _api_insights(self) -> None:
        try:
            profile = load_profile()
        except ProfileError:
            profile = None
        stats = pipeline_stats(profile)
        outreach = (profile or {}).get("outreach", {}) or {}
        self._send_json({"deliverability": _with_cap(deliverability.load()), "funnel": compute(),
                         "review_count": lead_review.count(), "reasons": stats.get("reply_reasons", []),
                         "tests": {"subject": stats.get("subject_ab_test", []),
                                   "opener": stats.get("opener_ab_test", []),
                                   "signature": stats.get("signature_ab_test", []),
                                   "subject_texts": outreach.get("subject_variants") or [],
                                   "opener_texts": outreach.get("opener_variants") or []}})

    def _api_today(self) -> None:
        try:
            profile = load_profile()
        except ProfileError:
            profile = None
        self._send_json(today.summary(profile, schedule_status=_cached_schedule_status))

    def _api_deliverability_check(self) -> None:
        load_env_file()
        try:
            profile = load_profile()
        except ProfileError:
            profile = {}
        result = deliverability.refresh_if_stale(profile, os.environ, force=True)
        self._send_json(_with_cap(result))

    def _api_lead_review(self) -> None:
        self._send_json({"leads": lead_review.pending()})

    def _api_lead_review_decide(self) -> None:
        try:
            payload = json.loads(self.body.decode("utf-8"))
            lead_id, action = int(payload["id"]), str(payload["action"])
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            self._send_json({"success": False, "message": "Choose a lead and Approve or Reject."}, 400)
            return
        try:
            status = lead_review.decide(lead_id, action)
        except ValueError as exc:
            self._send_json({"success": False, "message": str(exc)}, 400)
            return
        except LookupError as exc:
            self._send_json({"success": False, "message": str(exc)}, 404)
            return
        log_event("Dashboard", "LeadReview", "success", f"Lead {lead_id}: {action}")
        self._send_json({"success": True, "status": status})
