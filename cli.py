"""Automated SDR by Fred — the `sdr` command.

    sdr                    home menu (status + the common actions); starts setup the first time
    sdr setup              the setup wizard (--answers FILE, --section NAME, --print-questions)
    sdr preview            every email of your sequence and the web searches (sends nothing)
    sdr test-email         one real sample email, to yourself
    sdr dashboard          the local dashboard in your browser (--set-password, --no-open)
    sdr run                find leads, email them and handle replies, now
    sdr report | status    the pipeline report
    sdr schedule on|off|status
    sdr update             install the latest release (--check, --scheduled, --force)
    sdr doctor             check everything (--online, --offline)
    sdr open               where your files are (for your editor or AI agent)
    sdr version

Older commands still work: leadgen, email, inbox, social, pipeline, run-all, export, requalify,
digest. `python3 cli.py <command>` is the same as `sdr <command>`.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable

from core.config import ProfileError, compliance_warnings, load_profile
from core.product import CLI_NAME, DISPLAY_NAME, ROOT_DIR, TAGLINE, version
from core.tui import UI, valid_email

SECTION_NAMES = ("brand", "audience", "style", "email", "replies", "schedule", "security", "updates")
OPEN_TARGETS = {
    "folder": ("the install folder", ""),
    "profile": ("your settings (config/profile.toml)", os.path.join("config", "profile.toml")),
    "example": ("the explained example settings", os.path.join("config", "profile.example.toml")),
    "logs": ("the scheduled-run log", os.path.join("data", "cron.log")),
}


def _profile_or_none() -> dict | None:
    try:
        return load_profile()
    except ProfileError:
        return None


def _print_json(result) -> None:
    print(json.dumps(result, indent=2, default=str))


# ── reports and previews ─────────────────────────────────────────────────────


def cmd_status() -> int:
    from core.db import get_recent_logs, init_db
    from core.report import pipeline_stats, render_report
    init_db()
    profile = _profile_or_none()
    product = profile["sender"]["product_name"] if profile else ""
    print("\n" + render_report(pipeline_stats(profile), product, profile))
    print("\nRecent activity:")
    for log in get_recent_logs(8):
        print(f" [{log['timestamp'][:19]}] {log['bot_name']} - {log['action']} ({log['status']}): {log['details']}")
    print()
    return 0


def cmd_preview() -> int:
    """Show every email of the sequence and the searches the profile produces. Sends nothing."""
    from bots.email_marketing import build_outreach_email, sequence_length
    from bots.leadgen_pipeline import build_search_queries

    profile = load_profile()
    targeting = profile["targeting"]
    sample_lead = {
        "name": "Rivera Studio", "first_name": "Alex", "company": "Rivera Studio",
        "category": targeting["ideal_client"], "location": targeting["cities"][0],
    }
    follow_ups = profile["outreach"].get("follow_ups", [])
    day = 0.0
    for step in range(1, sequence_length(profile) + 1):
        if step > 1:
            day += float(follow_ups[step - 2].get("days_after_previous", 3))
        subject, _html, text = build_outreach_email(profile, sample_lead, step)
        label = "first email" if step == 1 else f"follow-up {step - 1}"
        print(f"\n-- Email {step} of {sequence_length(profile)} · {label} · day {day:g} "
              "(to a made-up lead) " + "-" * 20)
        print(f"Subject: {subject}\n\n{text}")
        sample_lead.setdefault("thread_subject", subject)

    queries = build_search_queries(profile)
    print(f"\n-- Web searches ({len(queries)} total, first 5) --")
    for query in queries[:5]:
        print(f"  {query['query']}")
    for warning in compliance_warnings(profile):
        print(f"\nWarning: {warning}")
    print(f"\nSend one to yourself with `{CLI_NAME} test-email` (or --step N for a follow-up).\n")
    return 0


# ── sample email ─────────────────────────────────────────────────────────────


def cmd_test_email(ui: UI, to: str | None = None, step: int = 1, reply: str | None = None,
                   signature: str | None = None, engine=None) -> int:
    """One real sample to yourself (default: your own mailbox). Never to anyone else unless --to."""
    from core.setup_sample import SampleError, build_sample, default_recipient, send_sample
    profile = load_profile()
    recipient = (to or default_recipient(profile)).strip()
    if not valid_email(recipient):
        ui.error(f"No address to send to. Use --to you@yourcompany.com (or set up your mailbox with "
                 f"`{CLI_NAME} setup --section email`).")
        return 1
    try:
        sample = build_sample(profile, step, reply, signature)
    except SampleError as exc:
        ui.error(str(exc))
        return 1
    ui.info(f"Sending {sample['label']} to {recipient}, written to a made-up lead (Alex at Rivera Studio).")
    ui.panel(f"Subject: {sample['subject']}\n\n{sample['text']}", title="Preview")
    for warning in compliance_warnings(profile):
        ui.warn(warning)
    result = send_sample(profile, recipient, step, reply, signature, engine=engine, sample=sample)
    if result["sent"]:
        ui.success(f"Sent! Check the inbox at {recipient} (and the spam folder).")
        return 0
    ui.error(f"Not sent. Check your email login with `{CLI_NAME} doctor` (or `{CLI_NAME} setup --section email`).")
    return 1


# ── dashboard, schedule, updates ─────────────────────────────────────────────


def cmd_dashboard(ui: UI, set_password: bool = False, no_open: bool = False, port: int = 8080) -> int:
    if set_password:
        from core.setup_wizard import run_setup
        code = run_setup(section="security", ui=ui, new_password=True)
        if code:
            return code
    from dashboard.server import DashboardUnavailable, start_dashboard
    try:
        start_dashboard(port, open_browser=not no_open)
    except DashboardUnavailable as exc:
        ui.error(str(exc))
        return 1
    return 0


def cmd_schedule(ui: UI, action: str | None) -> int:
    from core import scheduler
    if action is None:
        from core.scheduler_helper import print_schedule_instructions
        print_schedule_instructions()
        return 0
    try:
        if action == "on":
            lines = scheduler.install_schedule(load_profile())
            ui.success("The schedule is on (this computer's local time):")
        elif action == "off":
            lines = scheduler.remove_schedule()
        else:
            return _schedule_status(ui, scheduler)
    except scheduler.ScheduleError as exc:
        ui.error(str(exc))
        return 1
    for line in lines:
        ui.info(line)
    return 0


def _schedule_status(ui: UI, scheduler) -> int:
    status = scheduler.schedule_status()
    if status.get("error"):
        ui.warn(status["error"])
    if status.get("installed"):
        ui.success("The schedule is on.")
    else:
        ui.info(f"The schedule is off. Turn it on with `{CLI_NAME} schedule on`.")
    profile = _profile_or_none()
    if profile is not None:
        try:
            for line in scheduler.describe(scheduler.schedule_config(profile)):
                ui.info(line)
        except scheduler.ScheduleError as exc:
            ui.warn(str(exc))
    if status.get("legacy"):
        ui.warn(f"Found old hand-added cron lines; `{CLI_NAME} schedule on` tidies them up.")
    return 0


def cmd_update(ui: UI, check: bool = False, scheduled: bool = False, force: bool = False) -> int:
    from core import updater
    if scheduled:
        result = updater.scheduled_run(_profile_or_none())
        print(result["message"])
        return 1 if result["result"] in ("rolled_back", "rollback_failed") else 0
    with ui.spinner("Checking for updates"):
        status = updater.check_for_update()
    if status.get("error"):
        ui.warn(status["error"])
        return 1
    if not status.get("available"):
        ui.success(f"You're on the latest version (v{status.get('current', version())}).")
        return 0
    ui.info(f"Version {status['latest']} is available (you have v{status['current']}).")
    if status.get("notes"):
        ui.panel(status["notes"], title="What's new")
    if check:
        ui.info(f"Install it with `{CLI_NAME} update`.")
        return 0
    if ui.interactive and not ui.confirm("update.install", f"Install v{status['latest']} now?", default=True,
                                         hint="Your data is backed up first; anything that fails is rolled back."):
        return 0
    with ui.spinner("Updating: backup, install, tests"):
        result = updater.apply_update(force=force, tag=status.get("latest_tag"))
    if result["result"] == "updated":
        ui.success(result["message"])
        return 0
    (ui.info if result["result"] == "none" else ui.error)(result["message"])
    if result.get("output"):
        ui.panel(result["output"], title="Details")
    return 0 if result["result"] == "none" else 1


# ── doctor, open, version ────────────────────────────────────────────────────


def cmd_doctor(ui: UI, online: bool = False, offline: bool = False) -> int:
    from core.cli_doctor import run_doctor
    return run_doctor(ui, online=online, offline=offline)


def _open_path(path: str) -> bool:
    """Open a file or folder with the system's default app. No shell involved."""
    try:
        if sys.platform == "darwin":
            return subprocess.run(["open", path], check=False, timeout=15).returncode == 0
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]  # nosec B606 - a local path we built
            return True
        opener = shutil.which("xdg-open")
        return bool(opener) and subprocess.run([opener, path], check=False, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_open(ui: UI, what: str = "folder") -> int:
    label, relative = OPEN_TARGETS[what]
    path = os.path.join(ROOT_DIR, relative) if relative else ROOT_DIR
    ui.info(f"{DISPLAY_NAME} is installed in: {ROOT_DIR}")
    ui.info("Your settings are in config/profile.toml; every option is explained in config/profile.example.toml.")
    ui.info("Using an AI agent (Claude Code, Cursor...)? Open this folder in it and point it at AGENTS.md.")
    if not os.path.exists(path):
        ui.warn(f"There's no {relative or 'folder'} yet.")
        return 1
    if ui.interactive and ui.confirm("open.confirm", f"Open {label} now?", default=True):
        if not _open_path(path):
            ui.warn(f"Couldn't open it automatically. It's here: {path}")
    return 0


# ── home menu ────────────────────────────────────────────────────────────────


def cmd_run_now(ui: UI) -> int:
    if not ui.confirm("home.run", "This finds new leads and emails real prospects now. Go ahead?", default=False):
        return 0
    from runner import run_task
    _print_json(run_task("pipeline"))
    return 0


def cmd_home(ui: UI) -> int:
    from core.cli_home import print_commands, run_home
    from core.setup_wizard import pick_section, run_setup
    profile = _profile_or_none()
    if profile is None:
        if ui.interactive:
            return run_setup(ui=ui)
        ui.banner()
        ui.info(f"Not set up yet. Run `{CLI_NAME} setup` (or see AGENTS.md to let an AI agent do it).")
        print_commands(ui)
        return 0

    def toggle_schedule() -> int:
        from core import scheduler
        return cmd_schedule(ui, "off" if scheduler.schedule_status().get("installed") else "on")

    actions = {
        "dashboard": lambda: cmd_dashboard(ui),
        "run": lambda: cmd_run_now(ui),
        "sample": lambda: cmd_test_email(ui),
        "preview": cmd_preview,
        "settings": lambda: run_setup(section=pick_section(ui), ui=ui),
        "schedule": toggle_schedule,
        "update": lambda: cmd_update(ui),
        "doctor": lambda: cmd_doctor(ui),
    }
    return run_home(ui, actions, profile)


# ── argument parsing ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CLI_NAME, description=f"{DISPLAY_NAME}: {TAGLINE}")
    parser.add_argument("--version", action="version", version=f"{DISPLAY_NAME} v{version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    setup = sub.add_parser("setup", help="Set up, or change one part of your settings")
    setup.add_argument("--answers", metavar="FILE", help="Read the answers from a TOML file (no questions)")
    setup.add_argument("--section", metavar="NAME", help="Re-run one part: " + ", ".join(SECTION_NAMES))
    setup.add_argument("--print-questions", action="store_true", help="Print every question as JSON (for AI agents)")

    sub.add_parser("preview", help="Preview your email sequence and searches (sends nothing)")
    test = sub.add_parser("test-email", help="Send one real sample email to yourself")
    test.add_argument("--to", help="Where to send it (default: your own mailbox)")
    test.add_argument("--step", type=int, default=1, help="Which email of the sequence (1 = the first)")
    test.add_argument("--reply", choices=["interested", "question", "not_now"],
                      help="Send this auto-reply instead of a sequence email")
    test.add_argument("--signature", choices=["plain", "logo"], help="Force a signature version (A/B test)")

    dash = sub.add_parser("dashboard", help="Open the local dashboard")
    dash.add_argument("--set-password", action="store_true", help="Set a new dashboard password first")
    dash.add_argument("--no-open", action="store_true", help="Don't open the browser")
    dash.add_argument("--port", type=int, default=8080, help="Port (default 8080)")

    sub.add_parser("run", help="Find leads, email them and handle replies now (emails real prospects)")
    sub.add_parser("status", help="Pipeline report + recent activity")
    sub.add_parser("report", help="Same as status")
    schedule = sub.add_parser("schedule", help="Run automatically: on, off or status")
    schedule.add_argument("action", nargs="?", choices=["on", "off", "status"])
    update = sub.add_parser("update", help="Install the latest version (backup, tests, rollback)")
    mode = update.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Only check; don't install")
    mode.add_argument("--scheduled", action="store_true", help="What the weekly scheduled check runs")
    update.add_argument("--force", action="store_true", help="Update even if you edited files (saved as a patch)")
    doctor = sub.add_parser("doctor", help="Check everything and explain how to fix problems")
    doctor.add_argument("--online", action="store_true", help="Also test your Brave Search key")
    doctor.add_argument("--offline", action="store_true", help="Skip the network checks (mailbox login)")
    open_parser = sub.add_parser("open", help="Where your files are; open them")
    open_parser.add_argument("what", nargs="?", choices=list(OPEN_TARGETS), default="folder")
    sub.add_parser("version", help="Show the version")

    requalify = sub.add_parser("requalify", help="Re-score leads saved before fit-scoring existed")
    requalify.add_argument("--dry-run", action="store_true", help="Show the result without changing anything")
    digest = sub.add_parser("digest", help="Email yourself the pipeline digest now")
    digest.add_argument("--force", action="store_true", help="Send even if already sent today")
    leadgen = sub.add_parser("leadgen", help="Find new leads")
    leadgen.add_argument("--count", type=int, help="Number of leads to find (default: leads_per_run)")
    email = sub.add_parser("email", help="Send cold emails to new leads")
    email.add_argument("--limit", type=int, help="Max emails to send (default: emails_per_run)")
    sub.add_parser("inbox", help="Handle replies, bounces and unsubscribes")
    social = sub.add_parser("social", help="Draft a social post (does not post to X)")
    social.add_argument("--topic", type=str, help="Topic for post/thread")
    sub.add_parser("pipeline", help="Find leads -> email them -> handle replies")
    sub.add_parser("run-all", help="Run every bot in sequence")
    sub.add_parser("export", help="Export leads to data/leads_export.csv")
    sub.add_parser("resume", help="Start sending again after a bounce-rate pause")
    importer = sub.add_parser("import", help="Import your own leads from a CSV file (sends nothing)")
    importer.add_argument("file", nargs="?", help="CSV file with an email column")
    importer.add_argument("--dry-run", action="store_true", help="Only show what would be imported")
    importer.add_argument("--no-verify", action="store_true", help="Skip the check that each domain receives email")
    importer.add_argument("--template", action="store_true", help="Print an example CSV to start from")
    return parser


def _skipped(ui: UI, busy: Exception) -> dict:
    """The runner's answer when another run holds the lock: say so, log it, run nothing."""
    from core.db import log_event
    ui.warn(str(busy))
    log_event("Runner", "Skipped", "info", str(busy))
    return {"status": "skipped", "reason": str(busy)}


def _run_locked(command: str, action: Callable[[], object], ui: UI):
    """Run one bot under the runner's lock (data/.runner.lock), with the runner's wait policy.

    The single-bot commands change the same leads the scheduled pipeline and the dashboard do:
    `sdr email` during the 09:00 pipeline would otherwise pick the same "new" leads and send a first
    email twice, and `sdr update` couldn't tell that a run is in progress. The first try doesn't
    wait, so a person at the terminal is told they're waiting instead of watching a silent prompt;
    the quick inbox check never waits, exactly like its 45-minute cron job.
    """
    import runner
    from contextlib import ExitStack
    wait = runner.LOCK_WAIT_SECONDS.get(command, runner.DEFAULT_LOCK_WAIT_SECONDS)
    with ExitStack() as held:
        try:
            held.enter_context(runner.run_lock(0))
        except runner.AlreadyRunning as busy:
            if wait <= 0:
                return _skipped(ui, busy)
            ui.info(f"Another run is in progress (the schedule or the dashboard). Waiting up to "
                    f"{wait / 60:.0f} minutes for it to finish...")
            try:
                held.enter_context(runner.run_lock(wait))
            except runner.AlreadyRunning as still_busy:
                return _skipped(ui, still_busy)
        return action()


def _social(topic: str | None) -> dict:
    from bots.social_bot import SocialBot
    bot = SocialBot()
    return {"scheduled": bot.generate_and_schedule_thread(topic), "published": bot.publish_pending_posts()}


def _legacy(args: argparse.Namespace, ui: UI):
    """The original bot commands; they print their result as JSON. The ones that change leads or
    send mail run under the runner's lock (see _run_locked); pipeline/run/run-all already do, inside
    runner.run_task."""
    if args.command == "leadgen":
        from bots.leadgen_pipeline import LeadGenPipeline
        return _run_locked("leadgen", lambda: LeadGenPipeline().run_pipeline(args.count), ui)
    if args.command == "email":
        from bots.email_marketing import EmailMarketingEngine
        return _run_locked("email", lambda: EmailMarketingEngine().run_outreach_campaign(limit=args.limit), ui)
    if args.command == "inbox":
        from bots.inbox_listener import InboxListenerEngine
        return _run_locked("inbox", lambda: InboxListenerEngine().check_inbox_and_auto_reply(), ui)
    if args.command == "social":
        return _run_locked("social", lambda: _social(args.topic), ui)
    if args.command in ("pipeline", "run"):
        from runner import run_task
        return run_task("pipeline")
    if args.command == "run-all":
        from runner import run_task
        return run_task("all")
    if args.command == "requalify":
        from bots.leadgen_pipeline import requalify_existing_leads
        return requalify_existing_leads(dry_run=args.dry_run)
    if args.command == "digest":
        from bots.digest import send_daily_digest
        return send_daily_digest(load_profile(), force=args.force)
    if args.command == "export":
        from bots.leadgen_pipeline import export_leads_to_csv
        return {"csv_path": export_leads_to_csv()}
    raise ValueError(f"Unknown command {args.command!r}")


def dispatch(args: argparse.Namespace, ui: UI) -> int:
    command = args.command
    if command is None:
        return cmd_home(ui)
    if command == "setup":
        from core.setup_wizard import run_setup
        return run_setup(answers_path=args.answers, section=args.section, ui=None if args.answers else ui,
                         print_questions=args.print_questions)
    if command == "version":
        print(f"{DISPLAY_NAME} v{version()}")
        return 0
    if command in ("status", "report"):
        return cmd_status()
    if command == "preview":
        return cmd_preview()
    if command == "test-email":
        return cmd_test_email(ui, args.to, args.step, args.reply, args.signature)
    if command == "dashboard":
        return cmd_dashboard(ui, args.set_password, args.no_open, args.port)
    if command == "schedule":
        return cmd_schedule(ui, args.action)
    if command == "update":
        return cmd_update(ui, args.check, args.scheduled, args.force)
    if command == "doctor":
        return cmd_doctor(ui, args.online, args.offline)
    if command == "open":
        return cmd_open(ui, args.what)
    from core.db import init_db
    if command == "resume":
        from core.outreach_rules import resume_sending
        profile = load_profile()
        init_db()
        if resume_sending(profile):
            ui.success("Sending resumed. Earlier bounces no longer count; the bounce limit is unchanged, "
                       "so another bad batch pauses it again.")
        else:
            ui.info("Sending isn't paused, so there's nothing to resume.")
        return 0
    if command == "import":
        from core.cli_import import cmd_import
        if args.template:
            return cmd_import(ui, None, False, False, True, {})
        profile = load_profile()
        init_db()
        return cmd_import(ui, args.file, args.dry_run, args.no_verify, False, profile)
    init_db()
    _print_json(_legacy(args, ui))
    return 0


def main(argv: list[str] | None = None, ui: UI | None = None) -> int:
    # Windows gives a redirected stdout/stderr (a pipe to an AI agent, `> log.txt`) the ANSI code
    # page with strict errors, so the first emoji a bot prints would abort the run with a
    # UnicodeEncodeError; from here on an unencodable character prints as "?" instead. Streams
    # that can't be reconfigured (pytest capture, StringIO, no console at all) are left alone.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    ui = ui or UI()
    try:
        return int(dispatch(args, ui) or 0)
    except ProfileError as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        ui.echo("")
        ui.info("Cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
