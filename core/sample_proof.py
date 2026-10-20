"""Proof from a real email: after `sdr test-email`, find the delivered sample in the owner's own
mailbox (read-only, by its Message-ID) and read the verdict the receiving server recorded in
Authentication-Results: did SPF, DKIM and DMARC pass, and did it land in the inbox or in spam?

Mail sent to a mailbox on the same server is sometimes delivered internally without that header;
then we say so and suggest sending the sample to a Gmail or Outlook address instead.
"""

from __future__ import annotations

import imaplib
import os
import time
from typing import Callable

from core.deliverability import parse_authentication_results

FOLDERS = ("INBOX", "[Gmail]/Spam", "Junk", "Spam", "Junk Email", "INBOX.Junk", "INBOX.Spam")
SPAM_FOLDERS = {f for f in FOLDERS if f != "INBOX"}


def _default_connect():
    from core.config import mail_server
    from core.email_checks import tls_context
    host, port = mail_server("imap")
    mail = imaplib.IMAP4_SSL(host, port, ssl_context=tls_context(), timeout=30)
    mail.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
    return mail


def _header_bytes(data) -> bytes:
    for part in data or []:
        if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
            return part[1]
    return b""


def _look(mail, message_id: str) -> dict | None:
    for folder in FOLDERS:
        try:
            status, _ = mail.select(folder, readonly=True)
        except imaplib.IMAP4.error:
            continue
        if status != "OK":
            continue
        status, found = mail.search(None, "HEADER", "Message-ID", message_id)
        ids = (found[0] or b"").split() if status == "OK" and found else []
        if not ids:
            continue
        status, data = mail.fetch(ids[-1], "(BODY.PEEK[HEADER.FIELDS (AUTHENTICATION-RESULTS)])")
        raw = _header_bytes(data).decode("utf-8", "replace")
        value = raw.split(":", 1)[1] if ":" in raw else ""
        return {**parse_authentication_results(value), "folder": folder, "spam": folder in SPAM_FOLDERS}
    return None


def fetch_auth_verdict(message_id: str, *, connect: Callable | None = None, attempts: int = 4, wait: float = 5,
                       sleep: Callable[[float], None] = time.sleep) -> dict | None:
    """{"spf","dkim","dmarc","seen_by","folder","spam"} for the delivered sample, or None if it
    can't be found (not arrived yet, or the mailbox can't be read). Never raises."""
    try:
        mail = (connect or _default_connect)()
    except (OSError, imaplib.IMAP4.error, ValueError):
        return None
    try:
        for attempt in range(attempts):
            if attempt:
                sleep(wait)
            try:
                found = _look(mail, message_id)
            except (OSError, imaplib.IMAP4.error):
                return None
            if found is not None:
                return found
        return None
    finally:
        try:
            mail.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def describe_verdict(verdict: dict | None) -> str:
    """One or two plain sentences about the sample's verdict."""
    if not verdict:
        return "Couldn't find the sample in your mailbox yet, so there's no verdict to show."
    where = "in your spam folder" if verdict.get("spam") else "in your inbox"
    if not any(verdict.get(k) for k in ("spf", "dkim", "dmarc")):
        return (f"The sample arrived {where}, but your mail server didn't record an SPF/DKIM verdict for mail sent to "
                "itself. For a full check, send a sample to a Gmail or Outlook address: sdr test-email --to you@gmail.com")
    failed = [k.upper() for k in ("spf", "dkim", "dmarc") if verdict.get(k) and verdict[k] != "pass"]
    seen = f" ({verdict['seen_by']})" if verdict.get("seen_by") else ""
    if not failed:
        return f"The sample arrived {where}{seen}: SPF, DKIM and DMARC all passed."
    return (f"The sample arrived {where}{seen}, but {', '.join(failed)} didn't pass "
            f"({', '.join(f'{k.upper()}={verdict[k]}' for k in ('spf', 'dkim', 'dmarc') if verdict.get(k))}). "
            "Run `sdr doctor` to see how to fix it.")
