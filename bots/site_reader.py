"""Reading one business's website and turning it into a lead: polite fetching (robots.txt),
email extraction, page text, and the per-site ICP check with its optional AI first line.

Split out of bots/leadgen_pipeline.py (which re-exports these names, so existing imports such as
`from bots.leadgen_pipeline import scrape_site` keep working) to keep the finder focused on where
leads come from (web search, Thumbtack) rather than how one site is read and judged.
"""

import re
import time
from urllib.parse import urlsplit

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from core.db import log_event
from core.email_verifier import verify_lead_email, filter_scraped_emails
from core.qualify import FREEMAIL_DOMAINS, clean_company_name, pick_best_contact, root_domain
from core.ai import AIRun, explain_fit, write_opener
from core.robots import SCRAPER_HEADERS, RobotsRules, fetch_robots_txt

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

SKIP_DOMAINS_GLOBAL = {
    "theknot.com", "weddingwire.com", "weddingbee.com", "yelp.com",
    "facebook.com", "instagram.com", "twitter.com", "pinterest.com",
    "youtube.com", "tiktok.com", "linkedin.com", "amazon.com",
    "google.com", "bing.com", "guru.com", "thumbtack.com",
    "bark.com", "gigsalad.com", "yellowpages.com", "squarespace.com",
    "wix.com", "wordpress.com", "weebly.com", "microsoft.com",
    "apple.com", "careerexplorer.com", "indeed.com", "glassdoor.com",
    "wikipedia.org", "adobe.com", "forbes.com", "upwork.com",
    "fiverr.com", "freelancer.com", "quickonomics.com", "investopedia.com",
    "nytimes.com", "wsj.com", "bloomberg.com", "businessinsider.com",
    "medium.com", "substack.com", "github.com",
}


def extract_emails_from_text(text: str) -> list:
    raw = EMAIL_RE.findall(text)
    return filter_scraped_emails(raw)


# ─────────────────────────────────────────────────────────────────────────────
# robots.txt — we only read pages a site allows bots to read
# ─────────────────────────────────────────────────────────────────────────────

# The rules themselves live in core/robots.py (web search asks them too). These wrappers add the
# lead finder's cache and its once-per-host skip log; tests inject `fetch` or patch `_fetch_robots_txt`.
_ROBOTS = RobotsRules(SCRAPER_HEADERS["User-Agent"], clock=lambda: time.monotonic())  # `time` looked up per call
_ROBOTS_REPORTED: set[str] = set()
_fetch_robots_txt = fetch_robots_txt


def robots_allowed(url: str, fetch=None) -> bool:
    """True if the site's robots.txt lets our user agent fetch `url`. One robots.txt download per
    host per day (cached); `fetch(robots_url) -> (status, text)` is injectable for tests."""
    return _ROBOTS.allowed(url, fetch or _fetch_robots_txt)


def clear_robots_cache() -> None:
    """Forget every downloaded robots.txt (tests, or a long-running process that wants fresh rules)."""
    _ROBOTS.clear()
    _ROBOTS_REPORTED.clear()


def _note_robots_skip(url: str) -> None:
    """Log a skipped site once per host, so the activity log explains missing leads without spam."""
    host = urlsplit(url).netloc.lower()
    if host in _ROBOTS_REPORTED:
        return
    _ROBOTS_REPORTED.add(host)
    log_event("LeadGen", "RobotsTxt", "info", f"Skipped {host}: its robots.txt asks bots not to read this page.")


def safe_get(url: str, timeout: int = 12) -> object:
    """GET request with one retry on failure. Returns None (without fetching) when the site's
    robots.txt disallows the URL for our user agent."""
    if not REQUESTS_AVAILABLE:
        return None
    if not robots_allowed(url):
        _note_robots_skip(url)
        return None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=SCRAPER_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            return None
        except Exception:
            if attempt == 0:
                time.sleep(2)
    return None


def _visible_text(soup) -> str:
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def scrape_site(base_url: str, skip_domains: set = SKIP_DOMAINS_GLOBAL) -> dict:
    """Read a business's homepage + about/contact pages: every email on them, plus the text
    and name of the site (used to judge ICP fit and personalise the email)."""
    try:
        domain = base_url.split("/")[2].replace("www.", "").lower()
    except IndexError:
        return {}
    if any(skip in domain for skip in skip_domains):
        return {}

    site = {"domain": domain, "website": f"https://{domain}", "emails": [], "page_text": "",
            "site_name": "", "title": ""}
    texts = []
    for path in ["", "/about", "/contact", "/contact-us"]:
        if path == "/contact-us" and site["emails"]:
            break
        r = safe_get("https://" + domain + path, timeout=8)
        if not r:
            continue
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            if path == "":
                meta = soup.find("meta", property="og:site_name") or soup.find("meta", attrs={"name": "application-name"})
                site["site_name"] = (meta.get("content") or "").strip() if meta else ""
                site["title"] = soup.title.get_text(strip=True) if soup.title else ""
            for email in extract_emails_from_text(r.text):
                email_domain = email.split("@")[1].lower()
                if email_domain in FREEMAIL_DOMAINS or root_domain(email_domain) == root_domain(domain):
                    if email.lower() not in site["emails"]:
                        site["emails"].append(email.lower())
            texts.append(_visible_text(soup))
        except Exception as e:
            log_event("LeadGen", "ParseWarning", "warning", f"{domain}{path}: {e}")
        time.sleep(0.5)
    site["page_text"] = " ".join(texts)[:30000]
    return site


# One AI connection per profile for the whole run, so its circuit breaker and budget apply to
# every lead found in that run (not reset per lead).
_AI_RUNS: dict[int, AIRun] = {}


def ai_for(profile: dict) -> AIRun:
    run = _AI_RUNS.get(id(profile))
    if run is None or run.profile is not profile:
        run = _AI_RUNS[id(profile)] = AIRun(profile)
    return run


def reset_ai_runs() -> None:
    _AI_RUNS.clear()


def qualify_site(profile: dict, site: dict, *, source: str, location: str, category: str,
                 title: str, name_hint: str = "", page_title: str = "") -> dict | None:
    """Turn a scraped site into a lead: best contact, ICP fit score, clean names.
    Returns None if the site has no usable email; disqualified leads are returned with that status
    so they're remembered and never scraped again."""
    if not site or not site.get("emails"):
        return None
    company = name_hint or clean_company_name(site["site_name"], page_title or site["title"], site["domain"])
    email, fit = pick_best_contact(profile, site["emails"], site["domain"], site["page_text"], company)
    mx_reason, opener, opener_draft, ai_fit = "", None, None, None
    if fit["qualified"]:
        ok, mx_reason = verify_lead_email(email)
        if not ok:
            return None
        ai = ai_for(profile)
        if ai.enabled("openers"):
            text = write_opener(ai, company, category, site["page_text"])
            if ai.job_mode("openers") == "review":
                opener_draft = text      # saved to the review queue once the lead has an id
            else:
                opener = text
        if ai.enabled("lead_fit"):
            ai_fit = explain_fit(ai, profile, company, site["page_text"], fit["reasons"])
    return {
        "name": company,
        "company": company,
        "first_name": fit["first_name"],
        "email": email,
        "website": site["website"],
        "location": location,
        "category": category,
        "title": title,
        "source": source,
        "mx_verified": mx_reason,
        "fit_score": fit["score"],
        "fit_reasons": fit["reasons"],
        "status": ("review" if fit.get("needs_review") else "new") if fit["qualified"] else "disqualified",
        "opener": opener,
        "opener_draft": opener_draft,
        "ai_fit": ai_fit,
    }


GENERIC_NAME_WORDS = {"studio", "studios", "films", "film", "photography", "photo", "video", "videography",
                      "productions", "production", "media", "creative", "weddings", "wedding", "events",
                      "company", "group", "the", "and", "llc", "inc"}


def site_matches_name(site: dict, business_name: str) -> bool:
    """True if the scraped site plausibly belongs to `business_name` (guards against web search
    returning some unrelated big website for a small vendor's name)."""
    words = [w for w in re.findall(r"[a-z0-9]+", business_name.lower()) if len(w) >= 3 and w not in GENERIC_NAME_WORDS]
    if not words:
        return True
    haystack = " ".join((site.get("page_text", ""), site.get("site_name", ""), site.get("title", ""),
                         site.get("domain", ""))).lower()
    return any(w in haystack for w in words)
