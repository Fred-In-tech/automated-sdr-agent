"""Offline tests for the profile-driven setup. Safe to run: no network, no emails."""

import os
import tempfile
import unittest

from core.config import (
    EXAMPLE_PROFILE_PATH,
    ProfileError,
    compliance_warnings,
    load_profile,
    render,
    template_values,
)
from bots.leadgen_pipeline import build_search_queries, enrich_keywords, thumbtack_city_url
from bots.email_marketing import build_outreach_email
from bots.inbox_listener import is_opt_out, latest_reply_text, pick_auto_reply


def example_profile() -> dict:
    return load_profile(EXAMPLE_PROFILE_PATH)


class TestProfileLoading(unittest.TestCase):
    def test_example_profile_is_valid(self):
        profile = example_profile()
        self.assertEqual(profile["sender"]["product_name"], "Acme Scheduling")
        self.assertTrue(profile["targeting"]["professions"])

    def test_missing_profile_explains_how_to_fix(self):
        with self.assertRaises(ProfileError) as ctx:
            load_profile("/nonexistent/profile.toml")
        self.assertIn("cli.py setup", str(ctx.exception))

    def test_missing_required_fields_are_listed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[sender]\nproduct_name = "X"\n')
        try:
            with self.assertRaises(ProfileError) as ctx:
                load_profile(f.name)
            self.assertIn("[targeting] professions", str(ctx.exception))
        finally:
            os.unlink(f.name)

    def test_warns_when_postal_address_missing(self):
        profile = example_profile()
        self.assertEqual(compliance_warnings(profile), [])
        profile["sender"]["postal_address"] = ""
        self.assertEqual(len(compliance_warnings(profile)), 1)


class TestTemplates(unittest.TestCase):
    def test_render_fills_known_and_keeps_unknown_placeholders(self):
        self.assertEqual(render("Hi {{first_name}} {{nope}}", {"first_name": "Ana"}), "Hi Ana {{nope}}")

    def test_template_values_handles_empty_lead_name(self):
        values = template_values(example_profile(), {"name": "", "company": "Studio X"})
        self.assertEqual(values["first_name"], "there")
        self.assertEqual(values["company"], "Studio X")


class TestTargeting(unittest.TestCase):
    def test_search_queries_cover_every_profession_and_city(self):
        profile = example_profile()
        queries = build_search_queries(profile)
        targeting = profile["targeting"]
        self.assertEqual(len(queries), len(targeting["professions"]) * len(targeting["cities"]))
        self.assertIn('"wedding photographer" "Austin" contact', [q["query"] for q in queries])
        self.assertEqual(queries[0]["location"], "Austin, TX")

    def test_thumbtack_city_url(self):
        self.assertEqual(
            thumbtack_city_url("Los Angeles, CA", "videographers"),
            "https://www.thumbtack.com/ca/los-angeles/videographers/",
        )
        self.assertIsNone(thumbtack_city_url("London", "videographers"))
        self.assertIsNone(thumbtack_city_url("Austin, TX", ""))

    def test_enrich_keywords_derived_from_professions(self):
        profile = example_profile()
        self.assertEqual(enrich_keywords(profile), ["photographer"])
        profile["targeting"]["keywords"] = ["planner"]
        self.assertEqual(enrich_keywords(profile), ["planner"])


class TestOutreachEmail(unittest.TestCase):
    def test_email_is_personalised_and_has_compliance_footer(self):
        profile = example_profile()
        lead = {"name": "Silva Photo", "first_name": "Ana", "company": "Silva Photo", "category": "Wedding Photographer"}
        subject, html, text = build_outreach_email(profile, lead)
        self.assertEqual(subject, "question about Silva Photo")
        self.assertIn("Hi Ana,", text)
        self.assertIn(profile["sender"]["postal_address"], text)
        self.assertIn(profile["outreach"]["unsubscribe_line"], text)
        self.assertIn(profile["sender"]["postal_address"], html)
        self.assertNotIn("{{", text + html + subject)

    def test_scraped_values_cannot_inject_html(self):
        profile = example_profile()
        lead = {"name": "Eve", "company": 'https://x.com" onmouseover="alert(1)'}
        _subject, html, _text = build_outreach_email(profile, lead)
        self.assertNotIn('" onmouseover="', html)


class TestSetupWizard(unittest.TestCase):
    def test_wizard_output_is_a_valid_profile(self):
        from core.setup_wizard import build_profile_toml

        answers = {
            "product_name": 'Snap "Pro"', "product_url": "https://snap.example",
            "from_name": "Sam from Snap", "sign_off": "Sam", "offer": "free trial",
            "postal_address": "1 High St, Leeds, UK", "ideal_client": "florist",
            "pitch": "send quotes in one click", "professions": ["florist", "wedding florist"],
            "cities": ["Leeds", "Austin, TX"],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(build_profile_toml(answers))
        try:
            profile = load_profile(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(profile["sender"]["product_name"], 'Snap "Pro"')
        self.assertEqual(profile["targeting"]["cities"], ["Leeds", "Austin, TX"])
        subject, _html, text = build_outreach_email(profile, {"name": "Kim Flowers", "first_name": "Kim", "company": "Kim Flowers"})
        self.assertEqual(subject, "question about Kim Flowers")
        self.assertEqual(len(profile["outreach"]["follow_ups"]), 3)
        self.assertIn("interested", profile["auto_reply"]["replies"])
        self.assertIn('Snap "Pro" helps florists send quotes in one click.', text)
        self.assertEqual(compliance_warnings(profile), [])


class TestAutoReply(unittest.TestCase):
    def test_quoted_history_is_ignored(self):
        body = "Sounds good, how much is it?\n\nOn Mon, Jamie wrote:\n> Reply unsubscribe to stop"
        self.assertEqual(latest_reply_text(body), "Sounds good, how much is it?")

    def test_opt_out_detection(self):
        words = example_profile()["auto_reply"]["opt_out_words"]
        self.assertTrue(is_opt_out("Please UNSUBSCRIBE me", words))
        self.assertFalse(is_opt_out("How much is it?", words))

    def test_keywords_match_whole_words_only(self):
        self.assertTrue(is_opt_out("please stop", ["stop"]))
        self.assertFalse(is_opt_out("I stopped by your site", ["stop"]))
        self.assertTrue(is_opt_out("what are your prices?", ["price"]))
        self.assertFalse(is_opt_out("this is priceless", ["price"]))

    def test_approved_reply_per_intent(self):
        profile = example_profile()
        reply = pick_auto_reply(profile, "interested", {"first_name": "Ana"})
        self.assertTrue(reply.startswith("Hi Ana,"))
        self.assertIn(profile["sender"]["product_url"], reply)
        self.assertIsNone(pick_auto_reply(profile, "other", {"first_name": "Ana"}))
        self.assertIsNone(pick_auto_reply(profile, "not_interested", {"first_name": "Ana"}))

    def test_older_keyword_rule_profiles_still_work(self):
        profile = example_profile()
        del profile["auto_reply"]["replies"]
        profile["auto_reply"]["rules"] = [{"keywords": ["price"], "reply": "Pricing for {{first_name}}"}]
        profile["auto_reply"]["default_reply"] = "Thanks {{first_name}}"
        self.assertEqual(pick_auto_reply(profile, "question", {"first_name": "Ana"}, "what's the price?"),
                         "Pricing for Ana")
        self.assertEqual(pick_auto_reply(profile, "interested", {"first_name": "Ana"}, "yes"), "Thanks Ana")
        self.assertIsNone(pick_auto_reply(profile, "other", {"first_name": "Ana"}, "hm"))


if __name__ == "__main__":
    unittest.main()


class TestBrandedDesign(unittest.TestCase):
    def branded_profile(self) -> dict:
        profile = example_profile()
        profile["email_design"].update({"style": "branded", "logo_url": "https://example.com/logo.png",
                                        "tagline": "Less admin.", "highlights": ["Fast", "Simple"]})
        profile["outreach"]["body"] = "Hi {{first_name}},\n\nWorth a look?\n\n{{button}}\n\n{{sign_off}}"
        profile["outreach"]["button"] = {"text": "See how it works", "url": "{{product_url}}"}
        return profile

    def test_branded_email_has_button_logo_preheader_and_footer(self):
        profile = self.branded_profile()
        lead = {"first_name": "Ana", "company": "Silva <Photo>", "email": "ana@silva.test"}
        subject, html, text = build_outreach_email(profile, lead)
        self.assertIn(">See how it works &rarr;</a>", html)
        self.assertIn("utm_content=email1", html)             # button link is tracked
        self.assertIn('src="https://example.com/logo.png"', html)
        self.assertIn("Worth a look?", html.split("<table")[0] + html)  # preview text present
        self.assertIn(profile["sender"]["postal_address"], html)
        self.assertIn("Less admin.", html)
        self.assertNotIn("@@BUTTON@@", html)
        self.assertIn("See how it works: https://example.com?utm_source=outreach", text)
        self.assertNotIn("<Photo>", html)                      # scraped values escaped

    def test_unused_button_placeholder_leaves_no_gap(self):
        profile = self.branded_profile()
        del profile["outreach"]["button"]
        _subject, html, text = build_outreach_email(profile, {"first_name": "Ana"})
        self.assertNotIn("\n\n\n", text)
        self.assertNotIn("@@BUTTON@@", html)


class TestPersonalStyle(unittest.TestCase):
    """The default look for sales email: like a message typed in Gmail (Primary, not Promotions)."""

    def test_personal_email_has_no_marketing_signals(self):
        profile = example_profile()
        profile["outreach"]["body"] = "Hi {{first_name}},\n\nSee {{product_url}}.\n\n{{button}}\n\n{{sign_off}}"
        profile["outreach"]["button"] = {"text": "See how it works", "url": "{{product_url}}"}
        _subject, html, text = build_outreach_email(profile, {"first_name": "Ana", "email": "a@b.test"})
        for marketing_signal in ("<img", "<table", "display:none", "background", "border-radius", "@@BUTTON@@"):
            self.assertNotIn(marketing_signal, html)
        self.assertIn('>See how it works</a>', html)               # button is a plain link
        self.assertIn(">example.com</a>.", html)                    # clean link label, tracking hidden in href
        self.assertIn("utm_content=email1", html)
        self.assertIn(profile["outreach"]["unsubscribe_line"], html)

    def test_soft_opt_out_replies_are_honoured(self):
        from core.intent import classify_reply
        self.assertEqual(classify_reply("This isn't relevant to us", ["unsubscribe"]), "not_interested")
        self.assertEqual(classify_reply("Not the right fit, thanks", ["unsubscribe"]), "not_interested")
