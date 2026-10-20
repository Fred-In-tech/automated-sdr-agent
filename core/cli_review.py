"""`sdr review`: borderline leads waiting for your OK before their first email."""

from __future__ import annotations

from core import lead_review
from core.product import CLI_NAME
from core.tui import UI

ACTIONS = (("approve", "Approve", "email them with the next run"), ("reject", "Reject", "never email them"),
           ("later", "Decide later", ""))


def cmd_review(ui: UI) -> int:
    waiting = lead_review.pending()
    if not waiting:
        ui.info("No leads are waiting for review.")
        return 0
    for lead in waiting:
        who = f"{lead.get('company') or lead.get('name')} <{lead.get('email')}>"
        why = "\n".join(f"- {r}" for r in lead["reasons"]) or "-"
        extra = f"\nAI: {lead['ai_fit']}" if lead.get("ai_fit") else ""
        ui.panel(f"Fit score {lead.get('fit_score')}\n{why}{extra}\n{lead.get('website') or ''}", title=who)
        if not ui.interactive:
            continue
        action = ui.select("review.action", "Email this lead?", ACTIONS, default="approve")
        if action != "later":
            lead_review.decide(lead["id"], action)
            ui.success("Approved: it goes out with the next run." if action == "approve" else "Rejected.")
    if not ui.interactive:
        ui.info(f"Approve or reject them on the dashboard (`{CLI_NAME} dashboard`, Overview > Needs you), "
                f"or run `{CLI_NAME} review` in a terminal. Leads not reviewed within 7 days are skipped.")
    return 0
