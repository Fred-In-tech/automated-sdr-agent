"""First-line ({{opener}}) A/B test: which opening sentence gets more real replies."""

import unittest

from core.config import opener_choice, template_values

PROFILE = {
    "sender": {"product_name": "Acme", "product_url": "https://acme.test", "from_name": "Jamie", "sign_off": "Jamie"},
    "targeting": {"ideal_client": "florist"},
    "outreach": {"default_opener": "Default about {{company}}.",
                 "opener_variants": ["A about {{company}}.", "B about {{company}}."]},
}
LEAD = {"email": "owner@bloom.test", "company": "Bloom", "category": "florist", "location": "Austin, TX"}


class TestOpenerChoice(unittest.TestCase):
    def test_no_variants_uses_the_default_without_a_label(self):
        profile = {**PROFILE, "outreach": {"default_opener": "Default about {{company}}."}}
        self.assertEqual(opener_choice(profile, LEAD), ("", "Default about {{company}}."))
        self.assertEqual(template_values(profile, LEAD)["opener"], "Default about Bloom.")

    def test_each_lead_keeps_one_variant(self):
        label, template = opener_choice(PROFILE, LEAD)
        self.assertIn(label, ("A", "B"))
        self.assertEqual(opener_choice(PROFILE, {**LEAD, "email": "OWNER@bloom.test"}), (label, template))
        self.assertEqual(template_values(PROFILE, LEAD)["opener"], template.replace("{{company}}", "Bloom"))

    def test_both_variants_get_used(self):
        labels = {opener_choice(PROFILE, {**LEAD, "email": f"lead{i}@x.test"})[0] for i in range(40)}
        self.assertEqual(labels, {"A", "B"})

    def test_independent_of_the_subject_test(self):
        """Same hash for both tests would pair subject A with opener A forever, and the two results
        couldn't be told apart."""
        from core.outreach_rules import subject_variant
        profile = {**PROFILE, "outreach": {**PROFILE["outreach"], "subject": "s",
                                            "subject_variants": ["s1", "s2"]}}
        pairs = {(subject_variant(profile, f"l{i}@x.test")[0], opener_choice(profile, {"email": f"l{i}@x.test"})[0])
                 for i in range(60)}
        self.assertEqual(len(pairs), 4)

    def test_an_ai_written_line_is_labelled_ai(self):
        lead = {**LEAD, "opener": "Loved the peony arch on your homepage."}
        self.assertEqual(opener_choice(PROFILE, lead)[0], "ai")
        self.assertEqual(template_values(PROFILE, lead)["opener"], lead["opener"])

    def test_a_single_string_or_number_is_not_a_list(self):
        for raw in ("I came across {{company}}.", 3, {"a": "b"}, None):
            profile = {**PROFILE, "outreach": {"default_opener": "D.", "opener_variants": raw}}
            self.assertEqual(opener_choice(profile, LEAD), ("", "D."))
            self.assertEqual(template_values(profile, LEAD)["opener"], "D.")

    def test_blank_or_bad_variants_are_ignored(self):
        profile = {**PROFILE, "outreach": {"default_opener": "D.", "opener_variants": ["", 3, "  "]}}
        self.assertEqual(opener_choice(profile, LEAD), ("", "D."))


if __name__ == "__main__":
    unittest.main()
