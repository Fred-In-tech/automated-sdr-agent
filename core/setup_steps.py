"""The seven steps of `sdr setup`, one function each (core/setup_wizard.py runs them).

Each step asks its questions through core.tui.UI — so the same code serves a person at the
keyboard (arrow keys, spinners), a plain terminal, and an AI agent's answers file — and records
the answers in `ctx.a` using the answers-file keys ("business.website", ...). Secrets never go
there: they're collected in `ctx.env` and only ever written to .env.

Anything that touches the outside world (reading the website, the mailbox login, the
verification email, the schedule, the dashboard) goes through `Deps`, so tests run offline.
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable

from core.product import BRAND_COLOR, CLI_NAME, DISPLAY_NAME
from core.setup_profile import (ALL_DAYS, DEFAULT_DAILY_LIMIT, DEFAULT_DESIGN, DEFAULT_LEADS_PER_RUN,
                                DEFAULT_REPLY_CHECK, DEFAULT_RUN_TIMES, DEFAULT_SEND_DAYS, SEARCH_ENGINES,
                                build_profile_from_answers, plural)
from core.setup_questions import DAY_CHOICES, DAY_PRESETS, q
from core.tui import (MAX_ATTEMPTS, UI, InvalidAnswer, normalize_hex_color, normalize_hhmm, valid_hex_color,
                      valid_hhmm, valid_url)

MAX_TRIES = 3            # wrong login / wrong code / refused address, then we move on
BACKGROUND_TINT = 0.07   # how much brand colour goes into the branded email background
BRAVE_SIGNUP_URL = "https://brave.com/search/api/"
NO_ADDRESS = {"none", "no", "skip", "-", "n/a", "na"}


# ── the outside world (replaced in tests) ────────────────────────────────────


@contextlib.contextmanager
def temporary_env(values: dict[str, str]):
    """Set environment variables for the duration of a block, then restore them."""
    old = {key: os.environ.get(key) for key in values}
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _detect_brand(url: str) -> dict:
    from core.brand_detect import detect_brand
    return detect_brand(url)


def _guess_provider(address: str) -> str:
    from core.email_checks import guess_provider
    return guess_provider(address)


def _check_login(**settings) -> dict:
    from core.email_checks import check_login
    return check_login(**settings)


def _send_code(settings: dict, to: str, from_name: str) -> str:
    """Send the verification code through the SDR's real sending path, with the new login."""
    from bots.email_marketing import EmailMarketingEngine
    from core.email_checks import send_verification_code
    login = {"SMTP_HOST": settings["smtp_host"], "SMTP_PORT": str(settings["smtp_port"]),
             "SMTP_USER": settings["address"], "SMTP_PASS": settings["password"]}
    profile = {"sender": {"product_name": DISPLAY_NAME, "from_name": from_name, "sign_off": "",
                          "from_email": settings["address"]}, "outreach": {}}
    with temporary_env(login):
        engine = EmailMarketingEngine(profile)
    return send_verification_code(engine, to)


def _hash_password(password: str) -> str:
    from core.dashboard_auth import hash_password
    return hash_password(password)


def _install_schedule(profile: dict) -> list[str]:
    from core.scheduler import install_schedule
    return install_schedule(profile)


def _schedule_status() -> dict:
    from core.scheduler import schedule_status
    return schedule_status()


def _send_sample(profile: dict, to: str) -> bool:
    from core.setup_sample import send_sample
    return send_sample(profile, to)["sent"]


def _start_dashboard(background: bool = False, page: str = "") -> dict:
    from dashboard.server import start_dashboard
    return start_dashboard(open_browser=True, background=background, page=page)


def _find_leads(profile: dict) -> dict:
    from bots.leadgen_pipeline import LeadGenPipeline
    return LeadGenPipeline(profile).run_pipeline()


@dataclass
class Deps:
    detect_brand: Callable[[str], dict] = _detect_brand
    guess_provider: Callable[[str], str] = _guess_provider
    check_login: Callable[..., dict] = _check_login
    send_code: Callable[[dict, str, str], str] = _send_code
    hash_password: Callable[[str], str] = _hash_password
    install_schedule: Callable[[dict], list] = _install_schedule
    schedule_status: Callable[[], dict] = _schedule_status
    send_sample: Callable[[dict, str], bool] = _send_sample
    start_dashboard: Callable[..., Any] = _start_dashboard
    find_leads: Callable[[dict], dict] = _find_leads


@dataclass
class SetupContext:
    """Everything one setup run knows. `saved` = answers rebuilt from the saved profile and
    .env (defaults); `a` = this run's answers; `env` = .env values to write (secrets live only here)."""

    ui: UI
    deps: Deps = field(default_factory=Deps)
    saved: dict = field(default_factory=dict)
    saved_env: dict = field(default_factory=dict)
    profile: dict | None = None
    answers_mode: bool = False
    section: str | None = None
    a: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    login: str = "not set"   # works | sending works | not checked | failed | skipped | not set
    verified: bool = False
    install_schedule: bool = False
    schedule_installed: bool = False   # a schedule is already running: install_schedule updates it
    new_password: bool = False   # `sdr dashboard --set-password`: don't offer to keep the old one

    def value(self, key: str, fallback: Any = None) -> Any:
        """This run's answer, else the saved one, else `fallback`."""
        for source in (self.a, self.saved):
            if source.get(key) not in (None, "", []):
                return source[key]
        return fallback

    def suggest(self, key: str, suggestion: Any = None) -> Any:
        """Default for a question the answers file must answer: the saved value, or our
        suggestion — but only when a person can be asked (answers files must be explicit)."""
        saved = self.saved.get(key)
        if saved not in (None, "", []):
            return saved
        return suggestion if self.ui.interactive else None


# ── small helpers ────────────────────────────────────────────────────────────


def _https_image(value: str) -> bool | str:
    if valid_url(value, require_scheme=True) and value.lower().startswith("https://"):
        return True
    return "Please enter an https:// link to an image (or leave it blank)."


def _between(low: int, high: int, *, zero_ok: bool = False) -> Callable[[str], bool | str]:
    message = (f"Please enter 0 or a whole number from {low} to {high}." if zero_ok
               else f"Please enter a whole number from {low} to {high}.")

    def check(value: str) -> bool | str:
        text = value.strip()
        if not text.isdigit():
            return message
        number = int(text)
        return True if (zero_ok and number == 0) or low <= number <= high else message
    return check


def tint(hex_color: str, amount: float = BACKGROUND_TINT) -> str:
    """A very light version of a colour (the branded email background)."""
    channels = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(255 - (255 - c) * amount):02X}" for c in channels)


def _swatch(ui: UI, color: str) -> None:
    """A little block of the brand colour (fancy terminals only)."""
    if ui.console is None or not valid_hex_color(color):
        return
    with contextlib.suppress(Exception):
        from rich.text import Text
        ui.console.print(Text.assemble(("  ", ""), ("        ", f"on {color}"), (f"  {color}", "dim")))


def _record_invalid(ui: UI, key: str, label: str, reason: str) -> None:
    """Report a bad answer the way core.tui does: collected in answers mode, raised otherwise."""
    error = InvalidAnswer(key, label, reason)
    if not ui.collect_missing:
        raise error
    if all(existing.key != key for existing in ui.missing):
        ui.missing.append(error)


# ── 1. Your business ─────────────────────────────────────────────────────────


def _saved_brand(ctx: SetupContext) -> dict:
    fields = {"name": ctx.saved.get("business.name", ""), "tagline": ctx.saved.get("business.tagline", ""),
              "logo_url": ctx.saved.get("style.logo_url", ""), "brand_color": ctx.saved.get("style.brand_color", ""),
              "heading_color": ctx.saved.get("style.heading_color", ""),
              "text_color": ctx.saved.get("style.text_color", ""), "background": ctx.saved.get("style.background", "")}
    return {**fields, "found": {key: bool(value) for key, value in fields.items()}, "error": ""}


def _brand_for(ctx: SetupContext, website: str) -> tuple[dict, bool]:
    """(brand, freshly detected?). Re-running the brand step for the same website starts from
    what's saved (it may have been hand-tuned); a new website is read again."""
    if ctx.section and website == ctx.saved.get("business.website") and ctx.saved.get("business.name"):
        return _saved_brand(ctx), False
    with ctx.ui.spinner("Reading your website"):
        try:
            brand = ctx.deps.detect_brand(website) or {}
        except Exception:  # detect_brand never raises, but a replacement might: carry on by hand
            brand = {"error": "Couldn't read your website."}
    brand.setdefault("found", {})
    return brand, True


def _show_brand(ui: UI, brand: dict, detected: bool) -> None:
    color = brand.get("brand_color") or BRAND_COLOR
    rows = [("Name", brand.get("name") or "(not found)"),
            ("Tagline", brand.get("tagline") or "(none found)"),
            ("Logo", "found" if brand.get("logo_url") else "not found"),
            ("Brand colour", color)]
    if not detected:
        title = "Your current brand"
    elif brand.get("error"):
        title = "We couldn't read your website, so here's a starting point"   # never claim these were found
    else:
        title = "Here's what we found on your website"
    ui.summary(title, rows)
    _swatch(ui, color)


def _retype_website(ctx: SetupContext, website: str) -> str:
    """The website couldn't be read: the likeliest cause is a typo, so offer to type it again
    (Enter keeps it). Only for a person at the keyboard whose answer didn't come from a file."""
    ui = ctx.ui
    if not ui.interactive or ui.has_answer("business.website"):
        return website
    from core.brand_detect import normalize_url
    return ui.text("business.website", q("business.website").label, default=website, validate=valid_url,
                   normalize=normalize_url,
                   hint="Check the address and type it again, or press Enter to keep it and type the details yourself.")


def _apply_brand(ctx: SetupContext, brand: dict, edit: bool) -> None:
    ui, found = ctx.ui, brand.get("found") or {}
    name = brand.get("name") or ""
    if edit or not found.get("name") or ui.has_answer("business.name"):
        name = ui.text("business.name", q("business.name").label, default=name or None)
    tagline = brand.get("tagline") or ""
    if edit or ui.has_answer("business.tagline"):
        tagline = ui.text("business.tagline", q("business.tagline").label, default=tagline or None, required=False)
    logo = brand.get("logo_url") or ""
    if edit or ui.has_answer("style.logo_url"):
        logo = ui.text("style.logo_url", q("style.logo_url").label, default=logo or None, required=False,
                       validate=_https_image, hint=q("style.logo_url").help if edit else None)
    detected_color = (brand.get("brand_color") or BRAND_COLOR).upper()
    color = detected_color
    if edit or ui.has_answer("style.brand_color"):
        color = ui.text("style.brand_color", q("style.brand_color").label, default=detected_color,
                        normalize=normalize_hex_color, validate=valid_hex_color).upper()
    background = brand.get("background") or DEFAULT_DESIGN["background"]
    if valid_hex_color(color) and color != detected_color:
        background = tint(color)
    ctx.a.update({
        "business.name": name, "business.tagline": tagline, "style.logo_url": logo, "style.brand_color": color,
        "style.heading_color": brand.get("heading_color") or DEFAULT_DESIGN["heading_color"],
        "style.text_color": brand.get("text_color") or DEFAULT_DESIGN["text_color"],
        "style.background": background,
    })


def _ask_postal_address(ctx: SetupContext) -> None:
    ui, question, allow_q = ctx.ui, q("business.postal_address"), q("business.allow_no_address")
    saved_address = ctx.saved.get("business.postal_address") or ""
    saved_allow = bool(ctx.saved.get("business.allow_no_address"))
    if not ui.interactive:
        allow = ui.confirm("business.allow_no_address", allow_q.label, default=saved_allow)
        address = ui.text("business.postal_address", question.label,
                          default=saved_address or ("" if allow else None), required=not allow)
        ctx.a.update({"business.postal_address": address, "business.allow_no_address": allow and not address})
        return
    hint = question.help + ' No address yet? Type "none".'
    # Enter on "Send without an address? [y/N]" means no, so keep asking until the person gives
    # one or opts out; the cap only stops a script feeding the same line forever.
    for _ in range(MAX_ATTEMPTS):
        address = ui.text("business.postal_address", question.label, default=saved_address or None, hint=hint)
        if address.strip().lower() in NO_ADDRESS:
            address = ""
        if address:
            ctx.a.update({"business.postal_address": address, "business.allow_no_address": False})
            return
        ui.warn("Cold email without a real mailing address breaks the law in the US (CAN-SPAM), the UK and "
                "the EU, and spam filters notice too.")
        if ui.confirm("business.allow_no_address", "Send without an address for now? (not recommended)",
                      default=saved_allow):
            ctx.a.update({"business.postal_address": "", "business.allow_no_address": True})
            ui.info(f"OK. Add it any time with `{CLI_NAME} setup --section brand`.")
            return
        ui.answers.pop("business.allow_no_address", None)  # a "no" from the file: ask the person
        ui.info("OK, let's add one. A PO box or your registered office address works too.")
    raise InvalidAnswer("business.postal_address", question.label, "no mailing address given")


def step_business(ctx: SetupContext) -> None:
    ui, question = ctx.ui, q("business.website")
    from core.brand_detect import normalize_url
    website = ui.text("business.website", question.label, default=ctx.saved.get("business.website"),
                      validate=valid_url, normalize=normalize_url, hint=question.help)
    ctx.a["business.website"] = website
    if website:
        brand, detected = _brand_for(ctx, website)
        if brand.get("error") and detected:
            ui.warn(brand["error"])
            retyped = _retype_website(ctx, website)
            if retyped != website:
                ctx.a["business.website"] = website = retyped
                brand, detected = _brand_for(ctx, website)
                if brand.get("error"):
                    ui.warn(brand["error"])
        if brand.get("error"):
            ui.info("You can type the details instead.")
        _show_brand(ui, brand, detected)
        keep = ui.confirm("business.use_detected", "Use these?" if detected else "Keep these?", default=True)
        _apply_brand(ctx, brand, edit=not keep)
    offer_q, signup_q = q("business.offer"), q("business.signup_url")
    # No placeholder offer on a first run: "free 14-day trial" is a SaaS phrase that would land
    # verbatim in a dentist's or photographer's cold emails. The hint gives examples instead.
    ctx.a["business.offer"] = ui.text("business.offer", offer_q.label, default=ctx.suggest("business.offer"),
                                      hint=offer_q.help)
    saved_signup = ctx.saved.get("business.signup_url")
    signup_default = saved_signup if saved_signup and saved_signup != ctx.saved.get("business.website") else website
    signup = ui.text("business.signup_url", signup_q.label, default=signup_default or None, required=False,
                     validate=valid_url, normalize=normalize_url, hint=signup_q.help)
    ctx.a["business.signup_url"] = signup or website
    _ask_postal_address(ctx)


# ── 2. Your ideal clients ────────────────────────────────────────────────────


def _ask_brave_key(ctx: SetupContext, has_key: bool) -> bool:
    """True when a Brave key is available after this (saved or just typed)."""
    ui = ctx.ui
    if has_key and not ui.has_answer("audience.brave_api_key_env"):
        if not ui.interactive or ui.confirm("audience.keep_brave_key", "Keep the Brave Search key you saved before?",
                                            default=True):
            return True
    if ui.interactive:
        ui.info(f"Get a free key at {BRAVE_SIGNUP_URL} (the free plan is plenty for this).")
    key = ui.password("audience.brave_api_key", q("audience.brave_api_key_env").label, required=not ui.interactive,
                      hint="Press Enter to skip for now: DuckDuckGo is used until you add a key.")
    if key:
        ctx.env["BRAVE_API_KEY"] = key
        return True
    if ui.interactive:
        ui.info(f"No key for now, so we'll use DuckDuckGo. Add one later with `{CLI_NAME} setup --section audience`.")
    return False


def _ask_search_engine(ctx: SetupContext) -> None:
    ui, question = ctx.ui, q("audience.search_engine")
    has_key = bool(ctx.saved_env.get("BRAVE_API_KEY") or ui.environ.get("BRAVE_API_KEY"))
    saved = ctx.saved.get("audience.search_engine")
    default = saved if saved in SEARCH_ENGINES else ("brave" if has_key else "auto")
    engine = ui.select("audience.search_engine", question.label, question.choices, default=default)
    if engine == "bing":
        ui.warn("Bing's rules (its robots.txt) don't allow bots to read its search results, and it may block you.")
        if (ui.interactive and not ui.has_answer("audience.search_engine")
                and not ui.confirm("audience.accept_bing", "Use Bing anyway?", default=False)):
            engine = "auto"
    if engine == "brave" and not _ask_brave_key(ctx, has_key):
        engine = "auto"
    ctx.a["audience.search_engine"] = engine


def _show_searches(ui: UI, titles: list[str], cities: list[str]) -> None:
    examples = [f'"{title}" "{city.split(",")[0].strip()}" contact' for city in cities[:2] for title in titles[:1]]
    if examples:
        ui.info("We'll search the web like this: " + "   ".join(examples[:2]))


def _ask_cities(ctx: SetupContext, cities_q: Any) -> list[str]:
    """Cities, shown back as parsed. People type "Austin, Dallas" or "Richmond, Surrey" on one
    line, and no rule reads every such line right, so a person always gets to see and fix it."""
    ui = ctx.ui
    hint = cities_q.help
    for _attempt in range(MAX_TRIES):
        cities = ui.list("audience.cities", cities_q.label, '"Austin, TX"', default=ctx.saved.get("audience.cities"),
                         split_commas=False, hint=hint)
        if not (ui.interactive and cities and not ui.has_answer("audience.cities")):
            return cities
        count = f"{len(cities)} {'city' if len(cities) == 1 else 'cities'}"
        ui.info(f"Searching in {count}: " + "; ".join(cities))
        if ui.confirm("audience.cities_ok", "Is that right?", default=True):
            return cities
        hint = 'Put a ";" between cities, e.g. "Richmond, Surrey; Leeds, UK".'
    return cities


def step_audience(ctx: SetupContext) -> None:
    ui = ctx.ui
    client_q, pitch_q, titles_q, cities_q = (q("audience.ideal_client"), q("business.pitch"),
                                             q("audience.job_titles"), q("audience.cities"))
    client = ui.text("audience.ideal_client", client_q.label, default=ctx.suggest("audience.ideal_client"),
                     hint=client_q.help)
    name = ctx.value("business.name", "Your business")
    pitch = ui.text("business.pitch", f'Finish the sentence: "{name} helps {plural(client) or "your clients"} ..."',
                    default=ctx.suggest("business.pitch"), hint=pitch_q.help)
    titles = ui.list("audience.job_titles", titles_q.label, f'"{client or "wedding photographer"}"',
                     default=ctx.saved.get("audience.job_titles") or ([client] if client else None))
    cities = _ask_cities(ctx, cities_q)
    ctx.a.update({"audience.ideal_client": client, "business.pitch": pitch, "audience.job_titles": titles,
                  "audience.cities": cities})
    if ui.interactive and titles and cities:
        _show_searches(ui, titles, cities)
    _ask_search_engine(ctx)


# ── 3. Email style ───────────────────────────────────────────────────────────


def step_style(ctx: SetupContext) -> None:
    ui, kind_q = ctx.ui, q("style.kind")
    kind = ui.select("style.kind", kind_q.label, kind_q.choices, default=ctx.suggest("style.kind", "sales"))
    ctx.a["style.kind"] = kind
    logo_q, welcome_q = q("style.signature_logo_test"), q("replies.branded_welcome")
    current = ctx.value("style.signature_logo_test", True)
    if (kind == "sales" and ctx.value("style.logo_url")) or ui.has_answer("style.signature_logo_test"):
        current = ui.confirm("style.signature_logo_test", logo_q.label, default=current, hint=logo_q.help)
    ctx.a["style.signature_logo_test"] = current
    ctx.a["replies.branded_welcome"] = ui.confirm("replies.branded_welcome", welcome_q.label,
                                                  default=ctx.value("replies.branded_welcome", True),
                                                  hint=welcome_q.help)
    if ctx.section == "style" and kind != ctx.saved.get("style.kind"):
        ui.info("Your email wording stays as it is. Edit it any time in config/profile.toml.")


# ── 5. Replies ───────────────────────────────────────────────────────────────


def _shorten(text: str, lines: int = 12) -> str:
    rows = text.splitlines()
    return "\n".join(rows[:lines] + (["..."] if len(rows) > lines else []))


def _preview_replies(ctx: SetupContext) -> None:
    ui = ctx.ui
    try:
        from bots.inbox_listener import build_reply
        profile = ctx.profile if ctx.section and ctx.profile else tomllib.loads(
            build_profile_from_answers({**ctx.saved, **ctx.a}))
        lead = {"first_name": "Alex", "company": "Rivera Studio"}
        for intent, title in (("interested", 'When a lead says "sounds good, send me the link"'),
                              ("question", "When a lead asks a question"),
                              ("not_now", 'When a lead says "not right now"')):
            reply = build_reply(profile, intent, lead, "")
            if reply:
                ui.panel(_shorten(reply[0]), title=title)
    except Exception:  # a preview must never stop the setup
        return
    ui.info("Change the wording any time in config/profile.toml ([auto_reply.replies]).")


def step_replies(ctx: SetupContext) -> None:
    ui, question = ctx.ui, q("replies.enabled")
    enabled = ui.confirm("replies.enabled", question.label, default=ctx.value("replies.enabled", True),
                         hint=question.help)
    ctx.a["replies.enabled"] = enabled
    if enabled and ui.interactive and not ctx.answers_mode:
        _preview_replies(ctx)


# ── 6. Schedule ──────────────────────────────────────────────────────────────


SCHEDULE_KEYS = ("schedule.run_times", "schedule.send_days", "schedule.daily_send_limit",
                 "schedule.reply_check_minutes", "audience.leads_per_run")


def days_label(days: list[str]) -> str:
    if list(days) == DEFAULT_SEND_DAYS:
        return "weekdays"
    if list(days) == ALL_DAYS:
        return "every day"
    return ", ".join(days) or "no days"


def schedule_sentence(times: list[str], days: list[str], limit: int, minutes: int, leads: int) -> str:
    when = " and ".join(times) if times else "no set times"
    replies = f"checks replies every {minutes} min" if minutes else "checks replies at each run"
    return (f"Runs at {when} on {days_label(days)}, sends up to {limit} emails a day, {replies}, "
            f"finds {leads} new leads a run.")


def _ask_days(ctx: SetupContext, current: list[str]) -> list[str]:
    ui, question = ctx.ui, q("schedule.send_days")
    if ui.has_answer("schedule.send_days") or not ui.interactive:
        return ui.checkbox("schedule.send_days", question.label, DAY_CHOICES, default=current)
    preset = ui.select("schedule.days", question.label, DAY_PRESETS,
                       default="weekdays" if current == DEFAULT_SEND_DAYS else
                       "everyday" if current == ALL_DAYS else "custom")
    if preset == "weekdays":
        return list(DEFAULT_SEND_DAYS)
    if preset == "everyday":
        return list(ALL_DAYS)
    return ui.checkbox("schedule.send_days", "Pick the days", DAY_CHOICES, default=current)


def step_schedule(ctx: SetupContext) -> None:
    ui = ctx.ui
    times = list(ctx.value("schedule.run_times", DEFAULT_RUN_TIMES))
    days = list(ctx.value("schedule.send_days", DEFAULT_SEND_DAYS))
    limit = int(ctx.value("schedule.daily_send_limit", DEFAULT_DAILY_LIMIT))
    minutes = int(ctx.value("schedule.reply_check_minutes", DEFAULT_REPLY_CHECK))
    leads = int(ctx.value("audience.leads_per_run", DEFAULT_LEADS_PER_RUN))
    custom = ctx.section == "schedule" or any(ui.has_answer(key) for key in SCHEDULE_KEYS)
    if not custom and ui.interactive:
        ui.info("Suggested: " + schedule_sentence(times, days, limit, minutes, leads))
        custom = not ui.confirm("schedule.use_suggested", "Use this schedule?", default=True,
                                hint="Sending a few emails a day at first keeps your address out of spam folders.")
    if custom:
        times = sorted(set(ui.list("schedule.run_times", q("schedule.run_times").label, "09:00", default=times,
                                   validate=valid_hhmm, normalize=normalize_hhmm, hint=q("schedule.run_times").help)))
        days = _ask_days(ctx, days)
        limit = int(ui.text("schedule.daily_send_limit", q("schedule.daily_send_limit").label, default=str(limit),
                            validate=_between(1, 500), hint=q("schedule.daily_send_limit").help))
        minutes = int(ui.text("schedule.reply_check_minutes", q("schedule.reply_check_minutes").label,
                              default=str(minutes), validate=_between(5, 720, zero_ok=True),
                              hint=q("schedule.reply_check_minutes").help))
        leads = int(ui.text("audience.leads_per_run", q("audience.leads_per_run").label, default=str(leads),
                            validate=_between(1, 50)))
    ctx.a.update({"schedule.run_times": times, "schedule.send_days": days, "schedule.daily_send_limit": limit,
                  "schedule.reply_check_minutes": minutes, "audience.leads_per_run": leads})
    if ctx.section:
        return
    if ctx.profile is not None and _schedule_installed(ctx):
        # A running schedule follows the saved settings (as `--section schedule` does), whatever
        # `install` says: leaving it on stale times while claiming "off" would be worse.
        ctx.install_schedule = ctx.schedule_installed = True
        ui.info(f"Your schedule is already on, so it's updated with these settings when you save "
                f"(`{CLI_NAME} schedule off` stops it).")
        return
    install_q = q("schedule.install")
    # Off unless asked, for a person too: nobody has previewed an email yet, and a live schedule
    # emails real prospects. `sdr schedule on` turns it on later.
    ctx.install_schedule = ui.confirm("schedule.install", install_q.label, default=False, hint=install_q.help)


def _schedule_installed(ctx: SetupContext) -> bool:
    try:
        return bool(ctx.deps.schedule_status().get("installed"))
    except Exception:  # noqa: BLE001 - no crontab/schtasks here: treat as not installed
        return False


# ── 7. Security & updates ────────────────────────────────────────────────────


def step_security(ctx: SetupContext) -> None:
    """Optional dashboard password, stored only as a salted hash in .env."""
    from core.dashboard_auth import password_problem
    ui, question = ctx.ui, q("security.dashboard_password_env")
    answered = ui.has_answer("security.dashboard_password_env")
    if not ui.interactive and not answered:
        return  # nothing asked for: keep whatever is set
    if not answered and not ctx.new_password:
        if ctx.saved_env.get("DASHBOARD_PASSWORD_HASH"):
            if ui.confirm("security.keep_password", "Keep your current dashboard password?", default=True):
                return
        elif not ui.confirm("security.protect", "Protect your dashboard with a password? (recommended)",
                            default=True, hint="Otherwise anyone using this computer can open it."):
            return
    for _ in range(MAX_TRIES):
        try:
            password = ui.password("security.dashboard_password", question.label, confirm=True,
                                   hint="At least 8 characters. Only a scrambled version (hash) is saved.")
        except InvalidAnswer as exc:
            if not ui.interactive:
                raise
            # The two entries never matched: the password is optional, so the rest of the setup
            # must not be thrown away over it.
            ui.warn(f"No new password set ({exc.reason}). Try again with `{CLI_NAME} setup --section security`.")
            return
        if not password:
            return  # already reported as missing (answers mode)
        problem = password_problem(password)
        if not problem:
            ctx.env["DASHBOARD_PASSWORD_HASH"] = ctx.deps.hash_password(password)
            return
        if not ui.interactive:
            _record_invalid(ui, "security.dashboard_password_env", question.label, problem)
            return
        ui.warn(problem)
        ui.answers.pop("security.dashboard_password_env", None)
    ui.warn(f"No new password set. Try again with `{CLI_NAME} setup --section security`.")


def step_updates(ctx: SetupContext) -> None:
    question = q("updates.mode")
    ctx.a["updates.mode"] = ctx.ui.select("updates.mode", question.label, question.choices,
                                          default=ctx.value("updates.mode", "notify"))
