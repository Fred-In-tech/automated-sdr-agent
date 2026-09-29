"""Turns setup answers into config/profile.toml: the sales and marketing email templates.

Setup answers use one flat shape everywhere — the keys of the answers file ("business.website",
"audience.cities", "style.kind", ...), plus a few the wizard derives ("style.heading_color",
"schedule.emails_per_run"). The same dict drives:

- `build_profile_from_answers(a)` — a complete, commented profile for a first setup;
- `managed_values(a)` — just the settings setup owns, per table, which `sdr setup --section`
  writes back with core.setup_toml.update_profile_text (SECTION_KEYS says which step owns what),
  so re-running one step never touches the email copy someone rewrote by hand;
- `state_from_profile(profile, env)` — the reverse, so every question defaults to what's saved.

Two sequences, same shape (first email + 3 follow-ups + a "not now" check-in, replies):
- "sales" (style = "personal"): reads like a person typed it; lands in Primary, gets replies.
- "marketing" (style = "branded"): a little more visual — benefit bullets, button, brand pills.
Every {{placeholder}} is one core.config.template_values knows, so both render with
bots.email_marketing.build_outreach_email.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from core.product import BRAND_COLOR, CLI_NAME
from core.setup_toml import toml_multiline, toml_str, toml_value

DEFAULT_OFFER = "free 14-day trial"
UNSUBSCRIBE_LINE = "If this isn't relevant, just let me know and I won't reach out again."
DEFAULT_RUN_TIMES = ["09:00", "14:00"]
DEFAULT_SEND_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_DAILY_LIMIT = 10   # what the README tells a new address to start with; raise it after two weeks
MAX_EMAILS_PER_RUN = 10
DEFAULT_REPLY_CHECK = 45
DEFAULT_LEADS_PER_RUN = 4
DEFAULT_UPDATE_MODE = "notify"
SEARCH_ENGINES = ("auto", "brave", "duckduckgo", "bing")
DEFAULT_DESIGN = {"heading_color": "#0F1729", "text_color": "#1B2B4D", "background": "#F3F6FB"}
KINDS = {"sales": "personal", "marketing": "branded"}

# Which profile keys each setup step owns. `sdr setup --section X` rewrites exactly these.
SECTION_KEYS: dict[str, dict[str, list[str]]] = {
    "brand": {
        "sender": ["product_name", "product_url", "website", "offer", "postal_address", "require_postal_address"],
        "email_design": ["logo_url", "brand_color", "heading_color", "text_color", "background", "tagline"],
    },
    "audience": {
        "sender": ["pitch"],
        "targeting": ["ideal_client", "professions", "cities", "search_engine"],
    },
    "style": {
        "email_design": ["style", "signature_logo_url", "signature_logo_test"],
        "auto_reply": ["branded_intents"],
    },
    "email": {"sender": ["from_name", "from_email", "sign_off"], "sdr": ["alert_email"]},
    "replies": {"auto_reply": ["enabled"]},
    "schedule": {
        "schedule": ["run_times", "reply_check_minutes"],
        "outreach": ["send_days", "daily_send_limit", "emails_per_run"],
        "targeting": ["leads_per_run"],
    },
    "security": {},
    "updates": {"updates": ["mode"]},
}


# ── wording helpers ──────────────────────────────────────────────────────────


def plural(noun: str) -> str:
    """"florist" -> "florists", "agency" -> "agencies", "coach" -> "coaches". Good enough for
    job titles; anything already plural-looking is left alone."""
    text = (noun or "").strip()
    lower = text.lower()
    if not text or lower.endswith("s") and not lower.endswith(("ss", "us")):
        return text
    if re.search(r"[^aeiou]y$", lower):
        return text[:-1] + "ies"
    if lower.endswith(("ss", "sh", "ch", "x", "z", "us")):
        return text + "es"
    return text + "s"


def _first_lower(text: str) -> str:
    """Lower-case the first letter unless the first word is an acronym ("CRM", "AI")."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def _capitalize(text: str) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:]


def pitch_sentence(name: str, pitch: str, ideal_client: str) -> str:
    """The one-line pitch of the first email. People either finish the sentence we show them
    ("…helps florists" + "send quotes in one click") or type a whole sentence; handle both."""
    name, text = (name or "").strip(), " ".join((pitch or "").split()).rstrip(" .")
    if not text:
        return f"{name} helps {plural(ideal_client)} save time." if name else ""
    lower = text.lower()
    first_word = name.split()[0].lower() if name else ""
    if name and (lower.startswith(name.lower()) or (len(first_word) >= 3 and lower.startswith(first_word + " "))):
        sentence = text
    elif lower.startswith(("helps ", "help ")):
        sentence = f"{name} {text}"
    elif lower.startswith(("we ", "we'", "our ", "i ", "i'")):
        sentence = _capitalize(text)
    else:
        sentence = f"{name} helps {plural(ideal_client)} {_first_lower(text)}"
    return sentence if sentence[-1:] in ".!?" else sentence + "."


def pitch_phrase(pitch: str) -> str:
    """The pitch as a short benefit bullet ("send quotes in one click" -> "Send quotes in one click")."""
    return _capitalize(" ".join((pitch or "").split()).rstrip(" ."))


def highlights_for(a: Mapping[str, Any]) -> list[str]:
    """Small feature pills under a branded first email: the offer and who it's for."""
    offer, client = _get(a, "business.offer", ""), _get(a, "audience.ideal_client", "")
    pills = [_capitalize(offer)] if offer else []
    if client:
        pills.append(f"Made for {plural(client)}")
    return pills


def benefits_for(a: Mapping[str, Any]) -> list[str]:
    """2-3 benefit bullets for the marketing template and the welcome email."""
    tagline, pitch = _get(a, "business.tagline", ""), _get(a, "business.pitch", "")
    client, offer = _get(a, "audience.ideal_client", ""), _get(a, "business.offer", "")
    items = [tagline.rstrip(".") if tagline else pitch_phrase(pitch)]
    if client:
        items.append(f"Built for {plural(client)}, simple to start")
    if offer:
        items.append(f"{_capitalize(offer)}, so you can try it on real work")
    seen, result = set(), []
    for item in items:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


def _get(a: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = a.get(key)
    return default if value in (None, "") else value


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


# ── the two sequences ────────────────────────────────────────────────────────


def sequence_copy(a: Mapping[str, Any]) -> dict:
    """{"body", "button", "follow_ups": [(days, body, button)], "not_now": (body, button)}."""
    name = _get(a, "business.name", "us")
    client = _get(a, "audience.ideal_client", "business")
    website = _get(a, "business.website", "")
    kind = _get(a, "style.kind", "sales")
    sentence = pitch_sentence(name, _get(a, "business.pitch", ""), client)
    look = {"text": "See how it works", "url": website or "{{product_url}}"}
    start = {"text": "Start your {{offer}}", "url": "{{product_url}}"}

    if kind == "marketing":
        body = ("Hi {{first_name}},\n\n{{opener}}\n\n" + sentence + "\n\n" + _bullets(benefits_for(a))
                + "\n\n{{button}}\n\n{{sign_off}}")
        follow_up_1 = ("Hi {{first_name}},\n\nA quick follow-up with the short version:\n\n"
                       "- " + (pitch_phrase(_get(a, "business.pitch", "")) or "Less admin, more paid work") + "\n"
                       "- Set up in minutes, no training needed\n"
                       "- A {{offer}} to see if it fits\n\n{{button}}\n\n{{sign_off}}")
    else:
        # Sales emails carry no link at all: a cold email with a link reads as marketing (and is
        # likelier to be filtered), and "reply and I'll send it" starts the conversation. The
        # reply is answered with the welcome email, which has the button.
        body = ("Hi {{first_name}},\n\n{{opener}}\n\n" + sentence
                + "\n\nWorth a look? Just reply and I'll send you the link.\n\n{{sign_off}}")
        follow_up_1 = ("Hi {{first_name}},\n\nQuick follow-up in case my last email got buried.\n\n"
                       "Most {{category}}s I talk to don't need another tool, they need fewer hours on admin "
                       "that doesn't pay. That's the whole point of {{product_name}}.\n\n"
                       "Worth trying on your next project? Reply and I'll send you the link.\n\n{{sign_off}}")
    if kind == "marketing":
        follow_up_2 = ("Hi {{first_name}},\n\nIn case it's easier to just try it: there's a {{offer}} waiting "
                       "for you, no call needed.\n\n{{button}}\n\n{{sign_off}}")
    else:
        follow_up_2 = ("Hi {{first_name}},\n\nIn case it's easier to just try it: there's a {{offer}} waiting "
                       "for you, no call needed.\n\nWant the link? Just reply \"send me the link\".\n\n"
                       "{{sign_off}}")
    breakup = ("Hi {{first_name}},\n\nI haven't heard back, so I'll assume the timing isn't right and stop "
               "emailing.\n\nIf it's easier, just reply with a number:\n1 - send me the link\n2 - maybe later\n"
               "3 - not interested\n\nGood luck with everything at {{company}}.\n\n{{sign_off}}")
    if kind == "marketing":
        not_now = ("Hi {{first_name}},\n\nYou mentioned the timing wasn't right a while back, so I'm checking "
                   "in once. If it's useful now, you can start with a {{offer}}.\n\n{{button}}\n\n"
                   "No worries if not, I won't follow up again.\n\n{{sign_off}}")
        return {"body": body, "button": look,
                "follow_ups": [(3, follow_up_1, look), (4, follow_up_2, start), (7, breakup, None)],
                "not_now": (not_now, start)}
    not_now = ("Hi {{first_name}},\n\nYou mentioned the timing wasn't right a while back, so I'm checking in "
               "once. If it's useful now, you can start with a {{offer}}. Reply and I'll send you the link.\n\n"
               "No worries if not, I won't follow up again.\n\n{{sign_off}}")
    return {"body": body, "button": None,
            "follow_ups": [(3, follow_up_1, None), (4, follow_up_2, None), (7, breakup, None)],
            "not_now": (not_now, None)}


def reply_copy(a: Mapping[str, Any]) -> dict:
    """The approved auto-replies: interested (a welcome with checklist, steps and a button),
    question and not_now."""
    client = _get(a, "audience.ideal_client", "client")
    benefits = benefits_for(a)[:2] + ["Help from me personally if you get stuck"]
    interested = (
        "Hi {{first_name}},\n\nGreat to hear from you! Here's everything you need to get started.\n\n"
        "Your {{offer}} includes:\n" + _bullets(benefits) + "\n\n"
        "Getting started takes a few minutes:\n"
        "1. Create your account with the button below\n"
        f"2. Try it on one real {client} project\n"
        "3. Reply to this email with any questions\n\n{{button}}\n\n{{sign_off}}"
    )
    question = ("Hi {{first_name}},\n\nGood question, I'll get you a proper answer personally shortly.\n\n"
                "In the meantime you can try everything with a {{offer}}: {{product_url}}\n\n{{sign_off}}")
    not_now = ("Hi {{first_name}},\n\nTotally understand, thanks for letting me know. I won't keep "
               "emailing.\n\n{{sign_off}}")
    return {"interested": interested, "question": question, "not_now": not_now}


# ── managed settings ─────────────────────────────────────────────────────────


def emails_per_run(a: Mapping[str, Any]) -> int:
    """Per-run cap: a saved value is kept (but never above the daily limit); new setups get
    min(10, daily limit), so two runs a day can reach the limit."""
    limit = int(_get(a, "schedule.daily_send_limit", DEFAULT_DAILY_LIMIT))
    current = _get(a, "schedule.emails_per_run", MAX_EMAILS_PER_RUN)
    return max(1, min(int(current), limit))


def require_postal_address(a: Mapping[str, Any]) -> bool:
    return bool(_get(a, "business.postal_address", "")) or not bool(_get(a, "business.allow_no_address", False))


def signature_logo_url(a: Mapping[str, Any]) -> str:
    """Sales emails can A/B test a small logo in the signature; branded ones have a header logo.

    The rules, so a re-run never surprises anyone:
    - "yes" to the test: the saved signature logo (a hand-set one survives), else the header logo.
    - "no": no logo, unless a *different* signature logo was set by hand. At run time a URL with
      the test off means everyone gets the logo, so "no" must not quietly mean "logo for all".
    - marketing style: the saved value is kept untouched (branded emails ignore it), so
      switching back to sales later doesn't lose it."""
    saved = _get(a, "style.signature_logo_url", "")
    header = _get(a, "style.logo_url", "")
    if _get(a, "style.kind", "sales") != "sales":
        return saved
    if not _get(a, "style.signature_logo_test", True):
        return saved if saved and saved != header else ""
    return saved or header


def managed_values(a: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every setting setup owns, as {table: {key: value}} (a superset of SECTION_KEYS)."""
    website = _get(a, "business.website", "")
    kind = _get(a, "style.kind", "sales")
    return {
        "sender": {
            "product_name": _get(a, "business.name", "My business"),
            "product_url": _get(a, "business.signup_url", website),
            "website": website,
            "from_name": _get(a, "email.from_name", _get(a, "business.name", "")),
            "from_email": _get(a, "email.alias", ""),
            "sign_off": _get(a, "email.sign_off", ""),
            "offer": _get(a, "business.offer", DEFAULT_OFFER),
            "pitch": _get(a, "business.pitch", ""),
            "postal_address": _get(a, "business.postal_address", ""),
            "require_postal_address": require_postal_address(a),
        },
        "targeting": {
            "ideal_client": _get(a, "audience.ideal_client", ""),
            "professions": list(_get(a, "audience.job_titles", None) or [_get(a, "audience.ideal_client", "")]),
            "cities": list(_get(a, "audience.cities", [])),
            "search_engine": _get(a, "audience.search_engine", "auto"),
            "leads_per_run": int(_get(a, "audience.leads_per_run", DEFAULT_LEADS_PER_RUN)),
        },
        "outreach": {
            "send_days": list(_get(a, "schedule.send_days", DEFAULT_SEND_DAYS)),
            "daily_send_limit": int(_get(a, "schedule.daily_send_limit", DEFAULT_DAILY_LIMIT)),
            "emails_per_run": emails_per_run(a),
        },
        "email_design": {
            "style": KINDS.get(kind, "personal"),
            "logo_url": _get(a, "style.logo_url", ""),
            "brand_color": _get(a, "style.brand_color", BRAND_COLOR),
            "heading_color": _get(a, "style.heading_color", DEFAULT_DESIGN["heading_color"]),
            "text_color": _get(a, "style.text_color", DEFAULT_DESIGN["text_color"]),
            "background": _get(a, "style.background", DEFAULT_DESIGN["background"]),
            "tagline": _get(a, "business.tagline", ""),
            "signature_logo_url": signature_logo_url(a),
            "signature_logo_test": bool(_get(a, "style.signature_logo_test", True)),
        },
        "auto_reply": {
            "enabled": bool(_get(a, "replies.enabled", True)),
            "branded_intents": ["interested"] if _get(a, "replies.branded_welcome", True) else [],
        },
        "sdr": {"alert_email": _get(a, "email.alert_email", "")},
        "schedule": {
            "run_times": list(_get(a, "schedule.run_times", DEFAULT_RUN_TIMES)),
            "reply_check_minutes": int(_get(a, "schedule.reply_check_minutes", DEFAULT_REPLY_CHECK)),
        },
        "updates": {"mode": _get(a, "updates.mode", DEFAULT_UPDATE_MODE)},
    }


def section_updates(section: str, a: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The {table: {key: value}} that `sdr setup --section <section>` writes."""
    values = managed_values(a)
    return {table: {key: values[table][key] for key in keys} for table, keys in SECTION_KEYS[section].items()}


# ── the full profile ─────────────────────────────────────────────────────────


def _kv(key: str, value: Any, comment: str = "") -> str:
    line = f"{key} = {toml_value(value)}"
    return f"{line}   # {comment}" if comment and "\n" not in line else line


def _button_line(button: dict | None) -> str:
    return f"button = {toml_value(button)}\n" if button else ""


def build_profile_from_answers(a: Mapping[str, Any]) -> str:
    """A complete, commented config/profile.toml from setup answers (see the module doc)."""
    v = managed_values(a)
    s, t, o, d, r = v["sender"], v["targeting"], v["outreach"], v["email_design"], v["auto_reply"]
    copy = sequence_copy(a)
    kind = _get(a, "style.kind", "sales")
    follow_ups = "".join(
        f"\n[[outreach.follow_ups]]\ndays_after_previous = {days}\nbody = {toml_multiline(body)}\n"
        f"{_button_line(button)}"
        for days, body, button in copy["follow_ups"])
    replies = "".join(f"{intent} = {toml_multiline(text)}\n\n" for intent, text in reply_copy(a).items())
    not_now_body, not_now_button = copy["not_now"]
    highlights = highlights_for(a) if kind == "marketing" else []
    return f"""# Your outreach profile, created by `{CLI_NAME} setup`. Edit freely: every option is explained
# in config/profile.example.toml. Change one part later with `{CLI_NAME} setup --section <name>`
# (brand, audience, style, email, replies, schedule, security, updates); your email wording is kept.

[sender]
{_kv("product_name", s["product_name"])}
{_kv("product_url", s["product_url"], "where the 'try it' buttons go")}
{_kv("website", s["website"], "your homepage (dashboard link)")}
{_kv("from_name", s["from_name"], "the name people see in their inbox")}
{_kv("from_email", s["from_email"], "optional alias of your login mailbox; blank = the login address")}
{_kv("sign_off", s["sign_off"])}
{_kv("offer", s["offer"])}
{_kv("pitch", s["pitch"], "used by setup to write your first email")}
{_kv("postal_address", s["postal_address"], "legally required in cold email")}
{_kv("require_postal_address", s["require_postal_address"])}

[targeting]
{_kv("ideal_client", t["ideal_client"], "used as {{category}} in emails")}
{_kv("professions", t["professions"], "job titles / business types to search for")}
{_kv("cities", t["cities"])}
{_kv("search_engine", t["search_engine"], "auto | brave | duckduckgo | bing")}
search_extra_words = "contact"
keywords = []
thumbtack_categories = []          # optional (US): e.g. ["photographers"]
thumbtack_city_category = ""       # optional (US): e.g. "photographers"
skip_domains = []
{_kv("leads_per_run", t["leads_per_run"])}
min_fit_score = 50

[outreach]
{_kv("emails_per_run", o["emails_per_run"])}
{_kv("daily_send_limit", o["daily_send_limit"], "keep it low for the first 2 weeks")}
seconds_between_emails = 300
{_kv("send_days", o["send_days"])}
send_window = "08:00-17:00"        # only send during business hours (this computer's time)
max_bounce_rate = 10
track_links = true
utm_campaign = "sdr"
subject = {toml_str("question about {{company}}")}
default_opener = {toml_str("I came across {{company}} while looking at {{category}}s in {{location}}.")}
body = {toml_multiline(copy["body"])}
{_button_line(copy["button"])}unsubscribe_line = {toml_str(UNSUBSCRIBE_LINE)}
{follow_ups}
[outreach.not_now_follow_up]
days = 60
body = {toml_multiline(not_now_body)}
{_button_line(not_now_button)}
# "personal" = like an email typed in Gmail (Primary tab). "branded" = logo, colours, button.
[email_design]
{_kv("style", d["style"])}
{_kv("logo_url", d["logo_url"])}
{_kv("brand_color", d["brand_color"])}
{_kv("heading_color", d["heading_color"])}
{_kv("text_color", d["text_color"])}
{_kv("background", d["background"])}
{_kv("tagline", d["tagline"])}
{_kv("highlights", highlights, "small feature pills under a branded first email")}
{_kv("signature_logo_url", d["signature_logo_url"], "small logo beside your name (sales style)")}
{_kv("signature_logo_test", d["signature_logo_test"], "half get the logo, half don't; see `sdr report`")}

[auto_reply]
{_kv("enabled", r["enabled"])}
lookback_days = 14
opt_out_words = ["unsubscribe", "remove me", "stop emailing", "take me off"]
{_kv("branded_intents", r["branded_intents"], "replies sent in your branded design")}

[auto_reply.replies]
{replies}[auto_reply.buttons]
interested = {toml_value({"text": "Start your {{offer}}", "url": "{{product_url}}"})}

[sdr]
{_kv("alert_email", v["sdr"]["alert_email"], "hot-lead alerts + daily digest; blank = your sending inbox")}
daily_digest = true

[ai]
enabled = false
model = "claude-opus-5"

[schedule]
{_kv("run_times", v["schedule"]["run_times"], "full runs, this computer's time")}
{_kv("reply_check_minutes", v["schedule"]["reply_check_minutes"], "0 = off")}

[updates]
{_kv("mode", v["updates"]["mode"], "notify | auto | off")}

[social]
enabled = false
topics = []
posts = []
"""


def build_profile_toml(a: Mapping[str, Any]) -> str:
    """Backwards-compatible entry point: the old wizard's answer dict (product_name, product_url,
    from_name, sign_off, offer, postal_address, ideal_client, pitch, professions, cities,
    alert_email) -> a full "sales" profile."""
    mapped = {
        "business.name": a.get("product_name"), "business.website": a.get("website") or a.get("product_url"),
        "business.signup_url": a.get("product_url"), "business.offer": a.get("offer"),
        "business.pitch": a.get("pitch"), "business.postal_address": a.get("postal_address", ""),
        "audience.ideal_client": a.get("ideal_client"), "audience.job_titles": a.get("professions"),
        "audience.cities": a.get("cities"), "email.from_name": a.get("from_name"),
        "email.sign_off": a.get("sign_off"), "email.alert_email": a.get("alert_email", ""),
        "style.kind": a.get("kind", "sales"),
    }
    return build_profile_from_answers({k: v for k, v in mapped.items() if v is not None})


# ── reading a saved profile back ─────────────────────────────────────────────


def _table(profile: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = profile.get(name)
    return value if isinstance(value, Mapping) else {}


def provider_from_env(env: Mapping[str, str]) -> str | None:
    """Best guess of the provider key from saved SMTP settings (for the menu default)."""
    host = (env.get("SMTP_HOST") or "").strip().lower()
    if not host:
        return None
    from core.email_checks import PROVIDERS
    address = (env.get("SMTP_USER") or "").lower()
    if host == "smtp.gmail.com":
        return "gmail" if address.endswith(("@gmail.com", "@googlemail.com")) else "google_workspace"
    for key, info in PROVIDERS.items():
        if info["smtp_host"] and info["smtp_host"] == host:
            return key
    return "other"


def state_from_profile(profile: Mapping[str, Any] | None, env: Mapping[str, str] | None = None) -> dict:
    """Setup answers from a saved profile (+ saved .env settings, never the password), so
    re-running setup suggests what's there. Profiles from older versions (no [schedule],
    [updates] or website) get the defaults."""
    profile, env = profile or {}, env or {}
    sender, targeting, outreach = _table(profile, "sender"), _table(profile, "targeting"), _table(profile, "outreach")
    design, auto_reply = _table(profile, "email_design"), _table(profile, "auto_reply")
    schedule = _table(profile, "schedule")
    state = {
        "business.website": sender.get("website") or sender.get("product_url"),
        "business.name": sender.get("product_name"),
        "business.tagline": design.get("tagline"),
        "business.offer": sender.get("offer"),
        "business.signup_url": sender.get("product_url"),
        "business.pitch": sender.get("pitch"),
        "business.postal_address": sender.get("postal_address"),
        "business.allow_no_address": sender.get("require_postal_address", True) is False,
        "audience.ideal_client": targeting.get("ideal_client"),
        "audience.job_titles": targeting.get("professions"),
        "audience.cities": targeting.get("cities"),
        "audience.leads_per_run": targeting.get("leads_per_run"),
        "audience.search_engine": targeting.get("search_engine"),
        "style.kind": "marketing" if design.get("style") == "branded" or (
            not design.get("style") and design.get("enabled")) else ("sales" if design else None),
        "style.logo_url": design.get("logo_url"),
        "style.brand_color": design.get("brand_color"),
        "style.heading_color": design.get("heading_color"),
        "style.text_color": design.get("text_color"),
        "style.background": design.get("background"),
        "style.signature_logo_url": design.get("signature_logo_url"),
        # The saved flag is the truth (false + a URL = everyone gets the logo); older profiles
        # without the flag get "test it" when a signature logo is set.
        "style.signature_logo_test": (design["signature_logo_test"]
                                      if isinstance(design.get("signature_logo_test"), bool)
                                      else bool(design.get("signature_logo_url"))
                                      if "signature_logo_url" in design else None),
        "email.from_name": sender.get("from_name"),
        "email.sign_off": sender.get("sign_off"),
        "email.alias": sender.get("from_email"),
        "email.alert_email": _table(profile, "sdr").get("alert_email"),
        "email.address": env.get("SMTP_USER"),
        "email.provider": provider_from_env(env),
        "replies.enabled": auto_reply.get("enabled") if auto_reply else None,
        "replies.branded_welcome": ("interested" in (auto_reply.get("branded_intents") or [])
                                    if auto_reply else None),
        "schedule.run_times": schedule.get("run_times"),
        "schedule.reply_check_minutes": schedule.get("reply_check_minutes"),
        "schedule.send_days": outreach.get("send_days"),
        "schedule.daily_send_limit": outreach.get("daily_send_limit"),
        "schedule.emails_per_run": outreach.get("emails_per_run"),
        "updates.mode": _table(profile, "updates").get("mode"),
    }
    return {key: value for key, value in state.items() if value not in (None, "", []) and _fits(key, value)}


_LIST_KEYS = {"audience.job_titles", "audience.cities", "schedule.run_times", "schedule.send_days"}
_INT_KEYS = {"audience.leads_per_run", "schedule.reply_check_minutes", "schedule.daily_send_limit",
             "schedule.emails_per_run"}
_BOOL_KEYS = {"business.allow_no_address", "style.signature_logo_test", "replies.enabled", "replies.branded_welcome"}


def _fits(key: str, value: Any) -> bool:
    """Hand-edited profiles can hold anything; a value of the wrong type is ignored (the default
    is suggested instead) rather than crashing a question."""
    if key in _LIST_KEYS:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if key in _INT_KEYS:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if key in _BOOL_KEYS:
        return isinstance(value, bool)
    if key == "style.kind":
        return value in KINDS
    if key == "updates.mode":
        return value in ("notify", "auto", "off")
    if key == "audience.search_engine":
        return value in SEARCH_ENGINES
    return isinstance(value, str)
