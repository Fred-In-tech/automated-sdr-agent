"""The four AI jobs: validation, fallbacks, the circuit breaker and the monthly budget.
A fake chat function stands in for the AI; nothing here touches the network."""

import unittest

from core.ai.connectors import AIError, Reply
from core.ai.jobs import (AIRun, ai_phrase_problem, answer_question, classify_reply, explain_fit,
                          is_valid_answer, is_valid_opener, write_opener)

PROFILE = {
    "ai": {"mode": "local_only", "provider": "ollama", "model": "llama3.2",
           "facts": "Plans start at $29 a month. Free 30-day trial. Works on phone and laptop."},
    "sender": {"product_name": "Brightline", "offer": "a free planning call", "sign_off": "Jamie"},
    "targeting": {"ideal_client": "event planner"},
}


class FakeChat:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cfg, system, user, *, schema=None, max_tokens=400):
        self.calls.append({"system": system, "user": user, "schema": schema})
        answer = self.answers.pop(0) if self.answers else "{}"
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Reply) else Reply(answer, 100, 20)


class MemoryUsage:
    def __init__(self, spent=0.0):
        self.spent = spent

    def month_cost(self):
        return self.spent

    def add(self, cost):
        self.spent += cost


def run_with(*answers, profile=None, usage=None):
    fake = FakeChat(*answers)
    return AIRun(profile or PROFILE, environ={}, chat_fn=fake, usage=usage or MemoryUsage()), fake


class TestOpeners(unittest.TestCase):
    def test_good_opener_is_used(self):
        run, fake = run_with("Your rooftop wedding photos in East Austin caught my eye this week.")
        self.assertEqual(write_opener(run, "Lantern Hall", "event planner", "rooftop weddings East Austin"),
                         "Your rooftop wedding photos in East Austin caught my eye this week.")
        self.assertIn("<website_text>", fake.calls[0]["user"])

    def test_ai_phrases_and_unsafe_lines_fall_back(self):
        for bad in ("I hope this email finds you well and that business is great.", "NONE",
                    "Check out https://x.com for the details today.", "I came across your amazing website today!"):
            run, _ = run_with(bad)
            self.assertIsNone(write_opener(run, "A", "b", "text"), bad)

    def test_job_off_never_calls_the_ai(self):
        profile = {**PROFILE, "ai": {**PROFILE["ai"], "jobs": {"openers": "off"}}}
        run, fake = run_with("anything", profile=profile)
        self.assertIsNone(write_opener(run, "A", "b", "text"))
        self.assertEqual(fake.calls, [])

    def test_ai_phrase_problem(self):
        self.assertIsNotNone(ai_phrase_problem("I hope this finds you well."))
        self.assertIsNotNone(ai_phrase_problem("Just wanted to reach out about your studio."))
        self.assertIsNone(ai_phrase_problem("Your barn venue in Dripping Springs looks like a great fit."))
        self.assertTrue(is_valid_opener("Your barn venue in Dripping Springs looks like a great fit."))


class TestReplyIntent(unittest.TestCase):
    def test_valid_intent(self):
        run, fake = run_with('{"intent": "question"}')
        self.assertEqual(classify_reply(run, "how much is it?", ["unsubscribe"]), "question")
        self.assertIsNotNone(fake.calls[0]["schema"])

    def test_unsubscribe_never_reaches_the_ai(self):
        run, fake = run_with('{"intent": "interested"}')
        self.assertEqual(classify_reply(run, "please unsubscribe me", ["unsubscribe"]), "unsubscribe")
        self.assertEqual(fake.calls, [])

    def test_bad_answer_falls_back_to_rules(self):
        run, _ = run_with('{"intent": "buy_now"}')
        self.assertEqual(classify_reply(run, "yes send me the link", ["unsubscribe"]), "interested")

    def test_off_uses_rules(self):
        run, fake = run_with(profile={"ai": {"mode": "off"}})
        self.assertEqual(classify_reply(run, "not interested, thanks", ["unsubscribe"]), "not_interested")
        self.assertEqual(fake.calls, [])


class TestAnswers(unittest.TestCase):
    LEAD = {"first_name": "Ivy", "company": "Lantern Hall"}

    def test_answer_from_facts(self):
        run, fake = run_with("Plans start at $29 a month, and there's a free 30-day trial if you'd like to try it first.")
        text = answer_question(run, PROFILE, self.LEAD, "How much does it cost?")
        self.assertIn("$29", text)
        self.assertIn("Plans start at $29", fake.calls[0]["user"])          # owner facts are given to the AI
        self.assertIn("only", fake.calls[0]["system"].lower())

    def test_invented_price_is_rejected(self):
        run, _ = run_with("It's only $99 a year, best deal around.")
        self.assertIsNone(answer_question(run, PROFILE, self.LEAD, "How much?"))

    def test_jobs_leave_room_for_thinking_models(self):
        run, fake = run_with("Plans start at $29 a month.")
        captured = {}
        real = run._chat

        def spy(cfg, system, user, *, schema=None, max_tokens=400):
            captured["max_tokens"] = max_tokens
            return real(cfg, system, user, schema=schema, max_tokens=max_tokens)
        run._chat = spy
        answer_question(run, PROFILE, self.LEAD, "How much?")
        self.assertGreaterEqual(captured["max_tokens"], 1000)

    def test_answer_prompt_forbids_guessing(self):
        run, fake = run_with("x")
        answer_question(run, PROFILE, self.LEAD, "Do you integrate with HoneyBook?")
        self.assertIn("never guess", fake.calls[0]["system"].lower())

    def test_links_and_long_answers_rejected(self):
        self.assertFalse(is_valid_answer("See https://brightline.example for pricing.", PROFILE))
        self.assertFalse(is_valid_answer(" ".join(["word"] * 130), PROFILE))
        self.assertFalse(is_valid_answer("As an AI language model I can't say.", PROFILE))
        self.assertFalse(is_valid_answer("Hi {{first_name}}, sure.", PROFILE))
        self.assertTrue(is_valid_answer("Yes, it works on your phone and laptop.", PROFILE))


class TestLeadFit(unittest.TestCase):
    def test_one_short_line(self):
        run, _ = run_with("Plans weddings and corporate events in Austin, a close match.\nExtra line")
        self.assertEqual(explain_fit(run, PROFILE, "Lantern Hall", "weddings", ["has a contact email"]),
                         "Plans weddings and corporate events in Austin, a close match.")

    def test_too_long_is_cut(self):
        run, _ = run_with("word " * 80)
        self.assertLessEqual(len(explain_fit(run, PROFILE, "A", "b", [])), 160)


class TestSafetyNets(unittest.TestCase):
    def test_breaker_opens_after_two_failures(self):
        run, fake = run_with(AIError("down"), AIError("down"), '{"intent": "question"}')
        self.assertIsNone(run.ask("reply_intent", "s", "u"))
        self.assertIsNone(run.ask("reply_intent", "s", "u"))
        self.assertFalse(run.enabled("reply_intent"))
        self.assertIsNone(run.ask("reply_intent", "s", "u"))
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("down", run.last_error)

    def test_a_success_resets_the_count(self):
        run, _ = run_with(AIError("x"), "ok", AIError("x"), "ok")
        for _ in range(4):
            run.ask("reply_intent", "s", "u")
        self.assertTrue(run.enabled("reply_intent"))

    def test_bad_url_disables(self):
        run, _ = run_with(profile={"ai": {"mode": "local_only", "provider": "openai", "model": "m"}})
        self.assertFalse(run.enabled("openers"))

    def test_budget_stops_hosted_ai(self):
        profile = {"ai": {"mode": "hosted", "provider": "openai", "model": "m", "monthly_budget_usd": 1}}
        usage = MemoryUsage(spent=1.0)
        run = AIRun(profile, environ={"AI_API_KEY": "k"}, chat_fn=FakeChat("x"), usage=usage)
        self.assertFalse(run.enabled("openers"))
        self.assertIn("budget", run.status_note().lower())

    def test_hosted_usage_is_counted_local_is_free(self):
        hosted = {"ai": {"mode": "hosted", "provider": "openai", "model": "m"}}
        usage = MemoryUsage()
        AIRun(hosted, environ={"AI_API_KEY": "k"}, chat_fn=FakeChat(Reply("x", 1_000_000, 0)), usage=usage).ask("openers", "s", "u")
        self.assertGreater(usage.spent, 0)
        local_usage = MemoryUsage()
        run, _ = run_with(Reply("x", 1_000_000, 1_000_000), usage=local_usage)
        run.ask("openers", "s", "u")
        self.assertEqual(local_usage.spent, 0)
        self.assertEqual(run.tokens, 2_000_000)


if __name__ == "__main__":
    unittest.main()
