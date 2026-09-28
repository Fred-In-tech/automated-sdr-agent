"""Optional push alerts (Telegram / Discord) for hot leads and run summaries.

Both are off unless their env vars are set; without them every call is a printed dry run.
The bot token and the webhook URL are secrets (anyone holding them can post as you), so they
only ever travel over https and are never printed — request errors normally quote the full URL,
which is why failures are reported by error type / HTTP status only.

There is deliberately no `python3 core/notifications.py` smoke test: with the env vars set it would
post to your real chat. tests/test_security_hygiene.py exercises the module offline instead.
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

TIMEOUT_SECONDS = 10
TELEGRAM_API = "https://api.telegram.org"
# "<bot id>:<secret>" — checked so a stray value can't reshape the request path
TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")


def _is_https_url(url: str | None) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme == "https" and bool(parts.netloc)


class NotificationManager:
    def __init__(self, telegram_token: str = None, telegram_chat_id: str = None, discord_webhook_url: str = None):
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.discord_webhook_url = discord_webhook_url or os.getenv("DISCORD_WEBHOOK_URL")

    def send_telegram(self, message: str) -> bool:
        if not self.telegram_token or not self.telegram_chat_id:
            print(f"[Telegram DRY-RUN] {message}")
            return True
        if not TELEGRAM_TOKEN_RE.match(self.telegram_token):
            print("[Telegram Error] TELEGRAM_BOT_TOKEN doesn't look like a bot token (123456:ABC...) — not sent.")
            return False

        url = f"{TELEGRAM_API}/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": message, "parse_mode": "Markdown"}
        return self._post("Telegram", url, payload, ok_statuses=(200,))

    def send_discord(self, title: str, description: str, color: int = 0x3b82f6) -> bool:
        if not self.discord_webhook_url:
            print(f"[Discord DRY-RUN] {title} - {description}")
            return True
        if not _is_https_url(self.discord_webhook_url):
            print("[Discord Error] DISCORD_WEBHOOK_URL must be an https:// webhook URL — not sent.")
            return False

        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }
        return self._post("Discord", self.discord_webhook_url, payload, ok_statuses=(200, 204))

    @staticmethod
    def _post(service: str, url: str, payload: dict, ok_statuses: tuple) -> bool:
        """POST JSON; True on an expected status. Never lets the (secret) URL reach the output."""
        try:
            resp = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
        except Exception as e:  # an alert must never break the run that triggered it
            print(f"[{service} Error] Failed to send message ({type(e).__name__}).")
            return False
        if resp.status_code not in ok_statuses:
            print(f"[{service} Error] Failed to send message (HTTP {resp.status_code}).")
            return False
        return True

    def notify_all(self, title: str, message: str):
        full_msg = f"*{title}*\n{message}"
        self.send_telegram(full_msg)
        self.send_discord(title, message)
