"""Offline tests for core/tui_validation.py: the validators, list parsing, menu-choice
matching and answers-file loading behind the terminal UI. Pure functions, no terminal."""

import os
import tempfile
import unittest

from core.tui_validation import (
    choice_title,
    flatten_answers,
    load_answers,
    match_choice,
    normalize_choices,
    normalize_hex_color,
    normalize_hhmm,
    parse_list,
    unknown_answers,
    valid_email,
    valid_hex_color,
    valid_hhmm,
    valid_url,
    validator,
)


# ── validators ──────────────────────────────────────────────────────────────


class TestValidators(unittest.TestCase):
    def test_valid_email_accepts_real_addresses(self):
        for value in ["ana@acme.com", " ana.b+tag@sub.acme.co.uk ", "o'neil@acme.io"]:
            self.assertTrue(valid_email(value), value)

    def test_valid_email_rejects_malformed_addresses(self):
        for value in ["", "ana", "ana@", "@acme.com", "ana@acme", "ana @acme.com", "a@b@acme.com",
                      "ana@acme..com", ".ana@acme.com", "ana.@acme.com", "ana@-acme.com", None, 42]:
            self.assertFalse(valid_email(value), repr(value))

    def test_valid_url_accepts_http_urls_and_bare_domains(self):
        for value in ["https://acme.com", "http://acme.com/path?q=1", "acme.com", "www.acme.co.uk/pricing",
                      "https://acme.com:8443/x", " https://acme.com "]:
            self.assertTrue(valid_url(value), value)

    def test_valid_url_rejects_other_schemes_and_junk(self):
        for value in ["", "javascript:alert(1)", "mailto:ana@acme.com", "ftp://acme.com", "https://",
                      "not a url", "acme", "https://acme .com", "https://user:pw@acme.com", None]:
            self.assertFalse(valid_url(value), repr(value))

    def test_valid_url_can_require_a_scheme(self):
        self.assertFalse(valid_url("acme.com", require_scheme=True))
        self.assertTrue(valid_url("https://acme.com", require_scheme=True))

    def test_valid_hex_color_is_strict_rrggbb(self):
        self.assertTrue(valid_hex_color("#3B82F6"))
        self.assertTrue(valid_hex_color("#3b82f6"))
        for value in ["3B82F6", "#3B8", "#GGGGGG", "", "#3B82F6FF", None]:
            self.assertFalse(valid_hex_color(value), repr(value))

    def test_normalize_hex_color_is_forgiving(self):
        self.assertEqual(normalize_hex_color("3b82f6"), "#3B82F6")
        self.assertEqual(normalize_hex_color(" #3b82f6 "), "#3B82F6")
        self.assertEqual(normalize_hex_color("#3bf"), "#33BBFF")
        self.assertEqual(normalize_hex_color("blue"), "blue")

    def test_valid_hhmm(self):
        for value in ["09:00", "9:00", "23:59", "00:00", " 14:30 "]:
            self.assertTrue(valid_hhmm(value), value)
        for value in ["24:00", "12:60", "9", "", "9:5", "09:00pm", None]:
            self.assertFalse(valid_hhmm(value), repr(value))

    def test_normalize_hhmm_pads_the_hour(self):
        self.assertEqual(normalize_hhmm("9:00"), "09:00")
        self.assertEqual(normalize_hhmm(" 14:30 "), "14:30")
        self.assertEqual(normalize_hhmm("bad"), "bad")

    def test_parse_list_keeps_commas_inside_cities(self):
        self.assertEqual(parse_list("Austin, TX; Dallas, TX"), ["Austin, TX", "Dallas, TX"])
        self.assertEqual(parse_list("Austin, TX\nDallas, TX\n"), ["Austin, TX", "Dallas, TX"])

    def test_parse_list_splits_commas_when_there_is_no_other_separator(self):
        self.assertEqual(parse_list("plumbers, electricians"), ["plumbers", "electricians"])
        self.assertEqual(parse_list("Austin, TX", split_commas=False), ["Austin, TX"])

    def test_parse_list_drops_blanks_and_duplicates(self):
        self.assertEqual(parse_list(" a ;; b ; a ;"), ["a", "b"])
        self.assertEqual(parse_list(""), [])

    def test_parse_list_keeps_states_and_countries_with_their_city(self):
        cases = {
            "Austin, Dallas": ["Austin", "Dallas"],
            "Austin, TX, USA": ["Austin, TX, USA"],
            "London, UK, Paris, France": ["London, UK", "Paris, France"],
            "Rome, Italy, Milan, Italy": ["Rome, Italy", "Milan, Italy"],
            "Austin, Texas, USA, Denver, CO": ["Austin, Texas, USA", "Denver, CO"],
            "Paris, France": ["Paris, France"],
        }
        for text, expected in cases.items():
            self.assertEqual(parse_list(text, split_commas=False), expected, text)

    def test_parse_list_splits_several_cities_typed_on_one_line(self):
        """Job titles split on commas, so people type cities the same way. Two or more commas
        means several cities; a "City, ST" pair (2-letter or upper-case 3-letter code) stays together."""
        self.assertEqual(parse_list("Austin, Dallas, Houston", split_commas=False), ["Austin", "Dallas", "Houston"])
        self.assertEqual(parse_list("Austin, TX, Dallas, TX", split_commas=False), ["Austin, TX", "Dallas, TX"])
        self.assertEqual(parse_list("Austin, tx, Denver", split_commas=False), ["Austin, tx", "Denver"])
        self.assertEqual(parse_list("Sydney, NSW, Perth, WA", split_commas=False), ["Sydney, NSW", "Perth, WA"])
        self.assertEqual(parse_list("Paris, France", split_commas=False), ["Paris, France"])
        self.assertEqual(parse_list("Bath, Ely, York", split_commas=False), ["Bath", "Ely", "York"])
        self.assertEqual(parse_list("Austin, TX; Paris, France", split_commas=False), ["Austin, TX", "Paris, France"])

    def test_validator_wraps_a_predicate_with_a_message(self):
        check = validator(lambda s: s.startswith("x"), "Must start with x")
        self.assertIs(check("xy"), True)
        self.assertEqual(check("yy"), "Must start with x")


class TestMenuChoices(unittest.TestCase):
    OPTIONS = [("gmail", "Gmail", "App password"), ("outlook", "Outlook", "")]

    def test_normalize_choices_accepts_pairs_and_triples(self):
        self.assertEqual(normalize_choices([("a", "A"), ("b", "B", "desc")]), [("a", "A", ""), ("b", "B", "desc")])

    def test_normalize_choices_rejects_empty_and_malformed(self):
        with self.assertRaises(ValueError):
            normalize_choices([])
        with self.assertRaises(ValueError):
            normalize_choices([("only-one",)])

    def test_match_choice_by_value_label_or_number(self):
        self.assertEqual(match_choice(self.OPTIONS, "outlook", False), "outlook")
        self.assertEqual(match_choice(self.OPTIONS, " GMAIL ", False), "gmail")
        self.assertEqual(match_choice(self.OPTIONS, "Outlook", False), "outlook")
        self.assertEqual(match_choice(self.OPTIONS, "2", True), "outlook")

    def test_match_choice_numbers_only_when_allowed(self):
        with self.assertRaises(ValueError) as ctx:
            match_choice(self.OPTIONS, "2", False)
        self.assertIn("gmail, outlook", str(ctx.exception))

    def test_choice_title(self):
        self.assertEqual(choice_title(self.OPTIONS, "outlook"), "Outlook")
        self.assertEqual(choice_title(self.OPTIONS, "zzz"), "zzz")


class TestLoadAnswers(unittest.TestCase):
    def write(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_flatten_answers_uses_section_dot_key(self):
        data = {"business": {"website": "acme.com"}, "schedule": {"run_times": ["09:00"]}, "top": 1,
                "a": {"b": {"c": True}}}
        self.assertEqual(flatten_answers(data), {"business.website": "acme.com", "schedule.run_times": ["09:00"],
                                                 "top": 1, "a.b.c": True})

    def test_load_answers_reads_toml(self):
        path = self.write('[business]\nwebsite = "acme.com"\n[email]\npassword_env = "SDR_EMAIL_PASSWORD"\n')
        self.assertEqual(load_answers(path), {"business.website": "acme.com",
                                              "email.password_env": "SDR_EMAIL_PASSWORD"})

    def test_load_answers_refuses_passwords_in_the_file(self):
        path = self.write('[email]\npassword = "hunter2"\n')
        with self.assertRaises(ValueError) as ctx:
            load_answers(path)
        self.assertIn("password_env", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_load_answers_refuses_keys_tokens_and_secrets_too(self):
        """AGENTS.md promises files with keys like `api_key` are refused: so are the natural
        mis-spellings of the *_env keys (brave_api_key, brave_key) and any *_token / *_secret."""
        for line in ('[audience]\nbrave_api_key = "BSA-real-key"', '[audience]\nbrave_key = "BSA-real-key"',
                     '[audience]\napi_key = "BSA-real-key"', '[x]\naccess_token = "tok-real"',
                     '[x]\nclient_secret = "sec-real"'):
            path = self.write(line + "\n")
            with self.assertRaises(ValueError, msg=line) as ctx:
                load_answers(path)
            self.assertNotIn("real", str(ctx.exception))
        path = self.write('[audience]\nbrave_api_key_env = "SDR_BRAVE_API_KEY"\n[security]\n'
                          'dashboard_password_env = "SDR_DASHBOARD_PASSWORD"\n')
        self.assertEqual(set(load_answers(path)), {"audience.brave_api_key_env", "security.dashboard_password_env"})

    def test_unknown_answers_suggests_the_key_that_was_meant(self):
        known = ["business.website", "email.skip_login_check", "schedule.install", "updates.mode",
                 "finish.find_leads"]
        unknown = unknown_answers({"brand.website": "x", "email.skip_login_chek": True, "schedule.instal": True,
                                   "updates.modee": "auto", "finish.find_lead": True, "business.website": "ok",
                                   "zzz.qqq": 1}, known)
        self.assertEqual(unknown, {"brand.website": "business.website",
                                   "email.skip_login_chek": "email.skip_login_check",
                                   "schedule.instal": "schedule.install", "updates.modee": "updates.mode",
                                   "finish.find_lead": "finish.find_leads", "zzz.qqq": None})

    def test_load_answers_explains_toml_errors(self):
        path = self.write("[business\nwebsite = \n")
        with self.assertRaises(ValueError) as ctx:
            load_answers(path)
        self.assertIn(path, str(ctx.exception))

    def test_load_answers_missing_file(self):
        with self.assertRaises(ValueError):
            load_answers(os.path.join(tempfile.gettempdir(), "definitely-missing-answers.toml"))


if __name__ == "__main__":
    unittest.main()
