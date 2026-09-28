import re
import json
import random
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from core.db import get_connection, log_event, init_db
from core.notifications import NotificationManager
from core.email_verifier import verify_lead_email, filter_scraped_emails
from core.config import load_env_file, load_profile
from core.qualify import FREEMAIL_DOMAINS, clean_company_name, pick_best_contact, root_domain, score_lead
from core.ai import ai_enabled, write_opener
from core.robots import ROBOTS_CACHE_SECONDS, SCRAPER_HEADERS, RobotsRules, fetch_robots_txt  # noqa: F401 (re-exported)
from core.search import describe_search, search_stopped, search_web, start_search_run
from bots.lead_store import export_leads_to_csv, known_domains, save_leads  # noqa: F401 (re-exported)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — who to target comes from config/profile.toml ([targeting])
# ─────────────────────────────────────────────────────────────────────────────

def _city_name(city: str) -> str:
    return city.split(",")[0].strip()


def build_search_queries(profile: dict) -> list:
    """One web search per (city, profession) pair from the profile."""
    targeting = profile["targeting"]
    extra = targeting.get("search_extra_words", "").strip()
    return [
        {
            "query": f'"{profession}" "{_city_name(city)}" {extra}'.strip(),
            "profession": profession.title(),
            "location": city,
        }
        for city in targeting["cities"]
        for profession in targeting["professions"]
    ]


def enrich_keywords(profile: dict) -> list:
    """Short trade words used to find a business's own website (e.g. 'photographer')."""
    targeting = profile["targeting"]
    if targeting.get("keywords"):
        return list(targeting["keywords"])
    words = []
    for profession in targeting["professions"]:
        word = profession.split()[-1].lower()
        if word not in words:
            words.append(word)
    return words


def thumbtack_city_url(city: str, category: str) -> str | None:
    """'Los Angeles, CA' + 'videographers' -> Thumbtack city page. None if not a US 'City, ST'."""
    parts = [p.strip() for p in city.split(",")]
    if not category or len(parts) != 2 or len(parts[1]) != 2:
        return None
    city_slug = re.sub(r"[^a-z0-9]+", "-", parts[0].lower()).strip("-")
    return f"https://www.thumbtack.com/{parts[1].lower()}/{city_slug}/{category}/"


# Article-style page titles to skip in Option 1 (not individual studios)
ARTICLE_SKIP_KEYWORDS = [
    "best", "top", "guide", "list", "how to", "tips", "ideas", "cheapest",
    "affordable", "review", "comparison", "vs ", "near me", "venues",
    "cost", "price", "packages", "average", "what is", "what does",
    "find a", "hire a", "career", "salary", "job description", "become a",
    "ultimate", "complete", "everything you", "things to", "questions",
]


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

# The rules themselves live in core/robots.py (web search asks them too). These names stay here
# because the lead finder's callers and tests use them.
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


def qualify_site(profile: dict, site: dict, *, source: str, location: str, category: str,
                 title: str, name_hint: str = "", page_title: str = "") -> dict | None:
    """Turn a scraped site into a lead: best contact, ICP fit score, clean names.
    Returns None if the site has no usable email; disqualified leads are returned with that status
    so they're remembered and never scraped again."""
    if not site or not site.get("emails"):
        return None
    company = name_hint or clean_company_name(site["site_name"], page_title or site["title"], site["domain"])
    email, fit = pick_best_contact(profile, site["emails"], site["domain"], site["page_text"], company)
    mx_reason, opener = "", None
    if fit["qualified"]:
        ok, mx_reason = verify_lead_email(email)
        if not ok:
            return None
        if ai_enabled(profile):
            opener = write_opener(profile, company, category, site["page_text"])
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
        "status": "new" if fit["qualified"] else "disqualified",
        "opener": opener,
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


def count_qualified(leads: list) -> int:
    return sum(1 for lead in leads if lead["status"] == "new")


# ─────────────────────────────────────────────────────────────────────────────
# Web search — core/search.py picks the engine (Brave, DuckDuckGo or Bing)
# ─────────────────────────────────────────────────────────────────────────────

SEARCH_RESULTS_PER_QUERY = 10                      # one page; directories/social sites are dropped after
DIRECTORY_DOMAINS = ("thumbtack.com", "yelp.com")  # listing sites, never a business's own website


def result_domain(url: str) -> str:
    """'https://www.acme.com/about' -> 'acme.com' ("" when the URL has no host part)."""
    parts = url.split("/")
    return parts[2].replace("www.", "") if len(parts) > 2 else ""


def web_results(query: str, profile: dict, skip_domains: set, extra_skip: tuple = ()) -> list:
    """search_web() results as {title, url, domain}, minus skip_domains (directories, social media,
    publishers) and `extra_skip`."""
    results = []
    for result in search_web(query, max_results=SEARCH_RESULTS_PER_QUERY, profile=profile):
        domain = result_domain(result["url"])
        if not domain or any(skip in domain for skip in (*skip_domains, *extra_skip)):
            continue
        results.append({**result, "domain": domain})
    return results


# ─────────────────────────────────────────────────────────────────────────────
# OPTION 1: Web Search Portfolio Scraper
# ─────────────────────────────────────────────────────────────────────────────

class Option1_SearchPortfolioScraper:
    """
    Searches the web (core/search.py) for live business websites using the profile's
    professions x cities, visits each result's /contact or /about page, and extracts real emails.
    """

    def __init__(self, profile: dict, skip_domains: set, known_domains: set):
        self.profile = profile
        self.queries = build_search_queries(profile)
        self.skip_domains = skip_domains
        self.known_domains = known_domains

    def _search_results(self, query: str, max_results: int = 8) -> list:
        return web_results(query, self.profile, self.skip_domains)[:max_results]

    def scrape(self, count: int = 8) -> list:
        if not REQUESTS_AVAILABLE:
            log_event("Option1", "Error", "failed", "requests/bs4 not installed")
            return []

        leads = []
        searches = random.sample(self.queries, min(len(self.queries), max(count + 5, 8)))
        seen_emails = set()

        for search in searches:
            if count_qualified(leads) >= count or search_stopped(self.profile):
                break
            try:
                results = self._search_results(search["query"])
                for result in results:
                    if count_qualified(leads) >= count:
                        break
                    # Skip article/list pages — we want individual studio sites
                    title_lower = result["title"].lower()
                    if "?" in result["title"] or any(kw in title_lower for kw in ARTICLE_SKIP_KEYWORDS):
                        continue
                    # Skip obvious blog/aggregate domains
                    domain = result["domain"].lower()
                    if any(kw in domain for kw in ["blog.", "news.", "article", "guide", "venue", "magazine", "career", "edu"]):
                        continue


                    if root_domain(domain) in self.known_domains:
                        continue
                    self.known_domains.add(root_domain(domain))
                    lead = qualify_site(
                        self.profile, scrape_site(result["url"], self.skip_domains),
                        source="Option1_WebSearch", location=search["location"],
                        category=search["profession"], title=search["profession"], page_title=result["title"],
                    )
                    if lead and lead["email"] not in seen_emails:
                        seen_emails.add(lead["email"])
                        leads.append(lead)
                time.sleep(1.5)
            except Exception as e:
                log_event("Option1", "Warning", "partial_error", str(e))
                continue
        return leads



# ─────────────────────────────────────────────────────────────────────────────
# OPTION 2: Thumbtack Category Scraper + Web Search Enricher
# ─────────────────────────────────────────────────────────────────────────────

class Option2_ThumbtackCategoryScraper:
    """
    Scrapes business listings from the profile's Thumbtack category pages.
    Extracts vendor names and locations from JSON-LD ItemList, then searches
    the web for the business's real website to find their direct contact email.
    """

    def __init__(self, profile: dict, skip_domains: set, known_domains: set):
        targeting = profile["targeting"]
        self.profile = profile
        self.categories = list(targeting.get("thumbtack_categories", []))
        self.ideal_client = targeting["ideal_client"]
        self.search_terms = " OR ".join(enrich_keywords(profile))
        self.skip_domains = skip_domains
        self.known_domains = known_domains

    def _get_thumbtack_vendors(self, path: str) -> list:
        """Extract vendors from Thumbtack category JSON-LD ItemList."""
        url = f"https://www.thumbtack.com{path}"
        r = safe_get(url)
        if not r:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        vendors = []

        # Method 1: JSON-LD ItemList
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        biz = item.get("item", {})
                        name = biz.get("name")
                        item_url = biz.get("url", "")
                        if name:
                            vendors.append({"name": name, "thumbtack_url": item_url})
            except Exception:
                continue

        # Method 2: Fallback — parse service URLs from anchor tags
        if not vendors:
            seen_slugs = set()
            service_pattern = re.compile(r'/[a-z]{2}/[^/]+/[^/]+/([^/]+)/service/')
            for a in soup.find_all("a", href=service_pattern):
                href = a.get("href", "")
                m = service_pattern.search(href)
                if m:
                    slug = m.group(1)
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        name = slug.replace("-", " ").title()
                        # Parse location from URL like /tx/austin/...
                        parts = href.strip("/").split("/")
                        location = f"{parts[1].title()}, {parts[0].upper()}" if len(parts) >= 2 else "United States"
                        vendors.append({
                            "name": name,
                            "location": location,
                            "thumbtack_url": f"https://www.thumbtack.com{href}"
                        })
        return vendors

    def _find_studio_website(self, studio_name: str) -> str:
        """Search the web for the studio's official website (first result that isn't a directory)."""
        query = f'"{studio_name}" {self.search_terms} contact site:.com'
        results = web_results(query, self.profile, self.skip_domains, DIRECTORY_DOMAINS)
        return results[0]["url"] if results else ""

    def scrape(self, count: int = 8) -> list:
        if not REQUESTS_AVAILABLE:
            return []

        leads = []
        seen_emails = set()
        seen_names = set()
        shuffled = random.sample(self.categories, len(self.categories))

        for slug in shuffled:
            if count_qualified(leads) >= count or search_stopped(self.profile):
                break
            title = slug.replace("-", " ").title()
            try:
                vendors = self._get_thumbtack_vendors(f"/k/{slug}/near-me")
                random.shuffle(vendors)
                for vendor in vendors:
                    if count_qualified(leads) >= count or search_stopped(self.profile):
                        break
                    name = vendor["name"]
                    if name in seen_names:
                        continue
                    seen_names.add(name)

                    # Enrich: find the studio's own website with a name search
                    website_url = self._find_studio_website(name)
                    if not website_url:
                        continue

                    domain = root_domain(website_url.split("/")[2].replace("www.", ""))
                    if domain in self.known_domains:
                        continue
                    self.known_domains.add(domain)
                    site = scrape_site(website_url, self.skip_domains)
                    if not site_matches_name(site, name):
                        continue
                    lead = qualify_site(
                        self.profile, site, source="Option2_ThumbtackCategory",
                        location=vendor.get("location", ""), category=self.ideal_client, title=title,
                        name_hint=name,
                    )
                    if lead and lead["email"] not in seen_emails:
                        seen_emails.add(lead["email"])
                        leads.append(lead)
                    time.sleep(1.5)
            except Exception as e:
                log_event("Option2", "Warning", "partial_error", str(e))
                continue
        return leads


# ─────────────────────────────────────────────────────────────────────────────
# OPTION 3: Thumbtack City-Scoped Scraper + Web Search Enricher
# ─────────────────────────────────────────────────────────────────────────────

class Option3_ThumbtackCityScraper:
    """
    Scrapes city-specific Thumbtack pages for the profile's cities to find
    local businesses with accurate city/state data. Enriches each vendor by
    web-searching their name to find their real website and contact email.
    """

    def __init__(self, profile: dict, skip_domains: set, known_domains: set):
        targeting = profile["targeting"]
        self.profile = profile
        self.known_domains = known_domains
        category = targeting.get("thumbtack_city_category", "")
        self.city_urls = [
            (url, city)
            for city in targeting["cities"]
            if (url := thumbtack_city_url(city, category))
        ]
        self.ideal_client = targeting["ideal_client"]
        self.search_terms = " OR ".join(enrich_keywords(profile))
        self.skip_domains = skip_domains

    def _get_city_vendors(self, url: str, location: str) -> list:
        """Get vendor cards from a Thumbtack city URL."""
        r = safe_get(url)
        if not r:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        vendors = []

        # JSON-LD ItemList
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        biz = item.get("item", {})
                        name = biz.get("name")
                        item_url = biz.get("url", "")
                        if name:
                            vendors.append({"name": name, "thumbtack_url": item_url, "location": location})
            except Exception:
                continue

        # Fallback: parse service slugs
        if not vendors:
            seen_slugs = set()
            service_pattern = re.compile(r'/[a-z]{2}/[^/]+/[^/]+/([^/]+)/service/')
            for a in soup.find_all("a", href=service_pattern):
                href = a.get("href", "")
                m = service_pattern.search(href)
                if m:
                    slug = m.group(1)
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        name = slug.replace("-", " ").title()
                        vendors.append({
                            "name": name,
                            "thumbtack_url": f"https://www.thumbtack.com{href}",
                            "location": location
                        })
        return vendors

    def _find_studio_site(self, studio_name: str, location: str) -> dict:
        """Search the web for '[Business Name] [City] [trade keywords] contact' and read their site."""
        query = f'"{studio_name}" {_city_name(location)} {self.search_terms} contact'
        for result in web_results(query, self.profile, self.skip_domains, DIRECTORY_DOMAINS):
            domain = root_domain(result["domain"])
            if domain in self.known_domains:
                continue
            site = scrape_site(result["url"], self.skip_domains)
            if site.get("emails") and site_matches_name(site, studio_name):
                self.known_domains.add(domain)
                return site
        return {}

    def scrape(self, count: int = 8) -> list:
        if not REQUESTS_AVAILABLE:
            return []

        leads = []
        seen_emails = set()
        seen_names = set()
        shuffled = random.sample(self.city_urls, min(len(self.city_urls), max(count + 2, 4)))

        for page_url, location in shuffled:
            if count_qualified(leads) >= count or search_stopped(self.profile):
                break
            try:
                vendors = self._get_city_vendors(page_url, location)
                random.shuffle(vendors)
                for vendor in vendors:
                    if count_qualified(leads) >= count or search_stopped(self.profile):
                        break
                    name = vendor["name"]
                    if name in seen_names:
                        continue
                    seen_names.add(name)

                    site = self._find_studio_site(name, location)
                    lead = qualify_site(
                        self.profile, site, source="Option3_ThumbtackCitySearch", location=location,
                        category=self.ideal_client, title=self.ideal_client, name_hint=name,
                    )
                    if lead and lead["email"] not in seen_emails:
                        seen_emails.add(lead["email"])
                        leads.append(lead)
                    time.sleep(1.5)
            except Exception as e:
                log_event("Option3", "Warning", "partial_error", str(e))
                continue
        return leads


# ─────────────────────────────────────────────────────────────────────────────
# Unified Lead Generation Pipeline
# ─────────────────────────────────────────────────────────────────────────────

class LeadGenPipeline:
    """
    Orchestrates the live scraping engines, all driven by config/profile.toml:
      Option 1 — web search (core/search.py: Brave, DuckDuckGo or Bing) per profession x city
      Option 2 — Thumbtack category pages (if thumbtack_categories is set)
      Option 3 — Thumbtack city pages (if thumbtack_city_category is set)

    Every lead is ICP-scored (core/qualify.py); only qualified, MX-verified leads are
    emailed. Disqualified sites are remembered so they're never scraped twice.
    """

    def __init__(self, profile: dict | None = None):
        init_db()
        load_env_file()
        self.profile = profile or load_profile()
        self.notifier = NotificationManager()
        skip_domains = SKIP_DOMAINS_GLOBAL | set(self.profile["targeting"].get("skip_domains", []))
        known = known_domains()
        self.option1 = Option1_SearchPortfolioScraper(self.profile, skip_domains, known)
        self.option2 = Option2_ThumbtackCategoryScraper(self.profile, skip_domains, known)
        self.option3 = Option3_ThumbtackCityScraper(self.profile, skip_domains, known)


    def run_pipeline(self, count: int | None = None, category_filter: str = None) -> dict:
        count = count or self.profile["targeting"].get("leads_per_run", 4)
        log_event("LeadGenPipeline", "Start", "running",
                  f"Live multi-engine scrape: {count} leads.")

        # Split the target across whichever engines the profile enables.
        use_option2 = bool(self.option2.categories)
        use_option3 = bool(self.option3.city_urls)
        engine_count = 1 + use_option2 + use_option3
        per_engine = max(1, count // engine_count)
        option1_share = max(1, count - per_engine * (engine_count - 1))

        start_search_run()  # an engine that hit a limit last run gets a fresh chance
        engine = describe_search(self.profile)["label"]
        log_event("LeadGenPipeline", "Option1", "running", f"Web search ({engine}) starting...")
        raw1 = self.option1.scrape(count=option1_share)
        log_event("LeadGenPipeline", "Option1", "done", f"Option 1 found {len(raw1)} leads")

        raw2 = []
        if use_option2:
            log_event("LeadGenPipeline", "Option2", "running", "Thumbtack Category scraper starting...")
            raw2 = self.option2.scrape(count=per_engine)
            log_event("LeadGenPipeline", "Option2", "done", f"Option 2 found {len(raw2)} leads")

        raw3 = []
        if use_option3:
            log_event("LeadGenPipeline", "Option3", "running", "Thumbtack City scraper starting...")
            raw3 = self.option3.scrape(count=per_engine)
            log_event("LeadGenPipeline", "Option3", "done", f"Option 3 found {len(raw3)} leads")


        all_candidates = raw1 + raw2 + raw3
        if category_filter:
            filtered = [c for c in all_candidates
                        if category_filter.lower() in c.get("category", "").lower()]
            all_candidates = filtered or all_candidates

        # Deduplicate by email
        seen_emails = set()
        deduplicated = []
        for lead in all_candidates:
            email = lead.get("email", "").lower().strip()
            if email and email not in seen_emails:
                seen_emails.add(email)
                deduplicated.append(lead)

        saved = self._save_leads(deduplicated)
        new_leads = [lead for lead in saved if lead["status"] == "new"]
        disqualified = len(saved) - len(new_leads)
        csv_path = self.export_to_csv()

        summary = (f"Lead search complete: {len(new_leads)} qualified leads saved, "
                   f"{disqualified} sites rejected as poor fit. "
                   f"Option1={len(raw1)}, Option2={len(raw2)}, Option3={len(raw3)}")
        log_event("LeadGenPipeline", "Complete", "success", summary)

        if new_leads:
            best = max(new_leads, key=lambda lead: lead["fit_score"])
            self.notifier.notify_all(
                f"🎯 {self.profile['sender']['product_name']} — new leads",
                f"✅ {len(new_leads)} qualified leads found\n"
                f"Best fit: {best['company']} ({best['fit_score']}/100, {best['location']})"
            )

        return {
            "new_leads_count": len(new_leads),
            "disqualified_count": disqualified,
            "leads": new_leads,
            "csv_path": csv_path,
            "sources": {
                "option1_web_search": len(raw1),
                "option2_thumbtack_category": len(raw2),
                "option3_thumbtack_city": len(raw3),
            }
        }


    def _save_leads(self, candidates: list) -> list:
        """Save scored leads (qualified as 'new', others as 'disqualified'); skip known emails."""
        return save_leads(candidates, self.profile["targeting"]["ideal_client"])

    def export_to_csv(self) -> str:
        return export_leads_to_csv()


def _lead_website(row: dict) -> str:
    if row.get("website"):
        return row["website"]
    try:
        website = json.loads(row.get("enriched_info") or "{}").get("website", "")
    except ValueError:
        website = ""
    domain = row["email"].split("@")[-1].lower()
    return website or ("" if domain in FREEMAIL_DOMAINS else f"https://{domain}")


def requalify_existing_leads(profile: dict | None = None, dry_run: bool = False) -> dict:
    """Score leads saved before qualification existed. Poor fits become 'disqualified' (never emailed
    again); good fits get a real first name, a clean company name and — if they were already
    emailed once with the old copy — restart with the current first email. Leads who already
    replied keep their status (they're never re-emailed)."""
    profile = profile or load_profile()
    init_db()
    conn = get_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM leads WHERE fit_score IS NULL AND status IN "
        "('new', 'contacted', 'replied', 'interested', 'not_now')").fetchall()]
    results = {"checked": 0, "qualified": [], "disqualified": []}
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        results["checked"] += 1
        website = _lead_website(row)
        site = scrape_site(website) if website else {}
        if not site.get("page_text"):
            fit = {"qualified": False, "score": 0, "reasons": ["website unreachable — can't verify fit"],
                   "first_name": None}
            company = row["company"]
        else:
            company = clean_company_name(site["site_name"], site["title"], site["domain"])
            fit = score_lead(profile, row["email"], site["domain"], site["page_text"], company)
        entry = {"email": row["email"], "company": company, "score": fit["score"], "reasons": fit["reasons"]}
        results["qualified" if fit["qualified"] else "disqualified"].append(entry)
        if dry_run:
            continue

        if not fit["qualified"]:
            conn.execute("UPDATE leads SET status = 'disqualified', fit_score = ?, fit_reasons = ?, next_touch_at = NULL "
                         "WHERE id = ?", (fit["score"], json.dumps(fit["reasons"]), row["id"]))
            continue
        conn.execute("""UPDATE leads SET name = ?, company = ?, first_name = ?, website = ?, fit_score = ?,
                        fit_reasons = ? WHERE id = ?""",
                     (company, company, fit["first_name"], site["website"], fit["score"],
                      json.dumps(fit["reasons"]), row["id"]))
        if row["status"] == "contacted":
            # Their first email was written from bad name data (e.g. "Hi Golden"), so rather than
            # following up in that thread, restart them with the current, properly personalised first email.
            conn.execute("UPDATE leads SET status = 'new', sequence_step = 0, next_touch_at = NULL, "
                         "thread_subject = NULL, thread_message_id = NULL WHERE id = ?", (row["id"],))
        conn.commit()

    conn.commit()
    conn.close()
    log_event("LeadGenPipeline", "Requalify", "success",
              f"Re-checked {results['checked']} leads: {len(results['qualified'])} fit, "
              f"{len(results['disqualified'])} disqualified{' (dry run)' if dry_run else ''}.")
    return results


if __name__ == "__main__":
    pipeline = LeadGenPipeline()
    res = pipeline.run_pipeline(count=6)
    stats = {k: v for k, v in res.items() if k != "leads"}
    print(json.dumps(stats, indent=2))
    print(f"\n=== {res['new_leads_count']} new leads saved ===")
    for lead in res["leads"]:
        print(f"  [{lead['source']}] {lead['name']} <{lead['email']}> — {lead['location']}")

