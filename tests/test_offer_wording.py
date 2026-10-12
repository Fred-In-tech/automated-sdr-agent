"""The offer is typed by the user ("free 14-day trial", "a free planning call", "an intro session",
"3 free months") and dropped into templates that say "a {{offer}}". Rendering must read like a
person wrote it whichever way they typed it, including templates already saved in old profiles."""

import unittest

from core.config import render
from core.setup_profile import sequence_copy

FOLLOW_UP_2 = ("In case it's easier to just try it: there's a {{offer}} waiting for you, no call needed.")


def fill(template: str, offer: str) -> str:
    return render(template, {"offer": offer})


class TestArticleBeforeOffer(unittest.TestCase):
    def test_offer_without_an_article_gets_one(self):
        self.assertEqual(fill("there's a {{offer}} waiting", "free 14-day trial"), "there's a free 14-day trial waiting")

    def test_offer_that_already_starts_with_a_does_not_double_it(self):
        self.assertEqual(fill("there's a {{offer}} waiting", "a free planning call"),
                         "there's a free planning call waiting")

    def test_vowel_sound_gets_an(self):
        self.assertEqual(fill("start with a {{offer}}.", "intro session"), "start with an intro session.")
        self.assertEqual(fill("start with a {{offer}}.", "an hour of setup help"), "start with an hour of setup help.")

    def test_numbers_and_possessives_take_no_article(self):
        self.assertEqual(fill("start with a {{offer}}.", "3 free months"), "start with 3 free months.")
        self.assertEqual(fill("start with a {{offer}}.", "our starter plan"), "start with our starter plan.")

    def test_capitalised_article_at_sentence_start(self):
        self.assertEqual(fill("- A {{offer}} to see if it fits", "a free planning call"),
                         "- A free planning call to see if it fits")
        self.assertEqual(fill("- A {{offer}} to see if it fits", "3 free months"), "- 3 free months to see if it fits")
        self.assertEqual(fill("- A {{offer}} to see if it fits", "intro session"), "- An intro session to see if it fits")

    def test_your_or_the_before_offer_drops_the_offer_article(self):
        self.assertEqual(fill("Start your {{offer}}", "a free planning call"), "Start your free planning call")
        self.assertEqual(fill("Start your {{offer}}", "free 14-day trial"), "Start your free 14-day trial")

    def test_word_ending_in_a_is_not_an_article(self):
        self.assertEqual(fill("Try Nova {{offer}}", "a free trial"), "Try Nova a free trial")

    def test_bare_offer_is_left_exactly_as_typed(self):
        self.assertEqual(fill("Offer: {{offer}}", "a free planning call"), "Offer: a free planning call")


class TestNoCallNeeded(unittest.TestCase):
    def test_kept_when_the_offer_is_not_a_call(self):
        self.assertIn("no call needed", fill(FOLLOW_UP_2, "free 14-day trial"))

    def test_dropped_when_the_offer_is_a_call_or_meeting(self):
        for offer in ("a free planning call", "free 20-minute consultation", "a quick demo", "free strategy meeting"):
            text = fill(FOLLOW_UP_2, offer)
            self.assertNotIn("no call needed", text, offer)
            self.assertTrue(text.endswith("waiting for you."), text)

    def test_the_reported_case_end_to_end(self):
        copy = sequence_copy({"business.name": "Brightline Events", "audience.ideal_client": "event planner",
                              "style.kind": "sales"})
        email3 = fill(copy["follow_ups"][1][1], "a free planning call")
        self.assertIn("there's a free planning call waiting for you.", email3)
        self.assertNotIn("a a ", email3)
        self.assertNotIn("no call needed", email3)

    def test_templates_without_offer_are_untouched(self):
        self.assertEqual(render("a a {{first_name}}, no call needed", {"first_name": "Jo", "offer": "a call"}),
                         "a a Jo, no call needed")


if __name__ == "__main__":
    unittest.main()
