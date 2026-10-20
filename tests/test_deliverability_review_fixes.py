"""Fixes from the v1.5 code review: no false caps, no stale samples, quoted IMAP folders."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core import deliverability as dv
from core.sample_proof import fetch_auth_verdict
from test_deliverability import DOMAIN, ENV, HOSTINGER_OK, PROFILE, by_key, resolver
from test_sample_proof import FakeIMAP


class TestNoFalseCaps(unittest.TestCase):
    def test_unknown_provider_without_a_known_selector_is_only_a_warning(self):
        records = {(DOMAIN, "MX"): ["in1-smtp.messagingengine.com."], (DOMAIN, "TXT"): ["v=spf1 include:spf.messagingengine.com ~all"],
                   (f"_dmarc.{DOMAIN}", "TXT"): ["v=DMARC1; p=quarantine"]}
        result = dv.run_checks(PROFILE, {"SMTP_USER": f"a@{DOMAIN}", "SMTP_HOST": "smtp.fastmail.com"}, resolve=resolver(records))
        self.assertEqual(by_key(result)["dkim"]["status"], "warning")
        self.assertIn("test-email", by_key(result)["dkim"]["fix"])
        self.assertNotEqual(result["status"], "problem")

    def test_known_provider_missing_dkim_is_still_a_problem(self):
        records = {k: v for k, v in HOSTINGER_OK.items() if "_domainkey" not in k[0]}
        self.assertEqual(by_key(dv.run_checks(PROFILE, ENV, resolve=resolver(records)))["dkim"]["status"], "problem")

    def test_subdomain_without_its_own_mx_is_a_warning(self):
        sub = f"mail.{DOMAIN}"
        records = {**{(sub if k[0] == DOMAIN else k[0].replace(DOMAIN, sub), k[1]): v
                      for k, v in HOSTINGER_OK.items() if k[1] != "MX"},
                   (DOMAIN, "MX"): ["mx1.hostinger.com."]}
        profile = {"sender": {"from_email": f"hi@{sub}", "website": "https://other.example"}}
        result = dv.run_checks(profile, ENV, resolve=resolver(records))
        self.assertEqual(by_key(result)["mx"]["status"], "warning")

    def test_cap_can_be_turned_off(self):
        problem = {"status": "problem", "checks": [{"key": "dkim", "label": "DKIM", "status": "problem"}]}
        self.assertEqual(dv.capped_limit(20, problem, enabled=False), (20, ""))


class TestSamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "deliverability.json")
        self.no_dkim = {k: v for k, v in HOSTINGER_OK.items() if "_domainkey" not in k[0]}

    def check(self):
        return dv.refresh_if_stale(PROFILE, ENV, resolve=resolver(self.no_dkim), path=self.path, force=True)

    def test_sample_for_another_domain_is_ignored(self):
        dv.save_sample({"spf": "pass", "dkim": "pass", "dmarc": "pass"}, path=self.path, domain="old.example")
        self.assertEqual(by_key(self.check())["dkim"]["status"], "problem")

    def test_old_sample_is_ignored(self):
        dv.save_sample({"spf": "pass", "dkim": "pass", "dmarc": "pass"}, path=self.path, domain=DOMAIN)
        data = dv.load(self.path)
        data["sample"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        dv._save(data, self.path)
        self.assertEqual(by_key(self.check())["dkim"]["status"], "problem")

    def test_matching_recent_sample_counts_unless_dmarc_failed(self):
        dv.save_sample({"spf": "pass", "dkim": "pass", "dmarc": "pass"}, path=self.path, domain=DOMAIN)
        self.assertEqual(by_key(self.check())["dkim"]["status"], "ok")
        dv.save_sample({"spf": "pass", "dkim": "pass", "dmarc": "fail"}, path=self.path, domain=DOMAIN)
        self.assertEqual(by_key(self.check())["dkim"]["status"], "problem")

    def test_unknown_result_is_retried_not_cached_for_a_day(self):
        now = datetime.now(timezone.utc)
        dv.refresh_if_stale(PROFILE, ENV, now=now, resolve=resolver({}, down=True), path=self.path)
        result = dv.refresh_if_stale(PROFILE, ENV, now=now + timedelta(minutes=5), resolve=resolver(HOSTINGER_OK), path=self.path)
        self.assertEqual(result["status"], "ok")


class QuotingIMAP(FakeIMAP):
    def select(self, folder, readonly=False):
        if " " in folder.strip('"') and not folder.startswith('"'):
            raise __import__("imaplib").IMAP4.error("BAD unquoted")
        return super().select(folder, readonly)


class TestIMAPQuoting(unittest.TestCase):
    def test_folders_with_spaces_are_quoted(self):
        result = fetch_auth_verdict("<a@b>", connect=lambda: QuotingIMAP(found_in="Junk Email"), attempts=1, wait=0,
                                    sleep=lambda s: None)
        self.assertEqual(result["folder"], "Junk Email")

    def test_only_the_top_header_counts(self):
        two = (b"Authentication-Results: mx.google.com; dkim=fail; spf=pass; dmarc=fail\r\n"
               b"Authentication-Results: relay.example; dkim=pass; spf=pass; dmarc=pass\r\n\r\n")
        result = fetch_auth_verdict("<a@b>", connect=lambda: FakeIMAP(found_in="INBOX", header=two), attempts=1, wait=0,
                                    sleep=lambda s: None)
        self.assertEqual((result["dkim"], result["seen_by"]), ("fail", "mx.google.com"))


if __name__ == "__main__":
    unittest.main()
