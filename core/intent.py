"""Sorts inbound email the way an SDR triages an inbox.

Two questions, in order:
  1. Is this a real person replying? (not a newsletter, notification or autoresponder)
  2. If so, what do they want? -> one of the INTENTS below.

Rule-based and offline. core/ai.py can refine the answer with Claude when enabled.
"""

import re

INTENTS = (
    "interested",      # wants to try it / talk -> reply + alert you
    "question",        # asked something (pricing, how it works) -> reply + alert you
    "not_now",         # timing is wrong -> polite reply, stop the sequence
    "not_interested",  # clear no -> stop, never email again
    "unsubscribe",     # asked to be removed -> stop, never email again
    "wrong_person",    # not them / pointed elsewhere -> alert you (possible referral)
    "other",           # anything else -> alert you, no auto-reply
)

# Subjects that mean "a machine answered", not a person
AUTO_REPLY_SUBJECTS = (
    "out of office", "out of the office", "automatic reply", "auto-reply", "autoreply", "auto reply",
    "away from", "on vacation", "received your message", "received your email", "thank you for your email",
    "thanks for your email", "thank you for contacting", "thanks for contacting", "thank you for reaching out",
    "we've received", "we have received", "request received", "ticket #", "[ticket", "support ticket",
    "delivery status notification", "undeliverable", "mail delivery", "returned mail", "failure notice",
)
# Body phrases that only a machine writes (kept strict: people also say "thanks for reaching out")
AUTO_REPLY_BODY = (
    "this is an automated", "automated response", "automatic reply", "auto-reply", "i am out of the office",
    "i'm out of the office", "i am currently out of the office", "i'm currently out of the office",
    "limited access to email", "ticket number", "do not reply to this email", "please do not reply",
    # support-desk acknowledgements (seen live: Bespoke Post, JustWatch, Discord)
    "we've received your message", "we have received your message", "we received your message",
    "received a support ticket", "under review with our team", "under review by our team",
    "position in the queue", "currently assisting other customers", "handle incoming inquiries",
    "we will get back to you as quickly as possible", "a member of our team will",
)
# Human support agents at big companies redirecting you: treat as "wrong person", never hot
SUPPORT_DESK_REDIRECTS = (
    "customer support line", "customer experience line", "customer service line", "customer care team",
    "please reach out to one of these teams", "forwarding along your request", "forwarded your email to our",
)
BULK_PRECEDENCE = ("bulk", "junk", "list", "auto_reply")

NOT_INTERESTED = (
    "not interested", "no interest", "no thanks", "no thank you", "we're good", "we are good", "all set",
    "already have", "already use", "already using", "don't need", "do not need", "not a fit", "not for us",
    "please don't contact", "please do not contact", "don't email", "do not email", "don't send",
    "stop sending", "not relevant", "isn't relevant", "not a good fit", "not the right fit",
)
NOT_INTERESTED_RE = re.compile(r"\bnot\b[^.!?\n]{0,20}\binterested\b")
NOT_NOW = (
    "not right now", "not now", "maybe later", "later this year", "next year", "next season",
    "reach out later", "circle back", "check back", "busy season", "in a few months", "few months",
    "not at the moment", "not at this time", "after the season", "after wedding season", "revisit",
)
WRONG_PERSON = (
    "not the right person", "wrong person", "no longer with", "no longer work", "doesn't work here",
    "does not work here", "you should talk to", "you should contact", "you'll want to contact",
    "better person", "the person who handles", "looping in", "cc'ing", "cc'd", "forwarded your email",
)
INTERESTED = (
    "interested", "sounds good", "sounds great", "sounds interesting", "tell me more", "love to",
    "i'd like to", "i would like to", "let's talk", "lets talk", "set up a call", "schedule a call",
    "book a", "demo", "sign me up", "send me", "more info", "more information", "i'll check it out",
    "i will check it out", "will take a look", "i'll take a look", "i'll try", "i will try", "signed up",
    "how do i sign up", "how do i start", "count me in",
)
INTERESTED_START_RE = re.compile(r"^\s*(yes|yeah|yep|sure|absolutely|definitely|ok|okay)\b")
QUESTION = ("price", "pricing", "cost", "how much", "plans", "how does", "does it", "can it", "can i", "integrat")
BREAKUP_REPLIES = {"1": "interested", "2": "not_now", "3": "unsubscribe"}


def normalize(text: str) -> str:
    """Lower-case, straight apostrophes/quotes, single spaces — so "We’ve" matches "we've"."""
    text = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return " ".join(text.split())


def _has_any(text: str, phrases) -> bool:
    return any(re.search(r"\b" + re.escape(p) + r"(?:s|es)?\b", text) for p in phrases)


def automated_reason(headers: dict, subject: str, body: str) -> str | None:
    """Why this email is NOT a real person replying (or None if it looks human)."""
    h = {k.lower(): (v or "").lower() for k, v in headers.items()}
    if h.get("auto-submitted", "no") not in ("", "no"):
        return "auto-submitted header"
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return "autoresponder header"
    if h.get("precedence", "") in BULK_PRECEDENCE:
        return f"precedence: {h['precedence']}"
    if h.get("list-unsubscribe") or h.get("list-id"):
        return "newsletter / notification"
    subject_l = (subject or "").lower()
    if any(sig in subject_l for sig in AUTO_REPLY_SUBJECTS):
        return "autoresponder subject"
    body_l = normalize(body)
    if any(sig in body_l for sig in AUTO_REPLY_BODY):
        return "autoresponder text"
    return None


def classify_reply(text: str, opt_out_words=("unsubscribe",)) -> str:
    """Intent of a real person's reply. `text` should be only their new text (no quoted history)."""
    t = normalize(text)
    if t.strip(" .!") in BREAKUP_REPLIES:
        return BREAKUP_REPLIES[t.strip(" .!")]
    if _has_any(t, opt_out_words):
        return "unsubscribe"
    if _has_any(t, NOT_INTERESTED) or NOT_INTERESTED_RE.search(t):
        return "not_interested"
    if _has_any(t, NOT_NOW):
        return "not_now"
    if _has_any(t, WRONG_PERSON) or any(phrase in t for phrase in SUPPORT_DESK_REDIRECTS):
        return "wrong_person"
    if _has_any(t, INTERESTED) or INTERESTED_START_RE.search(t):
        return "interested"
    # Only the opening of a reply counts: signatures/footers ("Planning from an iPhone?") aren't questions
    opening = t[:400]
    if "?" in opening or _has_any(opening, QUESTION):
        return "question"
    return "other"
