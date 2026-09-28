"""Background runner for the outreach bots. Settings come from config/profile.toml.

Usage:
    python3 runner.py --task leadgen    (find new leads)
    python3 runner.py --task email      (send cold emails to new leads)
    python3 runner.py --task inbox      (handle replies, bounces and unsubscribes)
    python3 runner.py --task social     (draft social posts, if [social] enabled)
    python3 runner.py --task pipeline   (inbox -> leadgen -> email -> daily digest)
    python3 runner.py --task all
"""

import sys
import os
import argparse
import json
from contextlib import ExitStack, contextmanager
from core.db import DB_DIR, init_db, log_event
from core.locking import AlreadyLocked, file_lock
from core.config import ProfileError, load_profile
from bots.leadgen_pipeline import LeadGenPipeline
from bots.email_marketing import EmailMarketingEngine
from bots.social_bot import SocialBot
from bots.inbox_listener import InboxListenerEngine
from bots.digest import send_daily_digest

TASKS = ["leadgen", "email", "inbox", "social", "pipeline", "all"]


class AlreadyRunning(AlreadyLocked):
    """Another run (cron, Task Scheduler or dashboard) is in progress."""


# The quick inbox check (every 45 min) skips if something else is running; the main runs
# wait for it to finish instead, so a scheduled send is never skipped.
LOCK_WAIT_SECONDS = {"inbox": 0}
DEFAULT_LOCK_WAIT_SECONDS = 600
LOCK_FILE_NAME = ".runner.lock"


@contextmanager
def run_lock(wait_seconds: float = 0):
    """One run at a time, so cron + a dashboard click can never email the same lead twice.

    Uses core.locking, which works on macOS, Linux and Windows. DB_DIR is read at call time so
    tests (and AUTOMATIONS_DB_PATH) can point the lock somewhere else.
    """
    path = os.path.join(DB_DIR, LOCK_FILE_NAME)
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(path, wait_seconds=wait_seconds))
        except AlreadyLocked:
            raise AlreadyRunning(path, "Another run is in progress — skipping this one.") from None
        yield


def _run_social(profile: dict) -> dict:
    social = SocialBot(profile)
    return {"scheduled": social.generate_and_schedule_thread(), "published": social.publish_pending_posts()}


def run_task(task: str) -> dict:
    if task not in TASKS:
        raise ValueError(f"Unknown task '{task}'. Choose one of: {', '.join(TASKS)}")
    try:
        with run_lock(LOCK_WAIT_SECONDS.get(task, DEFAULT_LOCK_WAIT_SECONDS)):
            return _run_task(task)
    except AlreadyRunning as e:
        print(f"⏳ {e}")
        log_event("Runner", "Skipped", "info", str(e))
        return {"status": "skipped", "reason": str(e)}


def _run_task(task: str) -> dict:
    init_db()
    profile = load_profile()
    results = {}
    failed = []

    # Inbox FIRST: anyone who replied since the last run is taken out of the sequence
    # before any follow-up goes out.
    steps = [
        ("inbox", ("inbox", "email", "pipeline", "all"), "Checking inbox for replies...",
         lambda: InboxListenerEngine(profile).check_inbox_and_auto_reply()),
        ("leadgen", ("leadgen", "pipeline", "all"), "Finding new leads...",
         lambda: LeadGenPipeline(profile).run_pipeline()),
        ("email", ("email", "pipeline", "all"), "Sending outreach emails...",
         lambda: EmailMarketingEngine(profile).run_outreach_campaign()),
    ]
    if profile.get("social", {}).get("enabled"):
        steps.append(("social", ("social", "all"), "Drafting social posts...", lambda: _run_social(profile)))
    steps.append(("digest", ("pipeline", "all"), "Daily digest (once a day)...",
                  lambda: send_daily_digest(profile)))

    print(f"[Runner] Executing task: '{task}'...")
    log_event("Runner", "TaskStart", "info", f"Task '{task}' initiated.")

    for name, triggers, message, step in steps:
        if task not in triggers:
            continue
        print(f"-> {message}")
        try:
            results[name] = step()
        except Exception as e:  # one bot failing shouldn't stop the others
            failed.append(name)
            results[name] = {"status": "error", "error": str(e)}
            print(f"❌ [{name}] failed: {e}")
            log_event("Runner", f"{name}Error", "error", str(e))

    status = "error" if failed else "success"
    detail = f"Task '{task}' finished" + (f" with failures in: {', '.join(failed)}" if failed else ".")
    log_event("Runner", "TaskComplete", status, detail)
    print(f"[Runner] {detail}")
    return results


def main():
    # Same as cli.main: on Windows a pipe or log file gets the ANSI code page with strict errors,
    # and the first emoji a bot prints would abort the run (and the handler reporting it).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Outreach automation runner")
    parser.add_argument("--task", choices=TASKS, default="all",
                        help="The background task to execute (default: all)")
    args = parser.parse_args()

    try:
        results = run_task(args.task)
    except ProfileError as e:
        print(f"❌ {e}")
        sys.exit(1)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
