"""`sdr doctor`: checks the whole install and says how to fix what's wrong.

One line per check (OK / problem / heads-up) and exit code 1 when something critical fails:
an old Python, a missing core library, an unreadable profile, or a mailbox login that is set
but refused. Everything else (no dashboard password, schedule off, no update check yet) is
advice, not failure.

Offline by default except the mailbox login (it signs in to your own mail server and sends
nothing). `--online` also tries one test search with your Brave key; `--offline` skips every
network check. Secrets are never printed.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core.product import CLI_NAME
from core.tui import UI

MIN_PYTHON = (3, 11)
CORE_LIBRARIES = (("requests", "requests"), ("bs4", "beautifulsoup4"))
OPTIONAL_LIBRARIES = (("dns", "dnspython", "email checks use a slower fallback"),
                      ("rich", "rich", "the terminal looks plainer"),
                      ("questionary", "questionary", "menus use numbers instead of arrow keys"))


@dataclass
class Check:
    label: str
    status: str          # "ok" | "fail" | "warn" | "info"
    detail: str
    critical: bool = False


def _check_login(**settings) -> dict:
    from core.email_checks import check_login
    return check_login(**settings)


def _schedule_status() -> dict:
    from core.scheduler import schedule_status
    return schedule_status()


def _describe_search(profile: dict | None, env: Mapping[str, str]) -> dict:
    from core.search import describe_search
    return describe_search(profile, env)


def _check_brave_key(api_key: str) -> tuple[bool, str]:
    from core.search import check_brave_key
    return check_brave_key(api_key)


def _read_update_status() -> dict:
    from core.updater import read_status
    return read_status()


def _find_spec(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


@dataclass
class DoctorDeps:
    check_login: Callable[..., dict] = _check_login
    schedule_status: Callable[[], dict] = _schedule_status
    describe_search: Callable[[dict | None, Mapping[str, str]], dict] = _describe_search
    check_brave_key: Callable[[str], tuple] = _check_brave_key
    read_update_status: Callable[[], dict] = _read_update_status
    find_spec: Callable[[str], bool] = _find_spec
    python_version: tuple = field(default_factory=lambda: tuple(sys.version_info[:3]))


# ── the checks ───────────────────────────────────────────────────────────────


def check_python(deps: DoctorDeps) -> list[Check]:
    version = ".".join(str(part) for part in deps.python_version)
    if tuple(deps.python_version[:2]) >= MIN_PYTHON:
        return [Check("Python", "ok", version)]
    return [Check("Python", "fail", f"{version} is too old: install Python 3.11 or newer, then re-run the "
                                    "installer.", critical=True)]


def check_libraries(deps: DoctorDeps) -> list[Check]:
    missing = [package for module, package in CORE_LIBRARIES if not deps.find_spec(module)]
    checks = [Check("Libraries", "fail", f"missing {', '.join(missing)}: run `pip install -r requirements.txt` "
                                         "in the install folder", critical=True) if missing
              else Check("Libraries", "ok", "everything needed is installed")]
    for module, package, effect in OPTIONAL_LIBRARIES:
        if not deps.find_spec(module):
            checks.append(Check("Libraries", "warn", f"{package} isn't installed, so {effect}"))
    return checks


def check_profile(profile_path: str | None) -> tuple[list[Check], dict | None]:
    from core.config import ProfileError, compliance_warnings, load_profile
    try:
        profile = load_profile(profile_path)
    except ProfileError as exc:
        return [Check("Profile", "fail", f"{exc}".splitlines()[0] + f" Run `{CLI_NAME} setup`.", critical=True)], None
    checks = [Check("Profile", "ok", f"{profile['sender']['product_name']}: {profile['targeting']['ideal_client']} "
                                     f"in {len(profile['targeting']['cities'])} cities")]
    for warning in compliance_warnings(profile):
        checks.append(Check("Compliance", "warn", warning))
    try:
        from core.scheduler import schedule_config
        schedule_config(profile)
    except Exception as exc:  # ScheduleError: the message says how to fix it
        checks.append(Check("Schedule settings", "fail", str(exc), critical=True))
    return checks, profile


def check_email(env: Mapping[str, str], deps: DoctorDeps, offline: bool) -> list[Check]:
    address, password = env.get("SMTP_USER", ""), env.get("SMTP_PASS", "")
    if not address or not password:
        return [Check("Email login", "warn", f"not set yet, so nothing is sent (dry-run). Run `{CLI_NAME} setup "
                                             "--section email`.")]
    if offline:
        return [Check("Email login", "info", f"{address} (not checked: --offline)")]
    # Exactly the servers the SDR itself will use: no guessed host, so a pass here means a pass there.
    from core.config import EmailSettingsError, mail_server
    try:
        smtp_host, smtp_port = mail_server("smtp", env)
        imap_host, imap_port = mail_server("imap", env)
    except EmailSettingsError as exc:
        return [Check("Email login", "fail", f"{address}: {exc}", critical=True)]
    result = deps.check_login(address=address, password=password, smtp_host=smtp_host, smtp_port=smtp_port,
                              imap_host=imap_host, imap_port=imap_port)
    if result.get("smtp") and result.get("imap"):
        return [Check("Email login", "ok", f"{address}: sending and inbox work (nothing was sent)")]
    errors = " ".join(result.get("errors") or []) or "the login was refused"
    return [Check("Email login", "fail", f"{address}: {errors} Fix it with `{CLI_NAME} setup --section email`.",
                  critical=True)]


def check_search(profile: dict | None, env: Mapping[str, str], deps: DoctorDeps, online: bool) -> list[Check]:
    try:
        info = deps.describe_search(profile, env)
    except Exception:  # noqa: BLE001 - an older copy without core/search.py
        return [Check("Lead search", "info", "the search settings can't be read in this version")]
    detail = f"{info.get('label', info.get('engine', '?'))} (setting: {info.get('setting', 'auto')})"
    checks = [Check("Lead search", "warn" if info.get("note") else "ok",
                    detail + (f". {info['note']}" if info.get("note") else ""))]
    if info.get("engine") == "brave" and online:
        ok, message = deps.check_brave_key(env.get("BRAVE_API_KEY", ""))
        checks.append(Check("Brave Search key", "ok" if ok else "fail", message))
    elif info.get("engine") == "brave":
        checks.append(Check("Brave Search key", "info", "set (run `sdr doctor --online` to test it)"))
    return checks


def check_schedule(deps: DoctorDeps) -> list[Check]:
    try:
        status = deps.schedule_status()
    except Exception as exc:  # noqa: BLE001
        return [Check("Schedule", "warn", f"couldn't read it: {exc}")]
    if status.get("error"):
        return [Check("Schedule", "warn", status["error"])]
    checks = [Check("Schedule", "ok", "on") if status.get("installed")
              else Check("Schedule", "info", f"off. Turn it on with `{CLI_NAME} schedule on`.")]
    if status.get("legacy"):
        checks.append(Check("Schedule", "warn", f"old hand-added cron lines found; `{CLI_NAME} schedule on` tidies "
                                                "them up"))
    return checks


def check_dashboard(env: Mapping[str, str]) -> list[Check]:
    from core.dashboard_auth import ENV_KEY, is_valid_hash
    stored = env.get(ENV_KEY, "").strip()
    if not stored:
        return [Check("Dashboard", "warn", f"no password: anyone using this computer can open it. Add one with "
                                           f"`{CLI_NAME} setup --section security`.")]
    if not is_valid_hash(stored):
        return [Check("Dashboard", "fail", f"the saved password is damaged, so nobody can sign in. Set a new one "
                                           f"with `{CLI_NAME} dashboard --set-password`.")]
    return [Check("Dashboard", "ok", "password protected")]


def check_updates(profile: dict | None, deps: DoctorDeps) -> list[Check]:
    from core.product import version
    from core.updater import update_mode
    mode = update_mode(profile)
    try:
        status = deps.read_update_status()
    except Exception:  # noqa: BLE001
        status = {}
    current = version()
    if status.get("available") and status.get("latest"):
        return [Check("Updates", "warn", f"v{status['latest']} is available (you have v{current}). Run "
                                         f"`{CLI_NAME} update`.")]
    checked = str(status.get("checked_at") or "")[:10]
    detail = f"v{current}, mode {mode}" + (f", last checked {checked}" if checked else ", not checked yet")
    return [Check("Updates", "ok" if checked else "info", detail)]


# ── running and printing ─────────────────────────────────────────────────────


def collect(profile_path: str | None = None, env: Mapping[str, str] | None = None, *, online: bool = False,
            offline: bool = False, deps: DoctorDeps | None = None) -> list[Check]:
    deps = deps or DoctorDeps()
    if env is None:
        from core.config import load_env_file
        load_env_file()
        env = os.environ
    checks = check_python(deps) + check_libraries(deps)
    profile_checks, profile = check_profile(profile_path)
    checks += profile_checks
    checks += check_email(env, deps, offline)
    checks += check_search(profile, env, deps, online and not offline)
    checks += check_schedule(deps)
    checks += check_dashboard(env)
    checks += check_updates(profile, deps)
    return checks


def show(ui: UI, checks: list[Check]) -> int:
    ui.echo("Health check")
    for check in checks:
        line = f"{check.label}: {check.detail}"
        {"ok": ui.success, "fail": ui.error, "warn": ui.warn}.get(check.status, ui.info)(line)
    failed = [check for check in checks if check.status == "fail" and check.critical]
    if failed:
        ui.echo("")
        ui.error(f"{len(failed)} problem{'' if len(failed) == 1 else 's'} to fix before the SDR can work properly.")
        return 1
    ui.echo("")
    ui.success("Everything important works.")
    return 0


def run_doctor(ui: UI, *, online: bool = False, offline: bool = False, profile_path: str | None = None,
               env: Mapping[str, Any] | None = None, deps: DoctorDeps | None = None) -> int:
    return show(ui, collect(profile_path, env, online=online, offline=offline, deps=deps))
