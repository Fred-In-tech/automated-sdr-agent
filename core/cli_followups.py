"""`sdr followups`: today's follow-ups in the terminal (the dashboard tab has the buttons)."""

from __future__ import annotations

from datetime import date

from core import followups
from core.product import CLI_NAME
from core.tui import UI

LABELS = {"overdue": "Overdue", "today": "Due today", "none": "No next step", "upcoming": "Upcoming"}


def cmd_followups(ui: UI, today: date | None = None) -> int:
    deals = followups.today_list(today)
    if not deals:
        ui.info("No follow-ups yet. When a lead replies with interest or a question, they show up here "
                "with a next step.")
        return 0
    rows = []
    for deal in deals:
        who = deal.get("first_name") or deal.get("lead_name") or deal.get("email")
        when = deal.get("due") or "-"
        rows.append((f"{LABELS[deal['bucket']]}  {when}",
                     f"{deal.get('next_step') or 'Add a next step'}  ({who}, {deal.get('company') or ''}, "
                     f"{deal['stage_label']}){'  · stale' if deal['stale'] else ''}"))
    ui.summary("Follow-ups", rows)
    s = followups.summary(today)
    ui.info(f"{s['open']} open, {s['due_now']} due now, {s['won']} won. Mark them done on the dashboard's "
            f"Follow-ups tab (`{CLI_NAME} dashboard`).")
    return 0
