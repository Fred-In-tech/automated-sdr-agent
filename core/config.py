"""Loads secrets (.env) and the business profile (config/profile.toml).

Everything specific to one business — who you are, who you target, what your
emails say — lives in config/profile.toml, so the code itself can be shared.
Create it with `python3 cli.py setup`, or copy config/profile.example.toml.
"""

import os
import re
import tomllib
import zlib
from typing import Mapping

from core.product import CLI_NAME
from core.setup_toml import parse_env_value  # same quoting rules the wizard writes with

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
# PROFILE_PATH / ENV_FILE env vars let you keep several profiles (or test safely).
PROFILE_PATH = os.getenv("PROFILE_PATH") or os.path.join(CONFIG_DIR, "profile.toml")
EXAMPLE_PROFILE_PATH = os.path.join(CONFIG_DIR, "profile.example.toml")
ENV_PATH = os.getenv("ENV_FILE") or os.path.join(ROOT_DIR, ".env")

REQUIRED_FIELDS = {
    "sender": ["from_name", "product_name", "product_url", "sign_off"],
    "targeting": ["ideal_client", "professions", "cities"],
    "outreach": ["subject", "body"],
}

DEFAULT_UNSUBSCRIBE_LINE = 'Not interested? Just reply "unsubscribe" and I won\'t email you again.'


class ProfileError(Exception):
    """Raised when config/profile.toml is missing or incomplete."""


class EmailSettingsError(ProfileError):
    """Raised when .env has an email login but not the server it belongs to.

    A subclass of ProfileError on purpose: every entry point (cli, runner, dashboard) already
    turns a ProfileError into one friendly line and a clean exit, and a half-written .env
    deserves exactly that treatment."""


# Env var names and default port of each mail server the SDR talks to. There is deliberately no
# default host: the old fallback was the author's own provider, so anyone who wrote .env by hand
# without the host lines had their Gmail password sent to Hostinger's servers on every run.
MAIL_SERVERS = {"smtp": ("SMTP_HOST", "SMTP_PORT", 465), "imap": ("IMAP_HOST", "IMAP_PORT", 993)}


def mail_server(kind: str, env: Mapping[str, str] | None = None) -> tuple[str, int]:
    """(host, port) of the "smtp" or "imap" server from the environment (.env already loaded).

    Fails fast with EmailSettingsError when a login (SMTP_USER + SMTP_PASS) is set but the host
    is missing, or the port isn't a number, so a password is never sent to a guessed server.
    Without a login the host may be empty: the SDR then stays in dry-run and sends nothing."""
    if kind not in MAIL_SERVERS:
        raise ValueError(f"Unknown mail server kind {kind!r}. Choose one of: {', '.join(MAIL_SERVERS)}")
    env = os.environ if env is None else env
    host_var, port_var, default_port = MAIL_SERVERS[kind]
    host = (env.get(host_var) or "").strip()
    port_text = (env.get(port_var) or "").strip() or str(default_port)
    fix = f"Run `{CLI_NAME} setup --section email` to set it."
    try:
        port = int(port_text)
    except ValueError:
        raise EmailSettingsError(f"{port_var} in .env must be a port number like {default_port} "
                                 f"(got {port_text!r}). {fix}") from None
    has_login = bool((env.get("SMTP_USER") or "").strip()) and bool(env.get("SMTP_PASS"))
    if has_login and not host:
        raise EmailSettingsError(f"{host_var} is missing from .env, so the SDR doesn't know which mail server "
                                 f"your login belongs to (it will never guess one). {fix}")
    return host, port


def load_env_file(path: str = ENV_PATH) -> None:
    """Load KEY=value lines from .env without overriding variables already set."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), parse_env_value(val))


def load_profile(path: str | None = None) -> dict:
    """Read and validate the business profile. Raises ProfileError with a fix-it message."""
    path = path or PROFILE_PATH
    if not os.path.exists(path):
        raise ProfileError(
            f"No profile found at {path}.\n"
            "Run `python3 cli.py setup` to create one "
            "(or copy config/profile.example.toml to config/profile.toml and edit it)."
        )
    with open(path, "rb") as f:
        try:
            profile = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{path} has a formatting error: {e}") from e

    missing = [
        f"[{section}] {key}"
        for section, keys in REQUIRED_FIELDS.items()
        for key in keys
        if not profile.get(section, {}).get(key)
    ]
    if missing:
        raise ProfileError(f"{path} is missing required settings: " + ", ".join(missing))
    return profile


def compliance_warnings(profile: dict) -> list[str]:
    """Settings that are legally required for cold email in most countries."""
    warnings = []
    if not profile["sender"].get("postal_address"):
        warnings.append(
            "[sender] postal_address is empty. Cold emails must include a real mailing "
            "address (US CAN-SPAM). Add it to config/profile.toml."
        )
    return warnings


# The offer is typed by the user and dropped into templates as "a {{offer}}". People type it every
# way: "free 14-day trial", "a free planning call", "an intro session", "3 free months".
_ARTICLE_BEFORE_OFFER = re.compile(r"(?<![\w'])([Aa]n?) \{\{offer\}\}")
_DETERMINER_BEFORE_OFFER = re.compile(r"(?<![\w'])([Yy]our|[Tt]he|[Oo]ur) \{\{offer\}\}")
_OWN_ARTICLE = re.compile(r"^(an?|the)\s+", re.IGNORECASE)
_NEEDS_NO_ARTICLE = re.compile(r"^(an?|the|our|your|my|this|one|\d)\b", re.IGNORECASE)
_OFFER_IS_A_MEETING = re.compile(r"\b(call|meeting|consult\w*|demo|chat)\b", re.IGNORECASE)


def _offer_phrase(article: str, offer: str) -> str:
    """ "a" + offer, as a person would write it: no "a a", "an" before a vowel, no article before
    a number or "our". Keeps the template's capital letter."""
    if _NEEDS_NO_ARTICLE.match(offer):
        phrase = offer
    else:
        phrase = ("an " if offer[:1].lower() in "aeiou" else "a ") + offer
    return phrase[:1].upper() + phrase[1:] if article[:1].isupper() else phrase


def _fit_offer(template: str, offer: str) -> str:
    offer = offer.strip()
    if not offer:
        return template
    template = _ARTICLE_BEFORE_OFFER.sub(lambda m: _offer_phrase(m.group(1), offer), template)
    template = _DETERMINER_BEFORE_OFFER.sub(lambda m: f"{m.group(1)} {_OWN_ARTICLE.sub('', offer)}", template)
    if _OFFER_IS_A_MEETING.search(offer):
        template = template.replace(", no call needed", "")   # "a free planning call ..., no call needed"
    return template


def render(template: str, values: dict) -> str:
    """Replace {{placeholders}} in a template. Unknown placeholders are left as-is.

    The offer is worded to fit its sentence (see `_fit_offer`). That happens here rather than in the
    templates because existing profiles already hold their own copy of the wording."""
    if "{{offer}}" in template and values.get("offer"):
        template = _fit_offer(template, str(values["offer"]))
    for key, val in values.items():
        template = template.replace("{{" + key + "}}", str(val))
    return template


OPENER_LABELS = "ABCDEFGH"


def opener_variant_list(profile: Mapping) -> list[str]:
    """[outreach] opener_variants, cleaned: only a list of non-blank strings counts (a hand-typed
    single string must not become one-letter first lines), at most one per label. Label "A" is
    the first entry of this list everywhere it's shown."""
    raw = (profile.get("outreach", {}) or {}).get("opener_variants")
    if not isinstance(raw, list):
        return []
    return [v for v in raw if isinstance(v, str) and v.strip()][:len(OPENER_LABELS)]


def opener_choice(profile: Mapping, lead: Mapping | None) -> tuple[str, str]:
    """(label, first-line template) for this lead's {{opener}}.

    "ai" when the lead has its own AI-written line. With [outreach] opener_variants, each lead
    gets one of them for good (stable per address, hashed separately from the subject test so the
    two tests don't move in lockstep), labelled "A", "B"...; otherwise ("", default_opener)."""
    lead = lead or {}
    outreach = profile.get("outreach", {}) or {}
    if lead.get("opener"):
        return "ai", lead["opener"]
    variants = opener_variant_list(profile)
    if not variants:
        return "", outreach.get("default_opener", "")
    seed = ("opener:" + str(lead.get("email") or "")).lower().encode()
    index = zlib.crc32(seed) % len(variants)
    return OPENER_LABELS[index], variants[index]


def template_values(profile: dict, lead: dict | None = None) -> dict:
    """All placeholders available to email and reply templates."""
    sender = profile["sender"]
    values = {
        "product_name": sender["product_name"],
        "product_url": sender["product_url"],
        "from_name": sender["from_name"],
        "sign_off": sender["sign_off"],
        "offer": sender.get("offer", ""),
        "first_name": "there",
        "company": "your business",
        "category": profile["targeting"]["ideal_client"],
        "location": "your area",
    }
    if lead:
        # Only a real first name (found by core/qualify.py) — never the first word of a business name.
        values.update({
            "first_name": lead.get("first_name") or "there",
            "company": lead.get("company") or values["company"],
            "category": lead.get("category") or values["category"],
            "location": lead.get("location") or values["location"],
        })
    # {{opener}}: the lead's AI-written first line if we have one, else the A/B test's or the default
    label, opener = opener_choice(profile, lead)
    values["opener"] = opener if label == "ai" else render(opener, values)
    return values
