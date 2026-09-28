"""Offline tests for the `sdr` command: the home screen, doctor, test-email, schedule, update,
open, version and the older commands' argument parsing.

The database, profile and update status are temporary or mocked; the crontab, the mailbox,
git and the browser are never touched.
"""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock

import cli
import core.config as config
import core.db as db
from core.cli_doctor import DoctorDeps, collect, run_doctor
from core.cli_home import menu_choices, run_home, status_lines
from core.config import EXAMPLE_PROFILE_PATH, load_profile
from core.product import DISPLAY_NAME, version
from core.tui import UI

GOOD_HASH = "pbkdf2_sha256$1000$c2FsdHNhbHQ=$aGFzaGhhc2g="
GOOD_ENV = {"SMTP_USER": "jamie@acme.example", "SMTP_PASS": "secret-pass", "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "465", "IMAP_HOST": "imap.gmail.com", "IMAP_PORT": "993",
            "DASHBOARD_PASSWORD_HASH": GOOD_HASH}


def plain_ui(interactive: bool = False, inputs: list | None = None) -> tuple[UI, io.StringIO]:
    out = io.StringIO()
    replies = iter(inputs or [])
    return UI(plain=True, interactive=interactive, out=out, environ={},
              input_fn=lambda _prompt: next(replies)), out


class TempInstall(unittest.TestCase):
    """A temp profile (copy of the example) and database, so commands run against fake data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_path = os.path.join(self.tmp.name, "profile.toml")
        shutil.copy(EXAMPLE_PROFILE_PATH, self.profile_path)
        for patcher in (mock.patch.object(config, "PROFILE_PATH", self.profile_path),
                        mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
                        mock.patch.object(db, "DB_DIR", self.tmp.name)):
            patcher.start()
            self.addCleanup(patcher.stop)


class TestVersionAndParser(unittest.TestCase):
    def test_version_command(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["version"]), 0)
        self.assertEqual(out.getvalue().strip(), f"{DISPLAY_NAME} v{version()}")

    def test_every_command_parses(self):
        parser = cli.build_parser()
        for argv in (["setup", "--answers", "a.toml"], ["setup", "--section", "email"], ["setup", "--print-questions"],
                     ["test-email", "--to", "me@x.example", "--step", "2", "--signature", "logo"],
                     ["test-email", "--reply", "interested"], ["dashboard", "--set-password", "--no-open"],
                     ["run"], ["report"], ["status"], ["schedule"], ["schedule", "on"], ["schedule", "off"],
                     ["schedule", "status"], ["update", "--check"], ["update", "--scheduled"], ["update", "--force"],
                     ["doctor", "--online"], ["open", "profile"], ["preview"], ["requalify", "--dry-run"],
                     ["digest", "--force"], ["leadgen", "--count", "3"], ["email", "--limit", "2"], ["inbox"],
                     ["social", "--topic", "x"], ["pipeline"], ["run-all"], ["export"], []):
            parser.parse_args(argv)

    def test_print_questions_through_the_cli(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["setup", "--print-questions"]), 0)
        self.assertIn('"key": "business.website"', out.getvalue())

    def test_send_test_email_script_is_a_thin_wrapper(self):
        import send_test_email
        self.assertIs(send_test_email.main, cli.main)


class TestHome(TempInstall):
    def test_plain_home_prints_status_and_commands(self):
        ui, out = plain_ui()
        with mock.patch("core.scheduler.schedule_status", return_value={"installed": False}), \
                mock.patch("core.updater.read_status", return_value={"available": True, "latest": "1.1.0"}):
            self.assertEqual(cli.main([], ui=ui), 0)
        text = out.getvalue()
        self.assertIn(DISPLAY_NAME, text)
        self.assertIn("Acme Scheduling: 0 leads in your pipeline, 0 emails sent today, 0 hot leads", text)
        self.assertIn("Schedule: off", text)
        self.assertIn("Update available: v1.1.0", text)
        for command in ("sdr setup", "sdr test-email", "sdr dashboard", "sdr doctor", "AGENTS.md"):
            self.assertIn(command, text)

    def test_home_without_a_profile_explains_setup(self):
        ui, out = plain_ui()
        with mock.patch.object(config, "PROFILE_PATH", os.path.join(self.tmp.name, "missing.toml")):
            self.assertEqual(cli.main([], ui=ui), 0)
        self.assertIn("Not set up yet. Run `sdr setup`", out.getvalue())

    def test_menu_runs_the_chosen_action_until_quit(self):
        calls = []
        actions = {name: (lambda name=name: calls.append(name)) for name in
                   ("dashboard", "run", "sample", "preview", "settings", "schedule", "update", "doctor")}
        ui, out = plain_ui(interactive=True, inputs=["4", "", "9"])  # preview, Enter (dashboard), quit
        snap = {"product": "Acme", "leads": 3, "sent_today": 1, "hot": 1, "schedule_on": True}
        self.assertEqual(run_home(ui, actions, {}, snapshot_fn=lambda _p: snap), 0)
        self.assertEqual(calls, ["preview", "dashboard"])
        self.assertIn("Turn the schedule off", out.getvalue())
        self.assertEqual(status_lines(snap)[0], "Acme: 3 leads in your pipeline, 1 emails sent today, 1 hot lead")
        self.assertEqual(menu_choices({})[5][1], "Turn the schedule on")

    def test_run_now_needs_a_yes(self):
        ui, _out = plain_ui()
        with mock.patch("runner.run_task") as run_task:
            self.assertEqual(cli.cmd_run_now(ui), 0)
        run_task.assert_not_called()


class FakeEngine:
    from_email = "jamie@acme.example"

    def __init__(self, ok: bool = True):
        self.ok, self.sent = ok, []

    def send_real_email(self, to, subject, html, text, headers=None):
        self.sent.append((to, subject, text))
        return self.ok


class TestTestEmail(TempInstall):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("core.setup_sample.load_env_file")  # never read the real .env
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_to_your_own_mailbox_by_default(self):
        ui, out = plain_ui()
        engine = FakeEngine()
        with mock.patch.dict(os.environ, {"SMTP_USER": "jamie@acme.example"}):
            self.assertEqual(cli.cmd_test_email(ui, engine=engine), 0)
        self.assertEqual(len(engine.sent), 1)
        to, subject, text = engine.sent[0]
        self.assertEqual(to, "jamie@acme.example")
        self.assertEqual(subject, "question about Rivera Studio")
        self.assertIn("Hi Alex,", text)
        self.assertIn("Sent!", out.getvalue())

    def test_reply_and_step_options(self):
        ui, _out = plain_ui()
        engine = FakeEngine()
        self.assertEqual(cli.cmd_test_email(ui, to="me@x.example", step=2, engine=engine), 0)
        self.assertEqual(engine.sent[0][1], "Re: question about Rivera Studio")
        self.assertEqual(cli.cmd_test_email(ui, to="me@x.example", reply="interested", engine=engine), 0)
        self.assertIn("Great!", engine.sent[1][2])

    def test_bad_input_sends_nothing(self):
        ui, out = plain_ui()
        engine = FakeEngine()
        self.assertEqual(cli.cmd_test_email(ui, to="me@x.example", step=9, engine=engine), 1)
        self.assertIn("--step must be between 1 and 4", out.getvalue())
        with mock.patch.dict(os.environ, {"SMTP_USER": ""}):
            self.assertEqual(cli.cmd_test_email(ui, engine=engine), 1)
        self.assertEqual(engine.sent, [])

    def test_failed_send_is_reported(self):
        ui, out = plain_ui()
        self.assertEqual(cli.cmd_test_email(ui, to="me@x.example", engine=FakeEngine(ok=False)), 1)
        self.assertIn("Not sent", out.getvalue())


def doctor_deps(**overrides) -> DoctorDeps:
    values = {"check_login": lambda **k: {"smtp": True, "imap": True, "errors": []},
              "schedule_status": lambda: {"installed": True, "legacy": False, "error": None},
              "describe_search": lambda profile, env: {"setting": "auto", "engine": "duckduckgo", "label": "DuckDuckGo",
                                                       "has_brave_key": False, "note": ""},
              "check_brave_key": lambda key: (True, "Brave Search key works."),
              "read_update_status": lambda: {"checked_at": "2026-09-20T08:30:00+00:00", "available": False},
              "find_spec": lambda name: True, "python_version": (3, 12, 1)}
    values.update(overrides)
    return DoctorDeps(**values)


class TestDoctor(unittest.TestCase):
    def test_everything_fine(self):
        ui, out = plain_ui()
        code = run_doctor(ui, profile_path=EXAMPLE_PROFILE_PATH, env=GOOD_ENV, deps=doctor_deps())
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("OK: Python: 3.12.1", text)
        self.assertIn("sending and inbox work", text)
        self.assertIn("OK: Dashboard: password protected", text)
        self.assertIn("Lead search: DuckDuckGo", text)
        self.assertIn("last checked 2026-09-20", text)
        self.assertNotIn("secret-pass", text)

    def test_critical_problems_fail(self):
        login = {"smtp": False, "imap": False, "errors": ["The password was refused."]}
        deps = doctor_deps(python_version=(3, 10, 4), check_login=lambda **k: login,
                           find_spec=lambda name: name != "bs4")
        ui, out = plain_ui()
        self.assertEqual(run_doctor(ui, profile_path=EXAMPLE_PROFILE_PATH, env=GOOD_ENV, deps=deps), 1)
        text = out.getvalue()
        self.assertIn("3.10.4 is too old", text)
        self.assertIn("missing beautifulsoup4", text)
        self.assertIn("The password was refused.", text)
        self.assertIn("3 problems to fix", text)

    def test_login_without_a_mail_server_fails_instead_of_guessing_one(self):
        env = {k: v for k, v in GOOD_ENV.items() if k not in ("SMTP_HOST", "IMAP_HOST")}
        calls = []
        deps = doctor_deps(check_login=lambda **k: calls.append(k) or {"smtp": True, "imap": True, "errors": []})
        ui, out = plain_ui()
        self.assertEqual(run_doctor(ui, profile_path=EXAMPLE_PROFILE_PATH, env=env, deps=deps), 1)
        self.assertEqual(calls, [], "the password must never go to a guessed server")
        self.assertIn("SMTP_HOST is missing", out.getvalue())

    def test_advice_is_not_failure(self):
        env = {"SMTP_USER": "", "SMTP_PASS": ""}
        deps = doctor_deps(schedule_status=lambda: {"installed": False, "legacy": True, "error": None},
                           read_update_status=lambda: {"available": True, "latest": "9.9.9"},
                           find_spec=lambda name: name not in ("rich", "questionary"))
        ui, out = plain_ui()
        self.assertEqual(run_doctor(ui, profile_path=EXAMPLE_PROFILE_PATH, env=env, deps=deps), 0)
        text = out.getvalue()
        self.assertIn("not set yet, so nothing is sent", text)
        self.assertIn("no password", text)
        self.assertIn("v9.9.9 is available", text)
        self.assertIn("old hand-added cron lines", text)
        self.assertIn("arrow keys", text)

    def test_missing_profile_is_critical(self):
        ui, out = plain_ui()
        code = run_doctor(ui, profile_path="/nonexistent/profile.toml", env=GOOD_ENV, deps=doctor_deps())
        self.assertEqual(code, 1)
        self.assertIn("Run `sdr setup`", out.getvalue())

    def test_offline_and_online_network_checks(self):
        login = mock.Mock(return_value={"smtp": True, "imap": True})
        brave = mock.Mock(return_value=(False, "Rejected."))
        brave_search = mock.Mock(return_value={"setting": "brave", "engine": "brave", "label": "Brave Search",
                                               "note": ""})
        deps = doctor_deps(check_login=login, check_brave_key=brave, describe_search=brave_search)
        checks = collect(EXAMPLE_PROFILE_PATH, GOOD_ENV, offline=True, deps=deps)
        login.assert_not_called()
        brave.assert_not_called()
        self.assertIn("not checked: --offline", " ".join(c.detail for c in checks))
        checks = collect(EXAMPLE_PROFILE_PATH, {**GOOD_ENV, "BRAVE_API_KEY": "bk"}, online=True, deps=deps)
        brave.assert_called_once_with("bk")
        self.assertIn("Rejected.", [c.detail for c in checks if c.label == "Brave Search key"])


class TestScheduleUpdateOpen(TempInstall):
    def test_schedule_on_off_status(self):
        ui, out = plain_ui()
        with mock.patch("core.scheduler.install_schedule", return_value=["Full run at 09:00 and 14:00"]) as install:
            self.assertEqual(cli.main(["schedule", "on"], ui=ui), 0)
        self.assertEqual(install.call_args[0][0]["sender"]["product_name"], "Acme Scheduling")
        with mock.patch("core.scheduler.remove_schedule", return_value=["Removed the schedule."]):
            self.assertEqual(cli.main(["schedule", "off"], ui=ui), 0)
        with mock.patch("core.scheduler.schedule_status", return_value={"installed": False, "legacy": False,
                                                                         "error": None}):
            self.assertEqual(cli.main(["schedule", "status"], ui=ui), 0)
        text = out.getvalue()
        for needle in ("Full run at 09:00 and 14:00", "Removed the schedule.", "The schedule is off",
                       "Reply check every 45 min"):
            self.assertIn(needle, text)

    def test_schedule_error_is_friendly(self):
        from core.scheduler import ScheduleError
        ui, out = plain_ui()
        with mock.patch("core.scheduler.install_schedule", side_effect=ScheduleError("crontab isn't available.")):
            self.assertEqual(cli.main(["schedule", "on"], ui=ui), 1)
        self.assertIn("crontab isn't available.", out.getvalue())

    def test_update_check_only_reports(self):
        status = {"available": True, "latest": "1.2.0", "current": "1.0.0", "latest_tag": "v1.2.0",
                  "notes": "## [1.2.0]\n- Faster", "error": ""}
        ui, out = plain_ui()
        with mock.patch("core.updater.check_for_update", return_value=status), \
                mock.patch("core.updater.apply_update") as apply:
            self.assertEqual(cli.main(["update", "--check"], ui=ui), 0)
        apply.assert_not_called()
        self.assertIn("Version 1.2.0 is available", out.getvalue())
        self.assertIn("- Faster", out.getvalue())

    def test_update_installs_and_reports_rollback(self):
        status = {"available": True, "latest": "1.2.0", "current": "1.0.0", "latest_tag": "v1.2.0", "error": ""}
        rolled_back = {"result": "rolled_back", "message": "The tests failed, so nothing changed.", "output": "E1"}
        ui, out = plain_ui()
        with mock.patch("core.updater.check_for_update", return_value=status), \
                mock.patch("core.updater.apply_update", return_value=rolled_back) as apply:
            self.assertEqual(cli.main(["update"], ui=ui), 1)
        apply.assert_called_once_with(force=False, tag="v1.2.0")
        self.assertIn("nothing changed", out.getvalue())

    def test_scheduled_update_prints_for_the_log(self):
        with mock.patch("core.updater.scheduled_run", return_value={"result": "none", "message": "Up to date."}), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["update", "--scheduled"]), 0)
        self.assertEqual(out.getvalue().strip(), "Up to date.")

    def test_open_without_a_terminal_only_prints(self):
        ui, out = plain_ui()
        with mock.patch("cli._open_path") as opener:
            self.assertEqual(cli.main(["open"], ui=ui), 0)
        opener.assert_not_called()
        self.assertIn("AGENTS.md", out.getvalue())

    def test_profile_errors_are_one_friendly_line(self):
        ui, out = plain_ui()
        with mock.patch.object(config, "PROFILE_PATH", os.path.join(self.tmp.name, "missing.toml")):
            self.assertEqual(cli.main(["preview"], ui=ui), 1)
        self.assertIn("No profile found", out.getvalue())
        self.assertEqual(load_profile()["sender"]["product_name"], "Acme Scheduling")


class TestLegacyCommandsTakeTheRunLock(TempInstall):
    """`sdr leadgen/email/inbox/social` change the same leads the scheduled pipeline and the dashboard
    do, so they must hold the runner's lock (data/.runner.lock) while they run: `sdr email` during the
    09:00 pipeline would otherwise pick the same "new" leads and send a first email twice."""

    def setUp(self):
        super().setUp()
        import runner
        self.runner = runner
        patcher = mock.patch.object(runner, "DB_DIR", self.tmp.name)  # the lock file stays in the temp dir
        patcher.start()
        self.addCleanup(patcher.stop)

    def lock_is_held(self) -> bool:
        try:
            with self.runner.run_lock(0):
                return False
        except self.runner.AlreadyRunning:
            return True

    def test_each_bot_command_runs_under_the_lock_with_its_options(self):
        seen = {}

        def record(name):
            def bot(*args, **kwargs):
                seen[name] = (args, kwargs, self.lock_is_held())
                return {name: "done"}
            return bot

        ui, _out = plain_ui()
        with mock.patch("bots.leadgen_pipeline.LeadGenPipeline") as leadgen, \
                mock.patch("bots.email_marketing.EmailMarketingEngine") as email, \
                mock.patch("bots.inbox_listener.InboxListenerEngine") as inbox, \
                mock.patch("bots.social_bot.SocialBot") as social, \
                redirect_stdout(io.StringIO()) as printed:
            leadgen.return_value.run_pipeline.side_effect = record("leadgen")
            email.return_value.run_outreach_campaign.side_effect = record("email")
            inbox.return_value.check_inbox_and_auto_reply.side_effect = record("inbox")
            social.return_value.generate_and_schedule_thread.side_effect = record("social")
            social.return_value.publish_pending_posts.side_effect = record("publish")
            for argv in (["leadgen", "--count", "3"], ["email", "--limit", "2"], ["inbox"], ["social", "--topic", "x"]):
                self.assertEqual(cli.main(argv, ui=ui), 0, argv)
        self.assertEqual(seen["leadgen"][:2], ((3,), {}))
        self.assertEqual(seen["email"][:2], ((), {"limit": 2}))
        self.assertEqual(seen["inbox"][:2], ((), {}))
        self.assertEqual(seen["social"][:2], (("x",), {}))
        for name, (_args, _kwargs, held) in seen.items():
            self.assertTrue(held, f"{name} ran without the run lock")
        self.assertFalse(self.lock_is_held(), "the lock is released afterwards")
        self.assertIn('"leadgen": "done"', printed.getvalue())
        self.assertIn('"scheduled"', printed.getvalue())

    def test_a_run_in_progress_means_skipped_not_a_second_run(self):
        ui, out = plain_ui()
        with mock.patch("bots.inbox_listener.InboxListenerEngine") as inbox, self.runner.run_lock(), \
                redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main(["inbox"], ui=ui), 0)
        inbox.return_value.check_inbox_and_auto_reply.assert_not_called()
        self.assertIn('"status": "skipped"', printed.getvalue())
        self.assertIn("Another run is in progress", out.getvalue())
        self.assertEqual([(log["bot_name"], log["action"]) for log in db.get_recent_logs(1)], [("Runner", "Skipped")])

    def test_waits_for_a_busy_lock_like_the_runner_and_says_so(self):
        waits = []

        @contextmanager
        def fake_lock(wait_seconds=0):
            waits.append(wait_seconds)
            if len(waits) == 1:  # busy on the quick first try, free once we wait
                raise self.runner.AlreadyRunning("lock", "Another run is in progress — skipping this one.")
            yield

        ui, out = plain_ui()
        with mock.patch.object(self.runner, "run_lock", fake_lock), \
                mock.patch("bots.email_marketing.EmailMarketingEngine") as email, redirect_stdout(io.StringIO()):
            email.return_value.run_outreach_campaign.return_value = {"sent": 0}
            self.assertEqual(cli.main(["email"], ui=ui), 0)
        self.assertEqual(waits, [0, self.runner.DEFAULT_LOCK_WAIT_SECONDS])
        email.return_value.run_outreach_campaign.assert_called_once()
        self.assertIn("Waiting up to 10 minutes", out.getvalue())
        waits.clear()
        with mock.patch.object(self.runner, "run_lock", fake_lock), \
                mock.patch("bots.inbox_listener.InboxListenerEngine") as inbox, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["inbox"], ui=ui), 0)
        self.assertEqual(waits, [0], "the quick reply check never waits, exactly like the 45-minute cron job")
        inbox.return_value.check_inbox_and_auto_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
