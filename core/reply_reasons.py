"""Why replies didn't turn into interest: "not now", "already have a tool", "too expensive"...

The funnel says *how many* people said no; this says *why*, which is what tells the owner what to
change: the timing, the targeting, the offer or the wording. Rule-based and offline, and worked
out when the report is read (from the reply excerpt already saved), so replies that arrived
before this existed are counted too and nothing new is stored.
"""

from __future__ import annotations

import re
import sqlite3

from core.intent import normalize

POSITIVE_INTENTS = ("interested", "question")
SKIPPED_INTENTS = POSITIVE_INTENTS + ("auto_reply", "bounce")

# (key, label, phrases). Checked in this order, so the most specific reason wins.
REASONS = (
    ("opted_out", "Asked to be removed", ("unsubscribe", "remove me", "take me off", "stop emailing",
                                           "stop sending", "do not contact", "don't contact", "dont contact")),
    ("wrong_person", "Wrong person", ("not the right person", "wrong person", "no longer with", "no longer work",
                                      "doesn't work here", "does not work here", "talk to someone else",
                                      "better person", "the person who handles", "looping in")),
    ("has_tool", "Already use something", ("already have", "already use", "already using", "we use", "we're using",
                                           "we are using", "happy with our", "happy with what", "have a system",
                                           "have a tool", "have software", "built our own", "in-house")),
    ("price", "Price or budget", ("budget", "too expensive", "can't afford", "cannot afford", "afford",
                                  "cost too", "pricey", "price is", "no money", "cash flow")),
    ("timing", "Not now, maybe later", ("not right now", "not now", "maybe later", "later this year", "next year",
                                        "next season", "busy season", "few months", "not at the moment",
                                        "not at this time", "reach out later", "circle back", "check back",
                                        "revisit", "after the season")),
    ("no_need", "Don't need it", ("don't need", "do not need", "dont need", "no need", "we're good", "we are good",
                                  "all set", "we're fine", "we are fine", "do it ourselves", "do it myself",
                                  "doing fine", "works fine")),
    ("not_a_fit", "Not a fit for them", ("not a fit", "not a good fit", "not the right fit", "not relevant",
                                         "isn't relevant", "not for us", "doesn't apply", "does not apply",
                                         "we don't do", "we do not do", "retired", "out of business")),
)
LABELS = {key: label for key, label, _ in REASONS}
LABELS.update({"no_reason": "Said no, no reason given", "unclear": "Unclear reply"})
INTENT_FALLBACK = {"unsubscribe": "opted_out", "wrong_person": "wrong_person", "not_now": "timing",
                   "not_interested": "no_reason"}
EXAMPLE_CHARS = 120


def _has(text: str, phrase: str) -> bool:
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def reason_for(intent: str | None, text: str | None) -> str | None:
    """The reason key for one real reply that wasn't positive, or None for interested/question
    replies, autoresponders and bounces (they aren't a "no")."""
    if not intent or intent in SKIPPED_INTENTS:
        return None
    if intent in ("not_now", "wrong_person", "unsubscribe"):
        return INTENT_FALLBACK[intent]   # the reply's intent already says why; words could only blur it
    t = normalize(text)
    for key, _label, phrases in REASONS:
        if any(_has(t, phrase) for phrase in phrases):
            return key
    return INTENT_FALLBACK.get(intent, "unclear")


def breakdown(conn: sqlite3.Connection) -> list[dict]:
    """[{"reason", "label", "count", "example"}] for every reason seen, most common first.
    One row per lead (their latest reply), so a lead who wrote twice isn't counted twice."""
    rows = conn.execute("""
        SELECT m.lead_id, m.intent, m.excerpt FROM inbound_messages m
        WHERE m.lead_id IS NOT NULL AND m.id = (SELECT MAX(id) FROM inbound_messages x
                                                WHERE x.lead_id = m.lead_id
                                                  AND x.intent NOT IN ('auto_reply', 'bounce'))
    """).fetchall()
    found: dict[str, dict] = {}
    for _lead_id, intent, excerpt in rows:
        key = reason_for(intent, excerpt)
        if key is None:
            continue
        entry = found.setdefault(key, {"reason": key, "label": LABELS[key], "count": 0, "example": ""})
        entry["count"] += 1
        if not entry["example"] and excerpt:
            entry["example"] = " ".join(excerpt.split())[:EXAMPLE_CHARS]
    return sorted(found.values(), key=lambda e: (-e["count"], e["label"]))
