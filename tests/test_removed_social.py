"""The social post drafter was removed in v1.3.2: old schedule entries that still run
`runner.py --task social` log a note instead of failing, and nothing social is left in the UI."""

import os
import unittest
from unittest import mock

import runner
from core.product import ROOT_DIR


class TestSocialRemoved(unittest.TestCase):
    def test_old_schedule_entry_is_harmless(self):
        with mock.patch.object(runner, "log_event") as log:
            result = runner.run_task("social")
        self.assertEqual(result["status"], "removed")
        log.assert_called_once()
        self.assertNotIn("social", runner.TASKS)

    def test_runner_command_line_still_accepts_it(self):
        with mock.patch.object(runner, "run_task", return_value={"status": "removed"}) as run, \
                mock.patch("sys.argv", ["runner.py", "--task", "social"]):
            try:
                runner.main()
            except SystemExit as exc:
                self.assertIn(exc.code, (0, None))
        run.assert_called_once_with("social")

    def test_no_social_tab_or_module(self):
        with open(os.path.join(ROOT_DIR, "dashboard", "index.html"), encoding="utf-8") as f:
            page = f.read().lower()
        self.assertNotIn("social", page)
        self.assertFalse(os.path.exists(os.path.join(ROOT_DIR, "bots", "social_bot.py")))


if __name__ == "__main__":
    unittest.main()
