"""Offline tests for cross-platform scheduling (cron / Windows Task Scheduler).

Nothing here touches the real crontab or Task Scheduler: every `crontab` / `schtasks` call goes
through a fake runner. The run-script tests execute a *copy* of the script in a temp folder with
fake runner.py / cli.py files, so no bot ever runs and nothing is emailed.
"""

import io
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core import scheduler
from core.scheduler import (
    ScheduleError,
    battery_commands,
    cron_lines,
    describe,
    install_schedule,
    merge_crontab,
    parse_schtasks_query,
    remove_from_crontab,
    remove_schedule,
    schedule_config,
    schedule_status,
    schtasks_commands,
    schtasks_remove_commands,
    task_folder,
    times_for_interval,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = "/home/ann/automated-sdr"
SCRIPT = '"/home/ann/automated-sdr/run_cron_pipeline.sh"'
WIN_ROOT = "C:\\Users\\Ann\\automated-sdr"
WIN_FOLDER = task_folder(WIN_ROOT)
BEGIN = "# >>> automated-sdr (/home/ann/automated-sdr) >>>"
END = "# <<< automated-sdr (/home/ann/automated-sdr) <<<"


def default_cfg(**overrides) -> dict:
    return {**schedule_config(None), **overrides}


def jobs(lines: list[str]) -> list[str]:
    """Only the job lines (crontab comments dropped)."""
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def expand(lines: list[str], task: str) -> list[str]:
    """Turn cron job lines back into the sorted 'HH:MM' times they fire at, for one task."""
    times = set()
    for line in jobs(lines):
        if not line.endswith(" " + task):
            continue
        minutes, hours = line.split()[:2]
        for h in hours.split(","):
            for m in minutes.split(","):
                times.add(f"{int(h):02d}:{int(m):02d}")
    return sorted(times)


class FakeRunner:
    """Stands in for subprocess.run: records every command, replies from a script of results."""

    def __init__(self, crontab: str | None = "", responses: dict | None = None):
        self.crontab = crontab          # None = the user has no crontab yet
        self.responses = responses or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), **kwargs})
        key = " ".join(cmd[:2])
        if key in self.responses:
            result = self.responses[key]
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(cmd, *result)
        if cmd[:2] == ["crontab", "-l"]:
            if self.crontab is None:
                return subprocess.CompletedProcess(cmd, 1, "", "crontab: no crontab for ann\n")
            return subprocess.CompletedProcess(cmd, 0, self.crontab, "")
        if cmd[:2] == ["crontab", "-"]:
            self.crontab = kwargs["input"]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def commands(self, prefix: list[str]) -> list[list[str]]:
        return [c["cmd"] for c in self.calls if c["cmd"][:len(prefix)] == prefix]


# ── Schedule config ──────────────────────────────────────────────────────────


class TestScheduleConfig(unittest.TestCase):
    def test_defaults_without_a_profile(self):
        expected = {"run_times": ["09:00", "14:00"], "reply_check_minutes": 45, "update_mode": "notify"}
        self.assertEqual(schedule_config(None), expected)
        self.assertEqual(schedule_config({}), expected)
        self.assertEqual(schedule_config({"sender": {"product_name": "x"}}), expected)

    def test_reads_and_normalises_the_profile(self):
        cfg = schedule_config({"schedule": {"run_times": ["14:00", "9:05", "09:05"], "reply_check_minutes": 30},
                               "updates": {"mode": "Auto"}})
        self.assertEqual(cfg, {"run_times": ["09:05", "14:00"], "reply_check_minutes": 30, "update_mode": "auto"})

    def test_zero_turns_the_reply_check_off_and_empty_list_means_no_full_runs(self):
        cfg = schedule_config({"schedule": {"run_times": [], "reply_check_minutes": 0}})
        self.assertEqual((cfg["run_times"], cfg["reply_check_minutes"]), ([], 0))

    def test_rejects_bad_values_with_a_clear_message(self):
        bad = [
            {"schedule": {"run_times": ["25:00"]}},
            {"schedule": {"run_times": ["9am"]}},
            {"schedule": {"run_times": "09:00"}},
            {"schedule": {"reply_check_minutes": 2}},
            {"schedule": {"reply_check_minutes": 5000}},
            {"schedule": {"reply_check_minutes": True}},
            {"schedule": {"reply_check_minutes": "45"}},
            {"updates": {"mode": "sometimes"}},
        ]
        for profile in bad:
            with self.subTest(profile=profile), self.assertRaises(ScheduleError) as ctx:
                schedule_config(profile)
            self.assertIn("config/profile.toml", str(ctx.exception))


class TestTimesForInterval(unittest.TestCase):
    def test_every_45_minutes_covers_the_day(self):
        times = times_for_interval(45)
        self.assertEqual(len(times), 32)
        self.assertEqual(times[:4], ["00:00", "00:45", "01:30", "02:15"])
        self.assertEqual(times[-1], "23:15")

    def test_other_intervals(self):
        self.assertEqual(len(times_for_interval(60)), 24)
        self.assertEqual(times_for_interval(1000), ["00:00", "16:40"])
        self.assertEqual(times_for_interval(0), [])
        self.assertEqual(times_for_interval(-5), [])


# ── Cron ─────────────────────────────────────────────────────────────────────


class TestCronLines(unittest.TestCase):
    def test_default_schedule_exact_lines(self):
        self.assertEqual(jobs(cron_lines(default_cfg(), ROOT)), [
            f"0 9,14 * * * {SCRIPT} pipeline",
            f"0 0,3,6,12,15,18,21 * * * {SCRIPT} inbox",
            f"15 2,5,8,11,14,17,20,23 * * * {SCRIPT} inbox",
            f"30 1,4,7,10,13,16,19,22 * * * {SCRIPT} inbox",
            f"45 0,3,6,9,12,15,18,21 * * * {SCRIPT} inbox",
            f"30 8 * * 1 {SCRIPT} update",
        ])

    def test_reply_check_fires_every_45_minutes_except_during_full_runs(self):
        lines = cron_lines(default_cfg(), ROOT)
        self.assertEqual(expand(lines, "pipeline"), ["09:00", "14:00"])
        expected = [t for t in times_for_interval(45) if t not in ("09:00", "14:00")]
        self.assertEqual(expand(lines, "inbox"), expected)

    def test_run_times_on_the_same_minute_share_a_line(self):
        lines = cron_lines(default_cfg(run_times=["08:30", "12:00", "17:30"], reply_check_minutes=0), ROOT)
        self.assertEqual(jobs(lines)[:2], [f"0 12 * * * {SCRIPT} pipeline", f"30 8,17 * * * {SCRIPT} pipeline"])
        self.assertEqual(expand(lines, "inbox"), [])

    def test_reply_check_any_interval_round_trips(self):
        for minutes in (5, 20, 45, 60, 90, 180, 720):
            with self.subTest(minutes=minutes):
                lines = cron_lines(default_cfg(run_times=[], reply_check_minutes=minutes), ROOT)
                self.assertEqual(expand(lines, "inbox"), times_for_interval(minutes))

    def test_update_line_follows_the_mode(self):
        self.assertEqual(expand(cron_lines(default_cfg(update_mode="auto"), ROOT), "update"), ["08:30"])
        self.assertEqual(expand(cron_lines(default_cfg(update_mode="off"), ROOT), "update"), [])

    def test_paths_with_spaces_are_quoted(self):
        lines = jobs(cron_lines(default_cfg(), "/Users/ann/My Tools/sdr"))
        self.assertTrue(all('"/Users/ann/My Tools/sdr/run_cron_pipeline.sh"' in line for line in lines))

    def test_shell_special_characters_are_single_quoted(self):
        lines = jobs(cron_lines(default_cfg(), '/home/ann/$weird "dir"'))
        self.assertIn("'/home/ann/$weird \"dir\"/run_cron_pipeline.sh' pipeline", lines[0])

    def test_percent_or_newline_in_path_is_refused(self):
        for root in ("/home/ann/100%", "/home/ann/a\nb"):
            with self.subTest(root=root), self.assertRaises(ScheduleError):
                cron_lines(default_cfg(), root)


class TestMergeCrontab(unittest.TestCase):
    def setUp(self):
        self.lines = cron_lines(default_cfg(), ROOT)

    def test_into_an_empty_crontab(self):
        merged = merge_crontab("", self.lines, ROOT)
        out = merged.splitlines()
        self.assertEqual(out[0], BEGIN)
        self.assertEqual(out[-1], END)
        self.assertTrue(merged.endswith("\n"))
        for line in jobs(self.lines):
            self.assertIn(line, out)

    def test_keeps_other_jobs_verbatim_and_is_idempotent(self):
        existing = "MAILTO=me@example.com\n# backups\n*/5 * * * * /usr/bin/backup --fast   \n"
        once = merge_crontab(existing, self.lines, ROOT)
        self.assertTrue(once.startswith(existing))
        self.assertEqual(merge_crontab(once, self.lines, ROOT), once)

    def test_replaces_the_old_block_in_place(self):
        existing = "A=1\n" + merge_crontab("", self.lines, ROOT) + "0 1 * * * /bin/other\n"
        new_lines = cron_lines(default_cfg(run_times=["10:00"], reply_check_minutes=0), ROOT)
        merged = merge_crontab(existing, new_lines, ROOT)
        self.assertEqual(merged.count(BEGIN), 1)
        self.assertNotIn(" inbox", merged)
        self.assertIn(f"0 10 * * * {SCRIPT} pipeline", merged)
        out = merged.splitlines()
        self.assertEqual(out[0], "A=1")
        self.assertEqual(out[-1], "0 1 * * * /bin/other")

    def test_leaves_another_install_alone(self):
        other_root = "/home/ann/automated-sdr-2"
        other = merge_crontab("", cron_lines(default_cfg(), other_root), other_root)
        merged = merge_crontab(other, self.lines, ROOT)
        self.assertTrue(merged.startswith(other))
        self.assertEqual(remove_from_crontab(merged, ROOT), other)

    def test_removes_legacy_lines_and_their_comment(self):
        legacy = (
            "MAILTO=me@example.com\n"
            "# MyProposer Outbound Automations: full pipeline at 9 and 14\n"
            f"0 9,14 * * * {SCRIPT}\n"
            "# MyProposer SDR reply check every 45 min\n"
            f"0,45 0-21/3 * * * {SCRIPT} inbox\n"
            f"30 1-22/3 * * * {SCRIPT} inbox\n"
            f"15 2-23/3 * * * {SCRIPT} inbox\n"
            "# my own note\n"
            "0 3 * * * /usr/bin/backup\n"
        )
        merged = merge_crontab(legacy, self.lines, ROOT)
        self.assertNotIn("MyProposer", merged)
        self.assertNotIn("0-21/3", merged)
        self.assertTrue(merged.startswith("MAILTO=me@example.com\n# my own note\n0 3 * * * /usr/bin/backup\n"))
        self.assertEqual(merged.count(f"{SCRIPT} pipeline"), 1)

    def test_legacy_detection_needs_the_exact_script_path(self):
        existing = (
            "# MyProposer SDR reply check\n"
            '0 * * * * "/home/ann/automated-sdr-old/run_cron_pipeline.sh" inbox\n'
            '0 * * * * "/x/home/ann/automated-sdr/run_cron_pipeline.sh" inbox\n'
            f"# 0 9 * * * {SCRIPT}\n"
        )
        self.assertEqual(remove_from_crontab(existing, ROOT), existing)

    def test_remove_restores_the_original(self):
        for original in ("", "MAILTO=x@y.z\n", "0 3 * * * /usr/bin/backup\n\n# end\n"):
            with self.subTest(original=original):
                merged = merge_crontab(original, self.lines, ROOT)
                self.assertEqual(remove_from_crontab(merged, ROOT), original)

    def test_block_without_end_marker_only_drops_our_lines(self):
        broken = f"{BEGIN}\n0 9 * * * {SCRIPT} pipeline\n0 3 * * * /usr/bin/backup\n"
        self.assertEqual(remove_from_crontab(broken, ROOT), "0 3 * * * /usr/bin/backup\n")

    def test_windows_line_endings_are_understood(self):
        existing = "A=1\r\n" + merge_crontab("", self.lines, ROOT).replace("\n", "\r\n")
        self.assertEqual(remove_from_crontab(existing, ROOT), "A=1\n")


# ── Windows Task Scheduler ───────────────────────────────────────────────────


class TestTaskFolder(unittest.TestCase):
    """Every install folder gets its own Task Scheduler folder, so two copies on one PC (a live
    one and a test one, say) can never delete each other's tasks."""

    def test_folder_is_derived_from_the_install_path(self):
        self.assertRegex(WIN_FOLDER, r"^AutomatedSDR-[0-9a-f]{8}$")
        self.assertEqual(task_folder("c:/users/ann/automated-sdr/"), WIN_FOLDER)  # same folder to Windows
        self.assertNotEqual(task_folder("C:\\Users\\Ann\\automated-sdr-test"), WIN_FOLDER)


class TestSchtasks(unittest.TestCase):
    def test_default_schedule_exact_commands(self):
        run = '"C:\\Users\\Ann\\automated-sdr\\run_task.cmd"'
        self.assertEqual(schtasks_commands(default_cfg(), WIN_ROOT), [
            ["schtasks", "/Create", "/TN", f"{WIN_FOLDER}\\Pipeline-0900", "/TR", f"{run} pipeline",
             "/SC", "DAILY", "/ST", "09:00", "/F"],
            ["schtasks", "/Create", "/TN", f"{WIN_FOLDER}\\Pipeline-1400", "/TR", f"{run} pipeline",
             "/SC", "DAILY", "/ST", "14:00", "/F"],
            ["schtasks", "/Create", "/TN", f"{WIN_FOLDER}\\ReplyCheck", "/TR", f"{run} inbox",
             "/SC", "MINUTE", "/MO", "45", "/F"],
            ["schtasks", "/Create", "/TN", f"{WIN_FOLDER}\\UpdateCheck", "/TR", f"{run} update",
             "/SC", "WEEKLY", "/D", "MON", "/ST", "08:30", "/F"],
        ])

    def test_off_switches_drop_their_tasks(self):
        cmds = schtasks_commands(default_cfg(reply_check_minutes=0, update_mode="off"), WIN_ROOT)
        self.assertEqual([c[3] for c in cmds], [f"{WIN_FOLDER}\\Pipeline-0900", f"{WIN_FOLDER}\\Pipeline-1400"])

    def test_overlong_task_command_is_refused(self):
        with self.assertRaises(ScheduleError):
            schtasks_commands(default_cfg(), "C:\\" + "x" * 300)

    def test_remove_commands(self):
        self.assertEqual(schtasks_remove_commands([f"\\{WIN_FOLDER}\\ReplyCheck"]),
                         [["schtasks", "/Delete", "/TN", f"\\{WIN_FOLDER}\\ReplyCheck", "/F"]])

    def test_battery_commands_let_every_task_start_and_keep_running_unplugged(self):
        """schtasks /Create can't set power conditions, and its defaults skip or kill tasks on
        battery; cron has no such rule, so a laptop must behave the same on both."""
        cmds = battery_commands(default_cfg(), WIN_ROOT)
        self.assertEqual([c[:4] for c in cmds], [["powershell", "-NoProfile", "-NonInteractive", "-Command"]] * 4)
        for cmd, leaf in zip(cmds, ("Pipeline-0900", "Pipeline-1400", "ReplyCheck", "UpdateCheck")):
            self.assertIn(f"-TaskPath '\\{WIN_FOLDER}\\'", cmd[4])
            self.assertIn(f"-TaskName '{leaf}'", cmd[4])
            self.assertIn("-AllowStartIfOnBatteries", cmd[4])
            self.assertIn("-DontStopIfGoingOnBatteries", cmd[4])
        fewer = battery_commands(default_cfg(reply_check_minutes=0, update_mode="off"), WIN_ROOT)
        self.assertEqual(len(fewer), 2)

    def test_parse_query_finds_only_this_installs_tasks(self):
        other = task_folder("C:\\Users\\Ann\\automated-sdr-test")
        output = (
            f'\r\n"\\{WIN_FOLDER}\\Pipeline-0900","9/27/2026 9:00:00 AM","Ready"\r\n'
            f'"\\{WIN_FOLDER}\\ReplyCheck","9/26/2026 4:45:00 PM","Ready"\r\n'
            '"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Ready"\r\n'
            f'"\\{other}\\ReplyCheck","N/A","Ready"\r\n'
            f'"\\{WIN_FOLDER}X\\Other","N/A","Ready"\r\n'
            f'"\\{WIN_FOLDER}\\ReplyCheck","9/26/2026 4:45:00 PM","Ready"\r\n'
        )
        self.assertEqual(parse_schtasks_query(output, WIN_ROOT),
                         [f"\\{WIN_FOLDER}\\Pipeline-0900", f"\\{WIN_FOLDER}\\ReplyCheck"])


# ── install / remove / status ────────────────────────────────────────────────


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for name in ("run_cron_pipeline.sh", "run_task.cmd"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
                f.write("#!/bin/bash\n")
        self.begin = f"# >>> automated-sdr ({self.root}) >>>"

    def tearDown(self):
        self.tmp.cleanup()


class TestCronInstall(InstallTestCase):
    def test_install_adds_the_block_and_keeps_existing_jobs(self):
        fake = FakeRunner(crontab="0 3 * * * /usr/bin/backup\n")
        result = install_schedule({}, root_dir=self.root, runner=fake, system="posix")
        self.assertTrue(fake.crontab.startswith("0 3 * * * /usr/bin/backup\n"))
        self.assertIn(self.begin, fake.crontab)
        self.assertIn("Full run at 09:00 and 14:00", result)
        self.assertTrue(any("crontab" in line for line in result))
        self.assertTrue(os.access(os.path.join(self.root, "run_cron_pipeline.sh"), os.X_OK)
                        or os.name == "nt")

    def test_install_works_when_there_is_no_crontab_yet(self):
        fake = FakeRunner(crontab=None)
        install_schedule(None, root_dir=self.root, runner=fake, system="posix")
        self.assertIn(self.begin, fake.crontab)
        write = fake.commands(["crontab", "-"])
        self.assertEqual(len(write), 1)

    def test_install_twice_does_not_rewrite_an_unchanged_crontab(self):
        fake = FakeRunner(crontab=None)
        install_schedule({}, root_dir=self.root, runner=fake, system="posix")
        install_schedule({}, root_dir=self.root, runner=fake, system="posix")
        self.assertEqual(len(fake.commands(["crontab", "-"])), 1)

    def test_unreadable_crontab_is_never_overwritten(self):
        fake = FakeRunner(responses={"crontab -l": (1, "", "crontab: permission denied\n")})
        with self.assertRaises(ScheduleError):
            install_schedule({}, root_dir=self.root, runner=fake, system="posix")
        self.assertEqual(fake.commands(["crontab", "-"]), [])

    def test_missing_crontab_program_gives_a_friendly_error(self):
        fake = FakeRunner(responses={"crontab -l": FileNotFoundError("crontab")})
        with self.assertRaises(ScheduleError) as ctx:
            install_schedule({}, root_dir=self.root, runner=fake, system="posix")
        self.assertIn("cron", str(ctx.exception))

    def test_failed_write_raises(self):
        fake = FakeRunner(responses={"crontab -": (1, "", "crontab: installing new crontab failed\n")})
        with self.assertRaises(ScheduleError):
            install_schedule({}, root_dir=self.root, runner=fake, system="posix")

    def test_timeout_raises_schedule_error(self):
        fake = FakeRunner(responses={"crontab -l": subprocess.TimeoutExpired(["crontab", "-l"], 30)})
        with self.assertRaises(ScheduleError):
            install_schedule({}, root_dir=self.root, runner=fake, system="posix")

    def test_missing_run_script_is_reported(self):
        os.remove(os.path.join(self.root, "run_cron_pipeline.sh"))
        with self.assertRaises(ScheduleError):
            install_schedule({}, root_dir=self.root, runner=FakeRunner(), system="posix")

    def test_invalid_profile_schedule_is_reported_before_touching_cron(self):
        fake = FakeRunner()
        with self.assertRaises(ScheduleError):
            install_schedule({"schedule": {"run_times": ["99:99"]}}, root_dir=self.root, runner=fake,
                             system="posix")
        self.assertEqual(fake.calls, [])

    def test_status_then_remove(self):
        fake = FakeRunner(crontab="MAILTO=x@y.z\n")
        self.assertFalse(schedule_status(root_dir=self.root, runner=fake, system="posix")["installed"])
        install_schedule({}, root_dir=self.root, runner=fake, system="posix")
        status = schedule_status(root_dir=self.root, runner=fake, system="posix")
        self.assertTrue(status["installed"])
        self.assertEqual(status["backend"], "cron")
        self.assertEqual(len(status["lines"]), 6)
        self.assertTrue(all(not line.startswith("#") for line in status["lines"]))
        removed = remove_schedule(root_dir=self.root, runner=fake, system="posix")
        self.assertEqual(fake.crontab, "MAILTO=x@y.z\n")
        self.assertTrue(removed)
        self.assertFalse(schedule_status(root_dir=self.root, runner=fake, system="posix")["installed"])

    def test_status_sees_legacy_lines(self):
        script = posixpath.join(self.root, "run_cron_pipeline.sh")   # cron paths use "/" even when simulated on Windows
        fake = FakeRunner(crontab=f'0 9,14 * * * "{script}"\n')
        status = schedule_status(root_dir=self.root, runner=fake, system="posix")
        self.assertTrue(status["installed"])
        self.assertTrue(status["legacy"])

    def test_status_does_not_raise_when_cron_is_unavailable(self):
        fake = FakeRunner(responses={"crontab -l": FileNotFoundError("crontab")})
        status = schedule_status(root_dir=self.root, runner=fake, system="posix")
        self.assertFalse(status["installed"])
        self.assertTrue(status["error"])

    def test_remove_when_nothing_is_installed_does_not_write(self):
        fake = FakeRunner(crontab="MAILTO=x@y.z\n")
        result = remove_schedule(root_dir=self.root, runner=fake, system="posix")
        self.assertEqual(fake.commands(["crontab", "-"]), [])
        self.assertTrue(result)


class FakeTaskScheduler:
    """A schtasks/powershell stand-in with memory: created tasks stay until deleted, so a test can
    see what a second install (or its `schedule off`) does to the first install's tasks."""

    def __init__(self):
        self.tasks = {"\\Other\\Task"}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0] == "schtasks" and cmd[1] == "/Query":
            rows = "".join(f'"{name}","N/A","Ready"\r\n' for name in sorted(self.tasks))
            return subprocess.CompletedProcess(cmd, 0, rows, "")
        if cmd[0] == "schtasks":
            name = cmd[cmd.index("/TN") + 1]
            name = name if name.startswith("\\") else "\\" + name
            if cmd[1] == "/Create":
                self.tasks.add(name)
            elif cmd[1] == "/Delete":
                self.tasks.discard(name)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def names(self, folder: str) -> list[str]:
        return sorted(name for name in self.tasks if name.startswith("\\" + folder + "\\"))


class TestWindowsInstall(InstallTestCase):
    def setUp(self):
        super().setUp()
        self.folder = task_folder(self.root)
        self.query = f'"\\{self.folder}\\Pipeline-0800","N/A","Ready"\r\n"\\Other\\Task","N/A","Ready"\r\n'

    def test_install_replaces_old_tasks_then_creates_new_ones(self):
        fake = FakeRunner(responses={"schtasks /Query": (0, self.query, "")})
        result = install_schedule({}, root_dir=self.root, runner=fake, system="windows")
        deletes = fake.commands(["schtasks", "/Delete"])
        creates = fake.commands(["schtasks", "/Create"])
        self.assertEqual(deletes, [["schtasks", "/Delete", "/TN", f"\\{self.folder}\\Pipeline-0800", "/F"]])
        self.assertEqual(len(creates), 4)
        order = [c["cmd"][1] for c in fake.calls]
        self.assertLess(order.index("/Delete"), order.index("/Create"))
        self.assertIn("Reply check every 45 min", result)
        self.assertTrue(any("Task Scheduler" in line and self.folder in line for line in result))
        self.assertEqual(fake.commands(["crontab"]), [])

    def test_install_lets_every_task_run_on_battery(self):
        fake = FakeRunner()
        result = install_schedule({}, root_dir=self.root, runner=fake, system="windows")
        power = fake.commands(["powershell"])
        self.assertEqual(len(power), 4)
        for cmd in power:
            self.assertIn("-AllowStartIfOnBatteries", cmd[-1])
            self.assertIn(f"-TaskPath '\\{self.folder}\\'", cmd[-1])
        # The tasks exist before their power settings are changed.
        kinds = [" ".join(c["cmd"][:2]) for c in fake.calls]
        self.assertLess(kinds.index("schtasks /Create"), kinds.index("powershell -NoProfile"))
        self.assertFalse(any("battery" in line.lower() for line in result), result)

    def test_battery_settings_are_best_effort(self):
        """No PowerShell (or no permission) must not undo a schedule that was just installed."""
        for failure in ((1, "", "Set-ScheduledTask : Access is denied.\r\n"), FileNotFoundError("powershell")):
            with self.subTest(failure=failure):
                fake = FakeRunner(responses={"powershell -NoProfile": failure})
                result = install_schedule({}, root_dir=self.root, runner=fake, system="windows")
                self.assertEqual(len(fake.commands(["schtasks", "/Create"])), 4)
                self.assertTrue(any("battery" in line.lower() for line in result), result)

    def test_two_installs_keep_their_own_tasks(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        with open(os.path.join(other.name, "run_task.cmd"), "w", encoding="utf-8") as f:
            f.write("rem\n")
        other_folder = task_folder(other.name)
        fake = FakeTaskScheduler()
        install_schedule({}, root_dir=self.root, runner=fake, system="windows")
        install_schedule({"schedule": {"run_times": ["10:00"]}}, root_dir=other.name, runner=fake, system="windows")
        self.assertEqual(len(fake.names(self.folder)), 4)
        self.assertEqual(fake.names(other_folder), [f"\\{other_folder}\\Pipeline-1000", f"\\{other_folder}\\ReplyCheck",
                                                    f"\\{other_folder}\\UpdateCheck"])
        self.assertTrue(schedule_status(root_dir=self.root, runner=fake, system="windows")["installed"])
        remove_schedule(root_dir=other.name, runner=fake, system="windows")
        self.assertEqual(len(fake.names(self.folder)), 4)
        self.assertEqual(fake.names(other_folder), [])
        self.assertTrue(schedule_status(root_dir=self.root, runner=fake, system="windows")["installed"])
        remove_schedule(root_dir=self.root, runner=fake, system="windows")
        self.assertEqual(fake.tasks, {"\\Other\\Task"})

    def test_create_failure_raises_with_the_reason(self):
        fake = FakeRunner(responses={"schtasks /Create": (1, "", "ERROR: Access is denied.\r\n")})
        with self.assertRaises(ScheduleError) as ctx:
            install_schedule({}, root_dir=self.root, runner=fake, system="windows")
        self.assertIn("Access is denied", str(ctx.exception))

    def test_status_and_remove(self):
        fake = FakeRunner(responses={"schtasks /Query": (0, self.query, "")})
        status = schedule_status(root_dir=self.root, runner=fake, system="windows")
        self.assertEqual(status["backend"], "schtasks")
        self.assertTrue(status["installed"])
        self.assertEqual(status["tasks"], [f"\\{self.folder}\\Pipeline-0800"])
        remove_schedule(root_dir=self.root, runner=fake, system="windows")
        self.assertEqual(fake.commands(["schtasks", "/Delete"]),
                         [["schtasks", "/Delete", "/TN", f"\\{self.folder}\\Pipeline-0800", "/F"]])

    def test_status_when_nothing_is_installed(self):
        fake = FakeRunner(responses={"schtasks /Query": (0, '"\\Other\\Task","N/A","Ready"\r\n', "")})
        status = schedule_status(root_dir=self.root, runner=fake, system="windows")
        self.assertFalse(status["installed"])
        self.assertEqual(status["tasks"], [])


class TestMacPrivacyNote(unittest.TestCase):
    """macOS silently stops cron from reading Desktop/Documents/Downloads without Full Disk Access."""

    def test_protected_folders_get_a_note(self):
        for root in ("/Users/ann/Documents/sdr", "/Users/ann/Desktop", "/Users/ann/Downloads/x/y",
                     "/Users/ann/Library/Mobile Documents/com~apple~CloudDocs/sdr"):
            with self.subTest(root=root):
                self.assertIn("Full Disk Access", scheduler.macos_privacy_note(root, home="/Users/ann"))

    def test_other_folders_do_not(self):
        for root in ("/Users/ann/automated-sdr", "/Users/ann/Documentsx", "/opt/sdr"):
            with self.subTest(root=root):
                self.assertIsNone(scheduler.macos_privacy_note(root, home="/Users/ann"))


# ── Human descriptions ───────────────────────────────────────────────────────


class TestDescribe(unittest.TestCase):
    def test_default(self):
        lines = describe(default_cfg())
        self.assertEqual(lines[0], "Full run at 09:00 and 14:00")
        self.assertEqual(lines[1], "Reply check every 45 min")
        self.assertIn("Monday", lines[2])
        self.assertIn("tells you", lines[2])

    def test_variants(self):
        lines = describe(default_cfg(run_times=["08:00", "12:00", "17:00"], reply_check_minutes=120,
                                     update_mode="auto"))
        self.assertEqual(lines[0], "Full run at 08:00, 12:00 and 17:00")
        self.assertEqual(lines[1], "Reply check every 2 hours")
        self.assertIn("automatically", lines[2])
        lines = describe(default_cfg(run_times=["09:00"], reply_check_minutes=60, update_mode="off"))
        self.assertEqual(lines, ["Full run at 09:00", "Reply check every hour", "Update check off"])
        self.assertTrue(describe(default_cfg(run_times=[], reply_check_minutes=0))[1].startswith("Reply check off"))


class TestScheduleHelper(unittest.TestCase):
    def test_prints_the_plan_and_how_to_turn_it_on(self):
        from core import scheduler_helper
        out = io.StringIO()
        with mock.patch.object(scheduler_helper, "load_profile", return_value={}), redirect_stdout(out):
            scheduler_helper.print_schedule_instructions()
        text = out.getvalue()
        self.assertIn("Full run at 09:00 and 14:00", text)
        self.assertIn("sdr schedule on", text)

    def test_works_without_a_profile(self):
        from core import scheduler_helper
        from core.config import ProfileError
        out = io.StringIO()
        with mock.patch.object(scheduler_helper, "load_profile", side_effect=ProfileError("none")), \
                redirect_stdout(out):
            scheduler_helper.print_schedule_instructions()
        self.assertIn("Reply check every 45 min", out.getvalue())

    def test_bad_schedule_is_shown_not_raised(self):
        from core import scheduler_helper
        out = io.StringIO()
        with mock.patch.object(scheduler_helper, "load_profile",
                               return_value={"schedule": {"run_times": ["nope"]}}), redirect_stdout(out):
            scheduler_helper.print_schedule_instructions()
        self.assertIn("nope", out.getvalue())


# ── Run scripts ──────────────────────────────────────────────────────────────

FAKE_ENTRY = (
    "import os, sys\n"
    "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'called.txt'), 'a') as f:\n"
    "    f.write(' '.join([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\\n')\n"
    "print('fake ran')\n"
)


class RunScriptTestCase(unittest.TestCase):
    """Copies a run script next to fake runner.py / cli.py so the real bots never start."""

    script = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shutil.copy(os.path.join(REPO_ROOT, self.script), self.tmp.name)
        for name in ("runner.py", "cli.py"):
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
                f.write(FAKE_ENTRY)

    def tearDown(self):
        self.tmp.cleanup()

    def called(self) -> list[str]:
        with open(os.path.join(self.tmp.name, "called.txt"), encoding="utf-8") as f:
            return f.read().splitlines()

    def log(self) -> str:
        with open(os.path.join(self.tmp.name, "data", "cron.log"), encoding="utf-8") as f:
            return f.read()


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "bash script")
class TestRunCronPipelineScript(RunScriptTestCase):
    script = "run_cron_pipeline.sh"

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHON": sys.executable}
        return subprocess.run(["bash", os.path.join(self.tmp.name, self.script), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(REPO_ROOT, self.script)]).returncode, 0)

    def test_tasks_route_to_runner_or_cli(self):
        for args in ((), ("inbox",), ("update",)):
            self.assertEqual(self.run_script(*args).returncode, 0)
        self.assertEqual(self.called(), ["runner.py --task pipeline", "runner.py --task inbox",
                                         "cli.py update --scheduled"])
        self.assertIn("fake ran", self.log())


class TestRunTaskCmd(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "run_task.cmd"), "rb") as f:
            self.raw = f.read()
        self.text = self.raw.decode("ascii")

    def test_uses_windows_line_endings(self):
        """cmd.exe mis-parses labels/goto in LF-only batch files."""
        self.assertNotIn(b"\n", self.raw.replace(b"\r\n", b""))

    def test_finds_python_and_routes_tasks(self):
        lowered = self.text.lower()
        for needle in (".venv\\scripts\\python.exe", "py", "python", "data\\cron.log",
                       "runner.py --task", "cli.py update --scheduled", "pythonioencoding"):
            self.assertIn(needle, lowered)

    def test_python_call_and_exit_share_one_line(self):
        """A scheduled `update` replaces this very file while cmd.exe is still running it, and
        cmd.exe reads a batch file by byte offset after every command: nothing may be read after
        the Python call returns, so the exit sits on the same line (and keeps Python's exit code)."""
        lines = [line.strip() for line in self.text.split("\n")]
        code = [line for line in lines if not line.lower().startswith("rem")]
        python_lines = [line for line in code if "runner.py --task" in line or "cli.py update --scheduled" in line]
        self.assertEqual(len(python_lines), 2, python_lines)
        for line in python_lines:
            self.assertTrue(line.lower().endswith("& exit /b"), line)
        self.assertNotIn("exit /b %errorlevel%", [line.lower() for line in code])


@unittest.skipUnless(os.name == "nt", "Windows batch script")
class TestRunTaskCmdOnWindows(RunScriptTestCase):
    script = "run_task.cmd"

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHON": sys.executable}
        return subprocess.run(["cmd", "/c", os.path.join(self.tmp.name, self.script), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_tasks_route_to_runner_or_cli(self):
        for args in ((), ("inbox",), ("update",)):
            self.assertEqual(self.run_script(*args).returncode, 0)
        self.assertEqual(self.called(), ["runner.py --task pipeline", "runner.py --task inbox",
                                         "cli.py update --scheduled"])
        self.assertIn("fake ran", self.log())

    def test_unknown_task_is_rejected(self):
        self.assertNotEqual(self.run_script("leadgen; del *").returncode, 0)


if __name__ == "__main__":
    unittest.main()
