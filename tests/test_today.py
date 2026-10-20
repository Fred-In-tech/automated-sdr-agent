"""The dashboard's "Today" strip: sent vs. today's limit, the next run, the schedule, what's due."""

import unittest
from datetime import datetime
from unittest import mock

from core import today

PROFILE = {"outreach": {"daily_send_limit": 10, "send_days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
           "schedule": {"run_times": ["09:00", "14:00"]}}
TUESDAY_NOON = datetime(2026, 10, 20, 12, 0).astimezone()
FRIDAY_EVENING = datetime(2026, 10, 23, 18, 0).astimezone()


def _summary(profile=PROFILE, now=TUESDAY_NOON, installed=True, sent=3, result=None):
    with mock.patch.object(today, "_sent_today", return_value=sent), \
         mock.patch.object(today, "_followups_due", return_value=2), \
         mock.patch.object(today.deliverability, "load", return_value=result or {"status": "ok"}):
        return today.summary(profile, now=now, schedule_status=lambda: {"installed": installed})


class TestToday(unittest.TestCase):
    def test_counts_and_next_run_later_today(self):
        s = _summary()
        self.assertEqual((s["sent_today"], s["daily_limit"], s["follow_ups_due"]), (3, 10, 2))
        self.assertTrue(s["schedule_on"])
        self.assertTrue(s["send_day"])
        self.assertEqual(s["next_run"], "today at 14:00")

    def test_next_run_skips_to_the_next_send_day(self):
        self.assertEqual(_summary(now=FRIDAY_EVENING)["next_run"], "Mon at 09:00")

    def test_weekend_is_not_a_send_day(self):
        saturday = datetime(2026, 10, 24, 10, 0).astimezone()
        self.assertFalse(_summary(now=saturday)["send_day"])

    def test_schedule_off_has_no_next_run(self):
        s = _summary(installed=False)
        self.assertFalse(s["schedule_on"])
        self.assertIsNone(s["next_run"])

    def test_deliverability_problem_caps_the_limit(self):
        s = _summary(result={"status": "problem", "checks": [{"key": "dkim", "status": "problem"}]})
        self.assertEqual(s["daily_limit"], 5)
        self.assertTrue(s["limit_note"])

    def test_no_profile_is_empty_but_safe(self):
        s = _summary(profile=None)
        self.assertEqual(s["daily_limit"], 0)
        self.assertIsNone(s["next_run"])

    def test_a_broken_schedule_check_reads_as_off(self):
        with mock.patch.object(today, "_sent_today", return_value=0), \
             mock.patch.object(today, "_followups_due", return_value=0):
            s = today.summary(PROFILE, now=TUESDAY_NOON, schedule_status=mock.Mock(side_effect=OSError("no crontab")))
        self.assertFalse(s["schedule_on"])


if __name__ == "__main__":
    unittest.main()
