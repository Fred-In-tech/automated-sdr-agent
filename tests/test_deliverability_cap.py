"""A DNS Problem caps sending at 5 a day until it's fixed; warnings and failed lookups never do."""

from unittest import mock

from core import deliverability as dv
from test_sequence import SequenceTestCase

PROBLEM = {"status": "problem", "checks": [{"key": "dkim", "label": "DKIM", "status": "problem"}]}


class TestCap(SequenceTestCase):
    def setUp(self):
        super().setUp()
        self.engine.daily_limit = 20
        self.engine.max_per_run = 20
        for i in range(8):
            self.add_lead(f"lead{i}@shop{i}.test", 90 - i)

    def run_with(self, result):
        with mock.patch.object(dv, "refresh_if_stale", return_value=result):
            return self.engine.run_outreach_campaign()

    def test_problem_caps_at_five(self):
        self.run_with(PROBLEM)
        self.assertEqual(len(self.sent), dv.DELIVERABILITY_CAP)

    def test_warning_does_not_cap(self):
        self.run_with({"status": "warning", "checks": []})
        self.assertEqual(len(self.sent), 8)

    def test_a_broken_check_never_stops_sending(self):
        with mock.patch.object(dv, "refresh_if_stale", side_effect=RuntimeError("boom")):
            self.engine.run_outreach_campaign()
        self.assertEqual(len(self.sent), 8)
