"""Will your cold emails reach the inbox? Checks the sending domain's DNS: MX, SPF, DKIM, DMARC.

Since 2024 Gmail and Yahoo (and Microsoft for large senders since May 2025) expect senders to
prove who they are with SPF and DKIM, and bulk senders with DMARC too. Most small-business
owners don't know if theirs are set up, and without them cold email quietly goes to spam.

Each check is OK, Warning or Problem, with a plain-English fix and, where it helps, the exact
record to add for the owner's email provider. A Problem (no SPF, no DKIM, no MX) caps the daily
send limit (see `capped_limit`) until it's fixed, so a broken setup can't burn the domain.

A second, stronger signal: when the owner emails themselves a sample (`sdr test-email`), the
receiving server writes an Authentication-Results header ("spf=pass dkim=pass dmarc=pass").
`parse_authentication_results` reads it; a sample `dkim=pass` covers a custom DKIM selector the
DNS lookup didn't know to try. Results are cached in data/deliverability.json, refreshed daily.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from core.email_checks import FREEMAIL_PROVIDERS, MX_PROVIDERS

DELIVERABILITY_CAP = 5           # emails a day while a Problem is unfixed
REFRESH_EVERY = timedelta(hours=24)
COMMON_SELECTORS = ("default", "dkim", "mail", "k1", "s1", "s2", "selector1", "selector2", "google")
PROVIDER_INFO = {
    "google_workspace": {"label": "Google Workspace", "spf": "include:_spf.google.com", "spf_match": ("_spf.google.com",),
                         "selectors": ("google",),
                         "dkim_fix": "In the Google Admin console: Apps > Google Workspace > Gmail > Authenticate email. "
                                     "Generate a DKIM key, add the TXT record it shows to your DNS, then press Start authentication."},
    "outlook": {"label": "Microsoft 365", "spf": "include:spf.protection.outlook.com",
                "spf_match": ("spf.protection.outlook.com",), "selectors": ("selector1", "selector2"),
                "dkim_fix": "In Microsoft Defender: Email & collaboration > Policies > Email authentication settings > DKIM. "
                            "Add the two CNAME records it shows to your DNS, then enable signing."},
    "hostinger": {"label": "Hostinger", "spf": "include:_spf.mail.hostinger.com",
                  "spf_match": ("_spf.mail.hostinger.com",), "selectors": ("hostingermail-a", "hostingermail-b"),
                  "dkim_fix": "In Hostinger's hPanel: Emails > your domain > DNS / email authentication. Turn on DKIM; "
                              "if your domain's DNS is managed elsewhere, copy the DKIM record it shows there."},
    "zoho": {"label": "Zoho Mail", "spf": "include:zohomail.com", "spf_match": ("zoho",), "selectors": ("zmail", "zoho"),
             "dkim_fix": "In the Zoho Mail admin console: Domains > your domain > Email configuration > DKIM. "
                         "Add a selector, copy the TXT record to your DNS, then verify it."},
}
OTHER = {"label": "your email provider", "spf": "include:<your provider's SPF domain>", "spf_match": (), "selectors": (),
         "dkim_fix": "Turn on DKIM signing in your email provider's settings and add the DNS record it gives you."}
STATUS_RANK = {"ok": 0, "info": 0, "warning": 1, "problem": 2}


class DNSUnavailable(Exception):
    """The DNS lookup itself failed (offline, timeout): we don't know, which isn't a Problem."""


def _resolve(name: str, rtype: str) -> list[str]:
    """TXT strings or MX hosts for `name`; [] when there's no such record."""
    import dns.exception
    import dns.resolver
    try:
        answer = dns.resolver.resolve(name, rtype, lifetime=6)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise DNSUnavailable(str(exc)) from None
    except dns.exception.DNSException as exc:
        raise DNSUnavailable(str(exc)) from None
    if rtype == "MX":
        return [str(r.exchange).lower() for r in answer]
    return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]


def _check(key: str, label: str, status: str, detail: str, fix: str = "", record: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "fix": fix, "record": record}


def sending_domain(profile: Mapping, env: Mapping[str, str]) -> str | None:
    """The domain receivers judge: the alias you send from, else the mailbox you log in with."""
    address = (profile.get("sender", {}).get("from_email") or env.get("SMTP_USER") or "").strip().lower()
    return address.rsplit("@", 1)[1] if "@" in address else None


def _website_domain(profile: Mapping) -> str:
    site = str(profile.get("sender", {}).get("website") or "").lower()
    site = re.sub(r"^https?://", "", site).split("/")[0]
    return site[4:] if site.startswith("www.") else site


def _provider(mx_hosts: list[str], env: Mapping[str, str]) -> str:
    for host in mx_hosts:
        for marker, provider in MX_PROVIDERS:
            if marker in host:
                return provider
    from core.setup_profile import provider_from_env
    guess = provider_from_env(env) or "other"
    return "google_workspace" if guess == "gmail" else guess


def _spf(domain: str, txt: list[str], info: dict) -> dict:
    records = [t for t in txt if t.lower().startswith("v=spf1")]
    suggested = f"v=spf1 {info['spf']} ~all"
    if not records:
        return _check("spf", "SPF", "problem", "No SPF record: receivers can't tell which servers may send as you.",
                      f"Add this TXT record to {domain} (host @).", suggested)
    if len(records) > 1:
        return _check("spf", "SPF", "problem", "There are two SPF records; receivers then ignore both.",
                      "Merge them into one TXT record that lists every service that sends for you.", suggested)
    spf = records[0].lower()
    if "+all" in spf.split():
        return _check("spf", "SPF", "problem", "Your SPF ends in +all, which lets anyone send as you.",
                      "Change +all to ~all.", records[0].replace("+all", "~all"))
    if info["spf_match"] and not any(m in spf for m in info["spf_match"]):
        return _check("spf", "SPF", "warning", f"Your SPF record doesn't include {info['label']}.",
                      f"Add {info['spf']} to your existing SPF record (keep one record).")
    return _check("spf", "SPF", "ok", "SPF is set up.")


def _dkim(domain: str, resolve: Callable, info: dict, sample: Mapping | None) -> dict:
    for selector in dict.fromkeys(info["selectors"] + COMMON_SELECTORS):
        for text in resolve(f"{selector}._domainkey.{domain}", "TXT"):
            key = re.search(r"(?:^|;)\s*p=([^;\s]*)", text)
            if "v=dkim1" in text.lower() and key and key.group(1):
                return _check("dkim", "DKIM", "ok", f"DKIM is set up (selector {selector}).")
    if sample and sample.get("dkim") == "pass":
        return _check("dkim", "DKIM", "ok", "DKIM passed on your sample email (a custom selector).")
    return _check("dkim", "DKIM", "problem", "No DKIM signature record found: emails can't prove they really came from you.",
                  info["dkim_fix"])


def _dmarc(domain: str, resolve: Callable) -> dict:
    records = [t for t in resolve(f"_dmarc.{domain}", "TXT") if t.lower().startswith("v=dmarc1")]
    suggested = f"v=DMARC1; p=none; rua=mailto:dmarc@{domain}"
    if not records:
        return _check("dmarc", "DMARC", "warning", "No DMARC record. Gmail and Yahoo expect one from bulk senders.",
                      f"Add a TXT record at _dmarc.{domain}. Start with p=none, then move to p=quarantine "
                      "after a few weeks of clean reports.", suggested)
    policy = re.search(r"\bp\s*=\s*(\w+)", records[0].lower())
    if not policy or policy.group(1) == "none":
        return _check("dmarc", "DMARC", "warning", "DMARC is set to p=none: it reports but doesn't protect your domain.",
                      "When SPF and DKIM pass reliably, change it to p=quarantine.",
                      re.sub(r"p\s*=\s*none", "p=quarantine", records[0], flags=re.IGNORECASE))
    return _check("dmarc", "DMARC", "ok", f"DMARC is set up (p={policy.group(1)}).")


def overall(checks: list[dict]) -> str:
    worst = max((STATUS_RANK.get(c["status"], 0) for c in checks), default=0)
    return {0: "ok", 1: "warning", 2: "problem"}[worst]


def run_checks(profile: Mapping, env: Mapping[str, str], *, resolve: Callable[[str, str], list[str]] = _resolve,
               sample: Mapping | None = None) -> dict:
    """Check the sending domain now. status: ok | warning | problem | unknown (couldn't check)."""
    now = datetime.now(timezone.utc).isoformat()
    domain = sending_domain(profile, env)
    base = {"domain": domain, "provider": None, "checks": [], "consumer": False, "checked_at": now}
    if not domain:
        return {**base, "status": "unknown", "message": "No sending address saved yet."}
    if domain in FREEMAIL_PROVIDERS:
        note = _check("consumer", "Personal mailbox", "info",
                      f"You send from a personal {domain} address, so its provider handles SPF and DKIM. Keep volumes low: "
                      "a business domain is better for cold email.")
        return {**base, "consumer": True, "provider": FREEMAIL_PROVIDERS[domain], "checks": [note], "status": "ok"}
    try:
        mx = resolve(domain, "MX")
        provider = _provider(mx, env)
        info = PROVIDER_INFO.get(provider, OTHER)
        checks = [
            _check("mx", "MX (replies)", "ok", "Your domain can receive replies.") if mx else
            _check("mx", "MX (replies)", "problem", "Your domain has no MX record, so replies and bounces can't reach you.",
                   f"Add the MX records {info['label']} gives you."),
            _spf(domain, resolve(domain, "TXT"), info),
            _dkim(domain, resolve, info, sample),
            _dmarc(domain, resolve),
        ]
    except DNSUnavailable as exc:
        return {**base, "status": "unknown", "message": f"Couldn't look up DNS right now ({exc})."}
    website = _website_domain(profile)
    if website and website == domain:
        checks.append(_check("sending_domain", "Sending domain", "info",
                             "Cold email goes out from your main business domain, so complaints could affect your everyday email.",
                             f"Many senders use a second domain for outreach (for example get{domain}) and keep {domain} for "
                             "customers."))
    return {**base, "provider": provider, "checks": checks, "status": overall(checks)}


# ── cache, cap and sample ───────────────────────────────────────────────────


def _default_path() -> str:
    from core.product import ROOT_DIR
    return os.path.join(ROOT_DIR, "data", "deliverability.json")


def load(path: str | None = None) -> dict:
    try:
        with open(path or _default_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict, path: str | None) -> None:
    from core.db import ensure_private_dir, open_private
    target = path or _default_path()
    ensure_private_dir(os.path.dirname(target))
    temporary = target + ".tmp"
    with open_private(temporary, encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(temporary, target)


def save_sample(sample: Mapping, path: str | None = None) -> None:
    data = load(path)
    data["sample"] = {**sample, "checked_at": datetime.now(timezone.utc).isoformat()}
    _save(data, path)


def refresh_if_stale(profile: Mapping, env: Mapping[str, str], *, now: datetime | None = None,
                     resolve: Callable = _resolve, path: str | None = None, force: bool = False) -> dict:
    """The cached result, re-checked when older than a day (or `force`). A failed lookup keeps the
    last known result, so a flaky network never changes what we believe."""
    now = now or datetime.now(timezone.utc)
    cached = load(path)
    sample = cached.get("sample")
    try:
        fresh_enough = now - datetime.fromisoformat(cached["checked_at"]) < REFRESH_EVERY
    except (KeyError, TypeError, ValueError):
        fresh_enough = False
    if cached.get("checks") is not None and fresh_enough and not force \
            and cached.get("domain") == sending_domain(profile, env):
        return cached
    result = run_checks(profile, env, resolve=resolve, sample=sample)
    if result["status"] == "unknown" and cached.get("status") not in (None, "unknown"):
        return cached
    result["checked_at"] = now.isoformat()
    if sample:
        result["sample"] = sample
    _save(result, path)
    return result


def problems(result: Mapping) -> list[dict]:
    return [c for c in result.get("checks") or [] if c.get("status") == "problem"]


def capped_limit(limit: int, result: Mapping) -> tuple[int, str]:
    """(daily limit to use, why it's lower). A Problem caps sending until it's fixed."""
    if result.get("status") != "problem" or limit <= DELIVERABILITY_CAP:
        return limit, ""
    names = ", ".join(c.get("label", c.get("key", "")) for c in problems(result)) or "a DNS problem"
    return DELIVERABILITY_CAP, (f"Sending is limited to {DELIVERABILITY_CAP} a day until {names} is fixed "
                                "(see `sdr doctor` or the dashboard), so your domain's reputation isn't hurt.")


def parse_authentication_results(header: str) -> dict:
    """{"spf", "dkim", "dmarc", "seen_by"} from an Authentication-Results header ({} if none)."""
    text = " ".join((header or "").split())
    if not text:
        return {}
    result = {}
    for method in ("spf", "dkim", "dmarc"):
        match = re.search(rf"\b{method}=([a-z]+)", text, re.IGNORECASE)
        if match:
            result[method] = match.group(1).lower()
    first = text.split(";", 1)[0].strip()
    if result and first and "=" not in first:
        result["seen_by"] = first
    return result
