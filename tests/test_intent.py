"""Offline tests for inbound-reply triage. Safe to run: no network, no emails.

The replies below are modelled on real ones but every name, brand, address and handle is made
up (reserved `.test` / `.example` domains): this file is public."""

import re
import unittest
from pathlib import Path

from core.intent import automated_reason, classify_reply

OPT_OUT = ["unsubscribe", "remove me", "stop emailing", "take me off"]

# RFC 2606 / 6761 names that can never belong to anyone
RESERVED_SUFFIXES = (".test", ".example", ".invalid", ".localhost", "example.com", "example.net", "example.org")
REAL_LOOKING_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|us|uk|de|fr|ca|au|me|ai|app|dev)\b", re.I)


class TestAutomatedDetection(unittest.TestCase):
    def test_real_autoresponders_from_the_logs_are_caught(self):
        for subject in (
            "We’ve Received Your Message — Fast Help Inside! Re: Quick question for Jamie",
            "Thank you for your email! Re: Quick question for Acme",
            "Automatic reply: Quick question",
            "Out of Office: back Monday",
        ):
            self.assertIsNotNone(automated_reason({}, subject, "Hi"), subject)

    def test_social_notifications_are_bulk(self):
        headers = {"List-Unsubscribe": "<https://social.example/unsub>", "Precedence": "bulk"}
        self.assertIsNotNone(automated_reason(headers, "acme_studio: 2 unread messages", "..."))

    def test_auto_submitted_header(self):
        self.assertIsNotNone(automated_reason({"Auto-Submitted": "auto-replied"}, "Re: hi", "..."))

    def test_human_reply_is_not_automated(self):
        body = "Thanks for reaching out! How much is it after the trial?"
        self.assertIsNone(automated_reason({"Auto-Submitted": "no"}, "Re: proposals", body))


class TestReplyIntent(unittest.TestCase):
    def check(self, text: str, expected: str):
        self.assertEqual(classify_reply(text, OPT_OUT), expected, text)

    def test_interested(self):
        self.check("Yes, I'd like to try it. How do I start?", "interested")
        self.check("Sounds interesting — tell me more", "interested")
        self.check("sure", "interested")

    def test_question(self):
        self.check("What does it cost after the trial?", "question")
        self.check("Does it work with HoneyBook contracts?", "question")

    def test_not_interested_beats_interested(self):
        self.check("Not interested, thanks", "not_interested")
        self.check("I'm not really interested", "not_interested")
        self.check("We already use HoneyBook, no thanks", "not_interested")

    def test_not_now(self):
        self.check("It's busy season, maybe later this year", "not_now")
        self.check("Can you check back in a few months?", "not_now")

    def test_unsubscribe(self):
        self.check("Please remove me from your list", "unsubscribe")
        self.check("UNSUBSCRIBE", "unsubscribe")

    def test_wrong_person(self):
        self.check("I'm not the right person, you should talk to Dana", "wrong_person")

    def test_breakup_numbers(self):
        self.check("1", "interested")
        self.check("2.", "not_now")
        self.check("3", "unsubscribe")

    def test_other(self):
        self.check("Thanks.", "other")

    def test_words_inside_other_words_do_not_match(self):
        self.check("I'm not sure what this is", "other")


class TestLiveRegressions(unittest.TestCase):
    """Regressions from 2026-09-25, when support-desk replies were wrongly flagged as hot leads.
    The wording that fooled the classifier is kept; the companies and people are made up."""

    def test_support_desk_acknowledgements_are_automated(self):
        for body in (
            "Hey, thanks for reaching out! We’ve received your message. Our team is currently assisting other "
            "customers, but hang tight and you can expect a response from us within 24 hours.",
            "Hi there, Thank you so much for reaching out! We have received your message and it is under review "
            "with our team. We will get back to you as quickly as possible, usually within 48 hours.",
        ):
            self.assertIsNotNone(automated_reason({}, "Re: question", body), body[:40])

    def test_support_agents_redirecting_are_wrong_person_not_hot(self):
        planner = ("Hi there, Thanks so much for writing in and expressing your interest in Evermore! You've reached "
                   "the customer support line, so if you don't mind forwarding along your request to the appropriate "
                   "email address below: Press Inquiries: press@evermoreplanning.test If I can do anything more to "
                   "assist, please let me know. All my best, Sam Here for every step of the way "
                   "www.evermoreplanning.test Planning from an iPhone? Download our iOS app")
        self.assertEqual(classify_reply(planner, OPT_OUT), "wrong_person")
        retailer = ("Hi Acme, Thanks for reaching out! This is the Customer Experience line, but I've gone ahead "
                    "and forwarded your email to our team. They'll be sure to follow up if there's a fit.")
        self.assertEqual(classify_reply(retailer, OPT_OUT), "wrong_person")

    def test_question_mark_in_a_footer_is_not_a_question(self):
        body = "Thanks, noted. " + "Our studio news and updates. " * 20 + "Planning from an iPhone? Get our app."
        self.assertEqual(classify_reply(body, OPT_OUT), "other")

    def test_curly_apostrophes_still_match(self):
        self.assertEqual(classify_reply("I’m not interested, thanks", OPT_OUT), "not_interested")


class TestFixtureHygiene(unittest.TestCase):
    def test_fixtures_never_quote_a_real_person_or_company(self):
        """A support agent's name and a company's mailbox pasted from a private reply would be
        republished with the repo, so every address, domain and URL here must be a reserved name."""
        source = Path(__file__).read_text(encoding="utf-8")
        hosts = re.findall(r"[\w.+-]+@([\w.-]+)", source) + REAL_LOOKING_HOST.findall(source)
        self.assertTrue(hosts, "expected the fixtures to contain addresses or domains")
        real = sorted({h for h in hosts if not h.lower().endswith(RESERVED_SUFFIXES)})
        self.assertEqual(real, [], "real-looking domains in test fixtures")
