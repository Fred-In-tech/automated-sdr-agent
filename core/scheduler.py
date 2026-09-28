"""Turns the [schedule] in config/profile.toml into real scheduled jobs on this computer.

macOS / Linux: a managed block in the user's crontab, calling run_cron_pipeline.sh.
Windows:       tasks in Task Scheduler (folder "AutomatedSDR-<id>", one per install folder),
               calling run_task.cmd.

Why a managed block: people keep their own cron jobs, so we only ever touch the lines between
our `# >>> automated-sdr (<root>) >>>` markers (plus the lines older versions told people to paste
by hand). That makes `sdr schedule on` safe to run again and again, and `off` leaves the rest of
the crontab exactly as it was. The root folder is part of the marker, so two installs on one
machine don't overwrite each other; on Windows the task folder's suffix does the same job.

Every call to `crontab` / `schtasks` / `powershell` goes through an injectable `runner`
(subprocess.run by default), so tests never touch the real schedule.
"""

import csv
import hashlib
import io
import ntpath
import os
import posixpath
import re
import shlex
import subprocess
import sys
from collections.abc import Callable

from core.product import CLI_NAME, PRODUCT_NAME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_RUN_TIMES = ("09:00", "14:00")
DEFAULT_REPLY_CHECK_MINUTES = 45
# Checking more often than every 5 min just hammers the mailbox; less than twice a day isn't
# a "reply check" any more (and Task Scheduler's MINUTE schedule stops at 1439).
MIN_REPLY_CHECK_MINUTES = 5
MAX_REPLY_CHECK_MINUTES = 720
UPDATE_MODES = ("notify", "auto", "off")
DEFAULT_UPDATE_MODE = "notify"
UPDATE_CHECK_DAY = "MON"
UPDATE_CHECK_TIME = "08:30"

CRON_SCRIPT = "run_cron_pipeline.sh"
WINDOWS_SCRIPT = "run_task.cmd"
BLOCK_TAG = "automated-sdr"
TASK_FOLDER = "AutomatedSDR"  # prefix; task_folder() adds a per-install suffix
# Comments that pre-1.0 copy-paste instructions put above hand-added cron lines; kept so
# `sdr schedule on` can recognise and tidy up those old lines.
LEGACY_COMMENT_MARKERS = ("MyProposer SDR reply check", "MyProposer Outbound Automations")
MAX_TASK_COMMAND_LENGTH = 261  # schtasks /TR limit
SUBPROCESS_TIMEOUT = 60
PROFILE_HINT = "config/profile.toml"
# Task Scheduler's defaults skip a task that comes due on battery and kill one when the plug comes
# out; cron does neither, so the tasks are set to behave like cron.
POWER_SETTINGS = "New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
BATTERY_NOTE = ("Couldn't change the tasks' power settings, so Windows may skip runs while this computer "
                "is on battery. To fix it by hand: Task Scheduler > each task > Conditions > untick the "
                "battery boxes.")

# macOS privacy protection (TCC) blocks cron from these folders unless cron has Full Disk Access.
MACOS_PROTECTED_FOLDERS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

Runner = Callable[..., subprocess.CompletedProcess]


class ScheduleError(Exception):
    """The schedule can't be read or installed. The message says what to do about it."""


# ── Reading the profile ──────────────────────────────────────────────────────


def _config_error(message: str) -> ScheduleError:
    return ScheduleError(f"{message} Fix it in {PROFILE_HINT} (or run `{CLI_NAME} setup --section schedule`).")


def _normalize_time(value) -> str | None:
    """'9:05' -> '09:05'; None if it isn't a valid 24-hour HH:MM."""
    if not isinstance(value, str):
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _run_times(value) -> list[str]:
    if not isinstance(value, list):
        raise _config_error('[schedule] run_times must be a list of times, like ["09:00", "14:00"].')
    times = set()
    for item in value:
        normalized = _normalize_time(item)
        if normalized is None:
            raise _config_error(f"[schedule] run_times: {item!r} is not a 24-hour time like 09:00.")
        times.add(normalized)
    return sorted(times)


def _reply_check_minutes(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _config_error(f"[schedule] reply_check_minutes must be a whole number (got {value!r}).")
    if value != 0 and not MIN_REPLY_CHECK_MINUTES <= value <= MAX_REPLY_CHECK_MINUTES:
        raise _config_error(
            f"[schedule] reply_check_minutes must be 0 (off) or between {MIN_REPLY_CHECK_MINUTES} "
            f"and {MAX_REPLY_CHECK_MINUTES} (got {value})."
        )
    return value


def _update_mode(value) -> str:
    mode = value.strip().lower() if isinstance(value, str) else value
    if mode not in UPDATE_MODES:
        raise _config_error(f"[updates] mode must be one of {', '.join(UPDATE_MODES)} (got {value!r}).")
    return mode


def _section(profile: dict, name: str) -> dict:
    section = profile.get(name) or {}
    if not isinstance(section, dict):
        raise _config_error(f"[{name}] must be a table of settings.")
    return section


def schedule_config(profile: dict | None) -> dict:
    """The schedule settings with defaults filled in and validated.

    Returns {"run_times": ["09:00", "14:00"], "reply_check_minutes": 45, "update_mode": "notify"}.
    A missing value means "use the default"; an explicit empty run_times list means "no full runs"
    and reply_check_minutes = 0 means "no separate reply check". Raises ScheduleError on bad values
    so a typo never silently turns the schedule off.
    """
    profile = profile or {}
    schedule = _section(profile, "schedule")
    updates = _section(profile, "updates")
    return {
        "run_times": _run_times(schedule.get("run_times", list(DEFAULT_RUN_TIMES))),
        "reply_check_minutes": _reply_check_minutes(
            schedule.get("reply_check_minutes", DEFAULT_REPLY_CHECK_MINUTES)),
        "update_mode": _update_mode(updates.get("mode", DEFAULT_UPDATE_MODE)),
    }


def times_for_interval(minutes: int) -> list[str]:
    """Every `minutes` from 00:00 within one day, as 'HH:MM' (45 -> 00:00, 00:45, 01:30 … 23:15)."""
    if minutes <= 0:
        return []
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, minutes)]


def _reply_check_times(cfg: dict) -> list[str]:
    """Reply-check times, minus the full-run times: a full run checks replies first anyway."""
    full_runs = set(cfg["run_times"])
    return [t for t in times_for_interval(cfg["reply_check_minutes"]) if t not in full_runs]


# ── Human-readable summary ───────────────────────────────────────────────────


def _human_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _human_interval(minutes: int) -> str:
    if minutes == 60:
        return "hour"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} min"


def describe(cfg: dict) -> list[str]:
    """Plain-English lines for the setup summary, `sdr schedule status` and the dashboard."""
    times = cfg["run_times"]
    lines = [f"Full run at {_human_list(times)}" if times else "No full runs scheduled"]
    minutes = cfg["reply_check_minutes"]
    if minutes:
        lines.append(f"Reply check every {_human_interval(minutes)}")
    else:
        lines.append("Reply check off (replies are still handled at every full run)")
    when = f"every Monday at {UPDATE_CHECK_TIME}"
    lines.append({
        "notify": f"Update check {when} (tells you when a new version is out)",
        "auto": f"Update check {when} (installs new versions automatically)",
        "off": "Update check off",
    }[cfg["update_mode"]])
    return lines


# ── Cron (macOS / Linux) ─────────────────────────────────────────────────────


def _cron_root(root_dir: str) -> str:
    root = root_dir.rstrip("/") or "/"
    # cron turns an unescaped % into a newline, and a newline would split the job in two.
    if any(ch in root for ch in "%\n\r"):
        raise ScheduleError(
            f"The folder path {root!r} contains '%' or a line break, which cron can't run. "
            f"Move {PRODUCT_NAME} to a simpler folder (e.g. ~/automated-sdr) and try again."
        )
    return root


def _shell_quote(path: str) -> str:
    """Double quotes when that's safe (readable, like the old instructions), else strict quoting."""
    if any(ch in path for ch in "\"$`\\"):
        return shlex.quote(path)
    return f'"{path}"'


def _cron_fields(times: list[str]) -> list[tuple[str, str]]:
    """(minute, hour) cron fields that fire at exactly `times`: grouped by minute, and minutes
    that share the same hours merged into one line."""
    hours_by_minute: dict[int, list[int]] = {}
    for time_str in times:
        hour, minute = (int(part) for part in time_str.split(":"))
        hours_by_minute.setdefault(minute, []).append(hour)
    minutes_by_hours: dict[tuple[int, ...], list[int]] = {}
    for minute in sorted(hours_by_minute):
        minutes_by_hours.setdefault(tuple(sorted(set(hours_by_minute[minute]))), []).append(minute)
    return [(",".join(map(str, minutes)), ",".join(map(str, hours)))
            for hours, minutes in minutes_by_hours.items()]


def cron_lines(cfg: dict, root_dir: str) -> list[str]:
    """Crontab lines (short comments + jobs) for this schedule. Paths are quoted."""
    script = _shell_quote(posixpath.join(_cron_root(root_dir), CRON_SCRIPT))
    lines = []
    if cfg["run_times"]:
        lines.append("# Full run: check replies, find leads, send due emails, daily digest")
        lines += [f"{m} {h} * * * {script} pipeline" for m, h in _cron_fields(cfg["run_times"])]
    reply_times = _reply_check_times(cfg)
    if reply_times:
        interval = _human_interval(cfg["reply_check_minutes"])
        lines.append(f"# Reply check every {interval} (sends no cold email)")
        lines += [f"{m} {h} * * * {script} inbox" for m, h in _cron_fields(reply_times)]
    mode = cfg["update_mode"]
    if mode != "off":
        hour, minute = (int(part) for part in UPDATE_CHECK_TIME.split(":"))
        lines.append(f"# Update check, Mondays {UPDATE_CHECK_TIME} (mode: {mode})")
        lines.append(f"{minute} {hour} * * 1 {script} update")
    return lines


def _markers(root: str) -> tuple[str, str]:
    return f"# >>> {BLOCK_TAG} ({root}) >>>", f"# <<< {BLOCK_TAG} ({root}) <<<"


def _calls_script(line: str, script: str) -> bool:
    """True if a (non-comment) crontab line runs exactly this script path — not one in another
    folder whose path merely contains it."""
    if not line.strip() or line.lstrip().startswith("#"):
        return False
    start = line.find(script)
    while start != -1:
        end = start + len(script)
        before = line[start - 1] if start else " "
        after = line[end] if end < len(line) else " "
        if (before.isspace() or before in "\"'=") and (after.isspace() or after in "\"';"):
            return True
        start = line.find(script, start + 1)
    return False


def _is_legacy_comment(line: str) -> bool:
    return line.lstrip().startswith("#") and any(marker in line for marker in LEGACY_COMMENT_MARKERS)


def _block_spans(lines: list[str], begin: str, end: str) -> list[tuple[int, int]]:
    """(first, last) line indexes of each managed block. An opening marker with no closing one
    spans only itself, so a hand-edited crontab never loses lines that aren't ours."""
    spans, i = [], 0
    while i < len(lines):
        if lines[i].strip() == begin:
            close = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == end), None)
            spans.append((i, i if close is None else close))
            i = i + 1 if close is None else close + 1
        else:
            i += 1
    return spans


def _strip_ours(existing: str, root_dir: str) -> tuple[list[str], int | None]:
    """The crontab without our block(s) and legacy lines, plus where the block used to be."""
    root = _cron_root(root_dir)
    begin, end = _markers(root)
    script = posixpath.join(root, CRON_SCRIPT)
    lines = existing.splitlines()
    spans = _block_spans(lines, begin, end)
    block_starts = {first for first, _ in spans}
    inside = {i for first, last in spans for i in range(first, last + 1)}

    kept: list[tuple[int, str]] = []  # (original index, line) — the index tells us what was adjacent
    insert_at = None
    for i, line in enumerate(lines):
        if i in block_starts:
            if kept and kept[-1][0] == i - 1 and not kept[-1][1].strip():
                kept.pop()  # the blank separator line we add above the block
            if insert_at is None:
                insert_at = len(kept)
        if i in inside or line.strip() == end:
            continue
        if _calls_script(line, script):
            if kept and kept[-1][0] == i - 1 and _is_legacy_comment(kept[-1][1]):
                kept.pop()
            continue
        kept.append((i, line))
    return [line for _, line in kept], insert_at


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def merge_crontab(existing: str, lines: list[str], root_dir: str) -> str:
    """`existing` crontab text with our managed block set to `lines`.

    Replaces the block in place (or appends it), drops legacy hand-pasted lines for this folder,
    and keeps every other line verbatim. Idempotent. An empty `lines` removes the block.
    """
    kept, insert_at = _strip_ours(existing, root_dir)
    if not lines:
        return _join_lines(kept)
    begin, end = _markers(_cron_root(root_dir))
    note = f"# Managed by {PRODUCT_NAME} ({CLI_NAME} schedule on / off). Edits inside this block are replaced."
    block = [begin, note, *lines, end]
    insert_at = len(kept) if insert_at is None else insert_at
    before, after = kept[:insert_at], kept[insert_at:]
    if before and before[-1].strip():
        block.insert(0, "")
    return _join_lines(before + block + after)


def remove_from_crontab(existing: str, root_dir: str) -> str:
    """`existing` without our managed block and legacy lines; everything else untouched."""
    return merge_crontab(existing, [], root_dir)


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "no details"


def _crontab_call(runner: Runner, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    # surrogateescape round-trips any bytes already in the crontab, so we never corrupt them.
    kwargs = {"capture_output": True, "encoding": "utf-8", "errors": "surrogateescape",
              "timeout": SUBPROCESS_TIMEOUT}
    if stdin is not None:
        kwargs["input"] = stdin
    try:
        return runner(["crontab", *args], **kwargs)
    except FileNotFoundError:
        raise ScheduleError(
            "The `crontab` program isn't installed on this computer. Install cron "
            "(Debian/Ubuntu: `sudo apt install cron`, Fedora: `sudo dnf install cronie`) and try again."
        ) from None
    except (OSError, subprocess.SubprocessError) as e:
        raise ScheduleError(f"Couldn't run `crontab`: {e}") from None


def _read_crontab(runner: Runner) -> str:
    result = _crontab_call(runner, ["-l"])
    if result.returncode == 0:
        return result.stdout or ""
    if "no crontab" in (result.stderr or "").lower():
        return ""
    # Anything else: we couldn't read it, so writing would risk wiping the user's jobs.
    raise ScheduleError(f"Couldn't read your crontab ({_first_line(result.stderr)}). Nothing was changed.")


def _write_crontab(text: str, runner: Runner) -> None:
    result = _crontab_call(runner, ["-"], stdin=text)
    if result.returncode != 0:
        raise ScheduleError(f"Couldn't save your crontab ({_first_line(result.stderr)}).")


def macos_privacy_note(root_dir: str, home: str | None = None) -> str | None:
    """A warning when the install folder is one macOS hides from cron (Desktop, Documents, …)."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    root = root_dir.rstrip("/")
    for folder in MACOS_PROTECTED_FOLDERS:
        protected = f"{home}/{folder}"
        if root == protected or root.startswith(protected + "/"):
            return (
                f"This folder is inside ~/{folder}, which macOS hides from scheduled jobs. Move "
                f"{PRODUCT_NAME} (e.g. to ~/automated-sdr) or give /usr/sbin/cron Full Disk Access in "
                "System Settings > Privacy & Security."
            )
    return None


# ── Windows Task Scheduler ───────────────────────────────────────────────────


def task_folder(root_dir: str) -> str:
    """The Task Scheduler folder for the install in `root_dir`: "AutomatedSDR-<8 hex chars>".

    The suffix comes from the install path as Windows sees it (case and slash direction don't
    matter), so a second copy on the same PC gets its own folder and `schedule on` / `off` in one
    copy can never remove the other's tasks - the isolation the cron block's marker gives.
    """
    normalized = ntpath.normcase(ntpath.normpath(root_dir))
    digest = hashlib.blake2b(normalized.encode("utf-8"), digest_size=4).hexdigest()
    return f"{TASK_FOLDER}-{digest}"


def _tasks(cfg: dict) -> list[tuple[str, str, list[str]]]:
    """(task name, run_task.cmd argument, schtasks schedule switches) for each job in `cfg`."""
    tasks = [("Pipeline-" + t.replace(":", ""), "pipeline", ["/SC", "DAILY", "/ST", t]) for t in cfg["run_times"]]
    if cfg["reply_check_minutes"]:
        tasks.append(("ReplyCheck", "inbox", ["/SC", "MINUTE", "/MO", str(cfg["reply_check_minutes"])]))
    if cfg["update_mode"] != "off":
        tasks.append(("UpdateCheck", "update", ["/SC", "WEEKLY", "/D", UPDATE_CHECK_DAY, "/ST", UPDATE_CHECK_TIME]))
    return tasks


def schtasks_commands(cfg: dict, root_dir: str) -> list[list[str]]:
    """`schtasks /Create … /F` commands for this schedule (/F overwrites a task of the same name)."""
    script = ntpath.join(root_dir, WINDOWS_SCRIPT)
    folder = task_folder(root_dir)
    commands = []
    for leaf, task, when in _tasks(cfg):
        command = f'"{script}" {task}'
        if len(command) > MAX_TASK_COMMAND_LENGTH:
            raise ScheduleError(
                f"The folder path is too long for Windows Task Scheduler. Move {PRODUCT_NAME} to a "
                "shorter path (e.g. C:\\automated-sdr) and try again."
            )
        commands.append(["schtasks", "/Create", "/TN", folder + "\\" + leaf, "/TR", command, *when, "/F"])
    return commands


def battery_commands(cfg: dict, root_dir: str) -> list[list[str]]:
    """PowerShell commands that let each task start, and keep running, on battery power.

    `schtasks /Create` has no switch for power conditions, so without this a laptop user whose
    09:00 run comes due while unplugged gets nothing - no run, no log line, and `schedule status`
    still says "on". The [schedule] settings should mean the same on every OS.
    """
    path = "\\" + task_folder(root_dir) + "\\"
    return [["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Set-ScheduledTask -TaskPath '{path}' -TaskName '{leaf}' -Settings ({POWER_SETTINGS})"]
            for leaf, _task, _when in _tasks(cfg)]


def schtasks_remove_commands(task_names: list[str]) -> list[list[str]]:
    """`schtasks /Delete` commands for the given task names (from parse_schtasks_query)."""
    return [["schtasks", "/Delete", "/TN", name, "/F"] for name in task_names]


def parse_schtasks_query(output: str, root_dir: str) -> list[str]:
    """This install's task names from `schtasks /Query /FO CSV /NH` output. The task-name column
    isn't translated on non-English Windows, unlike the other columns, so it's safe to parse."""
    prefix = task_folder(root_dir).lower() + "\\"
    names: list[str] = []
    for row in csv.reader(io.StringIO(output)):
        if not row:
            continue
        name = row[0].strip()
        if name.lstrip("\\").lower().startswith(prefix):
            name = name if name.startswith("\\") else "\\" + name
            if name not in names:
                names.append(name)
    return names


def _schtasks(runner: Runner, cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return runner(cmd, capture_output=True, text=True, errors="replace", timeout=SUBPROCESS_TIMEOUT)
    except FileNotFoundError:
        raise ScheduleError("Windows Task Scheduler (schtasks) wasn't found on this computer.") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise ScheduleError(f"Couldn't run schtasks: {e}") from None


def _run_schtasks(runner: Runner, cmd: list[str], action: str) -> None:
    result = _schtasks(runner, cmd)
    if result.returncode != 0:
        reason = _first_line(result.stderr or result.stdout)
        raise ScheduleError(f"Couldn't {action} in Task Scheduler ({reason}).")


def _installed_tasks(runner: Runner, root_dir: str) -> list[str]:
    result = _schtasks(runner, ["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if result.returncode != 0:
        raise ScheduleError(f"Couldn't list scheduled tasks ({_first_line(result.stderr or result.stdout)}).")
    return parse_schtasks_query(result.stdout or "", root_dir)


def _relax_power(runner: Runner, cmd: list[str]) -> bool:
    """Best effort: a PC without PowerShell's ScheduledTasks module keeps the tasks it just got,
    and the caller only adds a note about battery power."""
    try:
        return _schtasks(runner, cmd).returncode == 0
    except ScheduleError:
        return False


# ── Install / remove / status ────────────────────────────────────────────────


def _is_windows(system: str | None) -> bool:
    if system is None:
        return os.name == "nt"
    return system.lower() in ("windows", "nt", "win32")


def _require_script(root_dir: str, name: str) -> str:
    path = os.path.join(root_dir, name)
    if not os.path.isfile(path):
        raise ScheduleError(f"{name} is missing from {root_dir}. Re-install {PRODUCT_NAME} or run `git pull`.")
    return path


def _ensure_executable(path: str) -> None:
    """cron runs the script directly, so it needs the execute bit (a zip download loses it)."""
    if os.name == "nt" or os.access(path, os.X_OK):
        return
    mode = os.stat(path).st_mode
    os.chmod(path, mode | ((mode & 0o444) >> 2))  # add x wherever r is set


def install_schedule(profile: dict | None, root_dir: str = ROOT, runner: Runner = subprocess.run,
                     system: str | None = None) -> list[str]:
    """Install (or update) the schedule from the profile. Returns plain-English lines describing
    it. Raises ScheduleError with a fix-it message; nothing is changed when the profile is invalid."""
    cfg = schedule_config(profile)
    if _is_windows(system):
        _require_script(root_dir, WINDOWS_SCRIPT)
        commands = schtasks_commands(cfg, root_dir)
        # Remove what's there first, so a run time you dropped doesn't keep firing.
        for cmd in schtasks_remove_commands(_installed_tasks(runner, root_dir)):
            _run_schtasks(runner, cmd, "remove an old scheduled task")
        for cmd in commands:
            _run_schtasks(runner, cmd, "create a scheduled task")
        relaxed = [_relax_power(runner, cmd) for cmd in battery_commands(cfg, root_dir)]
        lines = [*describe(cfg),
                 f"Added to Windows Task Scheduler (folder {task_folder(root_dir)}); runs while you're signed in."]
        return lines + ([] if all(relaxed) else [BATTERY_NOTE])

    _ensure_executable(_require_script(root_dir, CRON_SCRIPT))
    lines = cron_lines(cfg, root_dir)
    existing = _read_crontab(runner)
    updated = merge_crontab(existing, lines, root_dir)
    if updated != existing:
        _write_crontab(updated, runner)
    result = [*describe(cfg), "Added to your crontab; runs while this computer is on and awake."]
    note = macos_privacy_note(root_dir) if sys.platform == "darwin" else None
    return result + ([note] if note else [])


def remove_schedule(root_dir: str = ROOT, runner: Runner = subprocess.run, system: str | None = None) -> list[str]:
    """Remove this install's scheduled jobs (and nothing else). Returns what was done."""
    if _is_windows(system):
        tasks = _installed_tasks(runner, root_dir)
        for cmd in schtasks_remove_commands(tasks):
            _run_schtasks(runner, cmd, "remove a scheduled task")
        if not tasks:
            return ["No schedule was installed."]
        return [f"Removed {len(tasks)} scheduled task(s) from Windows Task Scheduler."]

    existing = _read_crontab(runner)
    updated = remove_from_crontab(existing, root_dir)
    if updated == existing:
        return ["No schedule was installed."]
    _write_crontab(updated, runner)
    return ["Removed the schedule from your crontab. Your other cron jobs are untouched."]


def schedule_status(root_dir: str = ROOT, runner: Runner = subprocess.run, system: str | None = None) -> dict:
    """What is installed right now. Never raises: problems are reported in "error".

    {"installed": bool, "backend": "cron" | "schtasks", "lines": [cron job lines],
     "tasks": [task names], "legacy": bool (old hand-pasted cron lines found), "error": str | None}
    """
    status = {"installed": False, "backend": "schtasks" if _is_windows(system) else "cron",
              "lines": [], "tasks": [], "legacy": False, "error": None}
    try:
        if _is_windows(system):
            status["tasks"] = _installed_tasks(runner, root_dir)
            status["installed"] = bool(status["tasks"])
            return status
        root = _cron_root(root_dir)
        script = posixpath.join(root, CRON_SCRIPT)
        lines = _read_crontab(runner).splitlines()
        begin, end = _markers(root)
        inside = {i for first, last in _block_spans(lines, begin, end) if last > first
                  for i in range(first, last + 1)}
        ours = [(i, line) for i, line in enumerate(lines) if _calls_script(line, script)]
        status["lines"] = [line.strip() for _, line in ours]
        status["legacy"] = any(i not in inside for i, _ in ours)
        status["installed"] = bool(ours)
    except ScheduleError as e:
        status["error"] = str(e)
    return status
