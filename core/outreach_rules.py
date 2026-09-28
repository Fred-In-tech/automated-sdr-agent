"""Sending rules an SDR team would enforce: business hours, a do-not-contact list,
a bounce-rate safety switch, link tracking and subject-line A/B tests."""

import os
import re
import zlib
from datetime import datetime
from urllib.parse import urlsplit

from core.config import CONFIG_DIR, render
from core.qualify import root_domain

DO_NOT_CONTACT_PATH = os.path.join(CONFIG_DIR, "do_not_contact.txt")
TRACKABLE_URL_RE = re.compile(r"https?://[^\s<>\"']+")
TRAILING_PUNCTUATION = ".,;:!?)"
VARIANT_LABELS = "ABCDEFGH"


# ── Business hours ──────────────────────────────────────────────────────
DEFAULT_SEND_WINDOW = "08:00-17:00"


def parse_send_window(window: str):
    """"08:00-17:00" -> (time, time). A typo falls back to business hours instead of crashing a run."""
    try:
        start, end = (datetime.strptime(part.strip(), "%H:%M").time() for part in window.split("-"))
        return start, end
    except ValueError:
        print(f'⚠️  [outreach] send_window "{window}" should look like "08:00-17:00" — using {DEFAULT_SEND_WINDOW}.')
        return parse_send_window(DEFAULT_SEND_WINDOW)


def in_send_window(profile: dict, now: datetime) -> bool:
    """[outreach] send_window = "08:00-17:00" (this computer's local time). "" = any time."""
    window = profile["outreach"].get("send_window", DEFAULT_SEND_WINDOW)
    if not window:
        return True
    start, end = parse_send_window(window)
    return start <= now.astimezone().time() <= end


# ── Do-not-contact list ─────────────────────────────────────────────────
def load_do_not_contact(path: str = DO_NOT_CONTACT_PATH) -> set:
    """Emails or whole domains (customers, partners, competitors) that must never be cold-emailed."""
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.split("#")[0].strip().lower() for line in f if line.split("#")[0].strip()}


def is_do_not_contact(email: str, blocked: set) -> bool:
    email = email.lower()
    domain = email.split("@")[-1]
    return email in blocked or domain in blocked or root_domain(domain) in blocked


# ── Bounce-rate safety switch ───────────────────────────────────────────
def bounce_alarm(cursor, profile: dict) -> str | None:
    """Stops sending when recent first emails bounce too often (protects your domain).
    Only counts fit-scored leads, so bounces from before qualification existed don't trip it."""
    outreach = profile["outreach"]
    max_rate = float(outreach.get("max_bounce_rate", 5))
    window = int(outreach.get("bounce_check_last", 30))
    rows = cursor.execute("""
        SELECT l.status FROM email_logs e JOIN leads l ON l.id = e.lead_id
        WHERE COALESCE(e.step, 1) = 1 AND l.fit_score > 0 ORDER BY e.id DESC LIMIT ?
    """, (window,)).fetchall()
    if len(rows) < 10:
        return None
    bounced = sum(1 for (status,) in rows if status == "bounced")
    rate = 100.0 * bounced / len(rows)
    if rate > max_rate:
        return (f"Sending paused: {bounced} of the last {len(rows)} first emails bounced ({rate:.0f}% > "
                f"{max_rate:g}%). Check lead quality, then raise [outreach] max_bounce_rate to resume.")
    return None


# ── Link tracking (UTM) ─────────────────────────────────────────────────
def add_tracking(text: str, profile: dict, content: str, html: bool = False) -> str:
    """Tag links to your own site so analytics shows which email drove the visit/signup.
    Use html=True on already-escaped HTML so the added separators are written as &amp;."""
    outreach = profile.get("outreach", {})
    if not outreach.get("track_links", True):
        return text
    own = {root_domain(urlsplit(profile["sender"]["product_url"]).hostname or "")}
    own |= {root_domain(d) for d in outreach.get("tracked_domains", [])}
    campaign = outreach.get("utm_campaign", "sdr")

    def tag(match):
        url = match.group(0)
        trail = ""
        while url and url[-1] in TRAILING_PUNCTUATION:
            url, trail = url[:-1], url[-1] + trail
        host = urlsplit(url).hostname or ""
        if root_domain(host) not in own or "utm_" in url:
            return match.group(0)
        amp = "&amp;" if html else "&"
        sep = amp if "?" in url else "?"
        params = amp.join((f"utm_source=outreach", "utm_medium=email", f"utm_campaign={campaign}",
                           f"utm_content={content}"))
        return f"{url}{sep}{params}{trail}"

    return TRACKABLE_URL_RE.sub(tag, text)


# ── Subject-line A/B test ───────────────────────────────────────────────
def subject_variant(profile: dict, email: str) -> tuple[str, str]:
    """(label, subject template) for this lead. With [outreach] subject_variants, each lead is
    assigned one variant (stable per address); otherwise ("", subject)."""
    outreach = profile["outreach"]
    variants = outreach.get("subject_variants") or []
    if not variants:
        return "", outreach["subject"]
    index = zlib.crc32(email.lower().encode()) % len(variants)
    return VARIANT_LABELS[index], variants[index]


def render_subject(template: str, values: dict) -> str:
    return " ".join(render(template, values).split())
