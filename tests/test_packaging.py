"""Offline tests for packaging: installers, the `sdr` launchers, docs for humans and AI agents,
CI config and ignore rules.

The installers are exercised for real where it is safe: install.sh is sourced function by
function (SDR_INSTALLER_TEST=1) and run end to end against a throwaway local git repository with
an offline pip, inside a temporary HOME. Nothing touches the network, the real home folder, the
user PATH, the crontab or the Task Scheduler.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.product import REPO_SLUG, REPO_URL

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"
BASH = shutil.which("bash")
GIT = shutil.which("git")
POSIX_BASH = not IS_WINDOWS and BASH is not None

REQUIRED_FILES = [
    "install.sh", "install.ps1", "bin/sdr", "bin/sdr.cmd", "LICENSE", "SECURITY.md",
    "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", ".github/workflows/tests.yml",
    ".github/dependabot.yml", ".env.example", ".gitignore", "requirements.txt", "cli.py", "VERSION",
]

# Every command the `sdr` CLI offers; AGENTS.md must teach all of them.
CLI_COMMANDS = [
    "setup", "preview", "test-email", "dashboard", "run", "report", "status", "schedule on",
    "schedule off", "schedule status", "update", "doctor", "open", "requalify", "export", "digest",
]

# The setup answers contract (section -> keys) that AI agents fill in for the user.
ANSWER_KEYS = {
    "business": ["website", "name", "offer", "signup_url", "pitch", "postal_address", "allow_no_address"],
    "audience": ["ideal_client", "job_titles", "cities", "leads_per_run"],
    "style": ["kind", "signature_logo_test", "brand_color", "logo_url"],
    "email": ["provider", "address", "password_env", "smtp_host", "smtp_port", "imap_host",
              "imap_port", "from_name", "sign_off", "alias", "alert_email"],
    "replies": ["enabled", "branded_welcome"],
    "schedule": ["run_times", "send_days", "daily_send_limit", "reply_check_minutes", "install"],
    "security": ["dashboard_password_env"],
    "updates": ["mode"],
}

DEFAULT_GIT_URL = REPO_URL + ".git"
RAW_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main"

# Real code path: git needs an identity to commit in throwaway repos (CI runners have none).
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

FAKE_CLI = textwrap.dedent("""\
    import json, os, sys
    print(json.dumps({"argv": sys.argv[1:], "prefix": sys.prefix, "cwd": os.getcwd(),
                      "marker": os.environ.get("SDR_TEST_PY", "")}))
    sys.exit(int(os.environ.get("SDR_TEST_EXIT", "0")))
""")


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def clean_env(home: Path, **extra: str) -> dict:
    """A predictable environment: temp HOME, no colours, no SDR_* settings leaking in from the
    developer's shell, and pip that can never reach the network."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SDR_", "PIP_"))}
    env.update({"HOME": str(home), "NO_COLOR": "1", "TERM": "dumb", "PIP_NO_INDEX": "1",
                "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    env.update(GIT_IDENTITY)
    env.update(extra)
    return env


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_script(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    make_executable(path)
    return path


def python_wrapper(path: Path, marker: str) -> Path:
    """A stand-in interpreter that tags its runs, so tests can see which Python a launcher chose."""
    return write_script(path, f'#!/bin/sh\nSDR_TEST_PY={marker} exec "{sys.executable}" "$@"\n')


def git(*args: str, cwd: Path) -> None:
    subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **GIT_IDENTITY})


def make_source_repo(path: Path) -> Path:
    """A tiny local 'Automated SDR' repository the installer can clone without the network."""
    path.mkdir(parents=True)
    (path / "bin").mkdir()
    shutil.copy2(ROOT / "bin" / "sdr", path / "bin" / "sdr")
    (path / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
    (path / "requirements.txt").write_text("# nothing to install in tests\n", encoding="utf-8")
    git("init", "-q", cwd=path)
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "initial", cwd=path)
    return path


def commit_file(repo: Path, name: str, tag: str | None = None) -> str:
    """Add one file as a new commit (optionally tagged) and return the commit's sha."""
    (repo / name).write_text(name + "\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    if tag:
        git("tag", tag, cwd=repo)
    return head_of(repo)


def head_of(repo: Path) -> str:
    result = subprocess.run([GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


class RequiredFilesTest(unittest.TestCase):
    def test_required_files_exist(self):
        missing = [f for f in REQUIRED_FILES if not (ROOT / f).is_file()]
        self.assertEqual(missing, [])

    @unittest.skipIf(IS_WINDOWS, "POSIX file modes")
    def test_posix_scripts_are_executable(self):
        for name in ("install.sh", "bin/sdr"):
            self.assertTrue(os.access(ROOT / name, os.X_OK), f"{name} must be chmod +x")

    def test_installer_files_are_plain_ascii(self):
        # Windows PowerShell 5.1 reads BOM-less scripts as ANSI and old consoles mangle other
        # characters, so the installers and launchers stay ASCII-only.
        for name in ("install.sh", "install.ps1", "bin/sdr", "bin/sdr.cmd"):
            data = (ROOT / name).read_bytes()
            self.assertTrue(data.isascii(), f"{name} contains non-ASCII characters")

    def test_license_is_mit_with_copyright(self):
        text = read("LICENSE")
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 Fred (github.com/Fred-In-tech)", text)
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', text)


class InstallShStaticTest(unittest.TestCase):
    def setUp(self):
        self.text = read("install.sh")

    @unittest.skipUnless(POSIX_BASH, "needs bash on macOS/Linux")
    def test_shell_scripts_parse(self):
        for name in ("install.sh", "bin/sdr"):
            result = subprocess.run([BASH, "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_strict_mode_and_documented_defaults(self):
        self.assertIn("set -euo pipefail", self.text)
        self.assertIn('${SDR_HOME:-$HOME/automated-sdr}', self.text)
        self.assertIn(f'SDR_DEFAULT_REPO="{DEFAULT_GIT_URL}"', self.text)
        self.assertIn('${SDR_REPO:-$SDR_DEFAULT_REPO}', self.text)
        self.assertIn("setup </dev/tty", self.text)
        self.assertIn("SDR_NO_SETUP", self.text)

    def test_sudo_is_only_ever_suggested(self):
        # Every "sudo" must sit inside a printed hint (say "..."), never in a command that runs.
        for number, line in enumerate(self.text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            outside_hints = re.sub(r'say "[^"]*"', "", stripped)
            self.assertNotIn("sudo", outside_hints, f"line {number} runs sudo: {stripped}")

    def test_main_runs_only_after_everything_is_downloaded(self):
        # A truncated `curl | bash` download must not be able to run half an installer.
        lines = [line for line in self.text.splitlines() if line.strip()]
        self.assertEqual(lines[-2:], ['    main "$@"', "fi"])
        self.assertEqual(self.text.count('main "$@"'), 1)


@unittest.skipUnless(POSIX_BASH, "needs bash on macOS/Linux")
class InstallShFunctionsTest(unittest.TestCase):
    """Source install.sh without running main() and call its functions one by one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_bash(self, snippet: str, **env: str) -> subprocess.CompletedProcess:
        script = f'source "{ROOT / "install.sh"}"\nsetup_colors\n{snippet}\n'
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60,
                              env=clean_env(self.home, SDR_INSTALLER_TEST="1", **env),
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def test_sourcing_does_not_run_the_installer(self):
        result = self.run_bash("echo sourced-ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sourced-ok")

    def test_find_python_accepts_a_modern_interpreter(self):
        result = self.run_bash("find_python", SDR_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), sys.executable)

    def test_find_python_rejects_an_old_explicit_interpreter(self):
        old = write_script(self.dir / "python3.9", "#!/bin/sh\nexit 1\n")
        result = self.run_bash("if find_python; then echo found; else echo none; fi", SDR_PYTHON=str(old))
        self.assertEqual(result.stdout.strip(), "none")

    def test_python_ok_is_false_for_missing_or_empty(self):
        result = self.run_bash('python_ok "" && echo yes || echo no; python_ok /nope/python && echo yes || echo no')
        self.assertEqual(result.stdout.split(), ["no", "no"])

    def test_absolute_dir_expands_tilde_and_relative_paths(self):
        result = self.run_bash('cd "$HOME"; absolute_dir "~/apps/sdr/"; absolute_dir "rel/dir"; absolute_dir /abs/x')
        self.assertEqual(result.stdout.splitlines(), [f"{self.home}/apps/sdr", f"{self.home}/rel/dir", "/abs/x"])

    @unittest.skipUnless(GIT, "needs git")
    def test_fetch_code_follows_main_only_before_the_first_release(self):
        source = make_source_repo(self.dir / "source")  # no release tag yet
        target = self.dir / "install"
        first = self.run_bash(f'fetch_code "{source}" "{target}"')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((target / "cli.py").is_file())
        self.assertIn("No release has been tagged yet", first.stdout)

        commit_file(source, "NEW_FILE")
        second = self.run_bash(f'fetch_code "{source}" "{target}"')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Updated the existing copy", second.stdout)
        self.assertIn("no release yet", second.stdout)
        self.assertTrue((target / "NEW_FILE").is_file())

    @unittest.skipUnless(GIT, "needs git")
    def test_fetch_code_installs_the_newest_release_tag_never_main_head(self):
        """SECURITY.md promises release tags only: a fresh install and an installer re-run must
        land on the newest vX.Y.Z tag (numerically newest, pre-releases ignored), not on `main`."""
        source = make_source_repo(self.dir / "source")
        commit_file(source, "OLDER", tag="v1.9.0")
        release = commit_file(source, "RELEASE", tag="v1.10.0")
        commit_file(source, "WIP", tag="v2.0.0-rc1")  # unreleased work after the tag
        target = self.dir / "install"
        first = self.run_bash(f'fetch_code "{source}" "{target}"')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(head_of(target), release)
        self.assertIn("v1.10.0", first.stdout)
        self.assertFalse((target / "WIP").exists())

        newer = commit_file(source, "NEWER", tag="v1.11.0")
        commit_file(source, "WIP2")
        second = self.run_bash(f'fetch_code "{source}" "{target}"')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(head_of(target), newer)
        self.assertIn("Updated the existing copy", second.stdout)
        self.assertIn("v1.11.0", second.stdout)
        self.assertFalse((target / "WIP2").exists())

    @unittest.skipUnless(GIT, "needs git")
    def test_fetch_code_moves_a_copy_left_on_main_back_to_the_release(self):
        source = make_source_repo(self.dir / "source")
        release = commit_file(source, "RELEASE", tag="v1.0.0")
        commit_file(source, "WIP")
        target = self.dir / "install"
        git("clone", "-q", str(source), str(target), cwd=self.dir)  # an old installer left it on main
        self.assertNotEqual(head_of(target), release)
        result = self.run_bash(f'fetch_code "{source}" "{target}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(head_of(target), release)

    @unittest.skipUnless(GIT, "needs git")
    def test_fetch_code_keeps_going_when_the_update_cannot_fast_forward(self):
        source = make_source_repo(self.dir / "source")
        target = self.dir / "install"
        self.run_bash(f'fetch_code "{source}" "{target}"')
        git("checkout", "-q", "--detach", cwd=target)  # a pull is impossible from here
        result = self.run_bash(f'fetch_code "{source}" "{target}"; echo still-running')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Keeping the version you have", result.stdout)
        self.assertIn("still-running", result.stdout)

    def test_fetch_code_refuses_a_folder_that_is_not_ours(self):
        target = self.dir / "busy"
        target.mkdir()
        (target / "notes.txt").write_text("mine\n", encoding="utf-8")
        result = self.run_bash(f'fetch_code "/nonexistent/repo" "{target}"')
        self.assertEqual(result.returncode, 1)
        self.assertIn("isn't an Automated SDR install", result.stderr)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["notes.txt"])

    def test_link_launcher_creates_a_symlink_and_path_hint(self):
        install = self.dir / "install"
        (install / "bin").mkdir(parents=True)
        (install / "bin" / "sdr").write_text("#!/bin/sh\n", encoding="utf-8")
        bindir = self.home / ".local" / "bin"
        result = self.run_bash(f'link_launcher "{install}" "{bindir}"; echo "cmd=$SDR_CMD"; path_hint "{bindir}"',
                               SHELL="/bin/zsh", PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        link = bindir / "sdr"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(install / "bin" / "sdr"))
        self.assertTrue(os.access(install / "bin" / "sdr", os.X_OK))
        self.assertIn(f"cmd={link}", result.stdout)
        self.assertIn("""echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc""", result.stdout)

    def test_path_hint_is_silent_when_already_on_path(self):
        bindir = self.home / ".local" / "bin"
        result = self.run_bash(f'path_hint "{bindir}"', PATH=f"/usr/bin:{bindir}:/bin")
        self.assertEqual(result.stdout, "")

    def make_install(self, name: str = "install") -> Path:
        install = self.dir / name
        (install / "bin").mkdir(parents=True)
        (install / "bin" / "sdr").write_text("#!/bin/sh\n", encoding="utf-8")
        return install

    def test_link_launcher_leaves_someone_elses_file_alone(self):
        install = self.make_install()
        bindir = self.dir / "bindir"
        bindir.mkdir()
        (bindir / "sdr").write_text("other tool\n", encoding="utf-8")
        result = self.run_bash(f'link_launcher "{install}" "{bindir}"; echo "cmd=$SDR_CMD"')
        self.assertEqual((bindir / "sdr").read_text(encoding="utf-8"), "other tool\n")
        self.assertIn(f"cmd={install / 'bin' / 'sdr'}", result.stdout)

    def test_link_launcher_leaves_a_symlink_to_another_tool_alone(self):
        """A pre-existing `sdr` link (another tool, or a second Automated SDR install for another
        business) is someone's working command: warn and use our direct path instead."""
        install = self.make_install()
        other = write_script(self.dir / "other" / "sdr", "#!/bin/sh\necho other\n")
        bindir = self.dir / "bindir"
        bindir.mkdir()
        (bindir / "sdr").symlink_to(other)
        result = self.run_bash(f'link_launcher "{install}" "{bindir}"; echo "cmd=$SDR_CMD"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(bindir / "sdr"), str(other))
        self.assertIn("left alone", result.stdout)
        self.assertNotIn("[ok]", result.stdout)
        self.assertIn(f"cmd={install / 'bin' / 'sdr'}", result.stdout)

    def test_link_launcher_replaces_a_dangling_symlink_and_relinks_its_own(self):
        install = self.make_install()
        bindir = self.dir / "bindir"
        bindir.mkdir()
        (bindir / "sdr").symlink_to(self.dir / "moved-away" / "bin" / "sdr")  # an install that was deleted
        result = self.run_bash(f'link_launcher "{install}" "{bindir}"; echo "cmd=$SDR_CMD"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(bindir / "sdr"), str(install / "bin" / "sdr"))
        self.assertIn(f"cmd={bindir / 'sdr'}", result.stdout)
        again = self.run_bash(f'link_launcher "{install}" "{bindir}"')  # re-running the installer
        self.assertIn("[ok]", again.stdout)
        self.assertEqual(os.readlink(bindir / "sdr"), str(install / "bin" / "sdr"))

    def test_start_setup_respects_no_setup_and_missing_terminal(self):
        skipped = self.run_bash('SDR_CMD=sdr; start_setup /bin/false; echo after', SDR_NO_SETUP="1")
        self.assertIn("Setup skipped (SDR_NO_SETUP=1)", skipped.stdout)
        self.assertIn("after", skipped.stdout)
        # start_new_session=True leaves the child without a controlling terminal: no /dev/tty.
        no_tty = self.run_bash('SDR_CMD=sdr; start_setup /bin/false; echo after')
        self.assertIn("No terminal to ask questions in", no_tty.stdout)
        self.assertIn("after", no_tty.stdout)

    def test_tilde_shortens_only_paths_inside_home(self):
        result = self.run_bash('tilde "$HOME"; tilde "$HOME/automated-sdr"; tilde "$HOME-other/x"; tilde /opt/sdr')
        self.assertEqual(result.stdout.splitlines(), ["~", "~/automated-sdr", f"{self.home}-other/x", "/opt/sdr"])

    @unittest.skipUnless(GIT, "needs git")
    def test_need_git_reports_the_version(self):
        result = self.run_bash("need_git")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[ok] git ", result.stdout)

    def test_install_hint_never_runs_anything(self):
        result = self.run_bash("install_hint git; install_hint python")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())


@unittest.skipUnless(POSIX_BASH and GIT, "needs bash and git on macOS/Linux")
class InstallShEndToEndTest(unittest.TestCase):
    """The whole installer against a local repo: clone, venv, offline pip, link, no setup."""

    def test_full_install_offline_then_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            home.mkdir()
            source = make_source_repo(tmp_path / "source")
            install = home / "automated-sdr"
            env = clean_env(home, SDR_REPO=str(source), SDR_PYTHON=sys.executable, PATH=os.environ.get("PATH", ""))

            def install_once() -> subprocess.CompletedProcess:
                return subprocess.run([BASH, str(ROOT / "install.sh")], capture_output=True, text=True,
                                      timeout=300, env=env, stdin=subprocess.DEVNULL, start_new_session=True)

            result = install_once()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Installed!", result.stdout)
            self.assertIn("No terminal to ask questions in", result.stdout)
            self.assertTrue((install / ".venv" / "bin" / "python").exists())
            link = home / ".local" / "bin" / "sdr"
            self.assertTrue(link.is_symlink())

            run = subprocess.run([str(link), "setup", "--answers", "a b.toml"], capture_output=True,
                                 text=True, timeout=60, cwd=tmp, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual(payload["argv"], ["setup", "--answers", "a b.toml"])
            self.assertEqual(Path(payload["prefix"]).resolve(), (install / ".venv").resolve())

            again = install_once()  # re-running updates in place and reuses the venv
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertIn("Using the existing environment", again.stdout)


@unittest.skipUnless(POSIX_BASH, "needs bash on macOS/Linux")
class SdrLauncherTest(unittest.TestCase):
    """bin/sdr must find its install through symlinks, prefer .venv and pass arguments through."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "install"
        (self.root / "bin").mkdir(parents=True)
        shutil.copy2(ROOT / "bin" / "sdr", self.root / "bin" / "sdr")
        (self.root / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
        self.work = self.dir / "work"
        self.work.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_sdr(self, launcher: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(launcher), *args], capture_output=True, text=True, timeout=60,
                              cwd=self.work, env={**os.environ, **env})

    def test_follows_symlink_chains_and_prefers_the_venv(self):
        python_wrapper(self.root / ".venv" / "bin" / "python", "venv")
        (self.dir / "links").mkdir()
        (self.dir / "other").mkdir()
        os.symlink(self.root / "bin" / "sdr", self.dir / "other" / "sdr-abs")
        os.symlink("../other/sdr-abs", self.dir / "links" / "sdr")  # relative hop, then absolute

        result = self.run_sdr(self.dir / "links" / "sdr", "setup", "--answers", "my answers.toml")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], ["setup", "--answers", "my answers.toml"])
        self.assertEqual(payload["marker"], "venv")
        self.assertEqual(Path(payload["cwd"]).resolve(), self.work.resolve())  # folder is kept

    def test_falls_back_to_python3_without_a_venv(self):
        fake_bin = self.dir / "fakebin"
        python_wrapper(fake_bin / "python3", "system")
        result = self.run_sdr(self.root / "bin" / "sdr", "doctor",
                              PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["marker"], "system")

    def test_exit_code_is_passed_through(self):
        python_wrapper(self.root / ".venv" / "bin" / "python", "venv")
        result = self.run_sdr(self.root / "bin" / "sdr", "run", SDR_TEST_EXIT="3")
        self.assertEqual(result.returncode, 3)


class SdrCmdLauncherTest(unittest.TestCase):
    def test_cmd_launcher_uses_the_venv_and_passes_arguments(self):
        text = read("bin/sdr.cmd")
        self.assertIn(r"%SDR_ROOT%\.venv\Scripts\python.exe", text)
        self.assertIn(r'"%SDR_ROOT%\cli.py" %* & exit /b', text)
        # a self-update may rewrite this file mid-run: nothing may follow the Python call
        self.assertNotIn("exit /b %ERRORLEVEL%", text)
        self.assertIn("setlocal", text)

    @unittest.skipUnless(IS_WINDOWS, "runs cmd.exe")
    def test_cmd_launcher_runs_cli_with_the_venv_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            (root / "bin").mkdir(parents=True)
            shutil.copy2(ROOT / "bin" / "sdr.cmd", root / "bin" / "sdr.cmd")
            (root / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
                           check=True, capture_output=True, timeout=120)
            result = subprocess.run([str(root / "bin" / "sdr.cmd"), "setup", "--section", "email"],
                                    capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "SDR_TEST_EXIT": "0"})
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["argv"], ["setup", "--section", "email"])
            self.assertEqual(Path(payload["prefix"]).resolve(), (root / ".venv").resolve())


class InstallPs1Test(unittest.TestCase):
    def setUp(self):
        self.text = read("install.ps1")

    def code_lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if not line.strip().startswith("#")]

    def test_never_calls_exit(self):
        # `irm ... | iex` runs in the user's own session: `exit` would close their window.
        offenders = [line for line in self.code_lines() if re.search(r"(?i)(^|[;{}\s])exit(\s|$|;)", line)]
        self.assertEqual(offenders, [])

    def test_defaults_and_steps(self):
        self.assertIn(f"$DefaultRepo = '{DEFAULT_GIT_URL}'", self.text)
        self.assertIn("Join-Path $env:USERPROFILE 'automated-sdr'", self.text)
        self.assertIn(r"Join-Path $env:USERPROFILE '.automated-sdr\bin'", self.text)
        for needle in ("$env:SDR_HOME", "$env:SDR_REPO", "$env:SDR_NO_SETUP", "$env:SDR_PYTHON",
                       "winget install --id Git.Git", "winget install --id Python.Python",
                       "'-m', 'venv'", "'-m', 'pip', 'install'", "& $sdr setup", "Set-StrictMode"):
            self.assertIn(needle, self.text)

    def test_runs_inside_one_script_block(self):
        code = "\n".join(self.code_lines()).strip()
        self.assertTrue(code.startswith("& {"), "everything must run inside & { ... }")
        self.assertTrue(code.endswith("}"))

    def test_installs_the_newest_release_tag_like_install_sh(self):
        # Mirrors install.sh: fetch tags, pick the newest vX.Y.Z numerically, check it out; `main`
        # is only followed before the first release.
        for needle in ("function Get-NewestRelease", "--sort=-v:refname", r"-match '^v\d+\.\d+\.\d+$'",
                       "fetch --tags --quiet origin", "'checkout', '--quiet', $tag", "checkout --quiet $tag",
                       "No release has been tagged yet", "no release yet"):
            self.assertIn(needle, self.text)
        self.assertEqual(self.text.count("pull --ff-only"), 1, "pull only as the pre-release fallback")

    def test_shim_of_another_install_is_left_alone(self):
        for needle in ("function Get-ShimTarget", "$sdr = Install-Shim $installDir $shimDir", "left alone"):
            self.assertIn(needle, self.text)
        self.assertNotIn("$sdr = Join-Path $shimDir 'sdr.cmd'", self.text)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "needs PowerShell")
    def test_parses_without_errors(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        path = str(ROOT / "install.ps1").replace("'", "''")
        command = (
            "$errors = $null; $tokens = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors); "
            "if ($errors) { $errors | ForEach-Object { $_.ToString() }; exit 1 } else { 'parsed-ok' }"
        )
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("parsed-ok", result.stdout)


class AgentDocsTest(unittest.TestCase):
    def setUp(self):
        self.text = read("AGENTS.md")

    def test_mentions_every_cli_command(self):
        missing = [c for c in CLI_COMMANDS if not re.search(rf"`sdr {re.escape(c)}\b", self.text)]
        self.assertEqual(missing, [])
        self.assertIn("| `sdr` |", self.text)

    def test_lists_every_setup_answer(self):
        for section, keys in ANSWER_KEYS.items():
            self.assertIn(f"`[{section}]`", self.text)
            for key in keys:
                self.assertRegex(self.text, rf"`{re.escape(key)}(`| =)", f"[{section}] {key} is not explained")

    def test_setup_flow_for_agents(self):
        for needle in ("setup-answers.toml", "sdr setup --answers setup-answers.toml",
                       "sdr setup --section", "sdr setup --print-questions", "SDR_NO_SETUP=1",
                       "password_env", "read -rs", "-AsSecureString", "python3 -m pytest tests/ -q"):
            self.assertIn(needle, self.text)

    def test_safety_rules(self):
        lowered = self.text.lower()
        for needle in ("never read, print", "never commit or push secrets", "never send email",
                       "never disable or weaken compliance checks", "`data/`", "`.env`"):
            self.assertIn(needle, lowered)

    def test_example_answers_file_has_no_password(self):
        block = self.text.split("```toml", 1)[1].split("```", 1)[0]
        for line in block.splitlines():
            key = line.split("=", 1)[0].strip().lower()
            if "pass" in key:
                self.assertTrue(key.endswith("_env"), f"password value in example: {line}")

    def test_install_commands_point_at_this_repo(self):
        self.assertIn(f"curl -fsSL {RAW_URL}/install.sh", self.text)
        self.assertIn(f"irm {RAW_URL}/install.ps1", self.text)

    def test_claude_md_defers_to_agents_md(self):
        text = read("CLAUDE.md")
        self.assertIn("AGENTS.md", text)
        self.assertIn("@AGENTS.md", text)


class HumanDocsTest(unittest.TestCase):
    def test_security_policy_covers_the_essentials(self):
        text = read("SECURITY.md")
        for needle in ("Supported versions", f"{REPO_URL}/security/advisories/new", "What the tool stores",
                       "`.env`", "600", "127.0.0.1", "DASHBOARD_PASSWORD_HASH", "fast-forward",
                       "CHANGELOG.md", "Responsible use", "CAN-SPAM", "data/backups/"):
            self.assertIn(needle, text)

    def test_contributing_explains_tests_and_scans(self):
        text = read("CONTRIBUTING.md")
        for needle in ("python3 -m pytest tests/ -q", "bandit -q -r core bots dashboard cli.py runner.py -ll",
                       "pip-audit -r requirements.txt", "SECURITY.md", "AGENTS.md", "# nosec"):
            self.assertIn(needle, text)


class IgnoreRulesTest(unittest.TestCase):
    MUST_IGNORE = [".env", ".env.local", "setup-answers.toml", "data/automations.db", "data/cron.log",
                   "data/backups/20260926-090000/.env", "data/update_status.json", "config/profile.toml",
                   "config/do_not_contact.txt", ".venv/bin/python", "leads.csv"]
    MUST_KEEP = [".env.example", "config/profile.example.toml", "config/do_not_contact.example.txt",
                 "install.sh", "install.ps1", "bin/sdr", "bin/sdr.cmd", "AGENTS.md", "requirements.txt"]

    def test_patterns_are_present(self):
        lines = {line.strip() for line in read(".gitignore").splitlines()}
        for pattern in (".env", "setup-answers.toml", "data/", "data/backups/", "config/profile.toml",
                        "config/do_not_contact.txt", ".venv/", "!.env.example"):
            self.assertIn(pattern, lines)

    @unittest.skipUnless(GIT and (ROOT / ".git").exists(), "needs git and a git checkout")
    def test_git_really_ignores_secrets_and_keeps_the_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty-gitconfig"
            empty.write_text("", encoding="utf-8")
            # Isolate from the developer's global excludes so only this repo's rules count.
            env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}

            def ignored(path: str) -> bool:
                result = subprocess.run([GIT, "-C", str(ROOT), "check-ignore", "--no-index", "-q", path],
                                        capture_output=True, env=env)
                return result.returncode == 0

            self.assertEqual([p for p in self.MUST_IGNORE if not ignored(p)], [])
            self.assertEqual([p for p in self.MUST_KEEP if ignored(p)], [])


class EnvExampleTest(unittest.TestCase):
    def setUp(self):
        self.values = {}
        for line in read(".env.example").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                self.values[key.strip()] = value.strip()

    def test_documents_the_current_keys(self):
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "IMAP_HOST", "IMAP_PORT",
                    "DASHBOARD_PASSWORD_HASH", "SDR_ALERT_EMAIL", "ANTHROPIC_API_KEY"):
            self.assertIn(key, self.values)

    def test_removed_hostinger_http_api(self):
        text = read(".env.example")
        self.assertNotIn("HOSTINGER_MAIL_API_TOKEN", text)
        self.assertNotIn("HOSTINGER_SENDER_EMAIL", text)

    def test_contains_no_real_secrets(self):
        for key, value in self.values.items():
            if re.search(r"PASS|TOKEN|KEY|HASH|WEBHOOK", key):
                self.assertIn(value, ("", "your-app-password"), f"{key} must be empty or a placeholder")


class CiConfigTest(unittest.TestCase):
    def test_workflow_matrix_and_security_job(self):
        text = read(".github/workflows/tests.yml")
        self.assertNotIn("\t", text, "YAML must not contain tabs")
        self.assertIn("os: [ubuntu-latest, macos-latest, windows-latest]", text)
        self.assertIn('python-version: ["3.11", "3.13"]', text)
        self.assertRegex(text, r'include:\s+- os: ubuntu-latest\s+python-version: "3\.12"')
        self.assertIn("python -m pytest tests/ -q", text)
        self.assertIn("pip install --disable-pip-version-check pip-audit bandit", text)
        self.assertIn("pip-audit -r requirements.txt", text)
        self.assertIn("bandit -q -r core bots dashboard cli.py runner.py -ll", text)
        self.assertIn("contents: read", text)

    def test_dependabot_watches_pip_and_actions_weekly(self):
        text = read(".github/dependabot.yml")
        self.assertIn("version: 2", text)
        self.assertIn('package-ecosystem: "pip"', text)
        self.assertIn('package-ecosystem: "github-actions"', text)
        self.assertEqual(text.count('interval: "weekly"'), 2)


class ConsistencyTest(unittest.TestCase):
    def test_both_installers_default_to_the_product_repo(self):
        self.assertIn(DEFAULT_GIT_URL, read("install.sh"))
        self.assertIn(DEFAULT_GIT_URL, read("install.ps1"))

    def test_installers_point_windows_and_unix_users_at_each_other(self):
        self.assertIn("install.ps1 | iex", read("install.sh"))
        self.assertIn(f"irm {RAW_URL}/install.ps1 | iex", read("install.ps1"))


if __name__ == "__main__":
    unittest.main()
