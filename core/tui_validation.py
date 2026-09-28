"""Input validation and answers-file parsing for the terminal UI (core/tui.py).

Pure functions with no terminal, rich or questionary dependency, kept apart so the
wizard, the CLI and tests can validate values (emails, URLs, colours, times, lists)
exactly the way the prompts do. core.tui re-exports the public names, so callers can
simply `from core.tui import valid_email, load_answers`.
"""

from __future__ import annotations

import difflib
import re
import tomllib
import urllib.parse
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "valid_email", "valid_url", "valid_hex_color", "valid_hhmm", "validator",
    "normalize_hex_color", "normalize_hhmm", "parse_list", "flatten_answers", "load_answers", "unknown_answers",
    "run_validator", "dedupe", "GENERIC_INVALID", "normalize_choices", "match_choice", "choice_title",
]

GENERIC_INVALID = "That doesn't look right. Please check it and try again."
_SECRET_NAMES = {"password", "passwd", "pass", "secret", "token", "api_key", "app_password", "smtp_pass", "key"}
_SECRET_SUFFIXES = ("_password", "_key", "_token", "_secret", "_pass")


# ── validators and normalizers ───────────────────────────────────────────────

_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(r"^(?:" + _LABEL + r"\.)+[A-Za-z]{2,63}$")
_LOCAL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_RE = re.compile(r"^" + _LOCAL_ATOM + r"(?:\." + _LOCAL_ATOM + r")*$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")  # "acme.com:8080" is a port, not a scheme
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _valid_domain(domain: str) -> bool:
    """A public-looking host name (with a TLD). Internationalised names are IDNA-encoded first."""
    if not domain or len(domain) > 253:
        return False
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return bool(_DOMAIN_RE.match(ascii_domain))


def valid_email(value: Any) -> bool:
    """Pragmatic address check: one @, sane local part, a domain with a TLD. Deliverability
    is checked elsewhere (core.email_verifier); this only catches typos early."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) > 254 or text.count("@") != 1:
        return False
    local, domain = text.split("@")
    return 0 < len(local) <= 64 and bool(_LOCAL_RE.match(local)) and _valid_domain(domain)


def valid_url(value: Any, require_scheme: bool = False) -> bool:
    """http(s) URL or bare domain ("acme.com"). Other schemes (javascript:, mailto:, ftp:)
    and URLs with embedded credentials are rejected: these end up in emails and links."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    if "://" not in text:
        if require_scheme or _SCHEME_RE.match(text):
            return False
        text = "https://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or parts.username is not None:
        return False
    return _valid_domain(parts.hostname or "")


def valid_hex_color(value: Any) -> bool:
    """Strict #RRGGBB, the only colour format the email templates and dashboard accept."""
    return isinstance(value, str) and bool(_HEX_RE.match(value.strip()))


def valid_hhmm(value: Any) -> bool:
    """24-hour "HH:MM" (a single-digit hour is fine: "9:00")."""
    return isinstance(value, str) and bool(_HHMM_RE.match(value.strip()))


def normalize_hex_color(value: str) -> str:
    """Forgive "3b82f6", "#3bf" and stray spaces -> "#3B82F6". Unparseable input is returned
    stripped so the validator can explain what's wrong."""
    text = value.strip()
    body = text[1:] if text.startswith("#") else text
    if re.fullmatch(r"[0-9A-Fa-f]{3}", body):
        body = "".join(ch * 2 for ch in body)
    if re.fullmatch(r"[0-9A-Fa-f]{6}", body):
        return "#" + body.upper()
    return text


def normalize_hhmm(value: str) -> str:
    """"9:00" -> "09:00" so schedules compare and sort as plain strings."""
    text = value.strip()
    match = _HHMM_RE.match(text)
    if not match:
        return text
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def validator(predicate: Callable[[str], Any], message: str) -> Callable[[str], bool | str]:
    """Wrap a yes/no check with the message to show when it says no."""
    def check(value: str) -> bool | str:
        return True if predicate(value) else message
    return check


_FRIENDLY = {
    valid_email: "Please enter an email address like you@company.com.",
    valid_url: "Please enter a website like acme.com or https://acme.com.",
    valid_hex_color: "Please enter a colour like #3B82F6.",
    valid_hhmm: "Please enter a time like 09:00 (24-hour clock).",
}


def _friendly(check: Callable[..., Any]) -> str:
    return _FRIENDLY.get(check) or _FRIENDLY.get(getattr(check, "func", None)) or GENERIC_INVALID


def run_validator(check: Callable[[str], Any] | None, value: str) -> None:
    """Run a caller's validator. It may return True/truthy (ok), False (generic or known
    message), a message string (not ok), or raise ValueError(message)."""
    if check is None:
        return
    try:
        result = check(value)
    except ValueError as exc:
        raise ValueError(str(exc) or _friendly(check)) from None
    if isinstance(result, str):
        raise ValueError(result or _friendly(check))
    if not result:
        raise ValueError(_friendly(check))


def dedupe(items: Iterable[str]) -> list[str]:
    """Drop repeats (case-insensitive), keeping the first spelling and the order."""
    seen: set[str] = set()
    result = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


_REGION_CODE_RE = re.compile(r"^(?:[A-Za-z]{2}|[A-Z]{3})$")  # TX, tx, UK, NSW: the "ST" of "City, ST"
# Names that qualify the place before them rather than name a new one ("Austin, Texas, USA").
# Not exhaustive: setup shows the parsed list and asks, so a miss is caught there.
_REGION_PHRASES = frozenset(name.strip() for name in (
    "usa, united states, america, uk, united kingdom, england, scotland, wales, ireland, "
    "northern ireland, canada, australia, new zealand, france, germany, italy, spain, portugal, "
    "netherlands, belgium, switzerland, austria, sweden, norway, denmark, finland, poland, greece, "
    "mexico, brazil, argentina, chile, colombia, india, japan, singapore, south africa, nigeria, "
    "ghana, kenya, uae, united arab emirates, "
    "alabama, alaska, arizona, arkansas, california, colorado, connecticut, delaware, florida, "
    "georgia, hawaii, idaho, illinois, indiana, iowa, kansas, kentucky, louisiana, maine, maryland, "
    "massachusetts, michigan, minnesota, mississippi, missouri, montana, nebraska, nevada, "
    "new hampshire, new jersey, new mexico, new york, north carolina, north dakota, ohio, oklahoma, "
    "oregon, pennsylvania, rhode island, south carolina, south dakota, tennessee, texas, utah, "
    "vermont, virginia, washington, west virginia, wisconsin, wyoming, ontario, quebec, "
    "british columbia, alberta, manitoba, saskatchewan, nova scotia, new south wales, victoria, "
    "queensland"
).split(","))
MAX_QUALIFIERS = 2  # "City, State, Country"


def _is_qualifier(part: str) -> bool:
    return bool(_REGION_CODE_RE.match(part)) or part.lower() in _REGION_PHRASES


def _split_places(text: str) -> list[str]:
    """"Austin, TX, Dallas, TX" -> ["Austin, TX", "Dallas, TX"]; "Austin, Dallas" -> two.
    A region code or a known state/country after a comma belongs to the place before it (up to
    "City, State, Country"); anything else starts a new place. Setup shows the result and lets
    the person re-type it, because no rule can tell "Paris, Ontario" from "Paris, Lyon"."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    places: list[list[str]] = []
    for part in parts:
        if places and len(places[-1]) <= MAX_QUALIFIERS and _is_qualifier(part):
            places[-1].append(part)
        else:
            places.append([part])
    return [", ".join(place) for place in places]


def parse_list(text: str, split_commas: bool = True) -> list[str]:
    """Split typed or file-provided lists. Newlines and semicolons always separate items;
    commas only when there's no other separator. With `split_commas` off (cities), a comma
    is part of the value ("Austin, TX"), unless the line holds several places at once —
    people type cities the way they typed job titles — see `_split_places`."""
    if re.search(r"[;\r\n]", text):
        parts = re.split(r"[;\r\n]+", text)
    elif split_commas:
        parts = text.split(",")
    else:
        parts = _split_places(text)
    return dedupe(part.strip() for part in parts if part.strip())


# ── menu choices ─────────────────────────────────────────────────────────────


def normalize_choices(choices: Sequence[tuple]) -> list[tuple[Any, str, str]]:
    """Accept (value, label, description) or (value, label) tuples."""
    options = []
    for choice in choices:
        if len(choice) not in (2, 3):
            raise ValueError(f"A choice must be (value, label[, description]), got {choice!r}")
        value, label = choice[0], str(choice[1])
        options.append((value, label, str(choice[2]) if len(choice) == 3 and choice[2] else ""))
    if not options:
        raise ValueError("select() needs at least one choice")
    return options


def match_choice(options: list[tuple[Any, str, str]], raw: Any, allow_index: bool) -> Any:
    """Find the option meant by `raw`: exact value, 1-based number (typed menus only),
    or a case-insensitive value/label. Raises ValueError listing the valid values."""
    for value, _label, _desc in options:
        if raw == value:
            return value
    if isinstance(raw, str):
        text = raw.strip()
        if allow_index and text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1][0]
        for value, label, _desc in options:
            if text.lower() in (str(value).lower(), label.lower()):
                return value
    valid = ", ".join(str(value) for value, _label, _desc in options)
    raise ValueError(f"Choose one of: {valid}.")


def choice_title(options: list[tuple[Any, str, str]], value: Any) -> str:
    return next((title for option, title, _desc in options if option == value), str(value))


# ── answers files ────────────────────────────────────────────────────────────


def flatten_answers(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """{"business": {"website": x}} -> {"business.website": x} (nested tables recurse)."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flat.update(flatten_answers(value, full))
        else:
            flat[full] = value
    return flat


def _looks_secret(key: str) -> bool:
    """`password`, `api_key`, `brave_api_key`, `access_token`, `client_secret`...: anything
    named like a credential, except the `*_env` keys that only name an environment variable."""
    last = key.rsplit(".", 1)[-1].lower()
    return not last.endswith("_env") and (last in _SECRET_NAMES or last.endswith(_SECRET_SUFFIXES))


def unknown_answers(flat: Mapping[str, Any], known: Iterable[str]) -> dict[str, str | None]:
    """{unknown key: the key probably meant, or None}. A key with the right name in the wrong
    table ("brand.website") points at the real one; otherwise the closest spelling wins."""
    known_keys = sorted(set(known))
    by_name: dict[str, list[str]] = {}
    for key in known_keys:
        by_name.setdefault(key.rsplit(".", 1)[-1], []).append(key)
    result: dict[str, str | None] = {}
    for key in flat:
        if key in known_keys:
            continue
        same_name = by_name.get(key.rsplit(".", 1)[-1])
        if same_name:
            result[key] = same_name[0]
            continue
        close = difflib.get_close_matches(key, known_keys, n=1, cutoff=0.75)
        result[key] = close[0] if close else None
    return result


def load_answers(path: str) -> dict[str, Any]:
    """Read a setup answers TOML file into the flat dict `UI(answers=...)` expects.

    Refuses files that contain passwords: answers files get shared with AI agents and
    pasted into chats, so secrets must come from environment variables (`*_env` keys).
    Raises ValueError with a message fit to show the user.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ValueError(f"Answers file not found: {path}") from None
    except OSError as exc:
        raise ValueError(f"Can't read the answers file {path}: {exc.strerror or exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} has a formatting error: {exc}") from None
    flat = flatten_answers(data)
    secrets = [key for key in flat if _looks_secret(key)]
    if secrets:
        listed = ", ".join(secrets)
        raise ValueError(
            f"Don't put passwords in the answers file ({listed}). Put each one in an environment "
            'variable and name it with the matching *_env key, e.g. email.password_env = "SDR_EMAIL_PASSWORD".'
        )
    return flat
