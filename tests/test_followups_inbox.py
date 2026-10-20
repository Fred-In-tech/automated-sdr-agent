"""Interested replies and questions open a follow-up automatically; other replies don't."""

from unittest import mock

from core import followups
from core.ai.connectors import Reply
from test_inbox import InboxTestCase, make_mail


class TestRepliesOpenFollowUps(InboxTestCase):
    def deals(self):
        return followups.all_deals()

    def test_interested_reply_opens_one_deal(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "Yes please, send me the link!", "<a1@x>")])
        deals = self.deals()
        self.assertEqual([(d["lead_id"], d["stage"]) for d in deals], [(self.lead_id, "replied")])
        self.assertIn("Sarah", deals[0]["next_step"])

    def test_question_opens_a_deal_too(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "How much is it after the trial?", "<a2@x>")])
        self.assertEqual(len(self.deals()), 1)

    def test_no_or_unsubscribe_opens_nothing(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "Not interested, thanks.", "<a3@x>")])
        self.assertEqual(self.deals(), [])

    def test_ai_review_path_still_opens_the_deal(self):
        self.profile["ai"] = {"mode": "local_only", "provider": "ollama", "model": "m", "answer_questions": "review",
                              "facts": "Plans start at $29 a month."}

        def chat(cfg, system, user, **kwargs):
            return Reply('{"intent": "question"}' if "triage" in system else "Plans start at $29 a month.")
        with mock.patch("core.ai.jobs.chat", chat):
            self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "How much is it?", "<a4@x>")])
        self.assertEqual(len(self.deals()), 1)
