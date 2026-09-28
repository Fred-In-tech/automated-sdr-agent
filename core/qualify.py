"""Lead qualification: is this business actually your ideal client, and who do we write to?

An SDR doesn't email every address a search turns up. Each lead gets a 0-100 fit
score with plain-English reasons, a cleaned-up company name and, when we can
find one, a real first name. Leads below [targeting] min_fit_score are saved as
'disqualified' (so they're never scraped or emailed again) instead of 'new'.
"""

import re

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "icloud.com", "me.com", "mac.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "aol.com", "proton.me", "protonmail.com",
}

# Mailboxes that never reach a decision-maker at a small studio
BLOCKED_MAILBOXES = {
    "investor", "investors", "ir", "press", "media", "pr", "news", "legal", "privacy", "abuse", "careers",
    "jobs", "hr", "recruiting", "recruitment", "noreply", "no-reply", "donotreply", "webmaster",
    "postmaster", "hostmaster", "billing", "accounts", "accounting", "finance", "security", "compliance",
    "dmca", "copyright", "ads", "advertising", "partners", "partnerships", "affiliates", "sponsorship",
    "sponsorships", "marketing", "reservations", "orders", "returns", "shipping", "tourdesk", "donations",
}
# Shared inboxes a small studio owner still reads
SMALL_BIZ_INBOXES = {
    "info", "hello", "hi", "hey", "contact", "studio", "team", "booking", "bookings", "book", "inquiries",
    "inquiry", "enquiries", "mail", "office", "films", "photo", "photos", "video", "weddings", "admin",
}
SUPPORT_INBOXES = {"support", "help", "service", "customerservice", "care", "sales", "sale", "information"}

# Words that look like names on a page but aren't
NOT_NAMES = {
    "home", "about", "contact", "info", "hello", "wedding", "weddings", "studio", "studios", "film",
    "films", "photo", "photos", "photography", "video", "videography", "media", "productions", "creative",
    "events", "event", "portfolio", "gallery", "pricing", "packages", "the", "our", "and", "with", "love",
    "stories", "story", "book", "booking", "team", "company", "group", "services",
}

# Common first names (US): a mailbox like jane@ or janedoe@ is trusted when the name also appears on the site
COMMON_FIRST_NAMES = set("""
aaron abby adam adrian aimee alan alex alexa alexis alice alicia allison amanda amber amy ana andre andrea
andrew andy angela angie anna anne annie anthony april ashley austin ava bailey becca ben benjamin beth
bethany bill blake bob brad brandon brenda brett brian brianna brittany brooke bryan caitlin caleb cameron
carla carlos carly carmen carol caroline carrie casey cassie catherine chad charles charlie chelsea chris
christian christina christine christopher cindy claire cody cole colin connor courtney craig crystal cynthia
dan dana daniel danielle david dawn dean deanna derek devin diana diego dominic donna drew dustin dylan
eddie edward elena elizabeth ella ellie emily emma eric erica erik erin ethan eva evan faith gabe gabriel
gabby grace greg gregory hailey haley hannah heather heidi henry holly hunter ian isaac isabel isabella
jack jackie jacob jake james jamie jan jane janet jared jasmine jason jay jeff jenn jenna jennifer jenny
jeremy jess jesse jessica jill jim joe joel john jon jonathan jordan jose joseph josh joshua joy juan
julia julie justin kaitlyn karen kate katelyn katie kayla keith kelly kelsey ken kendra kevin kim
kimberly kristen kristin kyle lacey lance laura lauren leah lee leslie liam lily linda lindsay lindsey
lisa liz logan lori lucas lucy luis luke lydia madison maggie marcus maria marie mark marissa mary mason
matt matthew megan meghan melanie melissa mia michael michelle miguel mike molly monica morgan nancy
natalie nate nathan nicholas nick nicole noah olivia owen paige pam patrick paul peter rachel ray
rebecca riley rob robert robin ryan sam samantha sara sarah scott sean seth shannon shawn sierra sophia
spencer stacy stephanie stephen steve steven sydney tanya taylor teresa tessa thomas tiffany tim tina
todd tom tony tori travis trevor tyler valerie vanessa victor victoria vincent wes whitney will william
zach zachary zoe
""".split())

DEFAULT_BUYING_SIGNALS = (
    "packages", "pricing", "investment", "inquire", "inquiry", "book now", "booking", "quote",
    "check availability", "collections", "rates", "proposal", "contract",
)
DEFAULT_DISQUALIFIERS = (
    # big companies
    "investor relations", "nasdaq", "nyse", "press releases", "franchise opportunities", "job openings",
    "fortune 500",
    # online shops
    "shop now", "add to cart", "free shipping",
    # directories / marketplaces (they list your ICP, they aren't your ICP)
    "get matched", "the directory", "list your business", "claim your listing", "vendor directory",
    "browse by city",
)


def root_domain(host: str) -> str:
    """'mail.studio.co.uk' -> 'studio.co.uk', 'www.studio.com' -> 'studio.com'."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def mailbox_kind(local_part: str) -> str:
    base = re.split(r"[.+_-]", local_part.lower())[0]
    if base in BLOCKED_MAILBOXES or local_part.lower() in BLOCKED_MAILBOXES:
        return "blocked"
    if base in SUPPORT_INBOXES:
        return "support"
    if base in SMALL_BIZ_INBOXES:
        return "shared"
    return "personal"


def clean_company_name(site_name: str, title: str, domain: str) -> str:
    """Best human-readable business name: og:site_name, then page title, then the domain."""
    for candidate in (site_name, title):
        if not candidate:
            continue
        name = re.split(r"\s*[|–—·•»›]\s*|\s+[-:]\s+", candidate.strip())[0].strip()
        name = re.sub(r"^(home|welcome to|welcome)\b[\s|:-]*", "", name, flags=re.IGNORECASE).strip()
        if (2 <= len(name) <= 60 and re.search(r"[A-Za-z]{2}", name) and "$" not in name
                and name.lower() not in NOT_NAMES):
            return " ".join(name.split()[:6])
    return domain.split(".")[0].replace("-", " ").title()


def extract_first_name(local_part: str, page_text: str, company: str) -> str | None:
    """A first name only when we're confident: the mailbox name appears capitalized on their site."""
    if mailbox_kind(local_part) != "personal":
        return None
    capitalized = set(re.findall(r"\b([A-Z][a-z]{1,14})\b", page_text or ""))
    company_words = {w.lower() for w in re.findall(r"[A-Za-z]+", company or "")}
    token = re.split(r"[.+_\d-]", local_part.lower())[0]
    if not token.isalpha():
        return None

    def on_site(name: str) -> bool:
        return name.capitalize() in capitalized

    # 1. jane@ -> "Jane": a common first name that also appears on the site
    if token in COMMON_FIRST_NAMES and on_site(token):
        return token.capitalize()
    # 2. janedoe@ -> "Jane"
    for name in sorted(COMMON_FIRST_NAMES, key=len, reverse=True):
        if len(name) >= 3 and token.startswith(name) and len(token) > len(name) and on_site(name):
            return name.capitalize()
    # 3. Uncommon name that appears on the site, but isn't just a word from the business name
    if 2 <= len(token) <= 15 and token not in NOT_NAMES and token not in company_words and on_site(token):
        return token.capitalize()
    return None


def _count(text: str, phrases) -> int:
    return sum(len(re.findall(r"\b" + re.escape(p.lower()) + r"(?:s|es)?\b", text)) for p in phrases)


GENERIC_ICP_WORDS = {"company", "companies", "business", "services", "agency", "group", "studio", "owner"}


def icp_words(profile: dict) -> list:
    """Words a fitting website must mention: [targeting] keywords, else the trade word of each
    profession ("wedding photographer" -> "photographer"), never generic words like "company"."""
    targeting = profile["targeting"]
    if targeting.get("keywords"):
        return [k.lower() for k in targeting["keywords"]]
    words = []
    for profession in targeting["professions"]:
        last = profession.split()[-1].lower()
        if last not in words and last not in GENERIC_ICP_WORDS:
            words.append(last)
    return words


def score_lead(profile: dict, email: str, website_domain: str, page_text: str, company: str) -> dict:
    """Fit score 0-100 with reasons. `qualified` is False for hard disqualifiers or low scores."""
    targeting = profile["targeting"]
    min_score = int(targeting.get("min_fit_score", 50))
    text = (page_text or "").lower()
    local, _, email_domain = email.lower().partition("@")
    reasons, score = [], 0

    def reject(reason: str) -> dict:
        return {"qualified": False, "score": 0, "reasons": [reason], "first_name": None}

    for phrase in targeting.get("disqualify_words") or DEFAULT_DISQUALIFIERS:
        if phrase.lower() in text:
            return reject(f'looks like a large/retail company ("{phrase}" on site)')
    kind = mailbox_kind(local)
    if kind == "blocked":
        return reject(f"{local}@ never reaches a decision-maker")

    icp_hits = _count(text, icp_words(profile))
    if icp_hits == 0:
        return reject(f"site never mentions: {', '.join(icp_words(profile))}")
    score += 30 if icp_hits >= 3 else 20
    reasons.append(f"site mentions your ICP {icp_hits}x")

    signal_hits = _count(text, targeting.get("buying_signals") or DEFAULT_BUYING_SIGNALS)
    if signal_hits:
        score += 25 if signal_hits >= 3 else 20
        reasons.append("sells packages/quotes (proposal pain)")

    if email_domain in FREEMAIL_DOMAINS:
        score += 15
        reasons.append("solo operator (personal email)")
    elif website_domain and root_domain(email_domain) == root_domain(website_domain):
        score += 20
        reasons.append("email matches their website")
    else:
        return reject(f"email domain {email_domain} doesn't match website {website_domain}")

    first_name = extract_first_name(local, page_text, company)
    if first_name:
        score += 20
        reasons.append(f"personal inbox ({first_name})")
    elif kind == "shared":
        score += 10
        reasons.append(f"shared studio inbox ({local}@)")
    elif kind == "support":
        score -= 10
        reasons.append(f"support/sales desk ({local}@), less likely the owner")
    else:
        reasons.append(f"unrecognised inbox ({local}@)")

    qualified = score >= min_score
    if not qualified:
        reasons.append(f"below min_fit_score {min_score}")
    return {"qualified": qualified, "score": score, "reasons": reasons, "first_name": first_name}


def pick_best_contact(profile: dict, emails: list, website_domain: str, page_text: str, company: str):
    """Score every address found on the site and keep the best one: (email, result)."""
    scored = [(e, score_lead(profile, e, website_domain, page_text, company)) for e in dict.fromkeys(emails)]
    if not scored:
        return None, None
    return max(scored, key=lambda pair: (pair[1]["qualified"], pair[1]["score"]))
