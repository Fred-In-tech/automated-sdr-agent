"""robots.txt: which pages a website lets the lead finder read.

Why a shared module: two places read other people's websites — the lead finder
(bots/leadgen_pipeline.py, prospects' own sites) and web search (core/search.py, the search
engines). Both must ask robots.txt first, with the same browser identity, and neither may import
the other (core never depends on bots).

Rules follow RFC 9309: one robots.txt per site (scheme + host), reused for at most a day. Only a
successful download can restrict us: a missing file (4xx) means "no rules", and an unreachable one
(timeout, DNS, 5xx) is treated as allowed so a flaky robots.txt never silently empties the lead
pipeline.
"""

import time
from collections.abc import Callable
from urllib import robotparser
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # the module still imports; fetch_robots_txt fails and the site counts as allowed
    requests = None

# How the lead finder identifies itself to websites (a normal desktop browser). robots.txt rules
# are matched against this User-Agent.
SCRAPER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ROBOTS_TIMEOUT_SECONDS = 5
ROBOTS_MAX_BYTES = 500 * 1024           # RFC 9309: parsers must handle at least 500 KiB
ROBOTS_CACHE_SECONDS = 24 * 60 * 60     # RFC 9309: don't reuse a robots.txt for more than a day

RobotsFetch = Callable[[str], tuple[int, str]]


def fetch_robots_txt(robots_url: str) -> tuple[int, str]:
    """Download robots.txt with our normal headers: (status, text). Raises on network errors."""
    if requests is None:
        raise OSError("the 'requests' package is not installed")
    r = requests.get(robots_url, headers=SCRAPER_HEADERS, timeout=ROBOTS_TIMEOUT_SECONDS)
    return r.status_code, r.text[:ROBOTS_MAX_BYTES]


def parse_robots(robots_url: str, fetch: RobotsFetch) -> robotparser.RobotFileParser:
    """Download and parse one site's robots.txt; anything but a 2xx answer allows everything."""
    parser = robotparser.RobotFileParser(robots_url)
    try:
        status, text = fetch(robots_url)
    except Exception:
        status, text = 0, ""
    if 200 <= status < 300:
        parser.parse((text or "").lstrip("﻿").splitlines())
    else:
        parser.allow_all = True
    return parser


class RobotsRules:
    """Cached robots.txt answers for one user agent: one download per site per day.

    `fetch(robots_url) -> (status, text)` and `clock() -> seconds` are injectable so tests never
    touch the network or wait a day.
    """

    def __init__(self, user_agent: str = SCRAPER_HEADERS["User-Agent"], fetch: RobotsFetch | None = None,
                 clock: Callable[[], float] = time.monotonic, ttl_seconds: float = ROBOTS_CACHE_SECONDS):
        self.user_agent = user_agent
        self._fetch = fetch
        self._clock = clock
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, robotparser.RobotFileParser]] = {}

    def allowed(self, url: str, fetch: RobotsFetch | None = None) -> bool:
        """True if the site's robots.txt lets our user agent fetch `url`. Non-web URLs are not checked."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return True
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return True
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        cached = self._cache.get(origin)
        if cached is None or self._clock() - cached[0] > self._ttl:
            parser = parse_robots(origin + "/robots.txt", fetch or self._fetch or fetch_robots_txt)
            cached = (self._clock(), parser)
            self._cache[origin] = cached
        return cached[1].can_fetch(self.user_agent, url)

    def clear(self) -> None:
        """Forget every downloaded robots.txt (tests, or a long-running process that wants fresh rules)."""
        self._cache.clear()
