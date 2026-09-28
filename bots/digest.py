"""Daily SDR digest: one email a day with the funnel, hot leads and what's coming up."""

import os
from datetime import datetime, timezone

from core.config import load_env_file
from core.db import get_connection, log_event
from core.report import pipeline_stats, render_report
from core.updater import digest_lines
from bots.email_marketing import EmailMarketingEngine, text_to_html


def digest_sent_today() -> bool:
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM bot_logs WHERE action = 'DailyDigest' AND status = 'success' AND timestamp >= ?",
        (local_midnight.astimezone(timezone.utc).isoformat(),)).fetchone()[0]
    conn.close()
    return count > 0


def send_daily_digest(profile: dict, force: bool = False) -> dict:
    """Email the pipeline report to [sdr] alert_email (or your sending inbox), once per day."""
    if not profile.get("sdr", {}).get("daily_digest", True) and not force:
        return {"status": "skipped", "reason": "daily_digest is off"}
    if digest_sent_today() and not force:
        return {"status": "skipped", "reason": "already sent today"}
    load_env_file()
    to = profile.get("sdr", {}).get("alert_email") or os.getenv("SDR_ALERT_EMAIL") or os.getenv("SMTP_USER")
    if not to:
        return {"status": "skipped", "reason": "no alert_email / SMTP_USER"}

    stats = pipeline_stats(profile)
    product = profile["sender"]["product_name"]
    text = render_report(stats, product, profile)
    updates = digest_lines(profile)  # "Update available: vX.Y.Z" / "Updated to vX.Y.Z"
    if updates:
        text += "\n\nUPDATES\n" + "\n".join("  " + line for line in updates)
    hot = len(stats["hot_leads"])
    subject = f"📊 {product} SDR digest: {stats['sent_today']} sent today, {hot} hot lead{'s' if hot != 1 else ''}"
    sent = EmailMarketingEngine(profile).send_real_email(to, subject, text_to_html(text), text)
    log_event("SDR", "DailyDigest", "success" if sent else "info",
              f"Digest {'sent' if sent else 'not sent (no email login)'} to {to}")
    return {"status": "sent" if sent else "dry-run", "to": to}
