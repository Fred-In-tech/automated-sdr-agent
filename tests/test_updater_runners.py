"""Offline tests for the real git / pip / pytest wrappers in core/updater.py: which interpreter
they run, what environment they set, and that they never raise. subprocess.run is mocked
throughout, so nothing is installed, fetched or executed.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import updater


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["fake"], returncode, stdout, stderr)


class TestDefaultRunners(unittest.TestCase):
    """The real git/pip/pytest wrappers: commands and environment, with subprocess mocked."""

    def test_run_git_never_raises_when_git_is_missing(self):
        with mock.patch.object(updater.subprocess, "run", side_effect=FileNotFoundError("git")):
            result = updater.run_git(["status"], ".")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git", result.stderr)

    def test_run_git_turns_a_timeout_into_a_failed_result(self):
        with mock.patch.object(updater.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["git", "fetch"], 1)):
            result = updater.run_git(["fetch"], ".")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)

    def test_run_git_disables_credential_prompts(self):
        with mock.patch.object(updater.subprocess, "run", return_value=completed()) as run:
            updater.run_git(["fetch", "--tags"], "/repo")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["git", "fetch", "--tags"])
        self.assertEqual(kwargs["cwd"], "/repo")
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("shell", kwargs)

    def test_run_pip_reinstalls_requirements_with_the_running_interpreter_when_it_can(self):
        with mock.patch.object(updater, "pip_unavailable_reason", return_value=None), \
                mock.patch.object(updater.subprocess, "run", return_value=completed()) as run:
            updater.run_pip("/repo")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(cmd[-2:], ["-r", "requirements.txt"])
        self.assertEqual(run.call_args[1]["cwd"], "/repo")

    def test_run_pip_skips_with_a_note_when_pip_would_refuse(self):
        """On Debian/Ubuntu/Homebrew Python (PEP 668) `pip install` fails outside a venv, which used
        to roll back every update; skipping it (and saying so) lets the self-test decide."""
        reason = "python3 is externally managed"
        with mock.patch.object(updater, "pip_unavailable_reason", return_value=reason), \
                mock.patch.object(updater.subprocess, "run") as run:
            result = updater.run_pip("/repo")
        run.assert_not_called()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(updater.pip_was_skipped(result))
        self.assertIn(reason, result.stdout)
        self.assertIn("requirements.txt", result.stdout)
        self.assertFalse(updater.pip_was_skipped(completed()))

    def test_run_tests_runs_pytest_offline_with_a_timeout(self):
        env = {"RUN_LIVE_TESTS": "1", "KEEP": "yes"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(updater.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(updater.subprocess, "run", return_value=completed()) as run:
            updater.run_tests("/repo")
        cmd = run.call_args[0][0]
        kwargs = run.call_args[1]
        self.assertEqual(cmd[:3], [sys.executable, "-m", "pytest"])
        self.assertIn("-x", cmd)
        self.assertEqual(kwargs["timeout"], updater.TEST_TIMEOUT_SECONDS)
        self.assertEqual(updater.TEST_TIMEOUT_SECONDS, 300)
        # Live tests would scrape the web and send email: never during an update.
        self.assertNotIn("RUN_LIVE_TESTS", kwargs["env"])
        self.assertEqual(kwargs["env"]["KEEP"], "yes")

    def test_run_tests_falls_back_to_an_import_check_without_pytest(self):
        with mock.patch.object(updater.importlib.util, "find_spec", return_value=None), \
                mock.patch.object(updater.subprocess, "run", return_value=completed()) as run:
            updater.run_tests("/repo")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], [sys.executable, "-c"])
        self.assertIn("import", cmd[2])


class TestProjectInterpreter(unittest.TestCase):
    """`python3 cli.py update` may run with any Python, but the requirements live in <root>/.venv:
    pip and the self-test must use that interpreter whenever it exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make_venv_python(self) -> str:
        rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
        path = os.path.join(self.root, ".venv", *rel)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        return path

    def test_project_python_prefers_the_venv_and_falls_back_to_the_running_one(self):
        self.assertEqual(updater.project_python(self.root), sys.executable)
        python = self.make_venv_python()
        self.assertEqual(updater.project_python(self.root), python)

    def test_run_pip_installs_into_the_projects_venv(self):
        python = self.make_venv_python()
        with mock.patch.object(updater, "pip_unavailable_reason", return_value="would not matter"), \
                mock.patch.object(updater.subprocess, "run", return_value=completed()) as run:
            result = updater.run_pip(self.root)
        self.assertFalse(updater.pip_was_skipped(result))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], [python, "-m", "pip", "install"])
        self.assertEqual(run.call_args[1]["cwd"], self.root)

    def test_run_tests_uses_the_projects_venv_interpreter(self):
        python = self.make_venv_python()
        with mock.patch.object(updater.subprocess, "run", return_value=completed()) as run:
            updater.run_tests(self.root)
        cmds = [call[0][0] for call in run.call_args_list]
        self.assertEqual(cmds[0][:2], [python, "-c"])  # does that interpreter have pytest?
        self.assertIn("pytest", cmds[0][2])
        self.assertEqual(cmds[1][:3], [python, "-m", "pytest"])

    def test_run_tests_falls_back_to_an_import_check_when_the_venv_has_no_pytest(self):
        python = self.make_venv_python()
        with mock.patch.object(updater.subprocess, "run", side_effect=[completed(1), completed(0)]) as run:
            updater.run_tests(self.root)
        cmd = run.call_args_list[1][0][0]
        self.assertEqual(cmd[:2], [python, "-c"])
        self.assertIn("import cli", cmd[2])


class TestPipUnavailableReason(unittest.TestCase):
    def test_inside_a_virtual_environment_pip_always_works(self):
        with mock.patch.object(updater, "_in_virtualenv", return_value=True), \
                mock.patch.object(updater.importlib.util, "find_spec", return_value=None):
            self.assertIsNone(updater.pip_unavailable_reason())

    def test_missing_pip_is_detected(self):
        with mock.patch.object(updater, "_in_virtualenv", return_value=False), \
                mock.patch.object(updater.importlib.util, "find_spec", return_value=None):
            reason = updater.pip_unavailable_reason()
        self.assertIn("pip", reason)

    def test_externally_managed_python_is_detected(self):
        with tempfile.TemporaryDirectory() as stdlib:
            with open(os.path.join(stdlib, "EXTERNALLY-MANAGED"), "w", encoding="utf-8") as f:
                f.write("[externally-managed]\n")
            with mock.patch.object(updater, "_in_virtualenv", return_value=False), \
                    mock.patch.object(updater.importlib.util, "find_spec", return_value=object()), \
                    mock.patch.object(updater.sysconfig, "get_path", return_value=stdlib):
                reason = updater.pip_unavailable_reason()
            self.assertIn("externally managed", reason)
            os.remove(os.path.join(stdlib, "EXTERNALLY-MANAGED"))
            with mock.patch.object(updater, "_in_virtualenv", return_value=False), \
                    mock.patch.object(updater.importlib.util, "find_spec", return_value=object()), \
                    mock.patch.object(updater.sysconfig, "get_path", return_value=stdlib):
                self.assertIsNone(updater.pip_unavailable_reason())


class TestRedaction(unittest.TestCase):
    def test_credentials_in_urls_are_never_repeated(self):
        text = "fatal: unable to access 'https://fred:ghp_secret123@github.com/x.git/': 403"
        cleaned = updater.redact(text)
        self.assertNotIn("ghp_secret123", cleaned)
        self.assertNotIn("fred:", cleaned)
        self.assertIn("github.com", cleaned)


if __name__ == "__main__":
    unittest.main()
