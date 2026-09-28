"""Web search for the lead finder: one call, three engines.

`search_web(query)` returns [{"title", "url"}] from the engine chosen in config/profile.toml:

    [targeting]
    search_engine = "auto"     # "auto" | "brave" | "duckduckgo" | "bing"

- brave: the official Brave Search API (free key at https://brave.com/search/api/, kept in .env as
  BRAVE_API_KEY). The most reliable choice; the free plan allows about one search a second.
- duckduckgo: DuckDuckGo's plain-HTML results page, which its robots.txt lets bots read. No key,
  but it shows a captcha when one place searches a lot, so we stay slow (one search every 2 s).
- bing: the original Bing scraping. Bing's robots.txt forbids bots on /search, so it only runs
  when the owner explicitly picked it, and the activity log says so once per run.
- auto (default): brave when BRAVE_API_KEY is set, else duckduckgo. Never bing.

Rules this module keeps:
- search_web never raises: problems come back as [] plus one friendly line in the activity log.
- Every request asks robots.txt first (core/robots.py). The single exception is Bing's /search
  page when the owner chose "bing" — that is exactly what choosing Bing means.
- Rate limits, captchas, a rejected key or a robots.txt "no" stop that engine for the rest of the
  run (start_search_run() starts a fresh one), so a blocked engine isn't hit 30 more times. Brave
  gets one polite retry for its per-second limit, unless its monthly quota is used up.
- Brave requests never follow redirects. The API key travels in a custom header that `requests`
  would forward to whatever host a 3xx pointed at (it strips only `Authorization`), so a redirect
  is treated as a failed search instead of a second request.
- HTTP, robots.txt, sleep and clock are injectable (SearchClient), so tests never touch the
  network or wait.
"""

import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from html import unescape
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

try:
    import requests
except ImportError:  # the module still imports; http_get explains what's missing
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from core.config import ProfileError, load_profile
from core.db import log_event
from core.robots import SCRAPER_HEADERS, RobotsRules

log = logging.getLogger(__name__)

Fetch = Callable[[str, dict[str, str]], tuple[int, str, dict[str, str]]]

BRAVE_KEY_ENV = "BRAVE_API_KEY"
BRAVE_SIGNUP_URL = "https://brave.com/search/api/"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_COUNT = 20
# Header names that carry a credential. `requests` drops only `Authorization` when a redirect leaves
# the original host, so a request carrying one of these follows no redirects at all (see http_get).
CREDENTIAL_HEADERS = {"x-subscription-token"}
DDG_URL = "https://html.duckduckgo.com/html/"
BING_URL = "https://www.bing.com/search"
BING_HOSTS = {"www.bing.com", "bing.com"}

ENGINES = ("brave", "duckduckgo", "bing")
SETTINGS = ("auto",) + ENGINES
LABELS = {"brave": "Brave Search", "duckduckgo": "DuckDuckGo", "bing": "Bing"}
ALIASES = {"": "auto", "ddg": "duckduckgo"}

MIN_INTERVAL_SECONDS = {"brave": 1.1, "duckduckgo": 2.0, "bing": 1.5}   # between two searches, per engine
RETRY_WAIT_SECONDS = 2.0        # Brave 429 without a reset hint
MAX_RETRY_WAIT_SECONDS = 10     # a longer wait means "try next run", not "sleep now"
MAX_FAILURES = 3                # network errors in a row before an engine is skipped for the run
TIMEOUT_SECONDS = 15
MAX_RESPONSE_CHARS = 2_000_000
DEFAULT_MAX_RESULTS = 8

# Signs of DuckDuckGo's "are you a robot?" page. Only trusted when the page has no results, so a
# snippet that happens to contain one of these words can't stop a run.
DDG_BLOCK_STATUSES = {202, 403, 418, 429}
DDG_ANOMALY_MARKERS = ("anomaly-modal", "/anomaly.js", 'id="challenge-form"', "bots use duckduckgo too")

SWITCH_HINT = "Switch with `sdr setup --section audience` (or [targeting] search_engine in config/profile.toml)."
BING_WARNING = ('Lead search is using Bing because config/profile.toml says search_engine = "bing". '
                "Bing's robots.txt asks bots not to use its search pages, so this breaks Bing's rules and "
                "Bing may block you. Brave Search (free key) and DuckDuckGo follow the rules. " + SWITCH_HINT)
BRAVE_NO_KEY = ('search_engine is "brave" but BRAVE_API_KEY isn\'t set in .env, so lead search uses '
                "DuckDuckGo for now. Add your free key with `sdr setup --section audience`.")
BRAVE_LIMIT = ("Brave Search says we've reached its limit (the free plan allows about one search a second, "
               "plus a monthly total), so lead search stopped for this run. It will try again next run.")
BRAVE_KEY_REJECTED = ("Brave Search rejected the API key in .env (BRAVE_API_KEY). Check it at "
                      + BRAVE_SIGNUP_URL + " and add it again with `sdr setup --section audience`.")
BRAVE_REDIRECTED = ("Brave Search answered with a redirect, which we don't follow while the API key is attached "
                    "(it would hand the key to whichever site the redirect points at)")
DDG_BLOCKED = ("DuckDuckGo asked us to prove we're human (it does this when one place searches a lot), so "
               "lead search stopped for this run. It will try again next run. For steadier results, get a "
               "free Brave Search key at " + BRAVE_SIGNUP_URL + " and add it with `sdr setup --section audience`.")
BING_BLOCKED = "Bing is refusing our searches, so lead search stopped for this run. " + SWITCH_HINT
ROBOTS_BLOCKED = ("{label}'s robots.txt asks bots not to read its search results, so lead search stopped "
                  "for this run. " + SWITCH_HINT)


class SearchError(Exception):
    """One search failed (network, HTTP error, unreadable reply). The next query may work."""


class SearchStopped(Exception):
    """The engine said stop (rate limit, captcha, bad key, robots.txt): skip it for the rest of the run."""


# ─────────────────────────────────────────────────────────────────────────────
# Which engine
# ─────────────────────────────────────────────────────────────────────────────

def engine_setting(profile: dict | None) -> str:
    """The owner's [targeting] search_engine, normalised ("DDG " -> "duckduckgo", missing -> "auto")."""
    raw = ((profile or {}).get("targeting") or {}).get("search_engine", "auto")
    value = " ".join(str(raw or "").lower().split())
    return ALIASES.get(value, value)


def choose_engine(profile: dict | None, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(engine, note): the engine to use now, and a friendly line when the setting can't be honoured
    as written. "auto" never picks Bing: that needs the owner's explicit choice."""
    env = os.environ if env is None else env
    setting = engine_setting(profile)
    has_key = bool(str(env.get(BRAVE_KEY_ENV, "")).strip())
    if setting in ("duckduckgo", "bing"):
        return setting, ""
    if setting == "brave":
        return ("brave", "") if has_key else ("duckduckgo", BRAVE_NO_KEY)
    engine = "brave" if has_key else "duckduckgo"
    if setting == "auto":
        return engine, ""
    return engine, (f"config/profile.toml has search_engine = {setting!r}, which isn't one we know, so lead "
                    f"search uses {LABELS[engine]}. Choose auto, brave, duckduckgo or bing.")


def describe_search(profile: dict | None = None, env: Mapping[str, str] | None = None) -> dict:
    """What `sdr doctor` and the dashboard show: {"setting", "engine", "label", "has_brave_key", "note"}.
    Offline: it never checks the key (see check_brave_key)."""
    env = os.environ if env is None else env
    engine, note = choose_engine(profile, env)
    return {"setting": engine_setting(profile), "engine": engine, "label": LABELS[engine],
            "has_brave_key": bool(str(env.get(BRAVE_KEY_ENV, "")).strip()), "note": note}


def robots_exempt(engine: str, url: str) -> bool:
    """True only for Bing's own /search page while Bing is the chosen engine. Every other request —
    including other bing.com pages, and Bing's search page under any other engine — asks robots.txt."""
    if engine != "bing":
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc.lower() in BING_HOSTS and parts.path == "/search"


# ─────────────────────────────────────────────────────────────────────────────
# Reading each engine's answer (pure functions: easy to test with saved pages)
# ─────────────────────────────────────────────────────────────────────────────

def _text(tag) -> str:
    """A tag's visible text with whitespace collapsed ("Acme <b>Studio</b>" -> "Acme Studio")."""
    return " ".join(tag.get_text().split())


def _soup(markup: str):
    if BeautifulSoup is None:
        raise SearchError("the 'beautifulsoup4' package is missing (pip install -r requirements.txt)")
    return BeautifulSoup(markup or "", "html.parser")


def plain_text(value) -> str:
    """Brave titles can carry <strong> highlights and HTML entities; keep just the words."""
    return " ".join(unescape(re.sub(r"<[^>]*>", "", str(value or ""))).split())


def parse_brave(text: str) -> list[dict]:
    """Brave Search API JSON -> [{"title", "url"}] from web.results[]."""
    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        raise SearchError("Brave Search sent a reply we couldn't read") from exc
    web = data.get("web") if isinstance(data, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    return [{"title": plain_text(item.get("title")), "url": str(item.get("url") or "")}
            for item in (results or []) if isinstance(item, dict)]


def decode_ddg_url(href: str) -> str:
    """DuckDuckGo links go through //duckduckgo.com/l/?uddg=<real url, percent-encoded>. Returns the
    real address, or "" for ads (/y.js) and DuckDuckGo's own pages."""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
    except ValueError:
        return ""
    host = parts.netloc.lower()
    if host and host != "duckduckgo.com" and not host.endswith(".duckduckgo.com"):
        return href
    if parts.path.startswith("/l/"):
        return (parse_qs(parts.query).get("uddg") or [""])[0]
    return ""


def parse_duckduckgo(markup: str) -> list[dict]:
    """html.duckduckgo.com results page -> [{"title", "url"}]; sponsored results are skipped."""
    results = []
    for a in _soup(markup).select("a.result__a"):
        box = a.find_parent(class_="result")
        if box is not None and "result--ad" in (box.get("class") or []):
            continue
        url = decode_ddg_url(a.get("href", ""))
        if url:
            results.append({"title": _text(a), "url": url})
    return results


def is_ddg_anomaly(status: int, markup: str) -> bool:
    """True for DuckDuckGo's captcha / "unusual traffic" page (call it only when no results parsed)."""
    if status in DDG_BLOCK_STATUSES:
        return True
    page = (markup or "").lower()
    return any(marker in page for marker in DDG_ANOMALY_MARKERS)


def decode_bing_url(href: str) -> str:
    """Bing wraps results as bing.com/ck/a?...&u=a1<base64 url>...; unwrap to the real address
    (the link itself when it doesn't decode to one). Read from the raw query, not parse_qs, because
    "+" is a base64 character there, not a space."""
    try:
        for field in urlsplit(href).query.split("&"):
            if field.startswith("u=a1"):
                encoded = unquote(field[4:])
                padded = encoded + "=" * (-len(encoded) % 4)
                url = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
                return url if url.startswith(("http://", "https://")) else href
    except ValueError:  # includes binascii.Error
        pass
    return href


def parse_bing(markup: str) -> list[dict]:
    """Bing results page -> [{"title", "url"}] from each li.b_algo's heading link."""
    results = []
    for item in _soup(markup).find_all("li", class_="b_algo"):
        h2 = item.find("h2")
        a = h2.find("a") if h2 else None
        if not a:
            continue
        href = a.get("href", "")
        url = decode_bing_url(href) if "bing.com/ck" in href else href
        results.append({"title": _text(a), "url": url})
    return results


def clean_results(results: list[dict], limit: int) -> list[dict]:
    """Only http(s) links with a host, each once, at most `limit`, as plain {"title", "url"}."""
    clean, seen = [], set()
    for item in results:
        url = str(item.get("url") or "").strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.netloc or url in seen:
            continue
        seen.add(url)
        clean.append({"title": " ".join(str(item.get("title") or "").split()), "url": url})
        if len(clean) >= limit:
            break
    return clean


def _numbers(value) -> list[int]:
    return [int(part) for part in str(value or "").split(",") if part.strip().isdigit()]


def retry_wait(headers: Mapping[str, str] | None) -> float | None:
    """Seconds a Brave 429 asks us to wait, or None when waiting won't help this run (the monthly
    quota is used up, or the reset is further away than MAX_RETRY_WAIT_SECONDS).
    Brave sends "X-RateLimit-Remaining: <per second>, <per month>" and the same for -Reset."""
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    if 0 in _numbers(headers.get("x-ratelimit-remaining"))[1:]:
        return None
    reset = _numbers(headers.get("retry-after")) or _numbers(headers.get("x-ratelimit-reset"))
    wait = reset[0] if reset else RETRY_WAIT_SECONDS
    return None if wait > MAX_RETRY_WAIT_SECONDS else max(float(wait), 1.0)


def brave_url(query: str, count: int) -> str:
    return BRAVE_URL + "?" + urlencode({"q": query, "count": max(1, min(int(count), BRAVE_MAX_COUNT))})


def brave_headers(api_key: str) -> dict[str, str]:
    return {"Accept": "application/json", "X-Subscription-Token": api_key}


def http_get(url: str, headers: dict[str, str]) -> tuple[int, str, dict[str, str]]:
    """GET -> (status, text, headers with lower-case names). Raises on network errors.
    Redirects are followed (Bing needs that) unless the headers carry a credential: then a 3xx is
    returned as-is, so the key never travels to a host we didn't ask for."""
    if requests is None:
        raise SearchError("the 'requests' package is missing (pip install -r requirements.txt)")
    follow = not any(name.lower() in CREDENTIAL_HEADERS for name in headers)
    r = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS, allow_redirects=follow)
    return r.status_code, r.text[:MAX_RESPONSE_CHARS], {k.lower(): v for k, v in r.headers.items()}


def _saved_profile() -> dict:
    """The owner's profile, or {} (= auto) when there isn't a valid one yet."""
    try:
        return load_profile()
    except (ProfileError, OSError):
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# The client: throttling, robots.txt, stop-for-this-run
# ─────────────────────────────────────────────────────────────────────────────

class SearchClient:
    """Search state for one process: when each engine was last used (throttle), which engines are
    stopped for this run, and which warnings were already logged this run."""

    def __init__(self, fetch: Fetch | None = None, robots_allowed: Callable[[str], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 env: Mapping[str, str] | None = None):
        self._fetch = fetch or http_get
        self._robots_allowed = robots_allowed or RobotsRules(SCRAPER_HEADERS["User-Agent"]).allowed
        self._sleep = sleep
        self._clock = clock
        self._env = env
        self._last_request: dict[str, float] = {}
        self._stopped: dict[str, str] = {}
        self._failures: dict[str, int] = {}
        self._warned: set[str] = set()

    def start_run(self) -> None:
        """Forget the last run's stops and warnings. Throttle clocks stay: they're about real time."""
        self._stopped.clear()
        self._failures.clear()
        self._warned.clear()

    def stopped_reason(self, profile: dict | None = None) -> str:
        """Why the current engine is stopped for this run ("" if it isn't)."""
        engine, _ = choose_engine(_saved_profile() if profile is None else profile, self._environ())
        return self._stopped.get(engine, "")

    def search(self, query, max_results=DEFAULT_MAX_RESULTS, profile: dict | None = None) -> list[dict]:
        """What search_web() does, with this client's state. Never raises."""
        query = " ".join(str(query or "").split())
        try:
            limit = int(max_results)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_RESULTS
        if not query or limit <= 0:
            return []
        engine, note = choose_engine(_saved_profile() if profile is None else profile, self._environ())
        if note:
            self._warn_once(note)
        if engine in self._stopped:
            return []
        if engine == "bing":
            self._warn_once(BING_WARNING)
        try:
            raw = getattr(self, "_search_" + engine)(query, limit)
        except SearchStopped as stop:
            self._stop(engine, str(stop))
            return []
        except Exception as exc:
            self._fail(engine, exc)
            return []
        self._failures[engine] = 0
        return clean_results(raw, limit)

    # ── engines ──

    def _search_brave(self, query: str, limit: int) -> list[dict]:
        key = str(self._environ().get(BRAVE_KEY_ENV, "")).strip()
        url = brave_url(query, limit)
        for attempt in range(2):
            status, text, headers = self._request("brave", url, brave_headers(key))
            if status != 429:
                break
            wait = retry_wait(headers)
            if attempt or wait is None:
                raise SearchStopped(BRAVE_LIMIT)
            self._sleep(wait)
        if status in (401, 403):
            raise SearchStopped(BRAVE_KEY_REJECTED)
        if 300 <= status < 400:
            raise SearchError(BRAVE_REDIRECTED)
        if status != 200:
            raise SearchError(f"Brave Search answered HTTP {status}")
        return parse_brave(text)

    def _search_duckduckgo(self, query: str, limit: int) -> list[dict]:
        status, text, _ = self._request("duckduckgo", DDG_URL + "?" + urlencode({"q": query}), dict(SCRAPER_HEADERS))
        results = parse_duckduckgo(text) if status == 200 else []
        if not results and is_ddg_anomaly(status, text):
            raise SearchStopped(DDG_BLOCKED)
        if status != 200:
            raise SearchError(f"DuckDuckGo answered HTTP {status}")
        return results

    def _search_bing(self, query: str, limit: int) -> list[dict]:
        status, text, _ = self._request("bing", BING_URL + "?q=" + quote(query), dict(SCRAPER_HEADERS))
        if status == 429:
            raise SearchStopped(BING_BLOCKED)
        if status != 200:
            raise SearchError(f"Bing answered HTTP {status}")
        return parse_bing(text)

    # ── plumbing ──

    def _request(self, engine: str, url: str, headers: dict[str, str]) -> tuple[int, str, dict[str, str]]:
        """robots.txt, then the per-engine throttle, then GET."""
        if not robots_exempt(engine, url) and not self._robots_allowed(url):
            raise SearchStopped(ROBOTS_BLOCKED.format(label=LABELS[engine]))
        last = self._last_request.get(engine)
        if last is not None:
            wait = last + MIN_INTERVAL_SECONDS[engine] - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[engine] = self._clock()
        return self._fetch(url, headers)

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _stop(self, engine: str, message: str) -> None:
        self._stopped[engine] = message
        self._warn(message, action="Stopped")

    def _fail(self, engine: str, exc: Exception) -> None:
        """Count a failed search; after MAX_FAILURES in a row the engine is skipped for this run.
        Only our own SearchError text is shown: a raw exception can carry URLs and headers."""
        count = self._failures.get(engine, 0) + 1
        self._failures[engine] = count
        reason = str(exc) if isinstance(exc, SearchError) else type(exc).__name__
        if count >= MAX_FAILURES:
            self._stop(engine, f"{LABELS[engine]} failed {count} times in a row ({reason}), so lead search "
                               "stopped for this run. It will try again next run.")
        else:
            self._warn(f"{LABELS[engine]} search failed ({reason}); skipped that search.")

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            self._warn(message)

    def _warn(self, message: str, action: str = "Warning") -> None:
        log.warning(message)
        try:
            log_event("LeadSearch", action, "warning", message)
        except Exception as exc:  # the activity log must never break a search
            log.debug("could not write the activity log: %r", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_CLIENT = SearchClient()


def search_web(query: str, max_results: int = DEFAULT_MAX_RESULTS, profile: dict | None = None) -> list[dict]:
    """Search the web with the owner's engine: [{"title", "url"}], at most `max_results`.
    Never raises; problems are logged once and give []. `profile=None` reads config/profile.toml."""
    return _CLIENT.search(query, max_results, profile)


def start_search_run() -> None:
    """Call at the start of each lead search run: engines stopped last run get another chance."""
    _CLIENT.start_run()


def search_stopped(profile: dict | None = None) -> str:
    """Why the owner's engine is stopped for this run, or "" — lets callers skip pointless work."""
    return _CLIENT.stopped_reason(profile)


def check_brave_key(api_key: str | None = None, fetch: Fetch | None = None,
                    robots_allowed: Callable[[str], bool] | None = None) -> tuple[bool, str]:
    """One cheap test search, for `sdr doctor --online`: (key works?, plain-English message).
    Uses BRAVE_API_KEY when `api_key` is None. Never prints or returns the key."""
    key = str(os.environ.get(BRAVE_KEY_ENV, "") if api_key is None else api_key).strip()
    if not key:
        return False, f"No Brave Search key: {BRAVE_KEY_ENV} isn't set in .env."
    client = SearchClient(fetch=fetch, robots_allowed=robots_allowed, env={BRAVE_KEY_ENV: key})
    try:
        status, _, _ = client._request("brave", brave_url("test", 1), brave_headers(key))
    except SearchStopped as stop:
        return False, str(stop)
    except Exception as exc:
        return False, f"Couldn't reach Brave Search ({type(exc).__name__})."
    if status == 200:
        return True, "Brave Search key works."
    if status == 429:
        return True, "Brave Search accepted the key, but you're at its rate limit or monthly quota right now."
    if status in (401, 403):
        return False, BRAVE_KEY_REJECTED
    if 300 <= status < 400:
        return False, (BRAVE_REDIRECTED + ". Something on your network, like a captive portal or a proxy, may be "
                       "rewriting requests.")
    return False, f"Brave Search answered HTTP {status}."
