"""AI jobs inside the real pipeline: lead finding (openers, lead fit), the first email (waits for
a pending opener review, uses an approved one) and replies (answers to questions, auto or
review). The AI is faked with core.ai.jobs.chat; SMTP/IMAP use the existing test fakes."""

import json
import unittest
from unittest import mock

import core.db as db
from bots import leadgen_pipeline, site_reader
from bots.lead_store import save_leads
from core.ai import drafts
from core.ai.connectors import AIError, Reply
from test_inbox import InboxTestCase, make_mail
from test_sequence import SequenceTestCase

LOCAL_AI = {"mode": "local_only", "provider": "ollama", "model": "llama3.2",
            "facts": "Plans start at $29 a month."}


def fake_chat(answers):
    """chat() stand-in: answers by job, detected from the system prompt."""
    def chat(cfg, system, user, **kwargs):
        for key, answer in answers.items():
            if key in system:
                if isinstance(answer, Exception):
                    raise answer
                return Reply(answer)
        return Reply("")
    return chat


OPENER = "Your rooftop ceremonies in East Austin look beautifully planned."
FIT = "Plans weddings and corporate events in Austin, close match."
ANSWER = "Plans start at $29 a month, and you can try it free first."


class TestLeadFinding(SequenceTestCase):
    SITE = {"emails": ["ivy@lanternhall.test"], "domain": "lanternhall.test", "site_name": "Lantern Hall",
            "title": "Lantern Hall Weddings", "website": "https://lanternhall.test", "page_text": "rooftop weddings"}

    def qualify(self, jobs):
        self.profile["ai"] = {**LOCAL_AI, "jobs": jobs}
        with mock.patch("core.ai.jobs.chat", fake_chat({"first sentence": OPENER, "one short line": FIT})), \
                mock.patch.object(site_reader, "verify_lead_email", lambda e: (True, "ok")), \
                mock.patch.object(site_reader, "pick_best_contact",
                                  lambda *a: ("ivy@lanternhall.test", {"qualified": True, "score": 80,
                                                                       "first_name": "Ivy", "reasons": ["fits"]})):
            leadgen_pipeline.reset_ai_runs()
            return leadgen_pipeline.qualify_site(self.profile, self.SITE, source="t", location="Austin, TX",
                                                 category="event planner", title="Owner")

    def test_auto_opener_and_fit(self):
        lead = self.qualify({"openers": "auto", "lead_fit": "auto"})
        self.assertEqual(lead["opener"], OPENER)
        self.assertEqual(lead["ai_fit"], FIT)
        self.assertIsNone(lead["opener_draft"])

    def test_review_opener_becomes_a_draft_after_saving(self):
        lead = self.qualify({"openers": "review", "lead_fit": "off"})
        self.assertIsNone(lead["opener"])
        self.assertIsNone(lead["ai_fit"])
        saved = save_leads([lead], "event planner")
        pending = drafts.pending()
        self.assertEqual([(d["lead_id"], d["kind"], d["body"]) for d in pending], [(saved[0]["id"], "opener", OPENER)])

    def test_ai_off_changes_nothing(self):
        self.profile["ai"] = {"mode": "off"}
        leadgen_pipeline.reset_ai_runs()
        with mock.patch.object(site_reader, "verify_lead_email", lambda e: (True, "ok")):
            lead = leadgen_pipeline.qualify_site(self.profile, self.SITE, source="t", location="Austin, TX",
                                                 category="event planner", title="Owner")
        self.assertIsNone(lead["opener"])
        self.assertIsNone(lead["ai_fit"])

    def test_ai_fit_is_saved(self):
        lead = self.qualify({"openers": "off", "lead_fit": "auto"})
        saved = save_leads([lead], "event planner")
        self.assertEqual(self.lead(saved[0]["id"])["ai_fit"], FIT)


class TestFirstEmailWaitsForReview(SequenceTestCase):
    def test_pending_opener_holds_only_that_lead(self):
        waiting = self.add_lead("a@waiting.test", 90)
        other = self.add_lead("b@other.test", 50)
        drafts.add_draft(waiting, "opener", OPENER)
        self.engine.run_outreach_campaign()
        self.assertEqual([m["to"] for m in self.sent], ["b@other.test"])
        self.assertEqual(self.lead(waiting)["status"], "new")
        self.assertEqual(self.lead(other)["status"], "contacted")

    def test_approved_opener_is_used_and_marked_sent(self):
        lead_id = self.add_lead("a@approved.test", 90)
        draft = drafts.add_draft(lead_id, "opener", "draft text")
        drafts.decide(draft, "edit", OPENER)
        self.engine.run_outreach_campaign()
        self.assertIn(OPENER, self.sent[0]["text"])
        self.assertEqual(self.lead(lead_id)["opener"], OPENER)
        self.assertIsNone(drafts.approved_for(lead_id, "opener"))


class TestReviewNeverBlocksSending(SequenceTestCase):
    def test_waiting_leads_do_not_use_up_the_run(self):
        waiting = [self.add_lead(f"w{i}@waiting.test", 95) for i in range(3)]
        for lead_id in waiting:
            drafts.add_draft(lead_id, "opener", OPENER)
        other = self.add_lead("b@other.test", 40)
        self.engine.run_outreach_campaign(limit=1)
        self.assertEqual([m["to"] for m in self.sent], ["b@other.test"])
        self.assertEqual(self.lead(other)["status"], "contacted")

    def test_openers_left_unreviewed_for_days_fall_back(self):
        from datetime import datetime, timedelta, timezone
        lead_id = self.add_lead("old@waiting.test", 95)
        old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        with mock.patch.object(drafts, "_now", return_value=old):
            drafts.add_draft(lead_id, "opener", OPENER)
        self.engine.run_outreach_campaign()
        self.assertEqual([m["to"] for m in self.sent], ["old@waiting.test"])
        self.assertNotIn(OPENER, self.sent[0]["text"])          # the standard first line was used
        self.assertFalse(drafts.has_pending(lead_id, "opener"))


class TestAnswers(InboxTestCase):
    QUESTION = make_mail("sarah@bloomfilms.test", "Re: proposals", "How much is it after the trial?", "<q1@x>")

    def run_with_ai(self, jobs, answer=ANSWER):
        self.profile["ai"] = {**LOCAL_AI, "jobs": jobs}
        with mock.patch("core.ai.jobs.chat", fake_chat({"triage": '{"intent": "question"}', "answer a question": answer})):
            return self.run_inbox([self.QUESTION])

    def test_auto_answer_is_sent(self):
        result, _ = self.run_with_ai({"answer_questions": "auto"})
        reply = self.sent_to("sarah@bloomfilms.test")[0]
        self.assertIn(ANSWER, reply["text"])
        self.assertTrue(reply["text"].startswith("Hi Sarah,"))
        self.assertEqual(reply["headers"]["In-Reply-To"], "<q1@x>")
        self.assertEqual(result["hot_leads"][0]["intent"], "question")

    def test_review_answer_waits_in_the_queue(self):
        result, _ = self.run_with_ai({"answer_questions": "review"})
        self.assertEqual(self.sent_to("sarah@bloomfilms.test"), [])
        pending = drafts.pending()
        self.assertEqual([(d["kind"], d["body"]) for d in pending], [("answer", ANSWER)])
        self.assertEqual(pending[0]["context"]["message_id"], "<q1@x>")
        self.assertIn("review", result["hot_leads"][0]["action"])

    def test_bad_ai_answer_falls_back_to_the_template(self):
        self.run_with_ai({"answer_questions": "auto"}, answer="It's $99, see https://x.test")
        reply = self.sent_to("sarah@bloomfilms.test")[0]
        self.assertNotIn("$99", reply["text"])

    def test_ai_down_falls_back_to_the_template(self):
        self.run_with_ai({"answer_questions": "auto"}, answer=AIError("Ollama isn't running"))
        self.assertEqual(len(self.sent_to("sarah@bloomfilms.test")), 1)

    def test_no_answer_to_someone_who_just_unsubscribed(self):
        draft = drafts.add_draft(self.lead_id, "answer", ANSWER, {"subject": "Re: proposals", "message_id": "<q1@x>"})
        drafts.decide(draft, "approve")
        self.profile["ai"] = {**LOCAL_AI}
        with mock.patch("core.ai.jobs.chat", fake_chat({})):
            self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "Please unsubscribe me.", "<u1@x>")])
        self.assertFalse(any(ANSWER in m["text"] for m in self.sent_to("sarah@bloomfilms.test")))
        self.assertEqual(drafts.approved(kind="answer"), [])

    def test_an_unsafe_approved_answer_is_not_sent(self):
        draft = drafts.add_draft(self.lead_id, "answer", "x", {"subject": "Re: proposals"})
        drafts.decide(draft, "edit", "Call 512-555-0100 to pay your invoice.")
        self.profile["ai"] = {**LOCAL_AI}
        with mock.patch("core.ai.jobs.chat", fake_chat({})):
            self.run_inbox([])
        self.assertEqual(self.sent_to("sarah@bloomfilms.test"), [])
        self.assertEqual(drafts.approved(kind="answer"), [])

    def test_approved_answer_is_sent_on_the_next_check(self):
        draft = drafts.add_draft(self.lead_id, "answer", ANSWER, {"subject": "Re: proposals", "message_id": "<q1@x>"})
        drafts.decide(draft, "approve")
        self.profile["ai"] = {**LOCAL_AI}
        with mock.patch("core.ai.jobs.chat", fake_chat({})):
            self.run_inbox([])
        reply = self.sent_to("sarah@bloomfilms.test")[0]
        self.assertIn(ANSWER, reply["text"])
        self.assertEqual(reply["subject"], "Re: proposals")
        self.assertEqual(reply["headers"]["In-Reply-To"], "<q1@x>")
        self.assertEqual(drafts.approved(kind="answer"), [])


if __name__ == "__main__":
    unittest.main()
