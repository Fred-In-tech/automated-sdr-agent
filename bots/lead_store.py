"""Saving and exporting leads: the lead finder's side of the SQLite database.

Split out of bots/leadgen_pipeline.py (which re-exports these names, so existing imports such as
`from bots.leadgen_pipeline import export_leads_to_csv` keep working) to keep the finder focused
on finding and qualifying leads.
"""

import csv
import json
import os
from datetime import datetime, timezone

from core.db import ensure_private_dir, get_connection, log_event, open_private
from core.qualify import FREEMAIL_DOMAINS, root_domain

EXPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "leads_export.csv")
# Excel, LibreOffice and Google Sheets run a cell that starts with one of these as a formula:
# =HYPERLINK to a phishing page, or a DDE `=cmd|...` prompt. Company names and titles come
# straight from prospects' own web pages, so any of them could be crafted for that.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """The cell as spreadsheets will show it as text: a leading apostrophe (OWASP's advice) stops
    the formula, and is the same marker you'd type by hand. Numbers and plain text pass through."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def known_domains() -> set:
    """Websites / email domains already in the database, so we never scrape or email them twice."""
    conn = get_connection()
    rows = conn.execute("SELECT email, website, enriched_info FROM leads").fetchall()
    conn.close()
    domains = set()
    for row in rows:
        email_domain = row["email"].split("@")[-1].lower()
        if email_domain not in FREEMAIL_DOMAINS:
            domains.add(root_domain(email_domain))
        website = row["website"]
        if not website and row["enriched_info"]:
            try:
                website = json.loads(row["enriched_info"]).get("website", "")
            except ValueError:
                website = ""
        if website and "//" in website:
            domains.add(root_domain(website.split("/")[2].replace("www.", "")))
    return domains


def known_emails() -> set:
    """Every address already in the database, whatever its status."""
    conn = get_connection()
    rows = conn.execute("SELECT email FROM leads").fetchall()
    conn.close()
    return {row["email"].lower() for row in rows}


def save_leads(candidates: list, ideal_client: str) -> list:
    """Save scored leads (qualified as 'new', others as 'disqualified'); skip known emails.
    `ideal_client` fills in a missing title/category. Returns the saved rows with their ids."""
    conn = get_connection()
    cursor = conn.cursor()
    saved = []
    created_at = datetime.now(timezone.utc).isoformat()

    for item in candidates:
        email = item.get("email", "").strip().lower()
        if not email:
            continue
        cursor.execute("SELECT id FROM leads WHERE email = ?", (email,))
        if cursor.fetchone():
            continue

        enriched_data = {
            "lead_source": item.get("source", "Live Scraper"),
            "website": item.get("website", ""),
            "mx_verified": item.get("mx_verified", ""),
        }
        lead = {
            "name": item.get("name", "")[:100],
            "first_name": item.get("first_name"),
            "title": item.get("title") or ideal_client,
            "company": (item.get("company") or item.get("name", ""))[:100],
            "email": email,
            "category": item.get("category") or ideal_client,
            "location": item.get("location") or "",
            "website": item.get("website", ""),
            "fit_score": item.get("fit_score"),
            "fit_reasons": item.get("fit_reasons", []),
            "status": item.get("status", "new"),
            "source": item.get("source", "Live Scraper"),
            "opener": item.get("opener"),
        }
        try:
            cursor.execute("""
                INSERT INTO leads (name, first_name, title, company, email, phone, category, location,
                                   website, fit_score, fit_reasons, enriched_info, status, opener, created_at)
                VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (lead["name"], lead["first_name"], lead["title"], lead["company"], email,
                  lead["category"], lead["location"], lead["website"], lead["fit_score"],
                  json.dumps(lead["fit_reasons"]), json.dumps(enriched_data), lead["status"],
                  lead["opener"], created_at))
            saved.append({**lead, "id": cursor.lastrowid})
        except Exception as e:
            log_event("LeadGenPipeline", "SaveError", "warning", f"Failed to save {email}: {e}")

    conn.commit()
    conn.close()
    return saved


def export_leads_to_csv(path: str | None = None) -> str:
    """Write every lead to data/leads_export.csv (git-ignored and owner-only: it holds personal
    data). The header is the table's own column list, so it always matches the rows."""
    path = path or EXPORT_PATH
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM leads ORDER BY id DESC")
    header = [column[0] for column in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    with open_private(path, newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow([csv_safe(value) for value in tuple(row)])

    return path
