"""The dashboard's Settings page: read every setting, save one section at a time.

Saving runs the same code as `sdr setup --section <name>` with the form's values as the
answers, so the browser and the terminal can never disagree about what a setting means, which
checks run (the mailbox login is tested before it's saved) or what ends up in the profile.

Secrets only travel one way. The page can set a password or key; it is never sent back, only
whether one is saved.
"""

from __future__ import annotations

import io
import os
from typing import Any

from core import config
from core.product import CLI_NAME, ROOT_DIR
from core.setup_profile import state_from_profile
from core.setup_questions import BY_KEY, SECTION_HELP, SECTION_LABELS, SECTIONS
from core.setup_steps import Deps
from core.setup_toml import read_env_file
from core.tui import UI

# What each section's form shows, in order. Same grouping as the terminal steps.
SECTION_FIELDS = {
    "brand": ("business.website", "business.name", "business.tagline", "business.offer", "business.signup_url",
              "business.postal_address", "business.allow_no_address"),
    "audience": ("audience.ideal_client", "business.pitch", "audience.job_titles", "audience.cities",
                 "audience.search_engine", "audience.brave_api_key_env"),
    "style": ("style.kind", "style.signature_logo_test", "style.brand_color", "style.logo_url",
              "replies.branded_welcome"),
    "email": ("email.address", "email.provider", "email.password_env", "email.sign_off", "email.from_name",
              "email.alias", "email.alert_email", "email.smtp_host", "email.smtp_port", "email.imap_host",
              "email.imap_port"),
    "replies": ("replies.enabled",),
    "schedule": ("schedule.run_times", "schedule.send_days", "schedule.daily_send_limit",
                 "schedule.reply_check_minutes", "audience.leads_per_run", "schedule.install"),
    "security": ("security.dashboard_password_env",),
    "updates": ("updates.mode",),
}
# Secret fields -> (the .env name that tells us one is saved, the label a person understands).
SECRETS = {
    "email.password_env": ("SMTP_PASS", "Mailbox password or app password"),
    "audience.brave_api_key_env": ("BRAVE_API_KEY", "Brave Search API key (only for Brave Search)"),
    "security.dashboard_password_env": ("DASHBOARD_PASSWORD_HASH", "Dashboard password (at least 8 characters)"),
}
SECRET_HELP = {
    "email.password_env": "Gmail and Google Workspace need an App Password: "
                          "https://myaccount.google.com/apppasswords. It stays on this computer.",
    "audience.brave_api_key_env": "Free key from https://brave.com/search/api/. It stays on this computer.",
    "security.dashboard_password_env": "Only a scrambled version (hash) is saved.",
}
ONLY_FOR_OTHER = ("email.smtp_host", "email.smtp_port", "email.imap_host", "email.imap_port")
SECRET_ENV_PREFIX = "SDR_FORM_SECRET_"   # names inside the one-off environment handed to the setup code
MAX_MESSAGE_LINES = 12


def _paths(profile_path: str | None, env_path: str | None) -> tuple[str, str]:
    return profile_path or config.PROFILE_PATH, env_path or config.ENV_PATH


def _load(profile_path: str, env_path: str) -> tuple[dict | None, dict]:
    env = read_env_file(env_path)
    if not os.path.exists(profile_path):
        return None, env
    try:
        return config.load_profile(profile_path), env
    except config.ProfileError:
        return None, env


def _field(key: str, state: dict, env: dict) -> dict:
    question = BY_KEY[key]
    if key in SECRETS:
        env_name, label = SECRETS[key]
        return {"key": key, "label": label, "type": "secret", "help": SECRET_HELP[key],
                "is_set": bool(env.get(env_name)), "required": False, "choices": None}
    value = state.get(key)
    if value in (None, "") and question.type in ("bool", "int", "choice", "time_list", "days"):
        value = question.default
    field = {**question.as_json(), "value": value}
    field.pop("default", None)
    if key in ONLY_FOR_OTHER:
        field["only_for_provider"] = "other"
    if key == "schedule.install":
        field["label"] = "Run automatically on this schedule"
        field["help"] = "When on, it runs by itself and emails real prospects at the times above."
    return field


def checklist(profile: dict | None, env: dict, schedule_on: bool) -> list[dict]:
    """What's left before the SDR can work by itself, as the Settings page shows it."""
    sender = (profile or {}).get("sender", {})
    has_login = bool(env.get("SMTP_USER") and env.get("SMTP_PASS") and env.get("SMTP_HOST"))
    return [
        {"section": "brand", "label": "Tell us about your business", "done": profile is not None},
        {"section": "email", "label": "Connect the mailbox your emails go out from", "done": has_login,
         "why": "Until then nothing is sent."},
        {"section": "email", "label": "Add your first name, so emails are signed by a person",
         "done": bool(sender.get("sign_off")) and sender.get("sign_off") != sender.get("product_name")},
        {"section": "security", "label": "Protect this dashboard with a password",
         "done": bool(env.get("DASHBOARD_PASSWORD_HASH")), "why": "Otherwise anyone using this computer can open it."},
        {"section": "schedule", "label": "Turn on the schedule, so it runs by itself", "done": schedule_on,
         "why": "Do this last, after you've looked at a sample email."},
    ]


def read_settings(profile_path: str | None = None, env_path: str | None = None, deps: Deps | None = None) -> dict:
    """Everything the Settings page shows. Never includes a password, key or hash."""
    profile_path, env_path = _paths(profile_path, env_path)
    profile, env = _load(profile_path, env_path)
    try:
        schedule_on = bool((deps or Deps()).schedule_status().get("installed"))
    except Exception:  # noqa: BLE001 - no cron / Task Scheduler here: shown as off
        schedule_on = False
    state = state_from_profile(profile, env) if profile else {}
    state["schedule.install"] = schedule_on
    sections = [{"name": name, "label": SECTION_LABELS[name], "help": SECTION_HELP[name],
                 "fields": [_field(key, state, env) for key in SECTION_FIELDS[name]]}
                for name in SECTIONS]
    todo = checklist(profile, env, schedule_on)
    return {"configured": profile is not None, "sections": sections, "checklist": todo,
            "setup_complete": all(item["done"] for item in todo),
            "reopen_command": f"{CLI_NAME} dashboard"}


def _clean(section: str, values: dict) -> tuple[dict, dict]:
    """(answers, one-off environment) from the form. Unknown keys and empty secrets are dropped:
    an empty password box means "keep the one I have"."""
    answers: dict[str, Any] = {}
    environ: dict[str, str] = {}
    for key in SECTION_FIELDS[section]:
        if key not in values:
            continue
        value = values[key]
        if key in SECRETS:
            if isinstance(value, str) and value:
                name = SECRET_ENV_PREFIX + key.replace(".", "_").upper()
                answers[key], environ[name] = name, value
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        answers[key] = value.strip() if isinstance(value, str) else value
    return answers, environ


def _messages(output: str, secrets: list[str]) -> list[str]:
    """The setup code's own words, without terminal-only lines, and never echoing a secret."""
    lines = []
    for line in output.splitlines():
        line = line.strip()
        if any(secret in line for secret in secrets):
            continue
        if line.startswith("- ") and line.endswith(")") and " (" in line:
            # "- email.address: Email address you'll send from (reason)" -> "Email address...: reason"
            label, reason = line[2:].rsplit(" (", 1)
            lines.append(f"{label.split(': ', 1)[-1]}: {reason[:-1]}")
            continue
        for prefix in ("OK: ", "Error: ", "Warning: "):
            if line.startswith(prefix):
                text = line[len(prefix):]
                lines.append("Saved." if text.startswith("Saved. The rest of") else text)
    return lines[:MAX_MESSAGE_LINES]


def save_section(section: str, values: dict, profile_path: str | None = None, env_path: str | None = None,
                 backup_dir: str | None = None, deps: Deps | None = None) -> dict:
    """Save one section from the Settings page. Returns {"success", "messages", "password_changed"}."""
    from core.setup_wizard import run_setup   # imported late: the wizard imports half of core

    if section not in SECTION_FIELDS or not isinstance(values, dict):
        return {"success": False, "messages": ["That isn't a settings section."], "password_changed": False}
    profile_path, env_path = _paths(profile_path, env_path)
    deps = deps or Deps()
    answers, environ = _clean(section, values)
    turn_off = section == "schedule" and answers.get("schedule.install") is False
    if turn_off:
        answers.pop("schedule.install")
    if section == "email" and "email.password_env" not in answers and read_env_file(env_path).get("SMTP_PASS"):
        answers["email.keep_password"] = True
    out = io.StringIO()
    ui = UI(answers=answers, plain=True, interactive=False, collect_missing=True, out=out, environ=environ)
    try:
        code = run_setup(section=section, ui=ui, profile_path=profile_path, env_path=env_path,
                         backup_dir=backup_dir or os.path.join(ROOT_DIR, "data", "backups"), deps=deps)
    except Exception as exc:  # noqa: BLE001 - the page gets a sentence, never a stack trace
        return {"success": False, "messages": [f"That couldn't be saved: {exc}"], "password_changed": False}
    messages = _messages(out.getvalue(), list(environ.values()))
    if code == 0 and turn_off:
        messages += _turn_schedule_off()
    return {"success": code == 0, "messages": messages or (["Saved."] if code == 0 else ["That couldn't be saved."]),
            "password_changed": code == 0 and section == "security" and bool(environ)}


def _turn_schedule_off() -> list[str]:
    from core.scheduler import ScheduleError, remove_schedule
    try:
        remove_schedule()
    except ScheduleError as exc:
        return [str(exc)]
    return ["The schedule is off. Nothing runs by itself until you turn it on again."]
