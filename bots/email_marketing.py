import os
import re
import json
import time
import html
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid
from datetime import datetime, timedelta, timezone
from core.db import get_connection, log_event, init_db
from core.notifications import NotificationManager
from core.email_checks import tls_context
from core.email_verifier import verify_lead_email
from core.config import (
    DEFAULT_UNSUBSCRIBE_LINE,
    compliance_warnings,
    load_env_file,
    load_profile,
    mail_server,
    render,
    template_values,
)
from core.email_design import (BUTTON_TOKEN, email_style, render_branded_html, render_personal_html,
                               signature_variant)
from core.outreach_rules import (
    add_tracking,
    bounce_alarm,
    in_send_window,
    is_do_not_contact,
    load_do_not_contact,
    render_subject,
    subject_variant,
)

EMAIL_STYLE = "font-family: Arial, sans-serif; font-size: 15px; color: #111827; line-height: 1.6;"
URL_RE = re.compile(r"(https?://[^\s<]+)")


def _link(match) -> str:
    url, trail = match.group(0), ""
    while url and url[-1] in ".,;:!?)":  # "see https://x.com." -> link without the full stop
        url, trail = url[:-1], url[-1] + trail
    return f'<a href="{url}">{url}</a>{trail}'


def text_to_html(text: str) -> str:
    """Turn a plain-text email into simple HTML with clickable links."""
    linked = URL_RE.sub(_link, html.escape(text))
    paragraphs = "\n".join(f"  <p>{p.replace(chr(10), '<br>')}</p>" for p in linked.split("\n\n"))
    return f'<!DOCTYPE html>\n<html>\n<body style="{EMAIL_STYLE}">\n{paragraphs}\n</body>\n</html>'


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def sequence_length(profile: dict) -> int:
    """First email + every [[outreach.follow_ups]] entry."""
    return 1 + len(profile["outreach"].get("follow_ups", []))


def next_touch_at(profile: dict, step_sent: int, now: datetime) -> datetime | None:
    """When the follow-up after `step_sent` is due, or None if the sequence is finished."""
    follow_ups = profile["outreach"].get("follow_ups", [])
    if step_sent > len(follow_ups):
        return None
    return now + timedelta(days=float(follow_ups[step_sent - 1].get("days_after_previous", 3)))


def is_send_day(profile: dict, now: datetime) -> bool:
    days = profile["outreach"].get("send_days", WEEKDAYS[:5])
    return WEEKDAYS[now.astimezone().weekday()] in days


NURTURE = "nurture"


def _append_html_footer(html_body: str, footer_lines: list) -> str:
    footer_html = (
        '<p style="font-size: 12px; color: #6b7280; margin-top: 24px;">'
        + "<br>".join(html.escape(line) for line in footer_lines)
        + "</p>"
    )
    if "</body>" in html_body:
        return html_body.replace("</body>", f"  {footer_html}\n</body>")
    return html_body + footer_html


def build_outreach_email(profile: dict, lead: dict, step: int = 1, kind: str = "sequence") -> tuple[str, str, str]:
    """Render (subject, html_body, text_body) for one step of the sequence, with the compliance
    footer. Follow-ups (step >= 2) and the "not now" check-in reply in the same thread."""
    outreach = profile["outreach"]
    sender = profile["sender"]
    values = template_values(profile, lead)

    _label, subject_template = subject_variant(profile, lead.get("email", ""))
    first_subject = render_subject(subject_template, values)
    thread_subject = lead.get("thread_subject") or first_subject
    html_template = None
    if kind == NURTURE:
        step_config = outreach["not_now_follow_up"]
        subject = f"Re: {thread_subject}"
        link_tag = "nurture"
    elif step == 1:
        step_config = outreach
        subject = first_subject
        html_template = outreach.get("html_body")
        link_tag = "email1"
    else:
        step_config = outreach["follow_ups"][step - 2]
        subject = render_subject(step_config.get("subject", ""), values) or f"Re: {thread_subject}"
        link_tag = f"email{step}"

    # {{button}} in a body = a call-to-action: a styled button in HTML, "Text: link" in plain text
    button = None
    if step_config.get("button"):
        button = {"text": render(step_config["button"]["text"], values),
                  "url": add_tracking(render(step_config["button"]["url"], values), profile, link_tag)}
    button_as_text = f"{button['text']}: {button['url']}" if button else ""
    text_body = add_tracking(render(step_config["body"], {**values, "button": button_as_text}), profile, link_tag)

    footer_lines = [
        line for line in (
            sender.get("postal_address", ""),
            outreach.get("unsubscribe_line", DEFAULT_UNSUBSCRIBE_LINE),
        ) if line
    ]
    if html_template:
        escaped_values = {k: html.escape(str(v)) for k, v in values.items()}
        html_body = add_tracking(render(html_template, escaped_values), profile, link_tag, html=True)
        html_body = _append_html_footer(html_body, footer_lines)
    else:
        html_source = add_tracking(render(step_config["body"], {**values, "button": BUTTON_TOKEN}), profile, link_tag)
        if email_style(profile) == "branded":
            highlights = profile["email_design"].get("highlights", []) if step == 1 and kind != NURTURE else []
            html_body = render_branded_html(profile, html_source, button, footer_lines, highlights)
        else:
            # Signature A/B test: same email, with or without a small logo next to your name
            variant = lead.get("signature_variant") or signature_variant(profile, lead.get("email", ""))
            logo_url = profile.get("email_design", {}).get("signature_logo_url", "") if variant == "logo" else ""
            html_body = render_personal_html(html_source, button, footer_lines, sender["sign_off"], logo_url,
                                             sender["product_name"])

    # Legal footer only: mailing address + opt-out. The sender name is already in the From line
    # and the sign-off, so it isn't repeated here.
    text_body = re.sub(r"\n{3,}", "\n\n", text_body).strip() + "\n\n" + "\n".join(footer_lines)
    return subject, html_body, text_body


class EmailMarketingEngine:
    def __init__(self, profile: dict | None = None):
        init_db()
        load_env_file()
        self.profile = profile or load_profile()
        self.notifier = NotificationManager()

        # SMTP is the only way we send (any provider: Gmail, Outlook, Hostinger, Zoho, ...), so what
        # `sdr test-email` and the login check prove is exactly what the scheduled runs use.
        # No default host: a login without its server stops here with a fix-it message (core.config).
        self.smtp_host, self.smtp_port = mail_server("smtp")
        self.smtp_user = os.getenv("SMTP_USER")
        # Address people see and reply to. Can be an alias of the login mailbox (e.g. jamie@ on
        # hello@); the login mailbox stays the envelope sender, so bounces still arrive there.
        self.from_email = (self.profile["sender"].get("from_email") or os.getenv("SENDER_EMAIL")
                           or self.smtp_user)
        self.smtp_pass = os.getenv("SMTP_PASS")

        # Rate limiting — keep low while warming up a new sending address
        outreach = self.profile["outreach"]
        self.max_per_run = int(outreach.get("emails_per_run", 4))
        self.daily_limit = int(outreach.get("daily_send_limit", 20))
        self.send_interval_seconds = int(outreach.get("seconds_between_emails", 300))
        self.from_name = self.profile["sender"]["from_name"]
        self.total_steps = sequence_length(self.profile)

    def send_via_smtp(self, to_email: str, subject: str, html_body: str, text_body: str,
                      extra_headers: dict | None = None) -> bool:
        if not self.smtp_user or not self.smtp_pass:
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = f"{self.from_name} <{self.from_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        for key, value in (extra_headers or {}).items():
            msg[key] = value

        part_text = MIMEText(text_body, "plain", "utf-8")
        part_html = MIMEText(html_body, "html", "utf-8")

        msg.attach(part_text)
        msg.attach(part_html)

        try:
            context = tls_context()  # the same verifying, certifi-backed context the login check used
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=12) as server:
                    server.login(self.smtp_user, self.smtp_pass)
                    server.sendmail(self.smtp_user, to_email, msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=12) as server:
                    server.starttls(context=context)
                    server.login(self.smtp_user, self.smtp_pass)
                    server.sendmail(self.smtp_user, to_email, msg.as_string())
            print(f"✅ [SMTP Dispatched -> {to_email}] Subject: {subject}")
            return True
        except Exception as e:
            print(f"❌ [SMTP Error -> {to_email}]: {e}")
            return False

    def send_real_email(self, to_email: str, subject: str, html_body: str, text_body: str,
                        extra_headers: dict | None = None) -> bool:
        """Send one email over SMTP. Returns False (and sends nothing) without a login; a failed
        SMTP send is reported by send_via_smtp and also returns False — there is no fallback."""
        if self.smtp_user and self.smtp_pass:
            return self.send_via_smtp(to_email, subject, html_body, text_body, extra_headers)

        print("⚠️  [Dispatch Notice]: Email credentials not set in .env — nothing was sent.")
        print(f"[DRY-RUN -> {to_email}] Subject: {subject}")
        return False

    def _sent_today(self, cursor) -> int:
        local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        cursor.execute("SELECT COUNT(*) FROM email_logs WHERE sent_at >= ?",
                       (local_midnight.astimezone(timezone.utc).isoformat(),))
        return cursor.fetchone()[0]

    def _pick_leads(self, cursor, budget: int, region: str | None) -> list:
        """Due follow-ups first (finish what we started), then the best-fit new leads.

        The optional region filter is `(? IS NULL OR location LIKE ?)` so both queries stay fixed
        SQL text: no region binds NULL (filter off), a region binds its LIKE pattern."""
        now_iso = datetime.now(timezone.utc).isoformat()
        region_like = f"%{region}%" if region else None
        cursor.execute("""
            SELECT * FROM leads WHERE status IN ('contacted', 'not_now') AND next_touch_at IS NOT NULL
              AND next_touch_at <= ? AND (? IS NULL OR location LIKE ?) ORDER BY next_touch_at LIMIT ?
        """, [now_iso, region_like, region_like, budget])
        leads = [dict(r) for r in cursor.fetchall()]
        if len(leads) < budget:
            cursor.execute("""
                SELECT * FROM leads WHERE status = 'new' AND (? IS NULL OR location LIKE ?)
                ORDER BY COALESCE(fit_score, 0) DESC, id LIMIT ?
            """, [region_like, region_like, budget - len(leads)])
            leads += [dict(r) for r in cursor.fetchall()]
        return leads

    def _signature_variant(self, lead: dict) -> str | None:
        design = self.profile.get("email_design", {})
        if email_style(self.profile) != "personal" or not design.get("signature_logo_url"):
            return None
        return signature_variant(self.profile, lead["email"])

    def _record_send(self, cursor, lead: dict, step: int, subject: str, text_body: str, message_id: str,
                     kind: str = "sequence") -> None:
        now = datetime.now(timezone.utc)
        cursor.execute("""
            INSERT INTO email_logs (campaign_id, lead_id, lead_email, subject, body, status, sent_at, step, message_id)
            VALUES (1, ?, ?, ?, ?, 'sent', ?, ?, ?)
        """, (lead["id"], lead["email"], subject, text_body, now.isoformat(),
              0 if kind == NURTURE else step, message_id))
        if kind == NURTURE:  # one polite check-in, then we're done unless they reply
            cursor.execute("UPDATE leads SET status = 'nurtured', last_contacted_at = ?, next_touch_at = NULL "
                           "WHERE id = ?", (now.isoformat(), lead["id"]))
            return
        following = next_touch_at(self.profile, step, now)
        label, _template = subject_variant(self.profile, lead["email"])
        cursor.execute("""
            UPDATE leads SET status = ?, sequence_step = ?, last_contacted_at = ?, next_touch_at = ?,
                thread_subject = COALESCE(thread_subject, ?), thread_message_id = COALESCE(thread_message_id, ?),
                variant = COALESCE(variant, ?), signature_variant = COALESCE(signature_variant, ?)
            WHERE id = ?
        """, ("contacted" if following else "no_response", step, now.isoformat(),
              following.isoformat() if following else None, subject, message_id, label or None,
              self._signature_variant(lead), lead["id"]))

    def run_outreach_campaign(self, campaign_id: int = None, limit: int | None = None, region: str = None) -> dict:
        """Send the next email of the sequence to each lead that's due, within the daily cap."""
        now = datetime.now(timezone.utc)
        warnings = compliance_warnings(self.profile)
        blocking = bool(warnings) and self.profile["sender"].get("require_postal_address", True)
        for warning in warnings:
            print(f"⚠️  [Compliance]: {warning}")
            log_event("EmailMarketingEngine", "Compliance", "error" if blocking else "warning", warning)
        if blocking:
            return {"status": "blocked", "message": "Cold email paused until [sender] postal_address is set "
                    "in config/profile.toml (legally required).", "sent_count": 0}

        if not is_send_day(self.profile, now):
            log_event("EmailMarketingEngine", "RunCampaign", "info", "Not a send day — no emails sent.")
            return {"status": "info", "message": "Not a send day (see [outreach] send_days).", "sent_count": 0}
        if not in_send_window(self.profile, now):
            window = self.profile["outreach"].get("send_window", "08:00-17:00")
            log_event("EmailMarketingEngine", "RunCampaign", "info", f"Outside sending hours ({window}).")
            return {"status": "info", "message": f"Outside sending hours ({window}).", "sent_count": 0}

        conn = get_connection()
        cursor = conn.cursor()
        alarm = bounce_alarm(cursor, self.profile)
        if alarm:
            conn.close()
            print(f"🛑 {alarm}")
            log_event("EmailMarketingEngine", "BounceAlarm", "error", alarm)
            self.notifier.notify_all("🛑 Outreach paused: high bounce rate", alarm)
            return {"status": "blocked", "message": alarm, "sent_count": 0}
        sent_today = self._sent_today(cursor)
        budget = min(limit or self.max_per_run, self.max_per_run, self.daily_limit - sent_today)
        if budget <= 0:
            conn.close()
            log_event("EmailMarketingEngine", "RunCampaign", "info", f"Daily limit reached ({sent_today}/{self.daily_limit}).")
            return {"status": "info", "message": f"Daily limit reached ({sent_today}/{self.daily_limit}).", "sent_count": 0}

        target_leads = self._pick_leads(cursor, budget, region)
        if not target_leads:
            conn.close()
            log_event("EmailMarketingEngine", "RunCampaign", "info", "No leads due for an email right now.")
            return {"status": "info", "message": "No leads due for an email right now.", "sent_count": 0}

        sender_domain = (self.from_email or "localhost").split("@")[-1]
        # A List-Unsubscribe header tells Gmail "this is bulk mail" (Promotions). Low-volume 1:1 sales
        # email doesn't need it (the reply-to-opt-out line covers the law); opt in if you send in bulk.
        base_headers = ({"List-Unsubscribe": f"<mailto:{self.from_email}?subject=unsubscribe>"}
                        if self.from_email and self.profile["outreach"].get("list_unsubscribe_header") else {})
        sent_emails = []
        print(f"\n🚀 [Outreach] Up to {len(target_leads)} emails (sent today: {sent_today}/{self.daily_limit})...")

        do_not_contact = load_do_not_contact()
        for idx, lead in enumerate(target_leads):
            if is_do_not_contact(lead["email"], do_not_contact):
                cursor.execute("UPDATE leads SET status = 'disqualified', next_touch_at = NULL, fit_reasons = ? "
                               "WHERE id = ?", (json.dumps(["on your do-not-contact list"]), lead["id"]))
                conn.commit()
                print(f"🚫 [Do-not-contact] {lead['email']}")
                continue
            kind = NURTURE if lead["status"] == "not_now" else "sequence"
            if kind == NURTURE and not self.profile["outreach"].get("not_now_follow_up"):
                cursor.execute("UPDATE leads SET next_touch_at = NULL WHERE id = ?", (lead["id"],))
                continue
            step = (lead.get("sequence_step") or 0) + (0 if kind == NURTURE else 1)
            if kind != NURTURE and step > self.total_steps:
                continue
            if step == 1:
                is_valid, reason = verify_lead_email(lead["email"])
                if not is_valid:
                    print(f"🚫 [Verification Skipped -> {lead['email']}]: {reason}")
                    cursor.execute("UPDATE leads SET status = 'invalid_email' WHERE id = ?", (lead["id"],))
                    conn.commit()
                    continue

            subject, html_body, text_body = build_outreach_email(self.profile, lead, step, kind)
            message_id = make_msgid(domain=sender_domain)
            headers = {**base_headers, "Message-ID": message_id}
            if (step > 1 or kind == NURTURE) and lead.get("thread_message_id"):
                headers.update({"In-Reply-To": lead["thread_message_id"], "References": lead["thread_message_id"]})

            if self.send_real_email(lead["email"], subject, html_body, text_body, headers):
                self._record_send(cursor, lead, step, subject, text_body, message_id, kind)
                conn.commit()
                sent_emails.append({"lead_id": lead["id"], "company": lead["company"], "email": lead["email"],
                                    "step": step if kind != NURTURE else "nurture", "subject": subject})
                if idx < len(target_leads) - 1:
                    if self.smtp_pass:
                        print(f"⏱️ [Rate Limit]: Waiting {self.send_interval_seconds}s before next send...")
                        time.sleep(self.send_interval_seconds)
                    else:
                        time.sleep(1)

        conn.close()
        first_touches = sum(1 for e in sent_emails if e["step"] == 1)
        summary = (f"Sent {len(sent_emails)} emails ({first_touches} first touches, "
                   f"{len(sent_emails) - first_touches} follow-ups). Today: {sent_today + len(sent_emails)}/{self.daily_limit}.")
        log_event("EmailMarketingEngine", "RunCampaign", "success", summary)
        return {
            "region": region or "Global",
            "sent_count": len(sent_emails),
            "interval_seconds": self.send_interval_seconds,
            "sent_emails": sent_emails
        }

if __name__ == "__main__":
    engine = EmailMarketingEngine()
    res = engine.run_outreach_campaign(limit=3)
    print(json.dumps(res, indent=2))
