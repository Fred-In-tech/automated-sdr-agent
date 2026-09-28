"""Offline tests for lead qualification. Safe to run: no network, no emails.

Every business, owner and address below is made up (reserved `.test` domains): this file is
public, so it must never carry a real lead that was scraped and scored."""

import re
import unittest
from pathlib import Path

from core.config import EXAMPLE_PROFILE_PATH, load_profile
from core.qualify import clean_company_name, extract_first_name, pick_best_contact, score_lead

# RFC 2606 / 6761 names that can never belong to anyone
RESERVED_SUFFIXES = (".test", ".example", ".invalid", ".localhost", "example.com", "example.net", "example.org")
REAL_LOOKING_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|us|uk|de|fr|ca|au|me|ai|app|dev)\b", re.I)

STUDIO_PAGE = """Sunridge Wedding Films. Hi, I'm Jamie — an Austin wedding videographer and filmmaker.
Wedding films for couples who love story. Packages start at $3,500. View our investment guide
and check availability. Inquire today."""


def videographer_profile() -> dict:
    profile = load_profile(EXAMPLE_PROFILE_PATH)
    profile["targeting"]["professions"] = ["wedding videographer", "wedding filmmaker"]
    profile["targeting"]["keywords"] = ["videographer", "filmmaker"]
    return profile


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.profile = videographer_profile()

    def test_studio_with_owner_email_is_a_strong_fit(self):
        result = score_lead(self.profile, "jamie@sunridgefilms.test", "sunridgefilms.test",
                            STUDIO_PAGE, "Sunridge Wedding Films")
        self.assertTrue(result["qualified"])
        self.assertEqual(result["first_name"], "Jamie")
        self.assertGreaterEqual(result["score"], 80)

    def test_bank_investor_relations_is_rejected(self):
        page = "Bigbank Investor Relations. NYSE: BBK. Videographer careers available."
        result = score_lead(self.profile, "investorrelations@bigbank.test", "bigbank.test", page, "Bigbank")
        self.assertFalse(result["qualified"])

    def test_off_icp_site_is_rejected(self):
        page = "Premium pet accessories. Collars, leashes and toys. Free shipping on all orders."
        result = score_lead(self.profile, "info@pawsandcollars.test", "pawsandcollars.test", page, "Paws & Collars")
        self.assertFalse(result["qualified"])

    def test_email_on_another_domain_is_rejected(self):
        result = score_lead(self.profile, "report@vip.freemail.test", "studio.test", STUDIO_PAGE, "Studio")
        self.assertFalse(result["qualified"])
        self.assertIn("doesn't match", result["reasons"][0])

    def test_blocked_mailbox_is_rejected(self):
        result = score_lead(self.profile, "press@sunridgefilms.test", "sunridgefilms.test", STUDIO_PAGE, "SWF")
        self.assertFalse(result["qualified"])

    def test_job_board_support_desk_falls_below_threshold(self):
        page = "Videographer jobs near you. Apply to videographer and filmmaker jobs. Upload your resume."
        result = score_lead(self.profile, "support@jobboard.test", "jobboard.test", page, "JobBoard")
        self.assertFalse(result["qualified"])

    def test_directory_sites_are_rejected(self):
        page = ("FindAVideographer.test — describe your shoot, get matched. The directory: browse videographers "
                "by city. Wedding videographer packages and pricing.")
        result = score_lead(self.profile, "hello@findavideographer.test", "findavideographer.test", page,
                            "FindAVideographer.test")
        self.assertFalse(result["qualified"])

    def test_best_contact_prefers_the_owner(self):
        email, result = pick_best_contact(
            self.profile, ["info@sunridgefilms.test", "jamie@sunridgefilms.test"],
            "sunridgefilms.test", STUDIO_PAGE, "Sunridge Wedding Films")
        self.assertEqual(email, "jamie@sunridgefilms.test")
        self.assertTrue(result["qualified"])


class TestNames(unittest.TestCase):
    def test_first_name_from_mailbox(self):
        self.assertEqual(extract_first_name("sarah", "Meet Sarah, our lead shooter", "Bloom Films"), "Sarah")
        self.assertEqual(extract_first_name("janedoe", "Jane Doe Photography", "Jane Doe Photography"), "Jane")

    def test_business_word_is_not_a_name(self):
        self.assertIsNone(extract_first_name("golden", "Golden Hour Studio", "Golden Hour Studio"))
        self.assertIsNone(extract_first_name("info", "Info about Sarah", "Studio"))

    def test_name_must_appear_on_site(self):
        self.assertIsNone(extract_first_name("sarah", "We are a wedding film studio", "Bloom Films"))

    def test_company_name_cleanup(self):
        self.assertEqual(clean_company_name("", "Rowan Photography | Commercial | Events", "rowanphoto.test"),
                         "Rowan Photography")
        self.assertEqual(clean_company_name("Harbor Light Productions", "Home - HLP", "harborlight.test"),
                         "Harbor Light Productions")
        self.assertEqual(clean_company_name("", "$44k", "brightframe.test"), "Brightframe")
        self.assertEqual(clean_company_name("", "Home", "blue-door-photo.test"), "Blue Door Photo")


class TestFixtureHygiene(unittest.TestCase):
    def test_fixtures_never_name_a_real_mailbox_or_business(self):
        """Once a real studio's owner@ address is in a public test file it is in git history for
        good, so every address and domain in this file has to use a reserved name."""
        source = Path(__file__).read_text(encoding="utf-8")
        hosts = re.findall(r"[\w.+-]+@([\w.-]+)", source) + REAL_LOOKING_HOST.findall(source)
        self.assertTrue(hosts, "expected the fixtures to contain addresses")
        real = sorted({h for h in hosts if not h.lower().endswith(RESERVED_SUFFIXES)})
        self.assertEqual(real, [], "real-looking domains in test fixtures")
