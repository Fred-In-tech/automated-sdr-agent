"""Import your own leads from a CSV file (`sdr import`, or "Import leads" on the dashboard).

People arrive with a list already: a spreadsheet, a CRM export, a conference sheet. Imported
leads join the same pipeline as found ones, so every safeguard still applies to them: the
do-not-contact list, unsubscribes and bounces already on record, the daily limit and the
bounce-rate pause. Nothing here sends email.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Callable

from core.email_verifier import is_dummy_or_placeholder_email, is_valid_email_syntax, verify_lead_email
from core.outreach_rules import is_do_not_contact, load_do_not_contact
from core.qualify import extract_first_name, mailbox_kind

MAX_IMPORT_BYTES = 1024 * 1024   # about 10,000 leads; bigger files are split by the person
MAX_IMPORT_ROWS = 5000
MAX_REPORTED_PROBLEMS = 50       # the summary lists the first ones and counts the rest
# You picked these people yourself, so they go out ahead of borderline found leads. A score
# above zero also makes their bounces count toward the bounce-rate pause.
IMPORTED_FIT_SCORE = 70
IMPORT_SOURCE = "Imported"
TEMPLATE_CSV = ("email,first_name,company,title,website,location\n"
                "jamie@brightline.example,Jamie,Brightline Events,Owner,https://brightline.example,\"Austin, TX\"\n")

# Column names as spreadsheets and CRMs export them -> our field. Compared lower-case with
# spaces, dashes and underscores removed, so "E-mail Address" and "email_address" both match.
COLUMN_ALIASES = {
    "email": ("email", "emailaddress", "mail", "workemail", "contactemail", "primaryemail"),
    "first_name": ("firstname", "first", "givenname"),
    "last_name": ("lastname", "last", "surname", "familyname"),
    "name": ("name", "fullname", "contact", "contactname"),
    "company": ("company", "companyname", "business", "businessname", "organization", "organisation",
                "account", "accountname", "studio"),
    "title": ("title", "jobtitle", "role", "position"),
    "website": ("website", "url", "site", "web", "domain", "companywebsite"),
    "location": ("location", "city", "citystate", "region", "area"),
    "phone": ("phone", "phonenumber", "mobile", "tel", "telephone"),
}


class LeadImportError(ValueError):
    """The file can't be imported at all (too big, no email column...). The message says why."""


@dataclass
class ImportSummary:
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    blocked: int = 0          # on the do-not-contact list
    invalid: int = 0
    problems: list[str] = field(default_factory=list)
    dry_run: bool = False

    def note(self, line: int, email: str, reason: str) -> None:
        if len(self.problems) < MAX_REPORTED_PROBLEMS:
            self.problems.append(f"Row {line}: {email or '(no email)'}: {reason}")

    def as_dict(self) -> dict:
        skipped = self.duplicates + self.blocked + self.invalid
        return {"total": self.total, "imported": self.imported, "duplicates": self.duplicates,
                "blocked": self.blocked, "invalid": self.invalid, "skipped": skipped,
                "problems": self.problems, "more_problems": max(0, self.invalid + self.blocked - len(self.problems)),
                "dry_run": self.dry_run}


def _key(header: str) -> str:
    return "".join(ch for ch in (header or "").lower() if ch.isalnum())


def map_columns(headers: list[str]) -> dict[str, str]:
    """{our field: their column name}. The first matching column wins."""
    lookup = {alias: name for name, aliases in COLUMN_ALIASES.items() for alias in aliases}
    mapping: dict[str, str] = {}
    for header in headers:
        target = lookup.get(_key(header))
        if target and target not in mapping:
            mapping[target] = header
    return mapping


def parse_leads_csv(text: str) -> list[dict]:
    """Rows of a CSV as {line, email, first_name, company, title, website, location, phone}.
    Accepts comma, semicolon or tab separated files (Excel in Europe saves semicolons)."""
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise LeadImportError("That file is larger than 1 MB. Split it into smaller files and import them one by one.")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise LeadImportError("That file is empty.")
    first_line = text.split("\n", 1)[0]
    delimiter = max(",;\t", key=first_line.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = map_columns(list(reader.fieldnames or []))
    if "email" not in columns:
        raise LeadImportError('No "email" column found. The first row must name the columns, e.g.: '
                           "email, first_name, company, title, website, location")

    def cell(row: dict, name: str) -> str:
        return (row.get(columns.get(name, ""), "") or "").strip()

    rows = []
    for row in reader:
        if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
            continue   # blank line
        if len(rows) >= MAX_IMPORT_ROWS:
            raise LeadImportError(f"That file has more than {MAX_IMPORT_ROWS} leads. Split it into smaller files.")
        first, last, full = cell(row, "first_name"), cell(row, "last_name"), cell(row, "name")
        if not first and full:
            first = full.split()[0]
        rows.append({
            "line": reader.line_num,
            "email": cell(row, "email").lower(),
            "first_name": first,
            "name": full or " ".join(part for part in (first, last) if part),
            "company": cell(row, "company"),
            "title": cell(row, "title"),
            "website": cell(row, "website"),
            "location": cell(row, "location"),
            "phone": cell(row, "phone"),
        })
    if not rows:
        raise LeadImportError("That file has column names but no leads under them.")
    return rows


def _company_from(email: str) -> str:
    """"hello@brightline-events.com" -> "Brightline Events", for rows without a company."""
    domain = email.rsplit("@", 1)[-1].split(".")[0]
    return domain.replace("-", " ").replace("_", " ").title()


def _check(email: str, check_mail_server: bool, verify: Callable[[str], tuple[bool, str]]) -> str | None:
    """Why this address can't be imported, or None when it can."""
    if not email:
        return "no email address"
    if not is_valid_email_syntax(email):
        return "not a valid email address"
    if is_dummy_or_placeholder_email(email):
        return "a placeholder address, not a real mailbox"
    if mailbox_kind(email.split("@", 1)[0]) == "blocked":
        return "a no-reply / press / legal style mailbox that never reaches a person"
    if check_mail_server:
        ok, why = verify(email)
        if not ok:
            return f"its domain can't receive email ({why})"
    return None


def import_leads(rows: list[dict], profile: dict, *, dry_run: bool = False, check_mail_server: bool = True,
                 verify: Callable[[str], tuple[bool, str]] | None = None,
                 blocked: set | None = None, known: set | None = None,
                 save: Callable[[list, str], list] | None = None) -> ImportSummary:
    """Validate the rows and save the good ones as new leads. `dry_run` only reports.

    A lead already in the database is skipped whatever its status, so someone who unsubscribed,
    bounced or said no can never be brought back by importing them again."""
    from bots.lead_store import known_emails, save_leads   # imported late: bots imports core

    verify = verify or verify_lead_email   # looked up now, so tests can swap the network check
    summary = ImportSummary(total=len(rows), dry_run=dry_run)
    blocked = load_do_not_contact() if blocked is None else blocked
    known = set(known_emails() if known is None else known)
    ideal_client = profile["targeting"]["ideal_client"]
    accepted = []
    by_domain: dict[str, tuple[bool, str]] = {}

    def verify_once(email: str) -> tuple[bool, str]:
        """One mail-server lookup per domain: a list from one company is one DNS question."""
        domain = email.rsplit("@", 1)[-1]
        if domain not in by_domain:
            by_domain[domain] = verify(email)
        return by_domain[domain]

    for row in rows:
        email = row["email"]
        if email in known:
            summary.duplicates += 1
            continue
        if email and is_do_not_contact(email, blocked):
            summary.blocked += 1
            summary.note(row["line"], email, "on your do-not-contact list")
            continue
        problem = _check(email, check_mail_server, verify_once)
        if problem:
            summary.invalid += 1
            summary.note(row["line"], email, problem)
            continue
        known.add(email)   # the same address twice in one file counts once
        company = row["company"] or _company_from(email)
        first_name = row["first_name"] or extract_first_name(email.split("@", 1)[0], "", company)
        accepted.append({
            "email": email, "first_name": first_name or None, "name": row["name"] or company,
            "company": company, "title": row["title"], "website": row["website"],
            "location": row["location"], "category": ideal_client, "source": IMPORT_SOURCE,
            "fit_score": IMPORTED_FIT_SCORE, "fit_reasons": ["imported by you"], "status": "new",
            "mx_verified": "yes" if check_mail_server else "not checked",
        })
    if dry_run:
        summary.imported = len(accepted)
        return summary
    summary.imported = len((save or save_leads)(accepted, ideal_client))
    return summary


def import_csv_text(text: str, profile: dict, **options) -> ImportSummary:
    """Parse and import in one go (what the dashboard and `sdr import` call)."""
    return import_leads(parse_leads_csv(text), profile, **options)
