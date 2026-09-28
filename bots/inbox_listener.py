"""The SDR's inbox: triage every reply from a lead and act on it.

- Reads mail WITHOUT marking it read (you still see replies as unread) and remembers
  what it has handled, so nothing is processed twice or missed if you read it first.
- Ignores anything that isn't a lead (Instagram/TikTok notifications, newsletters, your own
  alerts) and anything automated (out-of-office, "we received your message" desks).
- Classifies real replies (core/intent.py, or Claude when enabled) and acts:
    interested / question -> approved reply from your profile + HOT LEAD alert to you
    not_now               -> polite reply, sequence stops
    not_interested / unsubscribe -> never emailed again, no reply
    wrong_person / other  -> alert you, no auto-reply (a human should answer)
"""

import os
import re
import json
import imaplib
import email
import email.utils
import hashlib
from email.header import decode_header, make_header
from datetime import datetime, timedelta, timezone
from core.db import get_connection, log_event, init_db
from core.config import load_env_file, load_profile, mail_server, render, template_values
from core.email_checks import tls_context
from core.intent import automated_reason, classify_reply
from core.notifications import NotificationManager
from core.qualify import FREEMAIL_DOMAINS
from core.outreach_rules import add_tracking
from core.email_design import BUTTON_TOKEN, email_style, render_branded_html, render_personal_html
from bots.email_marketing import EmailMarketingEngine, text_to_html

# Delivery-failure senders (newsletters and notifications are handled as "not a lead" instead)
BOUNCE_SENDER_SIGNATURES = ["mailer-daemon", "postmaster", "mail-daemon"]

BOUNCE_SUBJECT_SIGNATURES = [
    "delivery status notification", "undelivered mail", "failure notice",
    "returned mail", "mail delivery failed", "address rejected", "unrouteable address"
]

HEADER_FIELDS = ("FROM SUBJECT DATE MESSAGE-ID AUTO-SUBMITTED PRECEDENCE LIST-UNSUBSCRIBE LIST-ID "
                 "X-AUTOREPLY X-AUTORESPOND")
# Leads we have never emailed (or rejected) aren't expecting anything from us
NOT_CONTACTED = ("new", "disqualified", "invalid_email")
SUPPRESSED = ("unsubscribed", "not_interested", "bounced")
FIRST_REPLY_STATUSES = ("contacted", "no_response", "nurtured")
HOT_INTENTS = ("interested", "question")

QUOTE_START_RE = re.compile(
    r"^(-{2,}\s*original message\s*-{2,}|from:\s|_{5,}|sent from my )", re.IGNORECASE
)


def fallback_message_id(from_email: str, subject: str, date: str) -> str:
    """Id for mail that has no Message-ID header, stable across runs.

    SHA-1 here is a dedup fingerprint, not a security control (usedforsecurity=False). It must
    stay SHA-1: existing databases already store these ids, and a new hash would make the
    inbox handle old replies a second time."""
    fingerprint = f"{from_email}|{subject}|{date}".encode()
    return "gen-" + hashlib.sha1(fingerprint, usedforsecurity=False).hexdigest()


def latest_reply_text(body: str) -> str:
    """Only the new text of a reply — drops the quoted email history below it."""
    lines = body.replace("\r", "").split("\n")
    kept = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        is_on_wrote = stripped.lower().startswith("on ") and (
            stripped.endswith("wrote:") or next_line.endswith("wrote:")
        )
        if stripped.startswith(">") or is_on_wrote or QUOTE_START_RE.match(stripped):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def is_opt_out(text: str, opt_out_words: list) -> bool:
    return classify_reply(text, opt_out_words) == "unsubscribe"


def reply_template(profile: dict, intent: str, reply_text: str = "") -> str | None:
    """The approved reply template for this intent from [auto_reply.replies], or None (a human
    should answer). Older profiles with [[auto_reply.rules]] + default_reply are still supported."""
    auto_reply = profile.get("auto_reply", {})
    replies = auto_reply.get("replies")
    if replies is not None:
        template = replies.get(intent)
    elif intent in HOT_INTENTS:
        template = auto_reply.get("default_reply")
        for rule in auto_reply.get("rules", []):
            if any(re.search(r"\b" + re.escape(k.lower()), reply_text.lower()) for k in rule.get("keywords", [])):
                template = rule["reply"]
                break
    else:
        template = None
    return template


def build_reply(profile: dict, intent: str, lead: dict | None, reply_text: str = "") -> tuple[str, str] | None:
    """(plain text, html) of the approved reply, or None. Intents listed in [auto_reply]
    branded_intents get the branded design (e.g. the welcome email for "send me the link");
    {{button}} uses [auto_reply.buttons].<intent> = { text, url }."""
    template = reply_template(profile, intent, reply_text)
    if not template:
        return None
    auto_reply = profile.get("auto_reply", {})
    values = template_values(profile, lead)
    tag = f"reply-{intent}"
    button = None
    if auto_reply.get("buttons", {}).get(intent):
        config = auto_reply["buttons"][intent]
        button = {"text": render(config["text"], values), "url": add_tracking(render(config["url"], values), profile, tag)}
    text = add_tracking(render(template, {**values, "button": f"{button['text']}: {button['url']}" if button else ""}),
                        profile, tag)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    html_source = add_tracking(render(template, {**values, "button": BUTTON_TOKEN}), profile, tag)
    if intent in auto_reply.get("branded_intents", []) or email_style(profile) == "branded":
        html_body = render_branded_html(profile, html_source, button, [])
    else:
        html_body = render_personal_html(html_source, button, [])
    return text, html_body


def pick_auto_reply(profile: dict, intent: str, lead: dict | None, reply_text: str = "") -> str | None:
    """Plain-text version of the approved reply (see build_reply)."""
    reply = build_reply(profile, intent, lead, reply_text)
    return reply[0] if reply else None


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _person_first_name(display_name: str) -> str | None:
    """'Sarah Jones' -> 'Sarah' (only when the sender's display name looks like a person)."""
    parts = display_name.replace('"', "").split()
    if 2 <= len(parts) <= 3 and all(p.isalpha() for p in parts) and parts[0][0].isupper():
        return parts[0]
    return None


class InboxListenerEngine:
    def __init__(self, profile: dict | None = None):
        init_db()
        load_env_file()
        self.profile = profile or load_profile()
        self.auto_reply = self.profile.get("auto_reply", {})
        self.email_user = (os.getenv("SMTP_USER") or "").lower()
        self.email_pass = os.getenv("SMTP_PASS")
        # No default host: a login without its inbox server stops here with a fix-it message (core.config).
        self.imap_host, self.imap_port = mail_server("imap")
        self.email_engine = EmailMarketingEngine(self.profile)
        self.notifier = NotificationManager()
        sdr = self.profile.get("sdr", {})
        self.alert_email = sdr.get("alert_email") or os.getenv("SDR_ALERT_EMAIL") or self.email_user
        self.lookback_days = int(self.auto_reply.get("lookback_days", 14))
        self.classify = self._load_classifier()

    def _load_classifier(self):
        """Claude-powered triage when [ai] is enabled and a key is set, else the offline rules."""
        opt_out_words = self.auto_reply.get("opt_out_words", ["unsubscribe"])
        try:
            from core.ai import ai_enabled, classify_reply_with_ai
            if ai_enabled(self.profile):
                return lambda text: classify_reply_with_ai(self.profile, text, opt_out_words)
        except ImportError:
            pass
        return lambda text: classify_reply(text, opt_out_words)

    # ── helpers ────────────────────────────────────────────────────────────
    def is_bounce(self, from_email: str, subject: str) -> bool:
        """Determines if an incoming email is an automated delivery failure bounce."""
        from_lower = from_email.lower()
        subj_lower = subject.lower()
        if any(sig in from_lower for sig in BOUNCE_SENDER_SIGNATURES):
            return True
        return any(sig in subj_lower for sig in BOUNCE_SUBJECT_SIGNATURES)

    def extract_bounced_recipient(self, text: str) -> str:
        """Extracts the original recipient email that failed from bounce report text."""
        patterns = [
            r'Final-Recipient:\s*rfc822;\s*([^\s<>;]+@[^\s<>;]+)',
            r'Original-Recipient:\s*rfc822;\s*([^\s<>;]+@[^\s<>;]+)',
            r'To:\s*([^\s<>;]+@[^\s<>;]+)',
            r'<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>'
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                extracted = match.group(1).strip()
                if not any(b in extracted.lower() for b in ["noreply", "mailer-daemon", "postmaster"]):
                    return extracted
        return ""

    def _load_leads(self, cursor) -> tuple[dict, dict]:
        """Leads by exact address, and *active* leads (emailed, not suppressed) by company domain."""
        cursor.execute("SELECT * FROM leads ORDER BY COALESCE(last_contacted_at, created_at) DESC")
        by_email, by_domain = {}, {}
        for row in cursor.fetchall():
            lead = dict(row)
            by_email[lead["email"].lower()] = lead
            domain = lead["email"].split("@")[-1].lower()
            if domain not in FREEMAIL_DOMAINS and lead["status"] not in NOT_CONTACTED + SUPPRESSED:
                by_domain.setdefault(domain, []).append(lead)  # most recently contacted first
        return by_email, by_domain

    def _match_lead(self, from_email: str, by_email: dict, by_domain: dict) -> tuple[dict | None, list]:
        """(lead the reply is about, other active leads at the same company)."""
        colleagues = [l for l in by_domain.get(from_email.split("@")[-1], [])
                      if l["status"] not in NOT_CONTACTED + SUPPRESSED]
        lead = by_email.get(from_email) or (colleagues[0] if colleagues else None)
        return lead, [l for l in colleagues if lead is None or l["id"] != lead["id"]]

    def _body_text(self, mail, imap_id: bytes) -> str:
        status, data = mail.fetch(imap_id, "(BODY.PEEK[])")
        for part in data:
            if isinstance(part, tuple):
                msg = email.message_from_bytes(part[1])
                texts = []
                for sub in (msg.walk() if msg.is_multipart() else [msg]):
                    if sub.get_content_type() in ("text/plain", "text/rfc822-headers", "message/delivery-status"):
                        payload = sub.get_payload(decode=True) or b""
                        texts.append(payload.decode(sub.get_content_charset() or "utf-8", errors="ignore"))
                return "\n".join(texts)
        return ""

    def _record(self, cursor, message_id: str, lead: dict | None, from_email: str, subject: str,
                excerpt: str, intent: str, action: str) -> None:
        cursor.execute("""
            INSERT OR IGNORE INTO inbound_messages (message_id, lead_id, from_email, subject, excerpt, intent, action, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, lead["id"] if lead else None, from_email, subject[:300], excerpt[:1000], intent, action,
              datetime.now(timezone.utc).isoformat()))

    def _alert(self, lead: dict, intent: str, reply_text: str, action: str) -> None:
        """Hand the conversation to a human: email + Telegram/Discord with everything they need."""
        hot = intent in HOT_INTENTS
        company = lead.get("company") or lead["email"]
        subject = f"{'🔥 HOT LEAD' if hot else '💬 Reply'}: {company} ({intent.replace('_', ' ')})"
        try:
            reasons = ", ".join(json.loads(lead.get("fit_reasons") or "[]"))
        except ValueError:
            reasons = ""
        body = (
            f"{company} replied to your outreach.\n\n"
            f"Who:      {lead.get('first_name') or ''} <{lead['email']}>\n"
            f"Website:  {lead.get('website') or '-'}\n"
            f"Location: {lead.get('location') or '-'}\n"
            f"Fit:      {lead.get('fit_score') if lead.get('fit_score') is not None else '-'}/100 {reasons}\n"
            f"Intent:   {intent.replace('_', ' ')}\n"
            f"Bot did:  {action}\n\n"
            f"Their message:\n" + "\n".join(f"> {line}" for line in reply_text.splitlines()[:25]) + "\n\n"
            + ("Next step: reply personally while they're warm — speed wins deals."
               if hot else "Next step: have a look and reply if it needs a human answer.")
        )
        if self.alert_email:
            self.email_engine.send_real_email(self.alert_email, subject, text_to_html(body), body)
        self.notifier.notify_all(subject, body[:900])

    def _act(self, cursor, lead: dict, intent: str, reply_text: str, their_subject: str, message_id: str,
             from_name: str) -> str:
        """Apply the SDR playbook for one reply (updates `lead` in place). Returns what was done."""
        first_reply = lead["status"] in FIRST_REPLY_STATUSES
        person = {**lead, "first_name": lead.get("first_name") or _person_first_name(from_name)}
        new_status = {
            "interested": "interested", "question": "interested", "not_now": "not_now",
            "not_interested": "not_interested", "unsubscribe": "unsubscribed",
        }.get(intent, "replied")
        # "Not now" gets one polite check-in later ([outreach.not_now_follow_up]); everything else stops.
        check_in = None
        nurture = self.profile.get("outreach", {}).get("not_now_follow_up")
        if intent == "not_now" and nurture and lead["status"] != "nurtured":
            check_in = (datetime.now(timezone.utc) + timedelta(days=float(nurture.get("days", 60)))).isoformat()
        cursor.execute("UPDATE leads SET status = ?, next_touch_at = ? WHERE id = ?", (new_status, check_in, lead["id"]))
        lead["status"] = new_status  # later messages in this run are not a "first reply"

        if intent in ("unsubscribe", "not_interested"):
            return "stopped all emails to them"

        action = "no auto-reply — your turn"
        reply = None
        if first_reply and self.auto_reply.get("enabled", True):
            reply = build_reply(self.profile, intent, person, reply_text)
        if reply:
            reply_body, reply_html = reply
            subject = their_subject if their_subject.lower().startswith("re:") else f"Re: {their_subject}"
            headers = {"In-Reply-To": message_id, "References": message_id} if message_id else {}
            if self.email_engine.send_real_email(lead["email"], subject, reply_html, reply_body, headers):
                action = f"sent your approved '{intent}' reply"
            else:
                action = "auto-reply NOT sent (check email login) — your turn"
        if intent in HOT_INTENTS or intent in ("wrong_person", "other", "not_now"):
            self._alert(lead, intent, reply_text, action)
        return action

    # ── main loop ──────────────────────────────────────────────────────────
    def check_inbox_and_auto_reply(self, limit: int = 200) -> dict:
        """Process recent inbox mail from leads. Safe to run repeatedly."""
        if not self.email_user or not self.email_pass:
            print("⚠️  [IMAP Notice]: Email credentials not set in .env — inbox check skipped.")
            log_event("InboxListener", "CheckInbox", "info", "IMAP check skipped - credentials not set.")
            return {"status": "info", "message": "Email credentials not set in .env", "replies_sent": 0}

        conn = None
        summary = {"bounces": [], "unsubscribed": [], "hot_leads": [], "replies": [], "ignored_automated": 0}
        try:
            # imaplib's own default context skips certificate and hostname checks, which would hand
            # the mailbox password to anyone on the same network; verify like the login check does.
            mail = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=tls_context(), timeout=30)
            mail.login(self.email_user, self.email_pass)
            mail.select("inbox", readonly=True)  # read-only: never changes your read/unread flags

            since = (datetime.now() - timedelta(days=self.lookback_days)).strftime("%d-%b-%Y")
            status, response = mail.search(None, f"(SINCE {since})")
            imap_ids = response[0].split()[-limit:]
            if not imap_ids:
                mail.logout()
                return {"status": "info", "message": "No recent emails.", "replies_sent": 0}

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT message_id FROM inbound_messages")
            processed = {row[0] for row in cursor.fetchall()}
            leads_by_email, leads_by_domain = self._load_leads(cursor)

            status, data = mail.fetch(b",".join(imap_ids), f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
            headers_by_id = []
            for part in data:
                if isinstance(part, tuple):
                    imap_id = part[0].split()[0]
                    headers_by_id.append((imap_id, email.message_from_bytes(part[1])))

            for imap_id, hdr in headers_by_id:
                from_name, from_email = email.utils.parseaddr(_decode(hdr.get("From", "")))
                from_email = from_email.lower()
                subject = _decode(hdr.get("Subject", "")).strip()
                # Stable id even for mail without a Message-ID (IMAP sequence numbers change between runs)
                message_id = (hdr.get("Message-ID") or "").strip() or fallback_message_id(
                    from_email, subject, hdr.get("Date", ""))
                own_addresses = {self.email_user, (self.email_engine.from_email or "").lower()}
                if message_id in processed or not from_email or from_email in own_addresses:
                    continue

                # 1. Bounces: mark the lead so we never email that address again
                if self.is_bounce(from_email, subject):
                    target = self.extract_bounced_recipient(self._body_text(mail, imap_id)).lower()
                    bounced_lead = leads_by_email.get(target)
                    if bounced_lead:
                        cursor.execute("UPDATE leads SET status = 'bounced', next_touch_at = NULL WHERE id = ?", (bounced_lead["id"],))
                        bounced_lead["status"] = "bounced"
                        summary["bounces"].append(target)
                    self._record(cursor, message_id, bounced_lead, from_email, subject, "", "bounce",
                                 "marked bounced" if bounced_lead else "not one of your leads")
                    conn.commit()
                    continue

                # 2. Only mail from leads we've actually emailed (exact address, or a colleague at their domain)
                lead, colleagues = self._match_lead(from_email, leads_by_email, leads_by_domain)
                if not lead or lead["status"] in NOT_CONTACTED or lead["status"] in SUPPRESSED:
                    continue

                # 3. Autoresponders / out-of-office: note it, keep the sequence going
                headers = {k: _decode(v) for k, v in hdr.items()}
                body = self._body_text(mail, imap_id)
                reply_text = latest_reply_text(body)
                reason = automated_reason(headers, subject, reply_text)
                if reason:
                    summary["ignored_automated"] += 1
                    self._record(cursor, message_id, lead, from_email, subject, reply_text, "auto_reply", f"ignored ({reason})")
                    conn.commit()
                    continue

                # 4. A real person replied — classify and act like an SDR
                intent = self.classify(f"{subject}\n{reply_text}" if "unsub" in subject.lower() else reply_text)
                action = self._act(cursor, lead, intent, reply_text, subject, hdr.get("Message-ID", ""), from_name)
                for colleague in colleagues:  # the company answered: stop the sequence for everyone there
                    cursor.execute("UPDATE leads SET status = ?, next_touch_at = NULL WHERE id = ?",
                                   (lead["status"], colleague["id"]))
                    colleague["status"] = lead["status"]
                self._record(cursor, message_id, lead, from_email, subject, reply_text, intent, action)
                conn.commit()
                entry = {"email": from_email, "company": lead.get("company"), "intent": intent, "action": action}
                summary["hot_leads" if intent in HOT_INTENTS else "replies"].append(entry)
                if intent in ("unsubscribe", "not_interested"):
                    summary["unsubscribed"].append(from_email)
                print(f"📬 [{intent}] {from_email}: {action}")

            mail.logout()
            text = (f"Inbox: {len(summary['hot_leads'])} hot leads, {len(summary['replies'])} other replies, "
                    f"{len(summary['unsubscribed'])} opted out, {len(summary['bounces'])} bounces, "
                    f"{summary['ignored_automated']} autoresponders ignored.")
            log_event("InboxListener", "ProcessInbox", "success", text)
            return {**summary, "summary": text}

        except Exception as e:
            print(f"❌ [IMAP Error]: {e}")
            log_event("InboxListener", "Error", "error", str(e))
            return {"status": "error", "error": str(e), "replies_sent": 0}
        finally:
            if conn is not None:
                conn.close()

if __name__ == "__main__":
    listener = InboxListenerEngine()
    res = listener.check_inbox_and_auto_reply()
    print(json.dumps(res, indent=2))
