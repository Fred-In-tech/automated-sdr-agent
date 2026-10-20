"""The results funnel, built on replies and outcomes, never on opens (Apple Mail Privacy
Protection makes open rates meaningless): first emails sent -> delivered (didn't bounce) ->
replied (a real person) -> interested -> meeting (Follow-ups stage Meeting, Proposal or Won) -> won.
Each step shows its count, the rate from the step before and the rate from first emails.
"""

from __future__ import annotations

import sqlite3

from core.db import get_connection
from core.report import HUMAN_INTENTS, POSITIVE_INTENTS

STEPS = (("sent", "First emails sent"), ("delivered", "Delivered (didn't bounce)"), ("replied", "Replied (real people)"),
         ("interested", "Interested / questions"), ("meeting", "Meeting or later"), ("won", "Won"))


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def compute() -> dict:
    conn = get_connection()
    try:
        sent = {r[0] for r in conn.execute("SELECT DISTINCT lead_id FROM email_logs WHERE COALESCE(step, 1) = 1")}
        bounced = {r[0] for r in conn.execute("SELECT id FROM leads WHERE status = 'bounced'")} & sent
        replies = conn.execute("SELECT lead_id, intent FROM inbound_messages WHERE lead_id IS NOT NULL").fetchall()
        try:
            deals = conn.execute("SELECT lead_id, stage, value FROM deals").fetchall()
        except sqlite3.OperationalError:          # a database from before Follow-ups
            deals = []
    finally:
        conn.close()
    delivered = sent - bounced
    replied = {lead for lead, intent in replies if intent in HUMAN_INTENTS} & delivered
    interested = {lead for lead, intent in replies if intent in POSITIVE_INTENTS} & delivered
    meeting = {lead for lead, stage, _ in deals if stage in ("meeting", "proposal", "won")} & delivered
    won = {lead for lead, stage, _ in deals if stage == "won"} & delivered
    counts = {"sent": len(sent), "delivered": len(delivered), "replied": len(replied), "interested": len(interested),
              "meeting": len(meeting), "won": len(won)}
    steps, previous = [], None
    for key, label in STEPS:
        count = counts[key]
        steps.append({"key": key, "label": label, "count": count,
                      "rate": None if previous is None else _pct(count, previous),
                      "of_sent": _pct(count, counts["sent"])})
        previous = count
    won_value = round(sum(value or 0 for lead, stage, value in deals if stage == "won" and lead in delivered), 2)
    return {"steps": steps, "won_value": won_value}


def funnel_lines(funnel: dict) -> list[str]:
    """The funnel as aligned text for `sdr report` and the daily digest."""
    steps = funnel["steps"]
    if not steps[0]["count"]:
        return ["  No first emails yet: the funnel fills in as emails go out and people reply."]
    lines = []
    for step in steps:
        rate = "" if step["rate"] is None else f"  ({step['rate']}% of previous)"
        lines.append(f"  {step['label']:<27}{step['count']}{rate}")
    if funnel.get("won_value"):
        lines.append(f"  Won value                  ${funnel['won_value']:,.2f}")
    return lines
