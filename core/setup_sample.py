"""One real sample email, sent to yourself: `sdr test-email`, the end of `sdr setup` and the
home menu all use this, so "what you see is what leads get" holds everywhere.

The sample goes to a made-up lead (Alex at Rivera Studio) through the SDR's real sending path
(EmailMarketingEngine.send_real_email), so it proves the login, the From address and the
design at once. It never goes to anyone but the address you give (default: your own mailbox).
"""

from __future__ import annotations

import os
from email.utils import make_msgid

from core.config import load_env_file

SAMPLE_LEAD = {"name": "Rivera Studio", "first_name": "Alex", "company": "Rivera Studio"}
REPLY_INTENTS = ("interested", "question", "not_now")
SAMPLE_REPLY_TEXT = "Sounds good, send me the link"


class SampleError(Exception):
    """The sample can't be built (bad step number, no such reply). The message is user-facing."""


def sample_lead(profile: dict, signature: str | None = None) -> dict:
    targeting = profile.get("targeting") or {}
    cities = targeting.get("cities") or ["your city"]
    return {**SAMPLE_LEAD, "category": targeting.get("ideal_client", ""), "location": cities[0],
            "signature_variant": signature}


def build_sample(profile: dict, step: int = 1, reply: str | None = None, signature: str | None = None) -> dict:
    """{"subject", "html", "text", "label"} for one sequence email (or one auto-reply)."""
    from bots.email_marketing import build_outreach_email, sequence_length

    lead = sample_lead(profile, signature)
    total = sequence_length(profile)
    if reply:
        from bots.inbox_listener import build_reply
        if reply not in REPLY_INTENTS:
            raise SampleError(f"--reply must be one of: {', '.join(REPLY_INTENTS)}.")
        built = build_reply(profile, reply, lead, SAMPLE_REPLY_TEXT)
        if not built:
            raise SampleError(f"There's no approved '{reply}' reply in [auto_reply.replies] of config/profile.toml.")
        text, html = built
        first_subject = build_outreach_email(profile, lead, 1)[0]
        return {"subject": f"Re: {first_subject}", "html": html, "text": text,
                "label": f"the '{reply}' auto-reply"}
    if not 1 <= int(step) <= total:
        raise SampleError(f"--step must be between 1 and {total} (your sequence has {total} emails).")
    subject, html, text = build_outreach_email(profile, lead, int(step))
    label = "the first email" if step == 1 else f"follow-up {int(step) - 1}"
    return {"subject": subject, "html": html, "text": text, "label": f"{label} ({step} of {total})"}


def default_recipient(profile: dict | None = None) -> str:
    """Your own mailbox: the login address (SMTP_USER), else the profile's From address."""
    load_env_file()
    sender = (profile or {}).get("sender") or {}
    return (os.getenv("SMTP_USER") or sender.get("from_email") or "").strip()


def send_sample(profile: dict, to: str, step: int = 1, reply: str | None = None, signature: str | None = None,
                engine=None, sample: dict | None = None) -> dict:
    """Build (unless `sample` is given) and send one sample. Returns the sample plus
    {"sent": bool, "to": str}. Raises SampleError for a bad step/reply (nothing is sent then)."""
    sample = sample or build_sample(profile, step, reply, signature)
    if engine is None:
        from bots.email_marketing import EmailMarketingEngine
        engine = EmailMarketingEngine(profile)
    from_email = getattr(engine, "from_email", "") or "localhost"
    headers = {"Message-ID": make_msgid(domain=from_email.split("@")[-1])}
    if getattr(engine, "from_email", "") and (profile.get("outreach") or {}).get("list_unsubscribe_header"):
        headers["List-Unsubscribe"] = f"<mailto:{engine.from_email}?subject=unsubscribe>"
    sent = engine.send_real_email(to, sample["subject"], sample["html"], sample["text"], headers)
    return {**sample, "sent": bool(sent), "to": to}
