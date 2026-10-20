"""The dashboard's "Today" strip: what the SDR did today and what happens next.

One small, cheap answer to the question an owner opens the dashboard with ("is it working, and
when does it run again?"), so the Overview can lead with it instead of a wall of cards. Reads the
database and the profile only; the schedule check is passed in so the dashboard can cache it
(reading the crontab on every refresh would spawn a process every few seconds).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from core import deliverability
from core.db import get_connection

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_SEND_DAYS = WEEKDAYS[:5]
DEFAULT_RUN_TIMES = ("09:00", "14:00")
DEFAULT_DAILY_LIMIT = 20   # the email bot's own fallback when the profile has none
LOOKAHEAD_DAYS = 8         # one full week plus today


def _sent_today(now: datetime) -> int:
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM email_logs WHERE sent_at >= ?",
                           (midnight.astimezone(timezone.utc).isoformat(),)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _followups_due(now: datetime) -> int:
    """Open follow-ups that are overdue or due today (the Follow-ups tab's top two buckets)."""
    from core import followups
    try:
        return sum(1 for d in followups.today_list(now.date()) if d.get("bucket") in ("overdue", "today"))
    except Exception:  # noqa: BLE001 - an older database without the deals table: nothing is due
        return 0


def _run_times(profile: Mapping) -> list[str]:
    times = (profile.get("schedule") or {}).get("run_times", list(DEFAULT_RUN_TIMES))
    return sorted(t for t in times if isinstance(t, str) and len(t) == 5 and t[2] == ":")


def next_run(profile: Mapping, now: datetime) -> str | None:
    """"today at 14:00" / "tomorrow at 09:00" / "Mon at 09:00" for the next full run on a send
    day, or None when there are no run times or send days."""
    days = (profile.get("outreach") or {}).get("send_days", list(DEFAULT_SEND_DAYS))
    times = _run_times(profile)
    local = now.astimezone()
    for offset in range(LOOKAHEAD_DAYS):
        day = local + timedelta(days=offset)
        if WEEKDAYS[day.weekday()] not in days:
            continue
        for t in times:
            if offset == 0 and t <= local.strftime("%H:%M"):
                continue
            label = "today" if offset == 0 else "tomorrow" if offset == 1 else WEEKDAYS[day.weekday()]
            return f"{label} at {t}"
    return None


def _schedule_on(schedule_status: Callable[[], Mapping]) -> bool:
    try:
        return bool(schedule_status().get("installed"))
    except Exception:  # noqa: BLE001 - a broken check must not break the dashboard; it reads as off
        return False


def summary(profile: Mapping | None, *, now: datetime | None = None,
            schedule_status: Callable[[], Mapping]) -> dict:
    """{"sent_today", "daily_limit", "limit_note", "send_day", "schedule_on", "next_run",
    "follow_ups_due"} for the Overview's Today strip."""
    now = now or datetime.now().astimezone()
    profile = profile or {}
    outreach = profile.get("outreach") or {}
    limit, note = 0, ""
    if profile:
        limit = int(outreach.get("daily_send_limit", DEFAULT_DAILY_LIMIT))
        enabled = outreach.get("deliverability_cap", True) is not False
        limit, note = deliverability.capped_limit(limit, deliverability.load(), enabled=enabled)
    on = _schedule_on(schedule_status)
    return {
        "sent_today": _sent_today(now),
        "daily_limit": limit,
        "limit_note": note,
        "send_day": WEEKDAYS[now.astimezone().weekday()] in outreach.get("send_days", list(DEFAULT_SEND_DAYS)),
        "schedule_on": on,
        "next_run": next_run(profile, now) if on and profile else None,
        "follow_ups_due": _followups_due(now),
    }
