"""`sdr` with no arguments: a status line and a menu of the common actions.

On a terminal it's an arrow-key menu that loops until you quit. Without one (a script, an AI
agent, CI) it prints the status and the command list and exits 0, so it never hangs waiting
for input. The actions themselves live in cli.py and are passed in, which keeps this module
free of the bots' heavy imports until you pick something.
"""

from __future__ import annotations

from typing import Callable, Mapping

from core.product import CLI_NAME
from core.tui import UI

COMMANDS = (
    (f"{CLI_NAME} setup", "set up, or change one part with --section NAME"),
    (f"{CLI_NAME} preview", "see every email of your sequence (sends nothing)"),
    (f"{CLI_NAME} test-email", "send yourself a sample email"),
    (f"{CLI_NAME} dashboard", "open your dashboard in the browser"),
    (f"{CLI_NAME} run", "find leads, email them and handle replies now"),
    (f"{CLI_NAME} report", "your pipeline: leads, emails, replies, hot leads"),
    (f"{CLI_NAME} schedule on|off|status", "run automatically every day"),
    (f"{CLI_NAME} update", "install the latest version (backup + rollback)"),
    (f"{CLI_NAME} doctor", "check everything and explain how to fix it"),
    (f"{CLI_NAME} open", "where your files are (for your editor or AI agent)"),
)


def snapshot(profile: dict | None) -> dict:
    """Numbers for the status line. Each piece is optional: a problem just leaves it out."""
    result: dict = {"product": ((profile or {}).get("sender") or {}).get("product_name", "")}
    try:
        from core.db import init_db
        from core.report import pipeline_stats
        init_db()
        stats = pipeline_stats(profile)
        result.update(leads=stats.get("leads_found", 0), sent_today=stats.get("sent_today", 0),
                      hot=len(stats.get("hot_leads") or []))
    except Exception:  # noqa: BLE001 - a status line must never stop the menu
        pass
    try:
        from core import scheduler
        result["schedule_on"] = bool(scheduler.schedule_status().get("installed"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import updater
        status = updater.read_status()
        if status.get("available") and status.get("latest"):
            result["update"] = status["latest"]
    except Exception:  # noqa: BLE001
        pass
    return result


def status_lines(snap: Mapping) -> list[str]:
    lines = []
    if "leads" in snap:
        hot = snap.get("hot", 0)
        prefix = f"{snap['product']}: " if snap.get("product") else ""
        lines.append(f"{prefix}{snap['leads']} leads in your pipeline, {snap.get('sent_today', 0)} emails sent "
                     f"today, {hot} hot lead{'' if hot == 1 else 's'}")
    if "schedule_on" in snap:
        off = f"Schedule: off (turn it on with `{CLI_NAME} schedule on`)"
        lines.append("Schedule: on" if snap["schedule_on"] else off)
    if snap.get("update"):
        lines.append(f"Update available: v{snap['update']} (run `{CLI_NAME} update`)")
    return lines


def menu_choices(snap: Mapping) -> list[tuple[str, str, str]]:
    schedule = ("Turn the schedule off", "Stops the automatic runs") if snap.get("schedule_on") else (
        "Turn the schedule on", "Runs by itself every day and emails real prospects")
    return [
        ("dashboard", "Open dashboard", "Leads, emails and replies in your browser"),
        ("run", "Run now", "Find leads, email them and handle replies"),
        ("sample", "Send yourself a sample", "Your first email, to your own inbox"),
        ("preview", "Preview emails", "Every email of your sequence; sends nothing"),
        ("settings", "Change settings", "Re-run one part of the setup"),
        ("schedule", *schedule),
        ("update", "Check for updates", ""),
        ("doctor", "Health check", "Checks everything and says how to fix it"),
        ("quit", "Quit", ""),
    ]


def print_commands(ui: UI) -> None:
    width = max(len(command) for command, _ in COMMANDS)
    ui.echo("")
    ui.echo("Commands:")
    for command, what in COMMANDS:
        ui.echo(f"  {command.ljust(width)}  {what}")
    ui.echo("")
    ui.echo("Using an AI agent? Point it at AGENTS.md.")


def run_home(ui: UI, actions: Mapping[str, Callable[[], object]], profile: dict | None,
             snapshot_fn: Callable[[dict | None], dict] = snapshot) -> int:
    """Banner, status, then the menu until Quit. `actions` maps menu values to callables."""
    ui.banner()
    snap = snapshot_fn(profile)
    for line in status_lines(snap):
        ui.info(line)
    if not ui.interactive:
        print_commands(ui)
        return 0
    while True:
        ui.echo("")
        choice = ui.select("home.action", "What would you like to do?", menu_choices(snap), default="dashboard")
        if choice == "quit":
            return 0
        try:
            actions[choice]()
        except KeyboardInterrupt:
            ui.echo("")  # Ctrl+C in an action goes back to the menu
        if choice in ("schedule", "run", "update", "settings"):
            snap = snapshot_fn(profile)
