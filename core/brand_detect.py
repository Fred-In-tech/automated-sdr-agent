"""Read a business's brand from its homepage: name, tagline, logo and colours.

Why: the setup wizard asks for the website first and pre-fills the rest, so a new user confirms
their brand instead of typing hex codes. Branded emails, the signature logo and the dashboard all
read these values from [email_design] / [sender] in config/profile.toml (the keys returned here
use the same names as [email_design]).

Design choices:
- Stdlib HTML parsing (html.parser): real pages are messy, and we only need <meta>, <link>,
  <title>, <base>, <style> and style="" attributes. The parser tolerates broken markup.
- Everything is optional. A value we can't find falls back to the product defaults and is
  flagged False in `found`, so the wizard knows what to ask about. Network and parse problems
  never raise; they come back as a short, human `error` string.
- Bounded work: one page (<= 1.5 MB), at most two same-site stylesheets (<= 500 KB each) and at
  most two icon probes, each with a 10 s timeout. CSS scanning is linear-time on purpose.
- `fetch(url) -> (status, text, content_type)` is injectable, so tests (and callers with their
  own HTTP stack) never touch the network.
"""

import colorsys
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from functools import partial
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    import requests
except ImportError:  # the module still imports; default_fetch explains what's missing
    requests = None

from core.email_design import DEFAULTS
from core.product import BRAND_COLOR
from core.qualify import clean_company_name, root_domain

log = logging.getLogger(__name__)

Fetch = Callable[[str], tuple[int, str, str]]

TIMEOUT_SECONDS = 10
MAX_PAGE_BYTES = 1_500_000
MAX_CSS_BYTES = 500_000
MAX_CSS_FILES = 2
CHUNK_BYTES = 64 * 1024
TAGLINE_MAX = 90
MIN_CLAUSE = 20            # a clause shorter than this is too thin to stand alone as a tagline
DESCRIPTION_MAX = 300
BACKGROUND_TINT = 0.07     # share of the brand colour mixed into white for the page background
MIN_SATURATION = 0.30      # below this a colour reads as grey, not as a brand colour
MIN_LIGHTNESS, MAX_LIGHTNESS = 0.20, 0.80
MIN_TEXT_CONTRAST = 4.5    # WCAG AA against white: anything lighter is unreadable as body text
MIN_ALPHA = 0.9            # translucent colours are shadows/overlays, not brand colours

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,text/css,image/*;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FIELDS = ("name", "tagline", "description", "logo_url", "brand_color", "heading_color", "text_color",
          "background")
COLOR_FIELDS = ("brand_color", "heading_color", "text_color", "background")

SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)")   # "host:8080" is a port, not a scheme
HEX6_RE = re.compile(r"#[0-9A-F]{6}")
HEX_RE = re.compile(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})")
FUNCTION_RE = re.compile(r"(rgba?|hsla?)\((.*)\)", re.S)
HSL_TRIPLET_RE = re.compile(r"(-?[\d.]+)(?:deg)?\s+([\d.]+)%\s+([\d.]+)%(?:\s*/\s*([\d.]+%?))?")
VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.*))?\)", re.S)
# Bounded name lengths keep the scan linear even on pathological CSS.
DECLARATION_RE = re.compile(r"(--[\w-]{1,80}|[a-zA-Z-]{1,40})\s*:\s*([^;{}]+)")
COLOR_TOKEN_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\([^()]*\)", re.I)
SIZE_RE = re.compile(r"(\d{1,4})\s*[xX×]\s*(\d{1,4})")
SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")

ABBREVIATIONS = {"inc", "co", "corp", "ltd", "llc", "st", "dr", "mr", "mrs", "ms", "vs", "etc", "no",
                 "jr", "sr", "e.g", "i.e", "u.s"}
CLAUSE_SEPARATORS = (" — ", " – ", " - ", ": ", "; ", ", ")
BRAND_WORDS = ("brand", "primary", "accent", "theme", "main")   # custom-property words, best first
NOT_BRAND_WORDS = {
    "foreground", "fg", "text", "contrast", "inverse", "on", "light", "lighter", "lightest", "dark",
    "darker", "darkest", "hover", "active", "focus", "visited", "disabled", "bg", "background",
    "surface", "border", "outline", "ring", "shadow", "muted", "soft", "subtle", "pale", "alpha",
    "opacity", "rgb", "hsl", "font", "size", "width", "radius",
    "50", "100", "200", "300", "400", "700", "800", "900", "950",   # tints/shades, not the base colour
}
# WordPress and friends load library CSS first; the site's own theme says more about the brand.
LIBRARY_CSS_HINTS = ("wp-includes", "block-library", "/plugins/", "/vendor/", "bootstrap", "fontawesome",
                     "font-awesome", "normalize", "reset")


# ─────────────────────────────────────────────────────────────────────────────
# URLs and fetching
# ─────────────────────────────────────────────────────────────────────────────

def normalize_url(s: str | None) -> str:
    """'  acme .com ' -> 'https://acme.com'. People paste addresses with stray spaces/newlines and
    without a scheme; whitespace is removed, https:// added, scheme + host lowercased. Other
    schemes (javascript:, file:, ftp:) are left as-is so callers can reject them."""
    text = "".join((s or "").split())
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif not SCHEME_RE.match(text):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if parts.scheme.lower() not in ("http", "https"):
        return text
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _hostname(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _join(base: str, href: str) -> str:
    try:
        return urljoin(base, href.strip())
    except ValueError:
        return ""


def _read_capped(response, max_bytes: int) -> bytes:
    """At most `max_bytes`, and at most TIMEOUT_SECONDS in total: requests' timeout applies per
    read, so a server dripping bytes could otherwise hold the setup wizard forever."""
    chunks, size, started = [], 0, time.monotonic()
    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
        piece = chunk[: max_bytes - size]
        chunks.append(piece)
        size += len(piece)
        if size >= max_bytes or time.monotonic() - started > TIMEOUT_SECONDS:
            break
    return b"".join(chunks)


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=[\"']?([\w.:-]+)", content_type or "", re.I)
    try:
        return body.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:  # a charset Python doesn't know: UTF-8 is right for nearly every site
        return body.decode("utf-8", errors="replace")


def default_fetch(url: str, max_bytes: int = MAX_PAGE_BYTES) -> tuple[int, str, str]:
    """GET `url` like a desktop browser -> (status, text, content_type). Only http(s); the body is
    capped at `max_bytes`. Raises on network errors; detect_brand turns those into defaults."""
    if not _is_http_url(url):
        raise ValueError(f"only http(s) addresses can be fetched, not {url!r}")
    if requests is None:
        raise RuntimeError("the 'requests' package is missing: run pip install -r requirements.txt")
    with requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS, stream=True) as response:
        content_type = response.headers.get("Content-Type", "")
        body = _read_capped(response, max_bytes)
        return response.status_code, _decode(body, content_type), content_type


def _describe_error(exc: Exception) -> str:
    """Plain-English reason, without leaking stack traces into the wizard."""
    kind = type(exc).__name__.lower()
    if "timeout" in kind:
        return "the site took too long to answer"
    if "ssl" in kind:
        return "its secure connection (SSL certificate) failed"
    if "connection" in kind:
        return "couldn't connect, check the address"
    if isinstance(exc, ValueError):
        return "that doesn't look like a valid address"
    return "the request failed"


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

class _PageParser(HTMLParser):
    """Collects the handful of tags brand detection needs and ignores everything else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: list[dict] = []
        self.links: list[dict] = []
        self.base_href = ""
        self.title = ""
        self.styles: list[str] = []          # <style> contents and style="" attributes
        self._title_parts: list[str] | None = None
        self._style_parts: list[str] | None = None
        self._svg_depth = 0                  # <svg><title> is an icon label, not the page title

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if attributes.get("style"):
            self.styles.append(attributes["style"])
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "meta":
            self.metas.append(attributes)
        elif tag == "link":
            self.links.append(attributes)
        elif tag == "base" and not self.base_href:
            self.base_href = attributes.get("href", "")
        elif tag == "title" and not self.title and self._svg_depth == 0:
            self._title_parts = []
        elif tag == "style":
            self._style_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
        elif tag == "title" and self._title_parts is not None:
            self.title = " ".join("".join(self._title_parts).split())
            self._title_parts = None
        elif tag == "style" and self._style_parts is not None:
            self.styles.append("".join(self._style_parts))
            self._style_parts = None

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._style_parts is not None:
            self._style_parts.append(data)

    def flush(self) -> None:
        """Keep what an unclosed <title>/<style> held (truncated pages are common)."""
        self.handle_endtag("title")
        self.handle_endtag("style")


def _parse_page(text: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(text)
    parser.close()
    parser.flush()
    return parser


def _meta_content(page: _PageParser, *keys: str) -> str:
    """First non-empty <meta property|name=key content> for the keys, in priority order."""
    for key in keys:
        for meta in page.metas:
            names = {meta.get("property", "").strip().lower(), meta.get("name", "").strip().lower()}
            content = " ".join(meta.get("content", "").split())
            if key in names and content:
                return content
    return ""


def _link_rels(link: dict) -> list[str]:
    return link.get("rel", "").lower().split()


# ─────────────────────────────────────────────────────────────────────────────
# Name, tagline, description
# ─────────────────────────────────────────────────────────────────────────────

def _detect_name(page: _PageParser) -> str:
    for candidate in (_meta_content(page, "og:site_name"), _meta_content(page, "application-name"), page.title):
        name = clean_company_name(candidate, "", "") if candidate else ""
        if name:
            return name
    return ""


def _domain_name(url: str) -> str:
    """'https://www.acme-films.com' -> 'Acme Films': a sensible default the user can correct."""
    host = _hostname(url)
    host = host[4:] if host.startswith("www.") else host
    return clean_company_name("", "", host) if host else ""


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:—–-") + "…"


def _first_sentence(text: str) -> str:
    text = text[:1000]
    for match in SENTENCE_END_RE.finditer(text):
        words = text[: match.start()].split()
        last_word = words[-1].lower().strip("(\"'") if words else ""
        if text[match.start()] == "." and last_word in ABBREVIATIONS:
            continue
        return text[: match.end()]
    return text


def _tagline(description: str) -> str:
    """First sentence, <= 90 chars (it sits beside the brand name in email footers). Too long:
    cut at the first natural clause break, else at a word boundary."""
    sentence = _first_sentence(description).strip()
    if sentence.endswith(".") and not sentence.endswith(".."):
        sentence = sentence[:-1].rstrip()
    if len(sentence) <= TAGLINE_MAX:
        return sentence
    for separator in CLAUSE_SEPARATORS:
        index = sentence.find(separator, MIN_CLAUSE)
        if 0 <= index <= TAGLINE_MAX:
            return sentence[:index].rstrip()
    return _shorten(sentence, TAGLINE_MAX)


# ─────────────────────────────────────────────────────────────────────────────
# Logo
# ─────────────────────────────────────────────────────────────────────────────

def _icon_size(link: dict) -> int:
    """Largest dimension from sizes="180x180", else from a file name like icon-512x512.png."""
    dims = SIZE_RE.findall(link.get("sizes", "")) or SIZE_RE.findall(link.get("href", ""))
    return max((max(int(w), int(h)) for w, h in dims), default=0)


def _icon_format(link: dict) -> str:
    kind = link.get("type", "").lower()
    path = link.get("href", "").split("?")[0].split("#")[0].lower()
    if "png" in kind or path.endswith(".png"):
        return "png"
    if "svg" in kind or path.endswith(".svg"):
        return "svg"
    return "other"


def _logo_hrefs(page: _PageParser) -> list[str]:
    """Declared icons, best first: apple-touch-icons (largest), then icons: PNG by size, then SVG
    (Gmail and Outlook don't display SVG images in email), then anything else (.ico, .gif)."""
    apple_rels = {"apple-touch-icon", "apple-touch-icon-precomposed"}
    apple = [link for link in page.links if apple_rels & set(_link_rels(link))]
    icons = [link for link in page.links if "icon" in _link_rels(link)]
    format_rank = {"png": 0, "svg": 1, "other": 2}
    ordered = (sorted(apple, key=lambda link: -_icon_size(link))
               + sorted(icons, key=lambda link: (format_rank[_icon_format(link)], -_icon_size(link))))
    return [link["href"].strip() for link in ordered if link.get("href", "").strip()]


def _absolute_https(base: str, href: str) -> str:
    """Absolute https URL, or "" for data:/javascript: and friends. Email clients block or warn on
    plain-http images, and nearly every site serves the same file over https."""
    absolute = _join(base, href)
    if not _is_http_url(absolute):
        return ""
    return "https://" + absolute.split("://", 1)[1]


def _probe_image(fetch: Fetch, url: str, accepted: tuple[str, ...]) -> bool:
    """True when `url` answers 200 with an image type. Single-page apps answer 200 text/html for
    every path, so a bare 200 isn't proof the icon exists."""
    try:
        status, _, content_type = fetch(url)
    except Exception as exc:  # a missing icon is normal; never let it break detection
        log.debug("brand detection: probing %s failed: %r", url, exc)
        return False
    content_type = str(content_type or "").lower()
    return status == 200 and any(kind in content_type for kind in accepted)


def _detect_logo(page: _PageParser, base: str, page_url: str, fetch: Fetch) -> str:
    for href in _logo_hrefs(page):
        url = _absolute_https(base, href)
        if url:
            return url
    origin = "https://" + urlsplit(page_url).netloc
    probes = (("/apple-touch-icon.png", ("image/",)), ("/favicon.ico", ("image/", "icon", "octet-stream")))
    for path, accepted in probes:
        if _probe_image(fetch, origin + path, accepted):
            return origin + path
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────────────────────────────────────

def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def _to_hex(red: float, green: float, blue: float) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in (red, green, blue))


def _fraction(token: str) -> float:
    """'83%' -> 0.83; bare numbers above 1 are percentages too (CSS Color 4 allows hsl(262 83 58))."""
    if token.endswith("%"):
        return float(token[:-1]) / 100
    value = float(token)
    return value / 100 if value > 1 else value


def _hsl_to_hex(hue: str, saturation: str, lightness: str) -> str:
    h = (float(hue.lower().removesuffix("deg")) % 360) / 360
    red, green, blue = colorsys.hls_to_rgb(h, _fraction(lightness), _fraction(saturation))
    return _to_hex(red * 255, green * 255, blue * 255)


def _from_function(name: str, args: str) -> str | None:
    parts = [part for part in re.split(r"[\s,/]+", args.strip()) if part]
    if len(parts) not in (3, 4):
        return None
    if len(parts) == 4 and _fraction(parts[3]) < MIN_ALPHA:
        return None
    if name.startswith("rgb"):
        channels = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p) for p in parts[:3]]
        return _to_hex(*channels)
    return _hsl_to_hex(parts[0], parts[1], parts[2])


def _parse_color(value: str) -> str | None:
    """'#f60', 'rgb(228 87 46)', 'hsl(262,83%,58%)', or a shadcn-style '262 83% 58%' -> '#RRGGBB'.
    None for anything else, including translucent colours."""
    text = value.lower().replace("!important", "").strip()
    try:
        if match := HEX_RE.fullmatch(text):
            digits = match.group(1)
            if len(digits) in (3, 4):
                digits = "".join(ch * 2 for ch in digits)
            if len(digits) == 8 and int(digits[6:], 16) / 255 < MIN_ALPHA:
                return None
            return "#" + digits[:6].upper()
        if match := FUNCTION_RE.fullmatch(text):
            return _from_function(match.group(1), match.group(2))
        if match := HSL_TRIPLET_RE.fullmatch(text):
            if match.group(4) and _fraction(match.group(4)) < MIN_ALPHA:
                return None
            return _hsl_to_hex(match.group(1), match.group(2) + "%", match.group(3) + "%")
    except ValueError:  # "1.2.3" and other malformed numbers
        return None
    return None


def _resolve(value: str, variables: dict, depth: int = 0) -> str | None:
    """A custom-property value, following var(--x, fallback) references a few levels deep."""
    match = VAR_RE.fullmatch(value.strip())
    if not match:
        return _parse_color(value)
    if depth >= 5:
        return None
    target = variables.get(match.group(1))
    resolved = _resolve(target, variables, depth + 1) if target is not None else None
    if resolved is None and match.group(2):
        resolved = _resolve(match.group(2), variables, depth + 1)
    return resolved


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    red, green, blue = _rgb(hex_color)
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast_with_white(hex_color: str) -> float:
    return 1.05 / (_luminance(hex_color) + 0.05)


def _is_chromatic(hex_color: str) -> bool:
    """A colour that reads as a brand colour: clearly saturated, neither near-white nor near-black."""
    _, lightness, saturation = colorsys.rgb_to_hls(*(c / 255 for c in _rgb(hex_color)))
    return saturation >= MIN_SATURATION and MIN_LIGHTNESS <= lightness <= MAX_LIGHTNESS


def _tint(hex_color: str, amount: float) -> str:
    """Mix `amount` of the colour into white: a background that belongs to the brand."""
    return _to_hex(*(amount * c + (1 - amount) * 255 for c in _rgb(hex_color)))


def _strip_comments(css: str) -> str:
    """Linear-time comment removal (a regex backtracks badly on unterminated comments)."""
    out, pos = [], 0
    while True:
        start = css.find("/*", pos)
        if start < 0:
            out.append(css[pos:])
            break
        out.append(css[pos:start])
        end = css.find("*/", start + 2)
        if end < 0:  # unterminated: browsers ignore the rest too
            break
        pos = end + 2
    return " ".join(out)


def _scan_css(css_texts: list[str]) -> tuple[Counter, Counter, dict]:
    """(all colours, colours used for text, custom properties) across the CSS. Only declaration
    values are scanned, so id selectors like #add or #fab are never mistaken for colours."""
    all_colors, text_colors, variables = Counter(), Counter(), {}
    for css in css_texts:
        for prop, value in DECLARATION_RE.findall(_strip_comments(css)):
            value = value.strip()
            if prop.startswith("--"):
                variables.setdefault(prop, value)   # :root (light theme) usually comes first
            for token in COLOR_TOKEN_RE.findall(value):
                color = _parse_color(token)
                if color:
                    all_colors[color] += 1
                    if prop.lower() == "color":
                        text_colors[color] += 1
    return all_colors, text_colors, variables


def _variable_rank(name: str) -> int | None:
    """0 for --brand, 1 for --primary / --color-primary / --primaryColor, ...; None for anything
    that isn't the base brand colour (--primary-foreground, --accent-light, --primary-100)."""
    words = [w for w in re.split(r"[-_]+", re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()) if w]
    if any(word in NOT_BRAND_WORDS for word in words):
        return None
    return next((rank for rank, word in enumerate(BRAND_WORDS) if word in words), None)


def _brand_variables(variables: dict) -> list[str]:
    ranked = []
    for order, name in enumerate(variables):
        rank = _variable_rank(name)
        if rank is not None:
            ranked.append((rank, order, name))
    colors = (_resolve(variables[name], variables) for _, _, name in sorted(ranked))
    return [color for color in colors if color]


def _theme_colors(page: _PageParser) -> list[str]:
    """<meta name=theme-color>, light-mode first, then msapplication-TileColor."""
    def rank(meta: dict) -> int:
        media = meta.get("media", "").lower()
        return 0 if not media else 1 if "light" in media else 2
    theme = sorted((m for m in page.metas if m.get("name", "").strip().lower() == "theme-color"), key=rank)
    tile = [m for m in page.metas if m.get("name", "").strip().lower() == "msapplication-tilecolor"]
    colors = (_parse_color(meta.get("content", "")) for meta in theme + tile)
    return [color for color in colors if color]


def _detect_brand_color(page: _PageParser, all_colors: Counter, variables: dict) -> str:
    """Declared colours (theme-color, then --brand/--primary/...) win when colourful; then the most
    frequent colourful value in the CSS; then a dark declared colour (black/navy brands) that can
    still carry white button text."""
    declared = _theme_colors(page) + _brand_variables(variables)
    frequent = [color for color, _ in all_colors.most_common()]
    for color in declared + frequent:
        if _is_chromatic(color):
            return color
    return next((c for c in declared if _contrast_with_white(c) >= MIN_TEXT_CONTRAST), "")


def _detect_text_colors(text_colors: Counter) -> tuple[str, str]:
    """(heading, text) from the colours used for text that stay readable on white: heading is the
    darkest of the three most frequent, text the most frequent of the rest."""
    dark = [color for color, _ in text_colors.most_common() if _contrast_with_white(color) >= MIN_TEXT_CONTRAST]
    if not dark:
        return "", ""
    heading = min(dark[:3], key=_luminance)
    return heading, next((color for color in dark if color != heading), heading)


def _stylesheet_urls(page: _PageParser, base: str, page_url: str) -> list[str]:
    """Up to two same-site stylesheets (third-party CSS says nothing about this brand)."""
    site = root_domain(_hostname(page_url))
    urls = []
    for link in page.links:
        rels, href = _link_rels(link), link.get("href", "").strip()
        if "stylesheet" not in rels or "alternate" in rels or not href:
            continue
        url = _join(base, href)
        if _is_http_url(url) and root_domain(_hostname(url)) == site and url not in urls:
            urls.append(url)
    urls.sort(key=lambda url: any(hint in url.lower() for hint in LIBRARY_CSS_HINTS))
    return urls[:MAX_CSS_FILES]


def _linked_css(page: _PageParser, base: str, page_url: str, fetch: Fetch) -> list[str]:
    sheets = []
    for url in _stylesheet_urls(page, base, page_url):
        try:
            status, text, content_type = fetch(url)
        except Exception as exc:  # one broken stylesheet shouldn't lose what the page told us
            log.debug("brand detection: fetching %s failed: %r", url, exc)
            continue
        if status == 200 and isinstance(text, str) and "html" not in str(content_type or "").lower():
            sheets.append(text[:MAX_CSS_BYTES])
    return sheets


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def _analyse(page_url: str, html_text: str, asset_fetch: Fetch) -> dict:
    page = _parse_page(html_text)
    base = _join(page_url, page.base_href) if page.base_href else page_url
    base = base if _is_http_url(base) else page_url
    description = _meta_content(page, "og:description", "description", "twitter:description")
    all_colors, text_colors, variables = _scan_css(page.styles + _linked_css(page, base, page_url, asset_fetch))
    brand = _detect_brand_color(page, all_colors, variables)
    heading, text = _detect_text_colors(text_colors)
    return {
        "name": _detect_name(page),
        "tagline": _tagline(description),
        "description": _shorten(description, DESCRIPTION_MAX),
        "logo_url": _detect_logo(page, base, page_url, asset_fetch),
        "brand_color": brand,
        "heading_color": heading,
        "text_color": text,
        "background": _tint(brand, BACKGROUND_TINT) if brand else "",
    }


def _defaults(url: str) -> dict:
    return {
        "name": _domain_name(url), "tagline": "", "description": "", "logo_url": "",
        "brand_color": BRAND_COLOR.upper(), "heading_color": DEFAULTS["heading_color"],
        "text_color": DEFAULTS["text_color"], "background": DEFAULTS["background"],
    }


def _result(url: str, detected: dict, error: str = "") -> dict:
    defaults = _defaults(url)
    values, found = {}, {}
    for field in FIELDS:
        value = detected.get(field) or ""
        if field in COLOR_FIELDS and not HEX6_RE.fullmatch(value):
            value = ""  # contract: colours are always #RRGGBB
        found[field] = bool(value)
        values[field] = value or defaults[field]
    return {"url": url, **values, "found": found, "error": error}


def detect_brand(url: str, fetch: Fetch | None = None) -> dict:
    """Brand of the website at `url`:
    {"url", "name", "tagline", "description", "logo_url", "brand_color", "heading_color",
     "text_color", "background", "found": {field: bool}, "error": str}.
    Missing values are defaults (found[field] = False). Never raises: unreachable sites, bad
    addresses and odd markup give defaults plus a human-readable `error` ("" on success).
    `url` is only fetched when it's http(s); the returned "url" is "" when it isn't."""
    page_url = normalize_url(url)
    if not _is_http_url(page_url):
        return _result("", {}, "Please enter a website address like https://yourcompany.com.")
    page_fetch = fetch or default_fetch
    asset_fetch = fetch or partial(default_fetch, max_bytes=MAX_CSS_BYTES)
    host = _hostname(page_url)
    try:
        status, text, content_type = page_fetch(page_url)
    except Exception as exc:  # any network/client error: defaults beat crashing the wizard
        log.debug("brand detection: fetching %s failed: %r", page_url, exc)
        return _result(page_url, {}, f"Couldn't reach {host}: {_describe_error(exc)}.")
    if not isinstance(status, int) or not 200 <= status < 300:
        return _result(page_url, {}, f"{host} answered with HTTP {status}.")
    mime = str(content_type or "").split(";")[0].strip().lower()
    if mime and "html" not in mime:
        return _result(page_url, {}, f"{host} didn't return a web page ({mime}).")
    try:
        detected = _analyse(page_url, text if isinstance(text, str) else "", asset_fetch)
    except Exception as exc:  # markup we didn't anticipate: defaults beat crashing the wizard
        log.debug("brand detection: reading %s failed: %r", page_url, exc)
        return _result(page_url, {}, f"Couldn't read the page at {host}.")
    return _result(page_url, detected)
