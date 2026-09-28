"""Step 4 of `sdr setup`: the mailbox the SDR sends from (and reads replies in).

Order matters for a smooth setup: the address first (so the provider can be guessed from it),
then the provider, the password and a real login check (sends nothing). A failed login offers
to retry, change servers, or skip — skipping saves everything except the password, so the SDR
stays in dry-run instead of failing every send. Then, for a person at the keyboard, a 6-digit
code is emailed through the real sending path and typed back, which proves delivery end to end.
The password only ever goes to `ctx.env` (.env), never to the profile or the answers file.
"""

from __future__ import annotations

import re
from typing import Any

from core.email_checks import (PROVIDERS, EmailCheckError, clean_app_password, code_matches, is_personal_microsoft,
                                provider_settings)
from core.product import CLI_NAME, DISPLAY_NAME
from core.setup_questions import q
from core.setup_steps import MAX_TRIES, SetupContext, _between
from core.tui import valid_email, valid_url

SERVER_KEYS = ("smtp_host", "smtp_port", "imap_host", "imap_port")

GENERIC_MAILBOXES = {"info", "hello", "hi", "hey", "contact", "sales", "admin", "team", "support", "office",
                     "mail", "email", "help", "enquiries", "inquiries", "booking", "bookings", "studio", "me",
                     "service", "marketing", "noreply", "hola", "accounts", "billing", "owner", "founder"}


def _valid_host(value: str) -> bool | str:
    if re.fullmatch(r"[A-Za-z0-9.-]+", value) and valid_url(value):
        return True
    return "Please enter a server name like smtp.yourprovider.com."


def first_name_from(address: str) -> str | None:
    """"jamie.lee@acme.com" -> "Jamie"; None for shared inboxes like hello@ or info@."""
    local = (address or "").split("@")[0]
    part = re.split(r"[._+-]", local)[0]
    if part.isalpha() and 2 <= len(part) <= 20 and part.lower() not in GENERIC_MAILBOXES:
        return part.capitalize()
    return None


def _ask_servers(ctx: SetupContext, address: str, same: bool, again: bool = False) -> dict:
    """Provider (+ server names for "other") -> {"provider", "smtp_host", "smtp_port", ...}."""
    ui, question = ctx.ui, q("email.provider")
    if again:
        for key in ("email.provider", "email.smtp_host", "email.smtp_port", "email.imap_host", "email.imap_port"):
            ui.answers.pop(key, None)
    default = ctx.saved.get("email.provider") if same else None
    if default is None and address and ui.interactive:
        with ui.spinner("Looking up your email provider"):
            try:
                default = ctx.deps.guess_provider(address)
            except Exception:
                default = "other"
    provider = ui.select("email.provider", question.label, question.choices,
                         default=default if default in PROVIDERS else None, hint=question.help)
    ui.info(PROVIDERS[provider]["help"])
    overrides: dict[str, Any] = {}
    if provider == "other":
        env = ctx.saved_env if same else {}
        port_ok = _between(1, 65535)
        overrides = {
            "smtp_host": ui.text("email.smtp_host", q("email.smtp_host").label, default=env.get("SMTP_HOST") or None,
                                 validate=_valid_host),
            "smtp_port": ui.text("email.smtp_port", q("email.smtp_port").label,
                                 default=env.get("SMTP_PORT") or "465", validate=port_ok),
            "imap_host": ui.text("email.imap_host", q("email.imap_host").label, default=env.get("IMAP_HOST") or None,
                                 validate=_valid_host),
            "imap_port": ui.text("email.imap_port", q("email.imap_port").label,
                                 default=env.get("IMAP_PORT") or "993", validate=port_ok),
        }
    ctx.a["email.provider"] = provider
    try:
        settings = provider_settings(provider, **overrides)
    except ValueError:  # a placeholder port while answers are missing: reported at the end
        settings = {**PROVIDERS[provider], "smtp_port": 465, "imap_port": 993}
    return {"provider": provider, **{k: settings[k] for k in SERVER_KEYS}}


def _ask_mailbox_password(ctx: SetupContext, provider: str, same: bool, again: bool) -> str:
    ui = ctx.ui
    saved = ctx.saved_env.get("SMTP_PASS", "") if same else ""
    if again:
        ui.answers.pop("email.password_env", None)
    elif saved and not ui.has_answer("email.password_env"):
        if not ui.interactive or ui.confirm("email.keep_password", "Keep the password you saved before?", default=True):
            return saved
    password = ui.password("email.password", PROVIDERS[provider]["password_label"])
    return clean_app_password(provider, password)


def _after_failed_login(ctx: SetupContext, result: dict, *, kept_saved: bool = False, last: bool = False,
                        address: str = "") -> str:
    """The menu after a failed check. With the password the person saved before (`kept_saved`),
    Enter leaves it alone: a check that couldn't run (offline, a timeout) must not erase a login
    that works, so removing it is a separate, explicit choice. On the last try there's nothing
    left to retry, but "keep it" must still be offered."""
    choices: list[tuple[str, str, str]] = []
    if result.get("smtp") and not result.get("imap"):
        choices.append(("keep", "Keep it: sending works, I'll fix the inbox later",
                        "Replies aren't read until the inbox login works"))
    if kept_saved:
        choices.append(("keep_saved", "Leave the saved password as it is",
                        "It isn't re-checked now; sending keeps working if it did before"))
    if not last:
        choices += [("retry", "Type the password again", ""),
                    ("provider", "Change the provider or server settings", "")]
    choices.append(("skip", "Remove the saved password" if kept_saved else "Skip for now",
                    "Nothing is sent until the login is fixed"))
    if len(choices) == 1:
        return "skip"
    default = "keep_saved" if kept_saved else ("retry" if not last else choices[0][0])
    if default == "retry" and is_personal_microsoft(address):
        default = "provider"  # no password works on Outlook.com/Hotmail, so retrying can't help
    return ctx.ui.select("email.login_failed", "What would you like to do?", choices, default=default)


def _login(ctx: SetupContext, address: str, servers: dict, same: bool) -> str:
    """Ask the password and check the login (retry / change servers / keep / skip). Returns the
    password to save ("" = none: the SDR stays in dry-run until it's set)."""
    ui = ctx.ui
    saved = ctx.saved_env.get("SMTP_PASS", "") if same else ""
    for attempt in range(1, MAX_TRIES + 1):
        last = attempt == MAX_TRIES
        if last and ui.interactive:
            ui.info("Last try.")
        password = _ask_mailbox_password(ctx, servers["provider"], same, again=attempt > 1)
        if not password:
            ctx.login = "not set"
            return ""
        kept_saved = bool(saved) and password == saved
        skip = ui.has_answer("email.skip_login_check") and ui.confirm(
            "email.skip_login_check", q("email.skip_login_check").label, default=False)
        if skip or ui.missing:  # other answers are missing: report those before signing in anywhere
            ctx.login = "not checked"
            return password
        with ui.spinner("Checking your login (nothing is sent)"):
            result = ctx.deps.check_login(address=address, password=password, **{k: servers[k] for k in SERVER_KEYS})
        if result.get("smtp") and result.get("imap"):
            ui.success("Your login works: sending and inbox.")
            ctx.login = "works"
            return password
        for message in result.get("errors") or ["The login didn't work."]:
            ui.error(message)
        if not ui.interactive:
            ctx.problems.append("The email login didn't work (see above). Check the password in the environment "
                                "variable named by email.password_env and the server settings, or set "
                                "email.skip_login_check = true to save it anyway.")
            ctx.login = "failed"
            return password
        choice = _after_failed_login(ctx, result, kept_saved=kept_saved, last=last, address=address)
        if choice == "keep":
            ctx.login = "sending works"
            return password
        if choice == "keep_saved":
            ctx.login = "not checked"
            return password
        if choice == "skip":
            break
        if choice == "provider":
            servers.update(_ask_servers(ctx, address, same, again=True))
    ui.warn("Continuing without the password: nothing is sent until the login is fixed. "
            f"Try again any time with `{CLI_NAME} setup --section email`.")
    ctx.login = "skipped"
    return ""


def _verify_sending(ctx: SetupContext, servers: dict, address: str, password: str) -> None:
    """Email a 6-digit code and have the owner type it back: proves sending works end to end."""
    ui = ctx.ui
    ui.info("Next, we'll email you a 6-digit code to be sure sending really works.")
    try:
        with ui.spinner("Sending the code"):
            code = ctx.deps.send_code({**servers, "address": address, "password": password}, address,
                                      ctx.value("business.name", DISPLAY_NAME))
    except EmailCheckError as exc:
        ui.warn(f"{exc} You can carry on: `{CLI_NAME} test-email` tries again later.")
        return
    except Exception:
        ui.warn(f"Couldn't send the code. You can carry on: `{CLI_NAME} test-email` tries again later.")
        return
    ui.info(f"Sent to {address}. It can take a minute, and check your spam folder too. Press Enter to skip.")
    for _ in range(MAX_TRIES):
        typed = ui.text("email.code", "The 6-digit code from that email", required=False)
        if not typed:
            ui.info("Skipped the code check.")
            return
        if code_matches(code, typed):
            ui.success("Sending works.")
            ctx.verified = True
            return
        ui.warn("That code doesn't match. Use the newest email.")
    ui.warn("Skipping the code check for now.")


def _ask_sender(ctx: SetupContext, address: str) -> None:
    ui = ctx.ui
    sign_q, name_q = q("email.sign_off"), q("email.from_name")
    alias_q, alert_q = q("email.alias"), q("email.alert_email")
    sign_off = ui.text("email.sign_off", sign_q.label, default=ctx.suggest("email.sign_off", first_name_from(address)))
    first = sign_off.split("\n")[0].strip()
    business = ctx.value("business.name", "")
    suggestion = f"{first} at {business}" if first and business else (first or business or None)
    from_name = ui.text("email.from_name", name_q.label, default=ctx.suggest("email.from_name", suggestion),
                        hint=name_q.help)
    alias, alert = ctx.saved.get("email.alias", ""), ctx.saved.get("email.alert_email", "")
    advanced = any(ui.has_answer(key) for key in ("email.alias", "email.alert_email")) or (
        ui.interactive and ui.confirm("email.more_options", "Send from an alias, or send lead alerts to another "
                                      "address?", default=bool(alias or alert)))
    if advanced:
        alias = ui.text("email.alias", alias_q.label, default=alias or None, required=False, validate=valid_email,
                        hint=alias_q.help)
        alert = ui.text("email.alert_email", alert_q.label, default=alert or None, required=False,
                        validate=valid_email, hint=alert_q.help)
    ctx.a.update({"email.sign_off": sign_off, "email.from_name": from_name, "email.alias": alias,
                  "email.alert_email": alert})


def step_email(ctx: SetupContext) -> None:
    ui, question = ctx.ui, q("email.address")
    saved_address = ctx.saved.get("email.address") or ""
    address = ui.text("email.address", question.label, default=ctx.suggest("email.address"), validate=valid_email,
                      hint=question.help).strip()
    ctx.a["email.address"] = address
    same = bool(address) and address.lower() == saved_address.lower()
    servers = _ask_servers(ctx, address, same)
    password = _login(ctx, address, servers, same)
    ctx.env.update({"SMTP_HOST": servers["smtp_host"], "SMTP_PORT": str(servers["smtp_port"]), "SMTP_USER": address,
                    "SMTP_PASS": password, "IMAP_HOST": servers["imap_host"], "IMAP_PORT": str(servers["imap_port"])})
    if ctx.login == "works" and ui.interactive and not ctx.answers_mode:
        _verify_sending(ctx, servers, address, password)
    _ask_sender(ctx, address)
