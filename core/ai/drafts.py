"""The review queue: AI drafts that wait for the owner's Approve / Edit / Skip.

Two kinds: "opener" (a lead's personal first line; that lead's first email waits for it) and
"answer" (a reply to a lead's question; sent on the next run once approved). Statuses move
pending -> approved | edited | skipped, and approved/edited -> sent. A newer draft of the same
kind for the same lead replaces an older pending one, so the queue never fills with duplicates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core import db

KINDS = ("opener", "answer")
ACTIONS = {"approve": "approved", "edit": "edited", "skip": "skipped"}
READY = ("approved", "edited")   # spelled out in the SQL below
STALE_AFTER = timedelta(hours=2)   # interested leads go cold fast
# A first line nobody reviews within this long is dropped and the standard one is used, so an
# unchecked review queue can never hold a lead back forever.
OPENER_REVIEW_WINDOW = timedelta(days=3)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row) -> dict:
    item = dict(row)
    try:
        item["context"] = json.loads(item.get("context") or "{}")
    except ValueError:
        item["context"] = {}
    return item


def add_draft(lead_id: int, kind: str, body: str, context: dict | None = None, *, cursor=None) -> int:
    """Queue a draft. Pass the caller's `cursor` to write it inside the caller's transaction
    (so the draft and the lead's new status are saved together, or not at all)."""
    if kind not in KINDS:
        raise ValueError(f"Unknown draft kind: {kind}")
    own = cursor is None
    conn = db.get_connection() if own else None
    cur = conn.cursor() if own else cursor
    try:
        cur.execute("DELETE FROM ai_drafts WHERE lead_id = ? AND kind = ? AND status = 'pending'", (lead_id, kind))
        cur.execute(
            "INSERT INTO ai_drafts (lead_id, kind, body, status, context, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (lead_id, kind, body, json.dumps(context or {}), _now()))
        draft_id = int(cur.lastrowid)
        if own:
            conn.commit()
        return draft_id
    finally:
        if own:
            conn.close()


def get(draft_id: int) -> dict | None:
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (draft_id,)).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def expire_pending(kind: str, older_than: timedelta) -> int:
    """Skip pending drafts of this kind older than `older_than`. Returns how many."""
    cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
    conn = db.get_connection()
    try:
        cursor = conn.execute("UPDATE ai_drafts SET status = 'skipped', decided_at = ? WHERE kind = ? "
                              "AND status = 'pending' AND created_at < ?", (_now(), kind, cutoff))
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def withdraw(draft_id: int) -> None:
    """Take back an approved draft that must not be sent (the lead opted out, or the text fails
    the safety checks at send time)."""
    conn = db.get_connection()
    try:
        conn.execute("UPDATE ai_drafts SET status = 'skipped', decided_at = ? WHERE id = ?", (_now(), draft_id))
        conn.commit()
    finally:
        conn.close()


def pending(limit: int = 50) -> list[dict]:
    """Drafts waiting for the owner, oldest first, with the lead's name, company and email."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT d.*, l.name AS lead_name, l.first_name, l.company, l.email FROM ai_drafts d "
            "JOIN leads l ON l.id = d.lead_id WHERE d.status = 'pending' ORDER BY d.created_at, d.id LIMIT ?",
            (limit,)).fetchall()
    finally:
        conn.close()
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    items = []
    for row in rows:
        item = _row(row)
        try:
            item["stale"] = datetime.fromisoformat(item["created_at"]) < cutoff
        except ValueError:
            item["stale"] = False
        items.append(item)
    return items


def decide(draft_id: int, action: str, body: str | None = None) -> dict:
    """Approve, edit (with the new text) or skip a pending draft. Returns the updated draft."""
    if action not in ACTIONS:
        raise ValueError("Choose approve, edit or skip.")
    if action == "edit" and not (body or "").strip():
        raise ValueError("The edited text is empty.")
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM ai_drafts WHERE id = ? AND status = 'pending'", (draft_id,)).fetchone()
        if row is None:
            raise LookupError("That draft isn't waiting for review any more.")
        new_body = body.strip() if action == "edit" else row["body"]
        conn.execute("UPDATE ai_drafts SET status = ?, body = ?, decided_at = ? WHERE id = ?",
                     (ACTIONS[action], new_body, _now(), draft_id))
        conn.commit()
        return _row(conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (draft_id,)).fetchone())
    finally:
        conn.close()


def has_pending(lead_id: int, kind: str) -> bool:
    conn = db.get_connection()
    try:
        return conn.execute("SELECT 1 FROM ai_drafts WHERE lead_id = ? AND kind = ? AND status = 'pending' LIMIT 1",
                            (lead_id, kind)).fetchone() is not None
    finally:
        conn.close()


def approved_for(lead_id: int, kind: str) -> dict | None:
    """The newest approved or edited draft for this lead that hasn't been sent yet."""
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM ai_drafts WHERE lead_id = ? AND kind = ? AND status IN ('approved', 'edited') "
                           "ORDER BY id DESC LIMIT 1", (lead_id, kind)).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def approved(kind: str) -> list[dict]:
    """Every approved/edited draft of this kind not yet sent, with the lead's details."""
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT d.*, l.email, l.first_name, l.company, l.status AS lead_status FROM ai_drafts d "
                            "JOIN leads l ON l.id = d.lead_id WHERE d.kind = ? AND d.status IN ('approved', 'edited') "
                            "ORDER BY d.id", (kind,)).fetchall()
    finally:
        conn.close()
    return [_row(row) for row in rows]


def mark_sent(draft_id: int) -> None:
    conn = db.get_connection()
    try:
        conn.execute("UPDATE ai_drafts SET status = 'sent', decided_at = COALESCE(decided_at, ?) WHERE id = ?",
                     (_now(), draft_id))
        conn.commit()
    finally:
        conn.close()
