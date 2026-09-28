"""Self-updates for installed copies: check for a release, apply it safely, roll back on failure.

An installed copy is a git clone, and releases are the git tags `vX.Y.Z` on `origin`. Updating
means fast-forwarding to the newest release tag: never to whatever happens to be on `main`, and
never a merge that could rewrite someone's own commits. The order of an update is:

    1. refuse if tracked files were edited (unless forced; the edits are then saved as a patch)
    2. back up the user's data into data/backups/<timestamp>/ (the newest 5 are kept)
    3. `git merge --ff-only <tag>`
    4. reinstall requirements into the project's .venv (or with the interpreter running us)
    5. run the test suite with that same interpreter
    6. any failure in 3-5: `git reset --hard <previous HEAD>` and reinstall the old requirements

User data (data/, .env, config/profile.toml, config/do_not_contact.txt) is git-ignored, so neither
the fast-forward nor the reset can touch it; the backup is there in case a release migrates it.

Every subprocess (git, pip, tests) is injectable, so the whole flow is testable offline. The only
network access is `git fetch` from `origin`.

The result of the last check/update lives in data/update_status.json, which the daily digest
and the dashboard read. `last_result` is one of RESULTS.
"""

import contextlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone

from core.product import CLI_NAME, ROOT_DIR

GitRunner = Callable[[list[str], str], subprocess.CompletedProcess]
StepRunner = Callable[[str], subprocess.CompletedProcess]

MODES = ("notify", "auto", "off")
DEFAULT_MODE = "notify"
# "none" = nothing attempted; the rest describe the last attempt to apply an update.
RESULTS = ("none", "updated", "rolled_back", "rollback_failed",
           "skipped_dirty", "skipped_diverged", "skipped_busy", "failed")

STATUS_RELPATH = os.path.join("data", "update_status.json")
BACKUP_RELDIR = os.path.join("data", "backups")
BACKUP_FILES = (
    os.path.join("data", "automations.db"),
    os.path.join("config", "profile.toml"),
    ".env",
    os.path.join("config", "do_not_contact.txt"),
)
KEEP_BACKUPS = 5
PATCH_NAME = "local-changes.patch"
# Same file runner.py locks, so an update never swaps code under a running pipeline.
LOCK_RELPATH = os.path.join("data", ".runner.lock")
SCHEDULED_LOCK_WAIT_SECONDS = 600

GIT_TIMEOUT_SECONDS = 120
PIP_TIMEOUT_SECONDS = 900
TEST_TIMEOUT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 60  # "does the .venv have pytest?"
# `args` of the result run_pip returns when it ran nothing (see pip_was_skipped).
PIP_SKIPPED_ARGS = ("pip", "skipped")
NOTES_MAX_CHARS = 4000
OUTPUT_TAIL_LINES = 20
DIGEST_RECENT = timedelta(hours=24)

# Status keys that describe the last update attempt; a plain check must not erase them.
PRESERVED_KEYS = ("last_result", "last_attempt_at", "last_target", "last_message", "last_backup")

NOT_A_CHECKOUT = ("This copy isn't a git checkout, so it can't update itself. "
                  "Reinstall with the one-line installer to get updates.")

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_BACKUP_NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
_HEADING_RE = re.compile(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?")
_LINK_REF_RE = re.compile(r"^\[[^\]]+\]:\s*\S+")
_URL_CREDENTIALS_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")

_DIGEST_PROBLEMS = {
    "rolled_back": "it failed its checks and was rolled back, so nothing changed",
    "rollback_failed": "it failed and could not be rolled back, so this copy needs attention",
    "skipped_dirty": "some project files were edited on this computer",
    "skipped_diverged": "this copy has its own commits",
    "skipped_busy": "a run was in progress",
    "failed": "it could not be applied, so nothing changed",
}


class UpdateBusy(Exception):
    """A pipeline run holds the runner lock; updating now could swap code under it."""


# ---------------------------------------------------------------------------------------
# Versions, tags and release notes
# ---------------------------------------------------------------------------------------

def parse_version(value: object) -> tuple[int, int, int] | None:
    """"1.2.3" or "v1.2.3" -> (1, 2, 3). Pre-releases and anything else -> None.

    Only stable releases are installable, so "v2.0.0-rc1" is deliberately not a version.
    """
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def latest_tag(tags: Iterable[str]) -> str | None:
    """The newest release tag (`vX.Y.Z`), compared numerically so v1.10.0 beats v1.9.0."""
    releases = [tag.strip() for tag in tags if _TAG_RE.match(tag.strip())]
    return max(releases, key=parse_version, default=None)


def read_version(root: str | None = None) -> str:
    """The version of the code in `root` (its VERSION file), "0.0.0" if unreadable."""
    try:
        with open(os.path.join(_root(root), "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def changelog_notes(text: str, current: str, latest: str | None = None,
                    max_chars: int = NOTES_MAX_CHARS) -> str:
    """The Keep-a-Changelog sections newer than `current` (up to `latest`), as markdown.

    "Unreleased" and link-reference lines are dropped: users should only ever read about
    what the update actually installs.
    """
    floor = parse_version(current) or (0, 0, 0)
    ceiling = parse_version(latest) if latest else None
    sections: list[list[str]] = []
    keep = False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            match = _HEADING_RE.match(line)
            version = parse_version(match.group(1)) if match else None
            keep = version is not None and version > floor and (ceiling is None or version <= ceiling)
            if keep:
                sections.append([line])
            continue
        if keep and not _LINK_REF_RE.match(line):
            sections[-1].append(line)
    notes = "\n\n".join("\n".join(section).strip() for section in sections)
    if len(notes) > max_chars:
        notes = notes[:max_chars - 1].rstrip() + "…"
    return notes


def update_mode(profile: dict | None) -> str:
    """The profile's [updates] mode: "notify" (default), "auto" or "off"."""
    updates = (profile or {}).get("updates")
    if not isinstance(updates, dict):
        return DEFAULT_MODE
    mode = str(updates.get("mode", DEFAULT_MODE)).strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


# ---------------------------------------------------------------------------------------
# Subprocess runners (the defaults; tests inject fakes)
# ---------------------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str, timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    """subprocess.run that never raises: a missing binary or a timeout is just a failed result,
    so a scheduled update can always write its status and exit cleanly."""
    label = " ".join(cmd[:3])
    try:
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{label} timed out after {timeout:g} s")
    except OSError as e:  # includes FileNotFoundError: git/python not installed or bad cwd
        return subprocess.CompletedProcess(cmd, 127, "", f"could not run {cmd[0]}: {e}")


def run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run git without ever waiting for a password prompt (cron has nobody to answer it)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    return _run(["git", *args], cwd, GIT_TIMEOUT_SECONDS, env)


def _venv_python(root: str) -> str | None:
    """The interpreter of <root>/.venv (what the installer creates), or None without one."""
    for parts in (("bin", "python"), ("Scripts", "python.exe")):
        path = os.path.join(root, ".venv", *parts)
        if os.path.isfile(path):
            return path
    return None


def project_python(root: str) -> str:
    """The Python that owns this copy's requirements: its .venv when there is one, else the
    interpreter running us. `python3 cli.py update` from a system Python must still install
    into, and test with, the environment the bots actually run in."""
    return _venv_python(root) or sys.executable


def _in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")


def pip_unavailable_reason() -> str | None:
    """Why `python -m pip install` can't work for the interpreter running us, or None if it can.

    Outside a virtual environment, Debian/Ubuntu and Homebrew mark their Python as externally
    managed (PEP 668) and pip refuses to install; Debian also ships without pip. Either used to
    fail every scheduled update and roll it back, so the copy could never move forward.
    """
    if _in_virtualenv():
        return None
    if importlib.util.find_spec("pip") is None:
        return f"pip isn't installed for {sys.executable}"
    if os.path.isfile(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")):
        return (f"{sys.executable} is externally managed (PEP 668) and refuses pip installs outside "
                "a virtual environment")
    return None


def skipped_pip_result(reason: str) -> subprocess.CompletedProcess:
    """A successful pip result that says the step was skipped, and why, so the update's message
    can pass it on (pip_was_skipped recognises it)."""
    note = (f"Requirements were not reinstalled: {reason}. If this release needs new packages, run "
            "`pip install -r requirements.txt` in the environment the bots run in.")
    return subprocess.CompletedProcess(list(PIP_SKIPPED_ARGS), 0, note, "")


def pip_was_skipped(result: subprocess.CompletedProcess) -> bool:
    return list(result.args) == list(PIP_SKIPPED_ARGS)


def run_pip(root: str) -> subprocess.CompletedProcess:
    """Reinstall requirements into the project's .venv, or with the interpreter running us.

    Without a .venv, and with a Python that pip can't install into (PEP 668, no pip), the step is
    skipped with a note instead of failing the whole update: the self-test still catches a
    release that really needs a new package.
    """
    python = _venv_python(root)
    if python is None:
        reason = pip_unavailable_reason()
        if reason:
            return skipped_pip_result(reason)
        python = sys.executable
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-r", "requirements.txt"]
    return _run(cmd, root, PIP_TIMEOUT_SECONDS)


def _has_pytest(python: str, root: str) -> bool:
    if python == sys.executable:
        return importlib.util.find_spec("pytest") is not None
    return _run([python, "-c", "import pytest"], root, PROBE_TIMEOUT_SECONDS).returncode == 0


def run_tests(root: str) -> subprocess.CompletedProcess:
    """Self-test the freshly installed code, with the project's interpreter, before trusting it.

    RUN_LIVE_TESTS is stripped so an update can never scrape the web or send email. Installs
    without pytest fall back to importing the entry points, which still catches a broken release.
    """
    env = {key: value for key, value in os.environ.items() if key != "RUN_LIVE_TESTS"}
    python = project_python(root)
    if _has_pytest(python, root):
        cmd = [python, "-m", "pytest", "tests", "-q", "-x", "-p", "no:cacheprovider"]
    else:
        cmd = [python, "-c", "import cli, runner"]
    return _run(cmd, root, TEST_TIMEOUT_SECONDS, env)


def redact(text: str) -> str:
    """Strip credentials from URLs (https://user:token@host) before output is shown or saved."""
    return _URL_CREDENTIALS_RE.sub(r"\1***@", text or "")


def _tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    return "\n".join(redact(text).strip().splitlines()[-lines:])


def _output(result: subprocess.CompletedProcess) -> str:
    return _tail("\n".join(part for part in (result.stdout, result.stderr) if part))


# ---------------------------------------------------------------------------------------
# Status file (data/update_status.json)
# ---------------------------------------------------------------------------------------

def _root(root: str | None) -> str:
    return os.path.abspath(root) if root else ROOT_DIR


def status_path(root: str | None = None) -> str:
    return os.path.join(_root(root), STATUS_RELPATH)


def read_status(root: str | None = None) -> dict:
    """The last written status, or {} if there is none (or it is unreadable)."""
    try:
        with open(status_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(status: dict, root: str | None = None) -> None:
    """Write atomically, so the dashboard never reads a half-written file."""
    path = status_path(root)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=folder, prefix=".update_status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------------------

def _same_path(a: str, b: str) -> bool:
    """Compare by file identity: git may print C:/Users/... for C:\\Users\\RUNNER~1\\..., and
    macOS temp folders live behind a /var -> /private/var symlink."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _is_checkout(root: str, git: GitRunner) -> bool:
    """True only if `root` is the top of its own repo, not a folder inside some other repo."""
    result = git(["rev-parse", "--show-toplevel"], root)
    return result.returncode == 0 and _same_path(result.stdout.strip(), root)


def _rev(root: str, git: GitRunner, ref: str) -> str | None:
    result = git(["rev-parse", "--verify", "--quiet", ref], root)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _collect_status(root: str, git: GitRunner, mode: str | None, fetch: bool) -> dict:
    previous = read_status(root)
    if mode not in MODES:
        mode = previous.get("mode") if previous.get("mode") in MODES else DEFAULT_MODE
    status = {
        "checked_at": _now_iso(), "current": read_version(root), "latest": None, "latest_tag": None,
        "available": False, "mode": mode, "notes": "", "error": "", "last_result": "none",
    }
    status.update({key: previous[key] for key in PRESERVED_KEYS if key in previous})
    if not _is_checkout(root, git):
        status["error"] = NOT_A_CHECKOUT
        return status

    if fetch:
        fetched = git(["fetch", "--tags", "--quiet", "origin"], root)
        if fetched.returncode != 0:
            # Keep going with the tags we already have: a flaky network shouldn't hide an
            # update that an earlier fetch already found.
            status["error"] = "Couldn't check for updates (git fetch failed): " + _tail(
                fetched.stderr or fetched.stdout, 3)

    tag = latest_tag(git(["tag", "--list", "v*"], root).stdout.split())
    if not tag:
        return status
    latest = tag[1:]
    status.update(latest=latest, latest_tag=tag)
    newer = parse_version(latest) > (parse_version(status["current"]) or (0, 0, 0))
    # Also require the tag to be missing from HEAD, so a VERSION file that disagrees with its
    # tag can't cause the same update to be "available" forever.
    contained = git(["merge-base", "--is-ancestor", tag, "HEAD"], root).returncode == 0
    status["available"] = newer and not contained
    if status["available"]:
        changelog = git(["show", f"{tag}:CHANGELOG.md"], root)
        if changelog.returncode == 0:
            status["notes"] = changelog_notes(changelog.stdout, status["current"], latest)
    return status


def check_for_update(root: str | None = None, git: GitRunner | None = None, mode: str | None = None,
                     fetch: bool = True, write: bool = True) -> dict:
    """Fetch release tags from origin and report whether a newer release exists.

    Returns (and by default writes to data/update_status.json) a dict with checked_at, current,
    latest, latest_tag, available, mode, notes (markdown), error ("" when fine), last_result and
    the other last_* keys of the previous update attempt. Never raises for git/network problems:
    they end up in "error". Changes no code.
    """
    root = _root(root)
    status = _collect_status(root, git or run_git, mode, fetch)
    if write:
        write_status(status, root)
    return status


# ---------------------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------------------

def _make_private(path: str, mode: int) -> None:
    """Best effort: backups hold secrets and lead data. Windows ignores most mode bits."""
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def _backup_sort_key(name: str) -> tuple[str, str, int]:
    parts = name.split("-")
    return parts[0], parts[1], int(parts[2]) if len(parts) > 2 else 1


def _new_backup_dir(root: str, now: datetime | None) -> str:
    base = os.path.join(root, BACKUP_RELDIR)
    os.makedirs(base, exist_ok=True)
    _make_private(base, 0o700)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate, n = os.path.join(base, stamp), 1
    while True:
        try:
            os.mkdir(candidate, 0o700)  # atomic, so two updates can never share a folder
            return candidate
        except FileExistsError:
            n += 1
            candidate = os.path.join(base, f"{stamp}-{n}")


def _prune_backups(base: str, keep: int, protect: str) -> None:
    """Delete all but the newest `keep` timestamped backups. Other folders are never touched."""
    names = sorted((name for name in os.listdir(base)
                    if _BACKUP_NAME_RE.match(name) and os.path.isdir(os.path.join(base, name))),
                   key=_backup_sort_key)
    removable = [name for name in names if not _same_path(os.path.join(base, name), protect)]
    for name in removable[:max(0, len(names) - keep)]:
        # A backup we can't delete (e.g. open in another program) just stays; that's harmless.
        with contextlib.suppress(OSError):
            shutil.rmtree(os.path.join(base, name))


def _copy_sqlite(src: str, dst: str) -> None:
    """SQLite's online backup gives a consistent copy even if the file is being written."""
    source = sqlite3.connect(src, timeout=30)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src.endswith(".db"):
        try:
            _copy_sqlite(src, dst)
        except sqlite3.Error:
            shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)
    _make_private(dst, 0o600)


def backup_user_data(root: str | None = None, now: datetime | None = None,
                     keep: int = KEEP_BACKUPS) -> str | None:
    """Copy the database, profile, .env and do-not-contact list into data/backups/<timestamp>/.

    Paths inside the backup mirror the project (data/automations.db, config/profile.toml, .env),
    so restoring is a plain copy back. Returns the folder, or None if there was nothing to save.
    Raises OSError if a copy fails, so the caller can refuse to update without a backup.
    """
    root = _root(root)
    present = [rel for rel in BACKUP_FILES if os.path.isfile(os.path.join(root, rel))]
    if not present:
        return None
    folder = _new_backup_dir(root, now)
    for rel in present:
        _copy_file(os.path.join(root, rel), os.path.join(folder, rel))
    _prune_backups(os.path.dirname(folder), keep, protect=folder)
    return folder


def _save_local_changes(root: str, git: GitRunner, folder: str) -> str:
    diff = git(["diff", "HEAD", "--binary"], root)
    if diff.returncode != 0:
        raise OSError("git diff failed: " + _tail(diff.stderr, 3))
    path = os.path.join(folder, PATCH_NAME)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(diff.stdout)
    _make_private(path, 0o600)
    return path


# ---------------------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------------------

def _lock_path(root: str) -> str:
    """The exact file runner.run_lock() locks. For this install that is <DB_DIR>/.runner.lock,
    which moves with AUTOMATIONS_DB_PATH; any other root (tests) uses <root>/data/.runner.lock."""
    if _same_path(root, ROOT_DIR):
        from core import db  # read at call time: the runner and tests may point it elsewhere

        return os.path.join(db.DB_DIR, os.path.basename(LOCK_RELPATH))
    return os.path.join(root, LOCK_RELPATH)


@contextlib.contextmanager
def _update_lock(root: str, wait_seconds: float) -> Iterator[None]:
    """Hold the runner's lock while code changes, so no pipeline runs half-updated code."""
    try:
        from core.locking import AlreadyLocked, file_lock
    except ImportError:  # a checkout from before core.locking existed: nothing to coordinate with
        yield
        return
    path = _lock_path(root)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(path, wait_seconds=wait_seconds))
        except AlreadyLocked as e:
            raise UpdateBusy("A pipeline run is in progress, so the update was postponed.") from e
        yield


def _result(result: str, message: str, **extra) -> dict:
    return {"result": result, "message": message, "backup_dir": None, "output": "", **extra}


def _modified_files(root: str, git: GitRunner) -> list[str] | None:
    """Tracked files edited locally (untracked and git-ignored files don't count), None on error."""
    result = git(["status", "--porcelain", "--untracked-files=no"], root)
    if result.returncode != 0:
        return None
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def _roll_back(root: str, git: GitRunner, pip: StepRunner, old_head: str, reason: str,
               output: str, backup_dir: str | None, note: str) -> dict:
    reset = git(["reset", "--hard", "--quiet", old_head], root)
    if reset.returncode != 0:
        where = backup_dir or os.path.join(root, BACKUP_RELDIR)
        return _result(
            "rollback_failed",
            f"{reason} Rolling back ALSO failed, so this copy may be half-updated. Fix it by running "
            f"`git reset --hard {old_head}` in {root}. Your data backup is in {where}.{note}",
            backup_dir=backup_dir, output=_tail(output + "\n" + _output(reset)))
    reinstall = pip(root)
    if reinstall.returncode != 0:
        note += (" Reinstalling the previous requirements failed too; run "
                 "`pip install -r requirements.txt` in the project's environment.")
    return _result("rolled_back", f"{reason} Rolled back to the version you had; nothing changed.{note}",
                   backup_dir=backup_dir, output=output)


def _apply_locked(root: str, git: GitRunner, pip: StepRunner, tests: StepRunner, tag: str,
                  force: bool, now: datetime | None) -> dict:
    old_head = _rev(root, git, "HEAD")
    target = _rev(root, git, f"{tag}^{{commit}}")
    if not old_head or not target:
        return _result("failed", f"Couldn't find {tag} in this copy, so nothing was changed.")
    # Nothing to do beats every other check: asking for a release we already contain is a no-op.
    if git(["merge-base", "--is-ancestor", target, old_head], root).returncode == 0:
        return _result("none", f"Already on {tag} or newer.")

    dirty = _modified_files(root, git)
    if dirty is None:
        return _result("failed", "Couldn't read this copy's git status, so nothing was changed.")
    if dirty and not force:
        shown = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        return _result("skipped_dirty",
                       f"Update skipped: these project files were edited on this computer: {shown}. "
                       f"Undo or commit those edits, or run `{CLI_NAME} update --force` "
                       "(your edits are saved as a patch in the backup first).")

    ancestry = git(["merge-base", "--is-ancestor", old_head, target], root)
    if ancestry.returncode != 0:
        if ancestry.returncode == 1:
            return _result("skipped_diverged",
                           f"This copy has its own commits that aren't in {tag}, so it can't simply move "
                           "forward. Nothing was changed. Update it by hand with git, or reinstall.")
        return _result("failed", f"Couldn't compare this copy with {tag}: {_tail(ancestry.stderr, 3)}")

    try:
        backup_dir = backup_user_data(root, now=now)
        note = ""
        if dirty:  # only reachable with force
            backup_dir = backup_dir or _new_backup_dir(root, now)
            note = f" Your local edits were saved to {_save_local_changes(root, git, backup_dir)}."
    except OSError as e:
        return _result("failed", f"Couldn't back up your data ({e}), so nothing was changed.")
    return _move_and_verify(root, git, pip, tests, tag, old_head, target, backup_dir, note)


def _move_and_verify(root: str, git: GitRunner, pip: StepRunner, tests: StepRunner, tag: str,
                     old_head: str, target: str, backup_dir: str | None, note: str) -> dict:
    """Fast-forward, reinstall, self-test; any failure resets to `old_head`."""
    merged = git(["merge", "--ff-only", "--quiet", target], root)
    if merged.returncode != 0:
        if _rev(root, git, "HEAD") != old_head:
            return _roll_back(root, git, pip, old_head, f"Moving to {tag} failed.",
                              _output(merged), backup_dir, note)
        return _result("failed", f"Couldn't move to {tag}, so nothing was changed: {_tail(merged.stderr, 3)}",
                       backup_dir=backup_dir, output=_output(merged))

    installed = pip(root)
    if installed.returncode != 0:
        return _roll_back(root, git, pip, old_head, f"Installing the requirements of {tag} failed.",
                          _output(installed), backup_dir, note)
    if pip_was_skipped(installed):
        note += " " + installed.stdout.strip()
    checked = tests(root)
    if checked.returncode != 0:
        return _roll_back(root, git, pip, old_head, f"{tag} didn't pass its self-test.",
                          _output(checked), backup_dir, note)

    saved = f" Your data was backed up to {backup_dir}." if backup_dir else ""
    return _result("updated", f"Updated to {tag}.{saved}{note}", backup_dir=backup_dir)


def _record(root: str, git: GitRunner, mode: str | None, outcome: dict) -> None:
    status = _collect_status(root, git, mode, fetch=False)
    status.update(last_result=outcome["result"], last_attempt_at=_now_iso(), last_target=outcome["target"],
                  last_message=outcome["message"], last_backup=outcome["backup_dir"])
    write_status(status, root)


def apply_update(root: str | None = None, git: GitRunner | None = None, pip: StepRunner | None = None,
                 tests: StepRunner | None = None, force: bool = False, tag: str | None = None,
                 mode: str | None = None, lock_wait_seconds: float = 0,
                 now: datetime | None = None) -> dict:
    """Install the newest release (or `tag`), backing up first and rolling back on any failure.

    Returns {"result", "message", "current", "target", "tag", "backup_dir", "output"} where result
    is one of RESULTS and message is a friendly sentence for the terminal/digest; "output" is the
    tail of the failing step's output. Every attempt is recorded in data/update_status.json.
    `force` only overrides the edited-files check (the edits are saved as a patch first);
    it never allows anything but a fast-forward to a release tag. User data is never deleted.
    """
    root = _root(root)
    git, pip, tests = git or run_git, pip or run_pip, tests or run_tests
    if tag is not None and not _TAG_RE.match(tag):
        raise ValueError(f"Not a release tag: {tag!r} (expected vX.Y.Z)")
    current = read_version(root)
    if tag is None:
        status = check_for_update(root, git, mode=mode)
        if not status["available"]:
            message = status["error"] or f"You're on the latest version (v{current})."
            return _result("none", message, current=current, target=status["latest"], tag=status["latest_tag"])
        tag = status["latest_tag"]

    try:
        with _update_lock(root, lock_wait_seconds):
            outcome = _apply_locked(root, git, pip, tests, tag, force, now)
    except UpdateBusy as e:
        outcome = _result("skipped_busy", f"{e} Run `{CLI_NAME} update` again when it has finished.")
    outcome.update(current=current, target=tag[1:], tag=tag)
    if outcome["result"] != "none":
        _record(root, git, mode, outcome)
    return outcome


# ---------------------------------------------------------------------------------------
# Scheduled runs and the daily digest
# ---------------------------------------------------------------------------------------

def scheduled_run(profile: dict | None, root: str | None = None, git: GitRunner | None = None,
                  pip: StepRunner | None = None, tests: StepRunner | None = None) -> dict:
    """What `sdr update --scheduled` does, per the profile's [updates] mode.

    off: nothing (no network, no status file). notify: check and record. auto: check, and
    install if a release is available (waiting up to 10 min for a running pipeline).
    Returns {"mode", "result", "message", "status"} (+ "update" with apply_update's result).
    """
    mode = update_mode(profile)
    if mode == "off":
        return {"mode": mode, "result": "none", "message": 'Update checks are off ([updates] mode = "off").',
                "status": {}}
    status = check_for_update(root, git, mode=mode)
    if not status["available"]:
        message = status["error"] or "You're on the latest version (v" + status["current"] + ")."
        return {"mode": mode, "result": "none", "message": message, "status": status}
    tag = status["latest_tag"]
    if mode == "notify":
        return {"mode": mode, "result": "none", "status": status,
                "message": f"Update available: {tag} — run `{CLI_NAME} update`"}
    outcome = apply_update(root, git, pip, tests, tag=tag, mode=mode,
                           lock_wait_seconds=SCHEDULED_LOCK_WAIT_SECONDS)
    return {"mode": mode, "result": outcome["result"], "message": outcome["message"],
            "status": read_status(root), "update": outcome}


def digest_lines(profile: dict | None = None, root: str | None = None,
                 now: datetime | None = None) -> list[str]:
    """Lines for the daily digest: a recent update (or why it didn't happen) and any update waiting.

    Attempts are only mentioned within 24 h, so the digest doesn't repeat them every day.
    """
    if update_mode(profile) == "off":
        return []
    status = read_status(root)
    if not status:
        return []
    now = now or datetime.now(timezone.utc)
    lines = []
    attempted = _parse_time(status.get("last_attempt_at"))
    target = status.get("last_target")
    result = status.get("last_result")
    if attempted and target and timedelta(0) <= now - attempted <= DIGEST_RECENT:
        if result == "updated":
            lines.append(f"Updated to v{target}")
        elif result in _DIGEST_PROBLEMS:
            lines.append(f"Update to v{target} didn't go through: {_DIGEST_PROBLEMS[result]}. "
                         f"Run `{CLI_NAME} update` for details.")
    latest = status.get("latest")
    if status.get("available") and latest:
        lines.append(f"Update available: v{latest} — run `{CLI_NAME} update`")
    return lines
