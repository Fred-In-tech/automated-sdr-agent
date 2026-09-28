import re
import socket

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Known valid major email providers (always pass, no DNS lookup needed)
# ─────────────────────────────────────────────────────────────────────────────
MAJOR_PROVIDERS = {
    "gmail.com", "yahoo.com", "icloud.com", "me.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "aol.com", "protonmail.com",
    "zoho.com", "fastmail.com", "hey.com"
}

# ─────────────────────────────────────────────────────────────────────────────
# Hard-blocked dummy / fictional / placeholder domains (previous mock data)
# ─────────────────────────────────────────────────────────────────────────────
BLOCKED_DUMMY_DOMAINS = {
    "example.com", "test.com", "domain.com",
    "vancecinema.com", "rostovastories.com", "apexcreative.co",
    "luminafilmworks.io", "starlightproduction.com", "highlandvisuals.com",
    "veritascut.net", "beaconfilms.com", "starlightfilm.com",
    "rachelgreenvisuals.com", "elenarostovaphoto.com", "liamcutmedia.com",
    "placeholder.io", "fake.com", "sample.com", "noemail.com",
    "donotreply.com", "no-reply.com"
}

# ─────────────────────────────────────────────────────────────────────────────
# Platform telemetry / system addresses / Big Tech & generic portals
# These are NOT creative prospects — always block them.
# ─────────────────────────────────────────────────────────────────────────────
BLOCKED_TELEMETRY_DOMAINS = {
    "sentry.io", "sentry-next.wixpress.com", "sentry.wixpress.com",
    "wixpress.com", "cloudflare.com", "shopify.com", "squarespace.com",
    "mailchimp.com", "klaviyo.com", "hubspot.com", "zendesk.com",
    "sendgrid.net", "mandrillapp.com", "amazonses.com",
    "bounce.wixanswers.com", "emailerserver.com", "noreply.com",
    # Major tech giants & platform ecosystems (not target creative agencies)
    "microsoft.com", "apple.com", "google.com", "adobe.com",
    "facebook.com", "meta.com", "twitter.com", "x.com",
    "linkedin.com", "careerexplorer.com", "wikipedia.org",
    "forbes.com", "upwork.com", "fiverr.com", "freelancer.com",
    "quickonomics.com", "investopedia.com", "nytimes.com", "wsj.com",
    "bloomberg.com", "businessinsider.com", "medium.com", "github.com",
    "counter-currents.com",
}


# CSS/JS stylesheet pseudo-emails (e.g. "Button@1.css" scraped from HTML)
CSS_EMAIL_PATTERN = re.compile(r'@\d+\.css$|@\d+\.js$', re.IGNORECASE)



def is_valid_email_syntax(email: str) -> bool:
    """Checks RFC 5322 compliant email syntax."""
    if not email or len(email) > 254:
        return False
    if CSS_EMAIL_PATTERN.search(email):
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email.strip()))


def is_dummy_or_placeholder_email(email: str) -> bool:
    """Detects if an email belongs to a fictional, dummy, or telemetry domain."""
    if not email or "@" not in email:
        return True
    domain = email.split("@")[1].strip().lower()
    if domain in BLOCKED_DUMMY_DOMAINS:
        return True
    if domain in BLOCKED_TELEMETRY_DOMAINS:
        return True
    for blocked in BLOCKED_TELEMETRY_DOMAINS:
        if domain.endswith("." + blocked):
            return True
    return False


def check_domain_has_mx(domain: str) -> tuple[bool, str]:
    """
    Performs real-time DNS MX record lookup to verify the domain can receive email.
    Returns (True, mx_host) if valid MX found, (False, reason) otherwise.
    """
    domain = domain.strip().lower()

    if domain in MAJOR_PROVIDERS:
        return True, f"Trusted major provider: {domain}"

    if not DNS_AVAILABLE:
        try:
            socket.getaddrinfo(domain, None)
            return True, "A record resolved (dnspython unavailable)"
        except socket.gaierror:
            return False, f"Domain does not resolve: {domain}"

    try:
        mx_records = dns.resolver.resolve(domain, "MX", lifetime=5)
        mx_hosts = [str(r.exchange).rstrip(".") for r in mx_records]
        return True, mx_hosts[0] if mx_hosts else domain
    except dns.resolver.NXDOMAIN:
        return False, f"Domain does not exist (NXDOMAIN): {domain}"
    except dns.resolver.NoAnswer:
        return False, f"No MX records for domain: {domain}"
    except dns.resolver.Timeout:
        return False, f"DNS lookup timed out: {domain}"
    except Exception as e:
        return False, f"DNS error: {e}"


def verify_lead_email(email: str) -> tuple[bool, str]:
    """
    Full verification pipeline:
    1. Syntax check
    2. CSS/JS pseudo-email filter
    3. Blocked dummy domain check
    4. Blocked telemetry address check
    5. DNS MX record validation
    """
    if not email:
        return False, "Empty email"

    email_clean = email.strip().lower()

    if not is_valid_email_syntax(email_clean):
        return False, "Invalid email syntax"

    if is_dummy_or_placeholder_email(email_clean):
        return False, "Blocked domain (dummy, placeholder, or telemetry)"

    domain = email_clean.split("@")[1]

    tld = domain.split(".")[-1]
    if len(tld) < 2:
        return False, "Invalid top-level domain"

    mx_ok, mx_info = check_domain_has_mx(domain)
    if not mx_ok:
        return False, f"Domain has no active mail server: {mx_info}"

    return True, f"Verified — MX: {mx_info}"


def filter_scraped_emails(raw_emails: list) -> list:
    """
    Filter a raw list of emails extracted from HTML/text:
    removes duplicates, CSS pseudo-emails, telemetry, and invalid formats.
    (DNS check is NOT done here — that runs in verify_lead_email per lead)
    """
    seen = set()
    valid = []
    for email in raw_emails:
        email = email.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        if not is_valid_email_syntax(email):
            continue
        if is_dummy_or_placeholder_email(email):
            continue
        valid.append(email)
    return valid


if __name__ == "__main__":
    test_emails = [
        "hello@example-studio.com",
        "john.doe@gmail.com",
        "8eb368c655b84e029ed@sentry.wixpress.com",
        "Button@1.css",
        "test@example.com",
        "invalid-email-format",
    ]
    print("\n=== Email Verifier Test ===")
    for e in test_emails:
        valid, reason = verify_lead_email(e)
        status = "VALID" if valid else "BLOCKED"
        print(f"  [{status}]  {e}  ->  {reason}")
