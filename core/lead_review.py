"""Borderline leads: qualified, but not clearly a fit, so they wait for the owner's OK.

A lead scoring between [targeting] min_fit_score and auto_send_score (default 70) is saved with
status "review". The sender only ever picks "new" leads, so these never use a sending slot.
Approve makes it "new" (emailed with the next run); Reject disqualifies it. One not reviewed
within 7 days is disqualified, so the list never grows stale.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core import db

REVIEW_WINDOW = timedelta(days=7)
ACTIONS = ("approve", "reject")


def _reasons(raw) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(r) for r in value] if isinstance(value, list) else []


def pending(limit: int = 100) -> list[dict]:
    """Leads waiting for review, best fit first, with why they scored as they did."""
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT id, name, first_name, company, email, website, location, fit_score, fit_reasons, "
                            "ai_fit, created_at FROM leads WHERE status = 'review' "
                            "ORDER BY COALESCE(fit_score, 0) DESC, id LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        item["reasons"] = _reasons(item.pop("fit_reasons"))
        items.append(item)
    return items


def count() -> int:
    conn = db.get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'review'").fetchone()[0]
    finally:
        conn.close()


def _disqualify(conn, lead_id: int, reasons_raw, why: str) -> None:
    reasons = _reasons(reasons_raw) + [why]
    conn.execute("UPDATE leads SET status = 'disqualified', next_touch_at = NULL, fit_reasons = ? WHERE id = ?",
                 (json.dumps(reasons), lead_id))


def decide(lead_id: int, action: str) -> str:
    """Approve or reject a lead waiting for review. Returns its new status."""
    if action not in ACTIONS:
        raise ValueError("Choose approve or reject.")
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT fit_reasons FROM leads WHERE id = ? AND status = 'review'", (lead_id,)).fetchone()
        if row is None:
            raise LookupError("That lead isn't waiting for review any more.")
        if action == "approve":
            conn.execute("UPDATE leads SET status = 'new' WHERE id = ?", (lead_id,))
            status = "new"
        else:
            _disqualify(conn, lead_id, row[0], "rejected by you")
            status = "disqualified"
        conn.commit()
        return status
    finally:
        conn.close()


def expire(older_than: timedelta = REVIEW_WINDOW) -> int:
    """Disqualify leads that waited longer than `older_than`. Returns how many."""
    cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT id, fit_reasons FROM leads WHERE status = 'review' AND created_at < ?",
                            (cutoff,)).fetchall()
        for lead_id, reasons in rows:
            _disqualify(conn, lead_id, reasons, f"not reviewed in {older_than.days} days")
        conn.commit()
        return len(rows)
    finally:
        conn.close()
