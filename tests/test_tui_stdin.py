"""Setup prompts on macOS under `curl | bash`: stdin is /dev/tty, which kqueue can't watch.

prompt_toolkit (questionary) turns that into EOFError, the same thing Ctrl-D raises, so setup
used to stop at the very first question with "Setup cancelled". These tests pin the repair:
read from the real terminal device instead, or fall back to simple prompts.
"""

import os
import unittest
from unittest import mock

from core import tui, tui_stdin


class TestStdinWatchable(unittest.TestCase):
    def patch(self, target, **kwargs):
        patcher = mock.patch.object(tui_stdin, target, **kwargs) if hasattr(tui_stdin, target) \
            else mock.patch(target, create=True, **kwargs)   # Windows has no os.ttyname to replace
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def setUp(self):
        self.patch("_is_windows", return_value=False)
        self.patch("core.tui_stdin.os.isatty", return_value=True)
        self.dup2 = self.patch("core.tui_stdin.os.dup2")
        self.close = self.patch("core.tui_stdin.os.close")
        self.open = self.patch("core.tui_stdin.os.open", return_value=42)

    def test_a_watchable_terminal_is_left_alone(self):
        self.patch("_pollable", return_value=True)
        self.assertTrue(tui_stdin.stdin_watchable())
        self.dup2.assert_not_called()

    def test_windows_is_never_touched(self):
        self.patch("_is_windows", return_value=True)
        pollable = self.patch("_pollable")
        self.assertTrue(tui_stdin.stdin_watchable())
        pollable.assert_not_called()

    def test_stdin_that_is_not_a_terminal_is_left_alone(self):
        self.patch("core.tui_stdin.os.isatty", return_value=False)
        pollable = self.patch("_pollable")
        self.assertTrue(tui_stdin.stdin_watchable())
        pollable.assert_not_called()
        self.dup2.assert_not_called()

    def test_dev_tty_is_swapped_for_the_real_terminal_device(self):
        # fd 0 = /dev/tty (not watchable); stdout is the real device, which is
        self.patch("_pollable", side_effect=lambda fd: fd == 42)
        self.patch("core.tui_stdin.os.ttyname", side_effect=lambda fd: "/dev/ttys003")
        self.assertTrue(tui_stdin.stdin_watchable())
        self.open.assert_called_once_with("/dev/ttys003", os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
        self.dup2.assert_called_once_with(42, 0)
        self.close.assert_called_once_with(42)

    def test_no_real_terminal_device_means_simple_prompts(self):
        self.patch("_pollable", return_value=False)
        self.patch("core.tui_stdin.os.ttyname", side_effect=OSError("not a tty"))
        self.assertFalse(tui_stdin.stdin_watchable())
        self.dup2.assert_not_called()

    def test_a_device_that_is_also_unwatchable_is_closed_and_skipped(self):
        self.patch("_pollable", return_value=False)
        self.patch("core.tui_stdin.os.ttyname", return_value="/dev/ttys003")
        self.assertFalse(tui_stdin.stdin_watchable())
        self.dup2.assert_not_called()
        self.assertEqual(self.close.call_count, 2)   # tried stdout's and stderr's device, closed both

    def test_dev_tty_itself_is_not_a_replacement(self):
        self.patch("_pollable", return_value=False)
        self.patch("core.tui_stdin.os.ttyname", return_value="/dev/tty")
        self.assertFalse(tui_stdin.stdin_watchable())
        self.open.assert_not_called()

    def test_pollable_reports_an_unregistrable_fd(self):
        self.assertFalse(tui_stdin._pollable(-1))


class TestUIUsesSimplePromptsWhenStdinCantBeWatched(unittest.TestCase):
    def make_ui(self, watchable):
        with mock.patch.object(tui, "_isatty", return_value=True), \
             mock.patch.object(tui, "_import_rich", return_value=mock.MagicMock()), \
             mock.patch.object(tui, "_import_questionary", return_value=mock.MagicMock()), \
             mock.patch.object(tui, "stdin_watchable", return_value=watchable) as probe:
            ui = tui.UI(environ={})
        return ui, probe

    def test_unwatchable_stdin_switches_to_plain_prompts_but_keeps_the_pretty_output(self):
        ui, probe = self.make_ui(watchable=False)
        probe.assert_called_once()
        self.assertIsNone(ui._questionary)
        self.assertIsNotNone(ui.console)

    def test_watchable_stdin_keeps_arrow_key_menus(self):
        ui, _ = self.make_ui(watchable=True)
        self.assertIsNotNone(ui._questionary)

    def test_explicit_interactive_flag_never_probes_the_real_stdin(self):
        with mock.patch.object(tui, "stdin_watchable") as probe:
            tui.UI(plain=True, interactive=True, environ={})
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
