"""`sdr doctor` shows each deliverability check, with the fix for anything that isn't OK."""

import unittest
from unittest import mock

from core import deliverability as dv
from core.cli_doctor import check_deliverability

RESULT = {"status": "problem", "domain": "brightline.example", "checks": [
    {"key": "spf", "label": "SPF", "status": "ok", "detail": "SPF is set up.", "fix": "", "record": ""},
    {"key": "dkim", "label": "DKIM", "status": "problem", "detail": "No DKIM record.", "fix": "Turn on DKIM.", "record": ""},
    {"key": "dmarc", "label": "DMARC", "status": "warning", "detail": "p=none", "fix": "Use quarantine.",
     "record": "v=DMARC1; p=quarantine"},
    {"key": "sending_domain", "label": "Sending domain", "status": "info", "detail": "Main domain.", "fix": "", "record": ""},
]}


class TestDoctor(unittest.TestCase):
    def test_each_check_becomes_a_line(self):
        with mock.patch.object(dv, "refresh_if_stale", return_value=RESULT):
            checks = check_deliverability({}, {}, offline=False)
        by_label = {c.label: c for c in checks}
        self.assertEqual(by_label["Deliverability: SPF"].status, "ok")
        self.assertEqual(by_label["Deliverability: DKIM"].status, "fail")
        self.assertIn("Turn on DKIM", by_label["Deliverability: DKIM"].detail)
        self.assertIn("limited to 5", by_label["Deliverability: DKIM"].detail)
        self.assertEqual(by_label["Deliverability: DMARC"].status, "warn")
        self.assertIn("v=DMARC1; p=quarantine", by_label["Deliverability: DMARC"].detail)
        self.assertFalse(any(c.critical for c in checks))     # never stops "everything important works"

    def test_offline_uses_the_saved_result(self):
        with mock.patch.object(dv, "load", return_value=RESULT) as load, \
                mock.patch.object(dv, "refresh_if_stale") as refresh:
            check_deliverability({}, {}, offline=True)
        load.assert_called_once()
        refresh.assert_not_called()

    def test_unknown(self):
        with mock.patch.object(dv, "refresh_if_stale", return_value={"status": "unknown", "message": "offline"}):
            checks = check_deliverability({}, {}, offline=False)
        self.assertEqual((checks[0].label, checks[0].status), ("Deliverability", "info"))
