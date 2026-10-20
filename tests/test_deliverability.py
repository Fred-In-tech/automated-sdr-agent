"""Deliverability checks (SPF, DKIM, DMARC, MX) with a fake DNS: statuses, fixes, cache, cap,
and reading the Authentication-Results header of a delivered sample."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core import deliverability as dv

DOMAIN = "brightline.example"
HOSTINGER_OK = {
    (DOMAIN, "MX"): ["mx1.hostinger.com.", "mx2.hostinger.com."],
    (DOMAIN, "TXT"): ["v=spf1 include:_spf.mail.hostinger.com ~all", "google-site-verification=abc"],
    (f"_dmarc.{DOMAIN}", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@brightline.example"],
    (f"hostingermail-a._domainkey.{DOMAIN}", "TXT"): ["v=DKIM1;k=rsa;p=MIIBIjANBgkq"],
    (f"hostingermail-b._domainkey.{DOMAIN}", "TXT"): ["v=DKIM1;p="],
}
PROFILE = {"sender": {"website": "https://brightline.example", "from_email": ""}}
ENV = {"SMTP_USER": f"jamie@{DOMAIN}", "SMTP_HOST": "smtp.hostinger.com"}


def resolver(records, down=False):
    def resolve(name, rtype):
        if down:
            raise dv.DNSUnavailable("timeout")
        return list(records.get((name, rtype), []))
    return resolve


def run(records, profile=PROFILE, env=ENV, **kw):
    return dv.run_checks(profile, env, resolve=resolver(records), **kw)


def by_key(result):
    return {c["key"]: c for c in result["checks"]}


class TestChecks(unittest.TestCase):
    def test_all_good(self):
        result = run(HOSTINGER_OK)
        checks = by_key(result)
        self.assertEqual((result["domain"], result["provider"]), (DOMAIN, "hostinger"))
        self.assertEqual([checks[k]["status"] for k in ("mx", "spf", "dkim", "dmarc")], ["ok"] * 4)
        self.assertIn("hostingermail-a", checks["dkim"]["detail"])
        self.assertEqual(result["status"], "ok")

    def test_dmarc_none_is_a_warning_not_a_problem(self):
        records = {**HOSTINGER_OK, (f"_dmarc.{DOMAIN}", "TXT"): ["v=DMARC1; p=none"]}
        result = run(records)
        self.assertEqual(by_key(result)["dmarc"]["status"], "warning")
        self.assertEqual(result["status"], "warning")

    def test_missing_spf_and_dkim_are_problems_with_fixes(self):
        records = {(DOMAIN, "MX"): ["mx1.hostinger.com."]}
        result = run(records)
        checks = by_key(result)
        self.assertEqual(checks["spf"]["status"], "problem")
        self.assertIn("include:_spf.mail.hostinger.com", checks["spf"]["record"])
        self.assertEqual(checks["dkim"]["status"], "problem")
        self.assertIn("Hostinger", checks["dkim"]["fix"])
        self.assertEqual(checks["dmarc"]["status"], "warning")
        self.assertIn("v=DMARC1", checks["dmarc"]["record"])
        self.assertEqual(result["status"], "problem")

    def test_spf_edge_cases(self):
        two = {**HOSTINGER_OK, (DOMAIN, "TXT"): ["v=spf1 include:_spf.mail.hostinger.com ~all", "v=spf1 -all"]}
        self.assertEqual(by_key(run(two))["spf"]["status"], "problem")
        plus_all = {**HOSTINGER_OK, (DOMAIN, "TXT"): ["v=spf1 +all"]}
        self.assertEqual(by_key(run(plus_all))["spf"]["status"], "problem")
        wrong_provider = {**HOSTINGER_OK, (DOMAIN, "TXT"): ["v=spf1 include:_spf.google.com ~all"]}
        self.assertEqual(by_key(run(wrong_provider))["spf"]["status"], "warning")

    def test_provider_from_mx_and_selectors(self):
        google = {(DOMAIN, "MX"): ["aspmx.l.google.com."], (DOMAIN, "TXT"): ["v=spf1 include:_spf.google.com ~all"],
                  (f"google._domainkey.{DOMAIN}", "TXT"): ["v=DKIM1; k=rsa; p=MIIB"],
                  (f"_dmarc.{DOMAIN}", "TXT"): ["v=DMARC1; p=reject"]}
        result = run(google, env={"SMTP_USER": f"a@{DOMAIN}", "SMTP_HOST": "smtp.gmail.com"})
        self.assertEqual(result["provider"], "google_workspace")
        self.assertEqual(result["status"], "ok")

    def test_custom_dkim_selector_found_by_sample(self):
        records = {k: v for k, v in HOSTINGER_OK.items() if "_domainkey" not in k[0]}
        sample = {"dkim": "pass", "spf": "pass", "dmarc": "pass", "seen_by": "mx.google.com", "domain": DOMAIN,
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        result = run(records, sample=sample)
        self.assertEqual(by_key(result)["dkim"]["status"], "ok")
        self.assertIn("sample", by_key(result)["dkim"]["detail"])

    def test_alias_domain_is_checked(self):
        profile = {"sender": {"website": "https://brightline.example", "from_email": "hello@getbrightline.example"}}
        result = dv.run_checks(profile, ENV, resolve=resolver({}))
        self.assertEqual(result["domain"], "getbrightline.example")

    def test_consumer_mailbox_skips_dns(self):
        result = dv.run_checks({"sender": {}}, {"SMTP_USER": "jamie@gmail.com", "SMTP_HOST": "smtp.gmail.com"},
                               resolve=resolver({}, down=True))
        self.assertTrue(result["consumer"])
        self.assertEqual(result["status"], "ok")

    def test_sending_from_the_website_domain_gets_a_note(self):
        self.assertIn("sending_domain", by_key(run(HOSTINGER_OK)))
        other = {"sender": {"website": "https://brightline.example", "from_email": "hi@getbrightline.example"}}
        self.assertNotIn("sending_domain", by_key(dv.run_checks(other, ENV, resolve=resolver({}))))
        self.assertEqual(by_key(run(HOSTINGER_OK))["sending_domain"]["status"], "info")

    def test_dns_down_is_not_a_problem(self):
        result = dv.run_checks(PROFILE, ENV, resolve=resolver({}, down=True))
        self.assertEqual(result["status"], "unknown")

    def test_no_address(self):
        self.assertEqual(dv.run_checks({"sender": {}}, {}, resolve=resolver({}))["status"], "unknown")


class TestCacheAndCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "data", "deliverability.json")

    def test_refresh_once_a_day_and_keep_cache_when_dns_is_down(self):
        now = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
        calls = []

        def resolve(name, rtype):
            calls.append(name)
            return resolver(HOSTINGER_OK)(name, rtype)
        first = dv.refresh_if_stale(PROFILE, ENV, now=now, resolve=resolve, path=self.path)
        self.assertEqual(first["status"], "ok")
        count = len(calls)
        dv.refresh_if_stale(PROFILE, ENV, now=now + timedelta(hours=3), resolve=resolve, path=self.path)
        self.assertEqual(len(calls), count)                                  # cached
        later = dv.refresh_if_stale(PROFILE, ENV, now=now + timedelta(days=2), resolve=resolver({}, down=True),
                                    path=self.path)
        self.assertEqual(later["status"], "ok")                              # DNS down: keep what we knew
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_cap(self):
        self.assertEqual(dv.capped_limit(20, {"status": "problem"})[0], dv.DELIVERABILITY_CAP)
        self.assertIn("SPF", dv.capped_limit(20, {"status": "problem", "checks": [
            {"key": "spf", "status": "problem", "label": "SPF"}]})[1])
        self.assertEqual(dv.capped_limit(20, {"status": "warning"}), (20, ""))
        self.assertEqual(dv.capped_limit(3, {"status": "problem"})[0], 3)
        self.assertEqual(dv.capped_limit(20, {}), (20, ""))

    def test_sample_verdict_is_kept_across_refreshes(self):
        dv.save_sample({"spf": "pass", "dkim": "pass", "dmarc": "pass", "seen_by": "mx.google.com"}, path=self.path)
        result = dv.refresh_if_stale(PROFILE, ENV, now=datetime.now(timezone.utc), resolve=resolver(HOSTINGER_OK),
                                     path=self.path, force=True)
        self.assertEqual(result["sample"]["dkim"], "pass")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sample"]["seen_by"], "mx.google.com")


class TestAuthenticationResults(unittest.TestCase):
    def test_gmail_header(self):
        header = ("mx.google.com; dkim=pass header.i=@brightline.example header.s=hostingermail-a; "
                  "spf=pass (google.com: domain of jamie@brightline.example designates 1.2.3.4 as permitted sender) "
                  "smtp.mailfrom=jamie@brightline.example; dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=brightline.example")
        self.assertEqual(dv.parse_authentication_results(header),
                         {"spf": "pass", "dkim": "pass", "dmarc": "pass", "seen_by": "mx.google.com"})

    def test_outlook_and_failures(self):
        header = "spf=softfail (sender IP is 1.2.3.4) smtp.mailfrom=x.example; dkim=none (message not signed); dmarc=fail action=none header.from=x.example"
        parsed = dv.parse_authentication_results(header)
        self.assertEqual((parsed["spf"], parsed["dkim"], parsed["dmarc"]), ("softfail", "none", "fail"))

    def test_garbage(self):
        self.assertEqual(dv.parse_authentication_results(""), {})


if __name__ == "__main__":
    unittest.main()
