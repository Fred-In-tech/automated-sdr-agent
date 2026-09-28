"""Prints the schedule from config/profile.toml and how to turn it on.

Kept as a thin wrapper for older imports (cli.py used to print copy-paste cron lines from here).
The real work — cron on macOS/Linux, Task Scheduler on Windows — lives in core/scheduler.py,
and `sdr schedule on` installs it for you.
"""

from core.config import ProfileError, load_profile
from core.product import CLI_NAME
from core.scheduler import ScheduleError, describe, schedule_config


def print_schedule_instructions() -> None:
    """User-facing CLI output: never raises for a missing or invalid profile, just explains."""
    try:
        profile = load_profile()
    except ProfileError:
        profile = None  # not set up yet: show the defaults
    try:
        cfg = schedule_config(profile)
    except ScheduleError as e:
        # Plain text: this can run with output redirected to a non-UTF-8 file on Windows.
        print(f"\nSchedule problem: {e}\n")
        return

    print("\nYour schedule (this computer's local time):")
    for line in describe(cfg):
        print(f"  • {line}")
    print(f"""
Turn it on:   {CLI_NAME} schedule on
Turn it off:  {CLI_NAME} schedule off
Check it:     {CLI_NAME} schedule status

Change the times with `{CLI_NAME} setup --section schedule` (or [schedule] in config/profile.toml),
then run `{CLI_NAME} schedule on` again. Output is logged to data/cron.log.

Tip: keep emails_per_run low (3-5) for the first 2 weeks so your
email address builds a good sending reputation.
""")
