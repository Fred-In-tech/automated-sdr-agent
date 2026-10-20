"""Follow-ups: what happens after someone says "interested".

Every lead who replies with interest (or a question) gets one deal, and every open deal always has
exactly one next step with a date (the Pipedrive model): marking it done asks for the next one, or
closes the deal as won or lost. The Today view lists what's overdue, then what's due today, then
deals with no next step. Nothing here sends email: replying stays in the owner's own mail app.

Due dates are calendar dates in the owner's local time ("2026-10-21"); `today` is injectable so
tests and the digest agree on what "today" means.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from core import db

STAGES = ("replied", "meeting", "proposal", "won", "lost")
OPEN_STAGES = ("replied", "meeting", "proposal")
STAGE_LABELS = {"replied": "Replied", "meeting": "Meeting", "proposal": "Proposal", "won": "Won", "lost": "Lost"}
STALE_AFTER_DAYS = 5
MAX_NEXT_STEP = 200
MAX_NOTES = 2000
SNOOZES = ("tomorrow", "3days", "monday")
BUCKET_ORDER = {"overdue": 0, "today": 1, "none": 2, "upcoming": 3}
_KEEP = object()   # "leave this field as it is" (None means "clear it")


def _today(today: date | None) -> date:
    return today or datetime.now().astimezone().date()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date(value) -> str:
    """A due date as "YYYY-MM-DD", or ValueError."""
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise ValueError("Pick a date for the next step.") from None


def _money(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        amount = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        raise ValueError("The deal value must be a number, like 1200.") from None
    if amount < 0:
        raise ValueError("The deal value can't be negative.")
    return round(amount, 2)


def _text(value, limit: int, what: str) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()) if what == "next step" else str(value).strip()
    if len(text) > limit:
        raise ValueError(f"Keep the {what} under {limit} characters.")
    return text or None


# ── creating ────────────────────────────────────────────────────────────────


def ensure_deal(lead_id: int, *, cursor=None, today: date | None = None) -> int:
    """The lead's deal, opened at "Replied" with a next step due tomorrow if there is none yet.
    A closed (won/lost) deal is never reopened. Pass `cursor` to write inside the caller's
    transaction (the inbox saves the reply and the deal together)."""
    own = cursor is None
    conn = db.get_connection() if own else None
    cur = conn.cursor() if own else cursor
    try:
        row = cur.execute("SELECT id FROM deals WHERE lead_id = ? ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        if row:
            return int(row[0])
        lead = cur.execute("SELECT first_name, company FROM leads WHERE id = ?", (lead_id,)).fetchone()
        who = (lead[0] or lead[1]) if lead else "them"
        now = _now()
        cur.execute("INSERT INTO deals (lead_id, stage, next_step, due, created_at, updated_at, stage_changed_at) "
                    "VALUES (?, 'replied', ?, ?, ?, ?, ?)",
                    (lead_id, f"Reply to {who} and suggest a call",
                     (_today(today) + timedelta(days=1)).isoformat(), now, now, now))
        deal_id = int(cur.lastrowid)
        if own:
            conn.commit()
        return deal_id
    finally:
        if own:
            conn.close()


# ── reading ─────────────────────────────────────────────────────────────────


def _rows(where: str = "", args: tuple = ()) -> list[dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT d.*, l.first_name, l.name AS lead_name, l.company, l.email FROM deals d "
            "JOIN leads l ON l.id = d.lead_id " + where + " ORDER BY d.id", args).fetchall()  # nosec B608 - fixed clauses
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _decorate(deal: dict, today: date) -> dict:
    due = deal.get("due")
    if deal["stage"] not in OPEN_STAGES:
        bucket = "closed"
    elif not due or not deal.get("next_step"):
        bucket = "none"
    elif due < today.isoformat():
        bucket = "overdue"
    elif due == today.isoformat():
        bucket = "today"
    else:
        bucket = "upcoming"
    try:
        changed = datetime.fromisoformat(deal["stage_changed_at"]).astimezone().date()
        stale = deal["stage"] in OPEN_STAGES and (today - changed).days >= STALE_AFTER_DAYS
    except (TypeError, ValueError):
        stale = False
    return {**deal, "bucket": bucket, "stale": stale, "stage_label": STAGE_LABELS.get(deal["stage"], deal["stage"])}


def all_deals(today: date | None = None) -> list[dict]:
    day = _today(today)
    return [_decorate(d, day) for d in _rows()]


def today_list(today: date | None = None) -> list[dict]:
    """Open deals in the order to work them: overdue, due today, no next step, upcoming."""
    day = _today(today)
    deals = [_decorate(d, day) for d in _rows("WHERE d.stage IN ('replied', 'meeting', 'proposal')")]
    return sorted(deals, key=lambda d: (BUCKET_ORDER[d["bucket"]], d.get("due") or "", d["id"]))


def _get(cur, deal_id: int) -> dict:
    row = cur.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if row is None:
        raise LookupError("That follow-up doesn't exist any more.")
    return dict(row)


# ── changing ────────────────────────────────────────────────────────────────


def update(deal_id: int, *, stage=_KEEP, value=_KEEP, notes=_KEEP, next_step=_KEEP, due=_KEEP,
           today: date | None = None) -> dict:
    """Change any of a deal's fields (validated). None clears next_step/due/value/notes."""
    changes: dict = {}
    if stage is not _KEEP:
        if stage not in STAGES:
            raise ValueError("Choose a stage: Replied, Meeting, Proposal, Won or Lost.")
        changes["stage"] = stage
    if value is not _KEEP:
        changes["value"] = _money(value)
    if notes is not _KEEP:
        changes["notes"] = _text(notes, MAX_NOTES, "notes")
    if next_step is not _KEEP:
        changes["next_step"] = _text(next_step, MAX_NEXT_STEP, "next step")
    if due is not _KEEP:
        changes["due"] = None if due in (None, "") else _date(due)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        current = _get(cur, deal_id)
        now = _now()
        if "stage" in changes and changes["stage"] != current["stage"]:
            changes["stage_changed_at"] = now
            closed = changes["stage"] in ("won", "lost")
            changes["closed_at"] = now if closed else None
            if closed:
                changes["next_step"], changes["due"] = None, None
        changes["updated_at"] = now
        assignments = ", ".join(f"{column} = ?" for column in changes)   # column names are fixed keys above
        cur.execute(f"UPDATE deals SET {assignments} WHERE id = ?", (*changes.values(), deal_id))  # nosec B608
        conn.commit()
        return _decorate(dict(_get(cur, deal_id)), _today(today))
    finally:
        conn.close()


def complete(deal_id: int, *, next_step: str | None = None, due=None, outcome: str | None = None,
             value=_KEEP, stage: str | None = None, today: date | None = None) -> dict:
    """Mark the current next step done. Either give the next one (and optionally move the stage),
    or close the deal with outcome "won" / "lost" (and optionally its value)."""
    if outcome in ("won", "lost"):
        return update(deal_id, stage=outcome, value=value, today=today)
    if outcome is not None:
        raise ValueError("Close a deal as won or lost.")
    if not (next_step or "").strip():
        raise ValueError("What's the next step? Add it, or close the deal as won or lost.")
    fields = {"next_step": next_step, "due": due or (_today(today) + timedelta(days=1)), "today": today}
    if stage:
        fields["stage"] = stage
    if value is not _KEEP:
        fields["value"] = value
    return update(deal_id, **fields)


def snooze(deal_id: int, when: str, today: date | None = None) -> dict:
    """Move the next step to tomorrow, in 3 days, or next Monday."""
    day = _today(today)
    if when == "tomorrow":
        due = day + timedelta(days=1)
    elif when == "3days":
        due = day + timedelta(days=3)
    elif when == "monday":
        due = day + timedelta(days=(7 - day.weekday()) or 7)
    else:
        raise ValueError("Snooze to tomorrow, 3 days or next Monday.")
    return update(deal_id, due=due, today=today)


# ── numbers ─────────────────────────────────────────────────────────────────


def summary(today: date | None = None) -> dict:
    deals = all_deals(today)
    open_deals = [d for d in deals if d["stage"] in OPEN_STAGES]
    won = [d for d in deals if d["stage"] == "won"]
    conn = db.get_connection()
    try:
        first_emails = conn.execute("SELECT COUNT(*) FROM email_logs WHERE COALESCE(step, 1) = 1").fetchone()[0]
    finally:
        conn.close()
    won_value = round(sum(d["value"] or 0 for d in won), 2)
    return {
        "open": len(open_deals),
        "due_now": sum(1 for d in open_deals if d["bucket"] in ("overdue", "today")),
        "pipeline_value": round(sum(d["value"] or 0 for d in open_deals), 2),
        "won": len(won),
        "lost": sum(1 for d in deals if d["stage"] == "lost"),
        "won_value": won_value,
        "first_emails": first_emails,
        "revenue_per_100_emails": round(won_value * 100 / first_emails, 2) if first_emails else None,
    }


def digest_lines(today: date | None = None) -> list[str]:
    """Overdue and due-today follow-ups for the daily digest email."""
    lines = []
    for deal in today_list(today):
        if deal["bucket"] not in ("overdue", "today"):
            continue
        who = deal.get("first_name") or deal.get("lead_name") or deal.get("company") or deal.get("email")
        when = "overdue" if deal["bucket"] == "overdue" else "today"
        lines.append(f"[{when}] {deal['next_step']} ({who}, {deal.get('company') or ''}) <{deal.get('email')}>")
    return lines
