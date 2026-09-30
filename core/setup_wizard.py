"""`sdr setup`: a short, friendly setup in the terminal, or from an answers file for AI agents.

    sdr setup                                  seven short steps, every question has a default
    sdr setup --section schedule               re-run one step; everything else is left alone
    sdr setup --answers setup-answers.toml     no questions: read the answers (AI agents, scripts)
    sdr setup --print-questions                every question as JSON, with its answers-file key

It writes config/profile.toml (targeting, email wording, schedule) and .env (the mailbox login,
the dashboard password hash, an optional Brave Search key; owner-only permissions). Passwords
never go in the profile or the answers file: in answers mode they come from environment
variables named by `*_env` keys.

The steps live in core/setup_steps.py, the questions in core/setup_questions.py, the profile
templates in core/setup_profile.py and the file writers in core/setup_toml.py; this module runs
them in order, saves, and offers the first actions (a sample email to yourself, the dashboard,
your first leads).
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime

from core import config
from core.product import CLI_NAME, ROOT_DIR
from core.setup_profile import (DEFAULT_DAILY_LIMIT, DEFAULT_LEADS_PER_RUN, DEFAULT_REPLY_CHECK,
                                build_profile_from_answers, build_profile_toml, pitch_sentence, plural,
                                section_updates, state_from_profile)
from core.setup_questions import (SEARCH_CHOICES, SECTION_HELP, SECTION_LABELS, SECTIONS, STEP_TITLES,
                                  UPDATE_CHOICES, known_answer_keys, normalize_section, questions_json)
from core.setup_email import step_email
from core.setup_steps import (Deps, SetupContext, schedule_sentence, step_audience, step_business, step_replies,
                              step_schedule, step_security, step_style, step_updates)
from core.setup_toml import (read_env_file, toml_list, toml_multiline, toml_str, update_env_file,
                             update_profile_text, write_text_atomic)
from core.tui import UI, MissingAnswer, MissingAnswers, load_answers, unknown_answers

__all__ = ["run_setup", "Deps", "build_profile_toml", "build_profile_from_answers", "update_env_file",
           "toml_str", "toml_multiline", "toml_list", "SECTIONS"]

FULL_STEPS = (
    ("Tell us about your business. We'll read the rest from your website.", (step_business,)),
    ("Who should we find for you?", (step_audience,)),
    ("How your emails look.", (step_style,)),
    ("The mailbox your emails go out from. Your password stays on this computer.", (step_email,)),
    ("What happens when someone writes back.", (step_replies,)),
    ("When it runs. You can change this any time.", (step_schedule,)),
    ("Last step.", (step_security, step_updates)),
)
HANDOFF_AFTER_STEP = 2         # business + ideal clients: the questions only the owner can answer
SETTINGS_PAGE = "#settings"
SECTION_STEPS = {
    "brand": (step_business,), "audience": (step_audience,), "style": (step_style,), "email": (step_email,),
    "replies": (step_replies,), "schedule": (step_schedule,), "security": (step_security,),
    "updates": (step_updates,),
}
LOGIN_LABELS = {
    "works": "checked: sending and inbox work",
    "sending works": "sending works; the inbox login needs fixing",
    "not checked": "saved, not checked",
    "failed": "not working",
    "skipped": "not saved yet (nothing is sent until it is)",
    "not set": "not set",
}


@dataclass(frozen=True)
class _Files:
    profile: str
    env: str
    backups: str


# ── entry point ──────────────────────────────────────────────────────────────


def run_setup(answers_path: str | None = None, section: str | None = None, ui: UI | None = None, *,
              print_questions: bool = False, profile_path: str | None = None, env_path: str | None = None,
              backup_dir: str | None = None, deps: Deps | None = None, new_password: bool = False) -> int:
    """Run setup; returns the exit code (0 ok, 1 not saved / error, 2 missing or bad answers,
    130 cancelled). Paths and `deps` default to the real ones; tests pass their own.
    `new_password` (with section="security") skips "keep your current password?"."""
    if print_questions:
        print(questions_json())
        return 0
    answers: dict = {}
    if answers_path:
        try:
            answers = load_answers(answers_path)
        except ValueError as exc:
            (ui or UI()).error(str(exc))
            return 2
        unknown = unknown_answers(answers, known_answer_keys())
        if unknown:
            _report_unknown(ui or UI(interactive=False), answers_path, unknown)
            return 2
    # `--answers` never prompts, even on a TTY: an agent's shell and the user's own terminal are
    # TTYs too, and a question there hangs the tool or surprises the person (see AGENTS.md).
    if ui is None:
        ui = UI(answers=answers, collect_missing=bool(answers_path), interactive=False if answers_path else None)
    elif answers_path:
        ui.answers.update({key: value for key, value in answers.items() if value is not None})
        ui.collect_missing = True
        ui.interactive = False
    wanted = normalize_section(section) if section else None
    if section and wanted is None:
        ui.error(f"There's no setup section called {section!r}. Choose one of: {', '.join(SECTIONS)}.")
        return 2
    if not ui.interactive and not ui.answers:
        ui.error("There's no terminal here to ask the setup questions on.")
        ui.echo(f"Write the answers to a file and run `{CLI_NAME} setup --answers setup-answers.toml` "
                f"(AGENTS.md explains how; `{CLI_NAME} setup --print-questions` lists every question).")
        return 2

    files = _Files(profile_path or config.PROFILE_PATH, env_path or config.ENV_PATH,
                   backup_dir or os.path.join(ROOT_DIR, "data", "backups"))
    try:
        text, profile = _read_profile(files.profile)
    except ValueError as exc:
        ui.error(str(exc))
        return 1
    saved_env = read_env_file(files.env)
    # Answers mode: nobody to confirm with, so nothing optional (sample email, dashboard, lead
    # search) happens unless the answers ask for it.
    answers_mode = bool(answers_path or ui.answers or not ui.interactive)
    ctx = SetupContext(ui=ui, deps=deps or Deps(), saved=state_from_profile(profile, saved_env),
                       saved_env=saved_env, profile=profile, answers_mode=answers_mode, new_password=new_password)
    saved = {"done": False}
    try:
        if wanted:
            return _run_section(ctx, wanted, text, files, saved)
        return _run_full(ctx, text, files, saved)
    except (MissingAnswers, MissingAnswer) as exc:
        errors = exc.errors if isinstance(exc, MissingAnswers) else [exc]
        if ui.interactive and not answers_mode:
            _report_stopped(ui, errors, wanted)   # a person at the keyboard: no "answers file" talk
        else:
            _report_missing(ui, errors)
        return 2
    except KeyboardInterrupt:
        ui.echo("")
        ui.warn("Stopped. Your settings were saved." if saved["done"]
                else f"Setup cancelled. Nothing was saved. Run `{CLI_NAME} setup` to start again.")
        return 130


# ── the two flows ────────────────────────────────────────────────────────────


def _run_full(ctx: SetupContext, text: str | None, files: _Files, saved: dict) -> int:
    ui = ctx.ui
    ui.banner()
    if ctx.profile is not None and ui.interactive and not ctx.answers_mode:
        choice = ui.select("setup.existing", "You're already set up. What would you like to do?", [
            ("section", "Change one part", "Everything else stays exactly as it is"),
            ("all", "Go through the whole setup again", "Your answers are suggested; your email wording is rewritten"),
            ("cancel", "Nothing, leave it as it is", ""),
        ], default="section")
        if choice == "cancel":
            ui.info("Nothing changed.")
            return 0
        if choice == "section":
            return _run_section(ctx, pick_section(ui), text, files, saved)
    ui.info("Takes about 5 minutes. Press Enter to accept a suggestion.")
    for number, (hint, steps) in enumerate(FULL_STEPS, 1):
        ui.step(number, len(FULL_STEPS), STEP_TITLES[number - 1], hint)
        for step in steps:
            step(ctx)
        if number == HANDOFF_AFTER_STEP and _wants_browser(ctx):
            return _finish_in_browser(ctx, text, files, saved)
    ui.raise_missing()
    if ctx.problems:
        for problem in ctx.problems:
            ui.error(problem)
        ui.warn("Nothing was saved.")
        return 1

    state = {**ctx.saved, **ctx.a}
    profile_text = build_profile_from_answers(state)
    try:
        profile = _checked(profile_text)
    except ValueError as exc:
        ui.error(str(exc))
        return 1
    _summary(ctx, state)
    if not ctx.answers_mode and not ui.confirm("finish.save", "Save these settings?", default=True):
        ui.warn(f"Nothing was saved. Run `{CLI_NAME} setup` again whenever you're ready.")
        return 1
    if ctx.profile is not None and ctx.answers_mode:
        ui.info("Replacing your existing profile (the old one is backed up first).")
    backup = _backup(files, text)
    _save(ctx, files, profile_text)
    saved["done"] = True
    if backup:
        ui.info(f"Your previous profile is backed up at {backup}")
    _apply_schedule(ctx, profile)
    _finish(ctx, profile)
    return 0


def _wants_browser(ctx: SetupContext) -> bool:
    """After the questions only the owner can answer, offer the rest as forms in the browser.
    Only asked of a person at a keyboard: answers files and agents carry on as before."""
    ui = ctx.ui
    if ctx.answers_mode or not ui.interactive:
        return False
    choice = ui.select("setup.continue_in", "The rest is settings. Where would you like to finish?", [
        ("browser", "In my browser (easiest)", "Opens your dashboard: forms and buttons, no more typing here"),
        ("terminal", "Here in the terminal", f"{len(FULL_STEPS) - HANDOFF_AFTER_STEP} more short steps"),
    ], default="browser")
    return choice == "browser"


def _finish_in_browser(ctx: SetupContext, text: str | None, files: _Files, saved: dict) -> int:
    """Save what we know with safe defaults (no mailbox login, so nothing can be sent; schedule
    off) and open the dashboard's Settings page, whose checklist covers what's left."""
    ui = ctx.ui
    ui.raise_missing()
    state = {**ctx.saved, **ctx.a}
    business = state.get("business.name") or "Your business"
    for key in ("email.sign_off", "email.from_name"):   # placeholders until the Settings page asks
        state.setdefault(key, business)
    profile_text = build_profile_from_answers(state)
    try:
        _checked(profile_text)
    except ValueError as exc:
        ui.error(str(exc))
        return 1
    backup = _backup(files, text)
    _save(ctx, files, profile_text)
    saved["done"] = True
    if backup:
        ui.info(f"Your previous profile is backed up at {backup}")
    ui.success("Saved. Your dashboard is opening in your browser.")
    ui.info("Finish on the Settings page: connect your mailbox, set a password, then turn on the schedule.")
    ui.info("Nothing is sent until you connect your mailbox.")
    ui.info(f"Keep this window open while you use the dashboard. Closed it by mistake? Type `{CLI_NAME} dashboard` "
            f"to open it again.")
    try:
        ctx.deps.start_dashboard(background=False, page=SETTINGS_PAGE)
    except Exception as exc:  # noqa: BLE001
        ui.warn(f"The dashboard couldn't start ({exc}). Finish here instead with `{CLI_NAME} setup`, "
                f"or try `{CLI_NAME} dashboard`.")
    return 0


def _run_section(ctx: SetupContext, section: str, text: str | None, files: _Files, saved: dict) -> int:
    ui = ctx.ui
    if ctx.profile is None or text is None:
        ui.error(f"There's no profile yet, so there's nothing to change. Run `{CLI_NAME} setup` first.")
        return 1
    ctx.section = section
    ui.step(SECTIONS[section], len(STEP_TITLES), SECTION_LABELS[section], "Press Enter to keep what's there now.")
    for step in SECTION_STEPS[section]:
        step(ctx)
    ui.raise_missing()
    if ctx.problems:
        for problem in ctx.problems:
            ui.error(problem)
        ui.warn("Nothing was changed.")
        return 1

    state = {**ctx.saved, **ctx.a}
    updates = _with_copy_refresh(ctx, section, state, section_updates(section, state))
    try:
        new_text = update_profile_text(text, updates) if any(updates.values()) else text
        profile = _checked(new_text)
    except ValueError as exc:
        ui.error(str(exc))
        return 1
    if new_text != text:
        backup = _backup(files, text)
        write_text_atomic(files.profile, new_text)
        if backup:
            ui.info(f"Your previous profile is backed up at {backup}")
    _write_env(ctx, files)
    saved["done"] = True
    ui.success(f"Saved. The rest of {_relative(files.profile)} is unchanged."
               if new_text != text or ctx.env else "Nothing to change.")
    if section == "schedule":
        _reapply_schedule(ctx, profile)
    elif section == "security" and "DASHBOARD_PASSWORD_HASH" in ctx.env:
        ui.info(f"If your dashboard is open, restart it (`{CLI_NAME} dashboard`) to use the new password.")
    elif section in ("brand", "audience", "style", "email", "replies"):
        ui.info(f"See how your emails look now with `{CLI_NAME} preview`.")
    return 0


def pick_section(ui: UI) -> str:
    """Menu of the setup sections (also used by the `sdr` home menu)."""
    choices = [(name, SECTION_LABELS[name], SECTION_HELP[name]) for name in SECTIONS]
    return ui.select("setup.section", "Which part would you like to change?", choices, default="brand")


# ── saving ───────────────────────────────────────────────────────────────────


def _read_profile(path: str) -> tuple[str | None, dict | None]:
    if not os.path.exists(path):
        return None, None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} has a formatting error ({exc}). Fix it by hand, or move it away and run "
                         f"`{CLI_NAME} setup` again.") from None


def _checked(text: str) -> dict:
    """Parse the profile we're about to write and make sure the SDR will accept it."""
    profile = tomllib.loads(text)
    missing = [f"[{table}] {key}" for table, keys in config.REQUIRED_FIELDS.items() for key in keys
               if not (profile.get(table) or {}).get(key)]
    if missing:
        raise ValueError("The profile would be missing: " + ", ".join(missing) + ". Nothing was saved.")
    return profile


def _relative(path: str) -> str:
    try:
        rel = os.path.relpath(path, ROOT_DIR)
    except ValueError:  # another drive on Windows
        return path
    return path if rel.startswith("..") else rel


def _backup(files: _Files, text: str | None) -> str | None:
    """Copy the profile we're about to replace into data/backups/ (git-ignored, private)."""
    if text is None or not os.path.exists(files.profile):
        return None
    os.makedirs(files.backups, exist_ok=True)
    target = os.path.join(files.backups, f"profile-{datetime.now().strftime('%Y%m%d-%H%M%S')}.toml")
    shutil.copy2(files.profile, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return _relative(target)


def _write_env(ctx: SetupContext, files: _Files) -> None:
    """Write secrets to .env (owner-only) and use them in this process right away."""
    if not ctx.env:
        return
    update_env_file(ctx.env, files.env)
    os.environ.update({key: str(value) for key, value in ctx.env.items()})


def _save(ctx: SetupContext, files: _Files, profile_text: str) -> None:
    write_text_atomic(files.profile, profile_text)
    _write_env(ctx, files)
    note = " and your login to .env (private: only your user can read it)" if ctx.env else ""
    ctx.ui.success(f"Saved your profile to {_relative(files.profile)}{note}.")


def _with_copy_refresh(ctx: SetupContext, section: str, state: dict, updates: dict) -> dict:
    """Keep generated copy in step with the answers it came from, without touching custom copy:
    the pitch line of the first email is swapped only if it's still the one setup wrote, and
    the signature logo follows the logo only if it was the same image."""
    profile = ctx.profile or {}
    if section in ("brand", "audience") and ctx.saved.get("business.pitch"):
        old = pitch_sentence(ctx.saved.get("business.name", ""), ctx.saved["business.pitch"],
                             ctx.saved.get("audience.ideal_client", ""))
        new = pitch_sentence(state.get("business.name", ""), state.get("business.pitch", ""),
                             state.get("audience.ideal_client", ""))
        body = (profile.get("outreach") or {}).get("body", "")
        if old != new and old in body:
            updates = {**updates, "outreach": {**updates.get("outreach", {}), "body": body.replace(old, new)}}
    design = profile.get("email_design") or {}
    new_logo = state.get("style.logo_url", "")
    old_signature = design.get("signature_logo_url") or ""
    if section == "brand" and old_signature and old_signature == design.get("logo_url") and new_logo != design.get(
            "logo_url"):
        updates = {**updates, "email_design": {**updates.get("email_design", {}), "signature_logo_url": new_logo}}
    # A signature logo that isn't the header logo was chosen by hand: the style step keeps it.
    new_signature = updates.get("email_design", {}).get("signature_logo_url")
    if section == "style" and new_signature and old_signature and old_signature != design.get("logo_url"):
        updates = {**updates, "email_design": {**updates["email_design"], "signature_logo_url": old_signature}}
    return updates


# ── after saving ─────────────────────────────────────────────────────────────


def _apply_schedule(ctx: SetupContext, profile: dict) -> None:
    ui = ctx.ui
    if not ctx.install_schedule:
        ui.info(f"The schedule is off for now. Turn it on any time with `{CLI_NAME} schedule on`.")
        return
    if not ctx.env.get("SMTP_PASS") and not ctx.saved_env.get("SMTP_PASS"):
        ui.warn("Your email login isn't saved yet, so scheduled runs find leads but send nothing until it is.")
    updating = ctx.schedule_installed
    try:
        with ui.spinner("Updating your schedule" if updating else "Turning the schedule on"):
            lines = ctx.deps.install_schedule(profile)
    except Exception as exc:  # noqa: BLE001 - settings are saved; explain and move on
        ui.warn(f"Couldn't {'update' if updating else 'turn on'} the schedule: {exc}")
        ui.info(f"Try again later with `{CLI_NAME} schedule on`"
                + (" (until then it keeps running with its previous settings)." if updating else "."))
        return
    ui.success("Your schedule is already on and now uses these settings." if updating else "The schedule is on.")
    for line in lines or []:
        ui.info(line)


def _reapply_schedule(ctx: SetupContext, profile: dict) -> None:
    try:
        installed = bool(ctx.deps.schedule_status().get("installed"))
    except Exception:  # noqa: BLE001
        installed = False
    if installed:
        ctx.ui.info("Updating your running schedule with the new times.")
        ctx.install_schedule = ctx.schedule_installed = True
    else:
        ctx.install_schedule = ctx.ui.confirm("schedule.install", "Turn the schedule on now?", default=False,
                                              hint="It then runs by itself and emails real prospects.")
        if not ctx.install_schedule:
            return
    _apply_schedule(ctx, profile)


def _summary(ctx: SetupContext, state: dict) -> None:
    get = state.get
    client, cities = get("audience.ideal_client", ""), get("audience.cities") or []
    engine = dict((value, label) for value, label, _desc in SEARCH_CHOICES).get(get("audience.search_engine", "auto"))
    login = LOGIN_LABELS.get(ctx.login, ctx.login) + (" (code confirmed)" if ctx.verified else "")
    has_password = bool(ctx.env.get("DASHBOARD_PASSWORD_HASH") or ctx.saved_env.get("DASHBOARD_PASSWORD_HASH"))
    schedule = schedule_sentence(get("schedule.run_times") or [], get("schedule.send_days") or [],
                                 get("schedule.daily_send_limit", DEFAULT_DAILY_LIMIT),
                                 get("schedule.reply_check_minutes", DEFAULT_REPLY_CHECK),
                                 get("audience.leads_per_run", DEFAULT_LEADS_PER_RUN))
    rows = [
        ("Business", f"{get('business.name', '')}  {get('business.website', '')}".strip()),
        ("Offer", get("business.offer", "")),
        ("Ideal clients", f"{plural(client)} in {len(cities)} {'city' if len(cities) == 1 else 'cities'}"),
        ("Lead search", engine or get("audience.search_engine", "auto")),
        ("Email style", "Marketing: branded" if get("style.kind") == "marketing" else "Sales: plain and personal"),
        ("Sender", f"{get('email.from_name', '')} <{get('email.alias') or get('email.address', '')}>"),
        ("Email login", login),
        ("Mailing address", get("business.postal_address") or "none (not recommended)"),
        ("Replies", "answered automatically" if get("replies.enabled", True) else "you answer them yourself"),
        ("Schedule", schedule + (" Already on: updating it now." if ctx.schedule_installed else
                                 " Turning it on now." if ctx.install_schedule else " Off for now.")),
        ("Dashboard", "password protected" if has_password else "no password"),
        ("Updates", dict((v, t) for v, t, _d in UPDATE_CHOICES).get(get("updates.mode", "notify"), "")),
    ]
    ctx.ui.summary("Your setup", rows)


def _cheat_sheet(ui: UI) -> None:
    ui.panel("\n".join([
        f"{CLI_NAME}                         home menu: status and the common actions",
        f"{CLI_NAME} preview                 see every email of your sequence (sends nothing)",
        f"{CLI_NAME} test-email              send yourself a sample",
        f"{CLI_NAME} dashboard               open your dashboard",
        f"{CLI_NAME} setup --section <name>  change one part: {', '.join(SECTIONS)}",
        f"{CLI_NAME} update                  install the latest version",
        "",
        "Using an AI agent (Claude Code, Cursor...)? Point it at AGENTS.md.",
    ]), title="You're all set")


def _finish(ctx: SetupContext, profile: dict) -> None:
    """The first actions: a sample to yourself, the dashboard, your first leads. Off by default
    in answers mode ([finish] send_sample / open_dashboard / find_leads turn them on)."""
    ui, suggest = ctx.ui, not ctx.answers_mode
    address = ctx.value("email.address", "")
    if ctx.env.get("SMTP_PASS") and address and ui.confirm(
            "finish.send_sample", "Send a sample email to yourself now?", default=suggest,
            hint=f"Your first email, to {address}. Nobody else gets it."):
        try:
            with ui.spinner("Sending your sample"):
                sent = ctx.deps.send_sample(profile, address)
        except Exception:  # noqa: BLE001
            sent = False
        if sent:
            ui.success(f"Sent to {address}. Have a look (check spam too).")
        else:
            ui.warn(f"The sample wasn't sent. Try `{CLI_NAME} test-email` for details.")
    open_dashboard = ui.confirm("finish.open_dashboard", "Open your dashboard now?", default=suggest)
    if ui.confirm("finish.find_leads", "Find your first leads now?", default=False,
                  hint="Takes a few minutes. Nothing is emailed yet."):
        try:
            with ui.spinner("Finding your first leads"):
                result = ctx.deps.find_leads(profile) or {}
            count = int(result.get("new_leads_count", 0))
            ui.success(f"Found {count} new lead{'' if count == 1 else 's'}. See them in your dashboard or with "
                       f"`{CLI_NAME} report`.")
        except Exception as exc:  # noqa: BLE001
            ui.warn(f"The lead search stopped: {exc}")
    _cheat_sheet(ui)
    if not open_dashboard:
        return
    if ctx.answers_mode:
        # A dashboard started here would die with this process, leaving a dead browser tab (and a
        # detached one would be a server nobody can Ctrl+C). Say how to run a lasting one instead.
        ui.info(f"To open your dashboard, run `{CLI_NAME} dashboard` in your terminal. It keeps running "
                "until you press Ctrl+C.")
        return
    try:
        ctx.deps.start_dashboard(background=False)   # runs in this terminal until Ctrl+C
    except Exception as exc:  # noqa: BLE001
        ui.warn(f"Couldn't open the dashboard ({exc}). Try `{CLI_NAME} dashboard`.")


def _report_stopped(ui: UI, errors: list, section: str | None) -> None:
    """A person at the keyboard hit a dead end (e.g. never gave a mailing address): say where
    and how to come back, in their words rather than an AI agent's."""
    first = errors[0]
    ui.error(f'Setup stopped at "{first.label}": {first.reason}. '
             + ("Nothing was changed." if section else "Nothing was saved."))
    again = f"{CLI_NAME} setup --section {section}" if section else f"{CLI_NAME} setup"
    ui.info(f"Run `{again}` again whenever you're ready.")


def _report_unknown(ui: UI, path: str, unknown: dict) -> None:
    """A misspelled key would silently be ignored (`skip_login_chek = true` still signs in), so
    it stops the run instead, with the key that was probably meant."""
    count = len(unknown)
    ui.error(f"{path} has {count} key{'' if count == 1 else 's'} setup doesn't know:")
    for key, suggestion in unknown.items():
        ui.echo(f"  - {key}" + (f" (did you mean {suggestion}?)" if suggestion else ""))
    ui.echo(f"Fix or remove {'it' if count == 1 else 'them'} and run the command again. "
            f"`{CLI_NAME} setup --print-questions` lists every question and its key.")


def _report_missing(ui: UI, errors: list) -> None:
    count = len(errors)
    ui.error(f"{count} setup answer{' is' if count == 1 else 's are'} missing or invalid:")
    for err in errors:
        ui.echo(f"  - {err.key}: {err.label} ({err.reason})")
    ui.echo(f"Add {'it' if count == 1 else 'them'} to your answers file and run the command again. "
            f"`{CLI_NAME} setup --print-questions` lists every question and its key.")
