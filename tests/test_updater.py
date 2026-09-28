"""Offline tests for core/updater.py (check, apply with backup + rollback, scheduled mode).

The git tests build throwaway repositories in a temp folder: a "dev" repo that cuts
releases, a bare "origin" it pushes to, and an "install" clone that plays the user's
copy. `git fetch` only ever talks to that local bare repo, so nothing leaves the machine.
pip and pytest are replaced by fakes; the real ones would take minutes.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core import updater

HAS_GIT = shutil.which("git") is not None

CHANGELOG_V1 = """# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Work in progress that must never show up in update notes.

## [1.0.0] - 2026-09-26

### Added
- First public release.

[1.0.0]: https://example.test/releases/tag/v1.0.0
"""

CHANGELOG_V11 = CHANGELOG_V1.replace(
    "## [1.0.0] - 2026-09-26",
    "## [1.1.0] - 2026-10-10\n\n### Added\n- Smarter follow-ups.\n\n## [1.0.0] - 2026-09-26",
)


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["fake"], returncode, stdout, stderr)


class Recorder:
    """Stands in for pip / the test suite: records every call, returns a fixed result."""

    def __init__(self, returncode: int = 0, output: str = "ok"):
        self.calls = []
        self.returncode = returncode
        self.output = output

    def __call__(self, root: str) -> subprocess.CompletedProcess:
        self.calls.append(root)
        return completed(self.returncode, self.output, "" if self.returncode == 0 else self.output)


def sh_git(cwd: str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------------------

class TestVersionParsing(unittest.TestCase):
    def test_parse_version_accepts_plain_and_v_prefixed(self):
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version(" v10.0.12\n"), (10, 0, 12))

    def test_parse_version_rejects_anything_that_is_not_a_stable_release(self):
        for bad in ("", "1.2", "v1.2.3-rc1", "1.2.3.4", "latest", "vv1.2.3", None):
            self.assertIsNone(updater.parse_version(bad), bad)

    def test_latest_tag_compares_numerically_not_alphabetically(self):
        tags = ["v1.9.0", "v1.10.0", "v1.2.0", "junk", "v2.0.0-beta", "1.99.0"]
        self.assertEqual(updater.latest_tag(tags), "v1.10.0")

    def test_latest_tag_is_none_without_release_tags(self):
        self.assertIsNone(updater.latest_tag([]))
        self.assertIsNone(updater.latest_tag(["nightly", "v1.0"]))


class TestChangelogNotes(unittest.TestCase):
    def test_only_sections_newer_than_current_are_included(self):
        notes = updater.changelog_notes(CHANGELOG_V11, current="1.0.0", latest="1.1.0")
        self.assertIn("1.1.0", notes)
        self.assertIn("Smarter follow-ups", notes)
        self.assertNotIn("First public release", notes)

    def test_unreleased_section_and_link_references_are_dropped(self):
        notes = updater.changelog_notes(CHANGELOG_V11, current="0.9.0", latest="1.1.0")
        self.assertNotIn("Work in progress", notes)
        self.assertNotIn("https://example.test", notes)
        self.assertIn("First public release", notes)

    def test_sections_beyond_latest_are_ignored(self):
        text = CHANGELOG_V11.replace("## [1.1.0]", "## [1.2.0] - 2026-11-01\n\n- Future.\n\n## [1.1.0]")
        notes = updater.changelog_notes(text, current="1.0.0", latest="1.1.0")
        self.assertNotIn("Future", notes)
        self.assertIn("Smarter follow-ups", notes)

    def test_long_notes_are_truncated(self):
        text = "## [2.0.0] - 2026-12-01\n\n" + ("- a change\n" * 2000)
        notes = updater.changelog_notes(text, current="1.0.0", max_chars=200)
        self.assertLessEqual(len(notes), 200)
        self.assertTrue(notes.endswith("…"))

    def test_empty_changelog_gives_empty_notes(self):
        self.assertEqual(updater.changelog_notes("", current="1.0.0"), "")


class TestUpdateMode(unittest.TestCase):
    def test_mode_defaults_to_notify_and_rejects_unknown_values(self):
        self.assertEqual(updater.update_mode(None), "notify")
        self.assertEqual(updater.update_mode({}), "notify")
        self.assertEqual(updater.update_mode({"updates": {"mode": "AUTO"}}), "auto")
        self.assertEqual(updater.update_mode({"updates": {"mode": "off"}}), "off")
        self.assertEqual(updater.update_mode({"updates": {"mode": "yolo"}}), "notify")


class TestStatusFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_corrupt_status_reads_as_empty(self):
        self.assertEqual(updater.read_status(self.root), {})
        write(updater.status_path(self.root), "{not json")
        self.assertEqual(updater.read_status(self.root), {})
        write(updater.status_path(self.root), "[1, 2]")
        self.assertEqual(updater.read_status(self.root), {})

    def test_write_then_read_round_trips(self):
        updater.write_status({"available": True, "latest": "1.1.0"}, self.root)
        self.assertEqual(updater.read_status(self.root), {"available": True, "latest": "1.1.0"})
        self.assertEqual(os.path.dirname(updater.status_path(self.root)), os.path.join(self.root, "data"))


class TestDigestLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, **fields) -> None:
        base = {"checked_at": self.now.isoformat(), "current": "1.0.0", "latest": "1.1.0",
                "latest_tag": "v1.1.0", "available": False, "mode": "notify", "notes": "",
                "last_result": "none"}
        updater.write_status({**base, **fields}, self.root)

    def test_no_status_file_means_no_lines(self):
        self.assertEqual(updater.digest_lines({}, root=self.root, now=self.now), [])

    def test_available_update_is_announced_with_the_command(self):
        self.status(available=True)
        lines = updater.digest_lines({}, root=self.root, now=self.now)
        self.assertEqual(lines, ["Update available: v1.1.0 — run `sdr update`"])

    def test_recent_auto_update_is_reported_once_it_happened(self):
        self.status(current="1.1.0", last_result="updated", last_target="1.1.0",
                    last_attempt_at=(self.now - timedelta(hours=1)).isoformat())
        self.assertEqual(updater.digest_lines({}, root=self.root, now=self.now), ["Updated to v1.1.0"])

    def test_old_update_is_not_repeated_every_day(self):
        self.status(current="1.1.0", last_result="updated", last_target="1.1.0",
                    last_attempt_at=(self.now - timedelta(days=3)).isoformat())
        self.assertEqual(updater.digest_lines({}, root=self.root, now=self.now), [])

    def test_recent_rollback_is_explained(self):
        self.status(available=True, last_result="rolled_back", last_target="1.1.0",
                    last_attempt_at=(self.now - timedelta(hours=2)).isoformat())
        lines = updater.digest_lines({}, root=self.root, now=self.now)
        self.assertEqual(len(lines), 2)
        self.assertIn("rolled back", lines[0])
        self.assertIn("v1.1.0", lines[0])
        self.assertTrue(lines[1].startswith("Update available: v1.1.0"))

    def test_mode_off_silences_the_digest(self):
        self.status(available=True)
        self.assertEqual(updater.digest_lines({"updates": {"mode": "off"}}, root=self.root, now=self.now), [])

    def test_garbage_timestamps_do_not_crash(self):
        self.status(last_result="updated", last_target="1.1.0", last_attempt_at="yesterday-ish")
        self.assertEqual(updater.digest_lines({}, root=self.root, now=self.now), [])


class TestBackups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make_user_data(self) -> None:
        os.makedirs(os.path.join(self.root, "data"), exist_ok=True)
        conn = sqlite3.connect(os.path.join(self.root, "data", "automations.db"))
        conn.execute("CREATE TABLE leads (email TEXT)")
        conn.execute("INSERT INTO leads VALUES ('ana@studio.test')")
        conn.commit()
        conn.close()
        write(os.path.join(self.root, "config", "profile.toml"), "[sender]\nfrom_name = 'Ana'\n")
        write(os.path.join(self.root, ".env"), "SMTP_PASS=do-not-print\n")
        write(os.path.join(self.root, "config", "do_not_contact.txt"), "boss@client.test\n")

    def test_backup_copies_every_user_file_keeping_its_relative_path(self):
        self.make_user_data()
        backup = updater.backup_user_data(self.root, now=datetime(2026, 10, 1, 8, 30, 0))
        self.assertEqual(os.path.basename(backup), "20261001-083000")
        self.assertEqual(os.path.dirname(backup), os.path.join(self.root, "data", "backups"))
        self.assertEqual(read(os.path.join(backup, ".env")), "SMTP_PASS=do-not-print\n")
        self.assertEqual(read(os.path.join(backup, "config", "profile.toml")), "[sender]\nfrom_name = 'Ana'\n")
        self.assertTrue(os.path.exists(os.path.join(backup, "config", "do_not_contact.txt")))
        conn = sqlite3.connect(os.path.join(backup, "data", "automations.db"))
        self.assertEqual(conn.execute("SELECT email FROM leads").fetchall(), [("ana@studio.test",)])
        conn.close()
        # The originals are untouched.
        self.assertEqual(read(os.path.join(self.root, ".env")), "SMTP_PASS=do-not-print\n")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_backed_up_secrets_are_private(self):
        self.make_user_data()
        backup = updater.backup_user_data(self.root, now=datetime(2026, 10, 1, 8, 30, 0))
        self.assertEqual(os.stat(os.path.join(backup, ".env")).st_mode & 0o777, 0o600)

    def test_missing_files_are_skipped_and_nothing_to_back_up_returns_none(self):
        self.assertIsNone(updater.backup_user_data(self.root))
        write(os.path.join(self.root, ".env"), "X=1\n")
        backup = updater.backup_user_data(self.root)
        self.assertTrue(os.path.exists(os.path.join(backup, ".env")))
        self.assertFalse(os.path.exists(os.path.join(backup, "data", "automations.db")))

    def test_two_backups_in_the_same_second_do_not_collide(self):
        self.make_user_data()
        moment = datetime(2026, 10, 1, 8, 30, 0)
        first = updater.backup_user_data(self.root, now=moment)
        second = updater.backup_user_data(self.root, now=moment)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.isdir(first) and os.path.isdir(second))

    def test_only_the_newest_backups_are_kept_and_other_folders_are_left_alone(self):
        self.make_user_data()
        keep_me = os.path.join(self.root, "data", "backups", "my-manual-copy")
        os.makedirs(keep_me)
        start = datetime(2026, 10, 1, 8, 0, 0)
        made = [updater.backup_user_data(self.root, now=start + timedelta(days=i)) for i in range(7)]
        remaining = sorted(os.listdir(os.path.join(self.root, "data", "backups")))
        self.assertEqual(len([m for m in made if os.path.isdir(m)]), updater.KEEP_BACKUPS)
        self.assertFalse(os.path.exists(made[0]))
        self.assertTrue(os.path.isdir(made[-1]))
        self.assertIn("my-manual-copy", remaining)


# ---------------------------------------------------------------------------------------
# Real git repositories (offline: origin is a local bare repo)
# ---------------------------------------------------------------------------------------

@unittest.skipUnless(HAS_GIT, "git is not installed")
class GitRepoTestCase(unittest.TestCase):
    """dev (cuts releases) -> origin.git (bare) -> install (the user's copy, on v1.0.0).

    The template install is cloned when only v1.0.0 exists; v1.1.0 is pushed afterwards,
    so every per-test copy of the install has an update waiting on origin.
    """

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = cls.class_tmp.name
        empty_config = os.path.join(base, "gitconfig")
        write(empty_config, "")
        cls.env_patch = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test",
            # Isolate from the developer's own git config (commit signing, hooks, autocrlf…).
            "GIT_CONFIG_GLOBAL": empty_config, "GIT_CONFIG_NOSYSTEM": "1",
        })
        cls.env_patch.start()

        cls.dev = os.path.join(base, "dev")
        cls.origin = os.path.join(base, "origin.git")
        cls.template = os.path.join(base, "install-template")
        os.makedirs(cls.dev)
        sh_git(cls.dev, "init", "-q")
        sh_git(cls.dev, "checkout", "-q", "-b", "main")
        write(os.path.join(cls.dev, ".gitignore"),
              "data/\n.env\nconfig/profile.toml\nconfig/do_not_contact.txt\n__pycache__/\n.pytest_cache/\n")
        write(os.path.join(cls.dev, "VERSION"), "1.0.0\n")
        write(os.path.join(cls.dev, "CHANGELOG.md"), CHANGELOG_V1)
        write(os.path.join(cls.dev, "app.py"), "print('v1')\n")
        write(os.path.join(cls.dev, "requirements.txt"), "requests>=2.31\n")
        sh_git(cls.dev, "add", "-A")
        sh_git(cls.dev, "commit", "-q", "-m", "Release 1.0.0")
        sh_git(cls.dev, "tag", "-a", "v1.0.0", "-m", "v1.0.0")  # annotated tag
        sh_git(base, "clone", "-q", "--bare", cls.dev, cls.origin)
        sh_git(cls.dev, "remote", "add", "origin", cls.origin)
        sh_git(base, "clone", "-q", cls.origin, cls.template)

        # The user's private data lives in git-ignored files.
        os.makedirs(os.path.join(cls.template, "data"))
        conn = sqlite3.connect(os.path.join(cls.template, "data", "automations.db"))
        conn.execute("CREATE TABLE leads (email TEXT)")
        conn.execute("INSERT INTO leads VALUES ('ana@studio.test')")
        conn.commit()
        conn.close()
        write(os.path.join(cls.template, ".env"), "SMTP_PASS=do-not-print\n")
        write(os.path.join(cls.template, "config", "profile.toml"), "[updates]\nmode = 'auto'\n")

        # Release 1.1.0 (lightweight tag) after the user installed.
        write(os.path.join(cls.dev, "VERSION"), "1.1.0\n")
        write(os.path.join(cls.dev, "CHANGELOG.md"), CHANGELOG_V11)
        write(os.path.join(cls.dev, "app.py"), "print('v1.1')\n")
        write(os.path.join(cls.dev, "new_feature.py"), "FEATURE = True\n")
        sh_git(cls.dev, "add", "-A")
        sh_git(cls.dev, "commit", "-q", "-m", "Release 1.1.0")
        sh_git(cls.dev, "tag", "v1.1.0")
        # Unreleased work on main after the tag must never be installed.
        write(os.path.join(cls.dev, "app.py"), "print('unreleased')\n")
        sh_git(cls.dev, "commit", "-q", "-am", "WIP")
        sh_git(cls.dev, "push", "-q", "origin", "main", "--tags")
        cls.v110_sha = sh_git(cls.dev, "rev-parse", "v1.1.0^{commit}")

    @classmethod
    def tearDownClass(cls):
        cls.env_patch.stop()
        cls.class_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "install")
        shutil.copytree(self.template, self.root, symlinks=True)
        self.v100_sha = sh_git(self.root, "rev-parse", "HEAD")
        self.pip = Recorder()
        self.tests = Recorder()

    def tearDown(self):
        self.tmp.cleanup()

    def head(self) -> str:
        return sh_git(self.root, "rev-parse", "HEAD")

    def apply(self, **kwargs) -> dict:
        kwargs.setdefault("pip", self.pip)
        kwargs.setdefault("tests", self.tests)
        return updater.apply_update(self.root, **kwargs)


class TestCheckForUpdate(GitRepoTestCase):
    def test_new_tag_on_origin_is_found_with_its_release_notes(self):
        status = updater.check_for_update(self.root)
        self.assertTrue(status["available"])
        self.assertEqual((status["current"], status["latest"], status["latest_tag"]), ("1.0.0", "1.1.0", "v1.1.0"))
        self.assertIn("Smarter follow-ups", status["notes"])
        self.assertNotIn("First public release", status["notes"])
        self.assertEqual(status["error"], "")

    def test_status_file_follows_the_shared_contract(self):
        updater.check_for_update(self.root, mode="notify")
        saved = updater.read_status(self.root)
        for key in ("checked_at", "current", "latest", "available", "mode", "notes", "last_result"):
            self.assertIn(key, saved)
        self.assertEqual(saved["mode"], "notify")
        self.assertEqual(saved["last_result"], "none")
        datetime.fromisoformat(saved["checked_at"])

    def test_check_does_not_change_the_code(self):
        updater.check_for_update(self.root)
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(read(os.path.join(self.root, "VERSION")).strip(), "1.0.0")

    def test_already_on_the_latest_release_means_no_update(self):
        sh_git(self.root, "fetch", "-q", "--tags", "origin")
        sh_git(self.root, "merge", "-q", "--ff-only", self.v110_sha)
        status = updater.check_for_update(self.root)
        self.assertFalse(status["available"])
        self.assertEqual(status["notes"], "")

    def test_unreachable_origin_is_reported_not_raised(self):
        sh_git(self.root, "remote", "set-url", "origin", os.path.join(self.tmp.name, "nowhere.git"))
        status = updater.check_for_update(self.root)
        self.assertFalse(status["available"])
        self.assertIn("git fetch", status["error"])
        self.assertTrue(os.path.exists(updater.status_path(self.root)))

    def test_folder_that_is_not_a_git_checkout_explains_itself(self):
        plain = os.path.join(self.tmp.name, "zip-download")
        write(os.path.join(plain, "VERSION"), "1.0.0\n")
        status = updater.check_for_update(plain)
        self.assertFalse(status["available"])
        self.assertIn("git", status["error"])

    def test_previous_apply_result_survives_a_new_check(self):
        updater.write_status({"last_result": "rolled_back", "last_target": "1.1.0"}, self.root)
        status = updater.check_for_update(self.root)
        self.assertEqual(status["last_result"], "rolled_back")
        self.assertEqual(status["last_target"], "1.1.0")

    def test_fake_git_runner_can_be_injected(self):
        calls = []

        def fake_git(args, cwd):
            calls.append(args)
            outputs = {"rev-parse": self.root, "tag": "v1.0.0\nv1.2.0\n"}
            if args[:2] == ["show", "v1.2.0:CHANGELOG.md"]:
                return completed(0, "## [1.2.0] - 2027-01-01\n\n- Shiny.\n")
            if args[:2] == ["merge-base", "--is-ancestor"]:
                return completed(1)
            return completed(0, outputs.get(args[0], ""))

        status = updater.check_for_update(self.root, git=fake_git, write=False)
        self.assertTrue(status["available"])
        self.assertEqual(status["latest"], "1.2.0")
        self.assertIn("Shiny", status["notes"])
        self.assertIn(["fetch", "--tags", "--quiet", "origin"], calls)
        self.assertFalse(os.path.exists(updater.status_path(self.root)))

    def test_release_already_in_head_is_not_offered_again_when_version_file_lags(self):
        # A release whose VERSION file wasn't bumped must not be "available" forever.
        sh_git(self.root, "fetch", "-q", "--tags", "origin")
        sh_git(self.root, "merge", "-q", "--ff-only", self.v110_sha)
        write(os.path.join(self.root, "VERSION"), "1.0.0\n")
        sh_git(self.root, "update-index", "--assume-unchanged", "VERSION")
        status = updater.check_for_update(self.root, write=False)
        self.assertEqual(status["latest"], "1.1.0")
        self.assertFalse(status["available"])


class TestApplyUpdate(GitRepoTestCase):
    def test_successful_update_fast_forwards_to_the_tag_and_keeps_user_data(self):
        result = self.apply()
        self.assertEqual(result["result"], "updated", result["message"])
        self.assertEqual(self.head(), self.v110_sha)  # the tag, never the unreleased WIP commit
        self.assertEqual(read(os.path.join(self.root, "VERSION")).strip(), "1.1.0")
        self.assertTrue(os.path.exists(os.path.join(self.root, "new_feature.py")))
        self.assertEqual(self.pip.calls, [self.root])
        self.assertEqual(self.tests.calls, [self.root])
        # User data is still there, and a backup was made first.
        self.assertEqual(read(os.path.join(self.root, ".env")), "SMTP_PASS=do-not-print\n")
        self.assertTrue(os.path.exists(os.path.join(self.root, "data", "automations.db")))
        self.assertTrue(os.path.exists(os.path.join(result["backup_dir"], ".env")))
        self.assertTrue(os.path.exists(os.path.join(result["backup_dir"], "config", "profile.toml")))
        status = updater.read_status(self.root)
        self.assertEqual((status["last_result"], status["current"], status["available"]), ("updated", "1.1.0", False))
        self.assertEqual(status["last_target"], "1.1.0")

    def test_nothing_to_do_when_already_up_to_date(self):
        self.assertEqual(self.apply()["result"], "updated")
        again = self.apply()
        self.assertEqual(again["result"], "none")
        self.assertEqual(len(self.pip.calls), 1)

    def test_asking_for_a_release_this_copy_already_contains_is_a_no_op(self):
        self.assertEqual(self.apply()["result"], "updated")
        write(os.path.join(self.root, "app.py"), "print('edited after updating')\n")
        result = self.apply(tag="v1.0.0")
        self.assertEqual(result["result"], "none")
        self.assertEqual(self.head(), self.v110_sha)
        self.assertEqual(updater.read_status(self.root)["last_result"], "updated")

    def test_modified_tracked_files_block_the_update(self):
        write(os.path.join(self.root, "app.py"), "print('my local tweak')\n")
        result = self.apply()
        self.assertEqual(result["result"], "skipped_dirty")
        self.assertIn("app.py", result["message"])
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(read(os.path.join(self.root, "app.py")), "print('my local tweak')\n")
        self.assertEqual(self.pip.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "data", "backups")))
        self.assertEqual(updater.read_status(self.root)["last_result"], "skipped_dirty")

    def test_force_updates_anyway_and_saves_the_local_changes_as_a_patch(self):
        write(os.path.join(self.root, "requirements.txt"), "requests>=2.31\n# my extra\n")
        result = self.apply(force=True)
        self.assertEqual(result["result"], "updated", result["message"])
        patch = read(os.path.join(result["backup_dir"], "local-changes.patch"))
        self.assertIn("# my extra", patch)

    def test_failing_tests_roll_back_to_the_previous_version(self):
        self.tests = Recorder(returncode=1, output="FAILED tests/test_x.py::test_y")
        result = self.apply()
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("test", result["message"].lower())
        self.assertIn("FAILED tests/test_x.py", result["output"])
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(read(os.path.join(self.root, "VERSION")).strip(), "1.0.0")
        self.assertFalse(os.path.exists(os.path.join(self.root, "new_feature.py")))
        self.assertEqual(len(self.pip.calls), 2)  # new requirements, then the old ones again
        self.assertEqual(read(os.path.join(self.root, ".env")), "SMTP_PASS=do-not-print\n")
        status = updater.read_status(self.root)
        self.assertEqual(status["last_result"], "rolled_back")
        self.assertTrue(status["available"])

    def test_skipped_requirements_install_is_explained_in_the_result(self):
        self.pip = lambda root: updater.skipped_pip_result("pip isn't installed for /usr/bin/python3")
        result = self.apply()
        self.assertEqual(result["result"], "updated", result["message"])
        self.assertEqual(self.head(), self.v110_sha)
        self.assertIn("Requirements were not reinstalled", result["message"])
        self.assertIn("pip isn't installed", result["message"])
        self.assertIn("Requirements were not reinstalled", updater.read_status(self.root)["last_message"])

    def test_failing_dependency_install_rolls_back_without_running_tests(self):
        self.pip = Recorder(returncode=1, output="ERROR: No matching distribution")
        result = self.apply()
        self.assertEqual(result["result"], "rolled_back")
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(self.tests.calls, [])

    def test_diverged_local_history_is_refused(self):
        write(os.path.join(self.root, "mine.py"), "x = 1\n")
        sh_git(self.root, "add", "mine.py")
        sh_git(self.root, "commit", "-q", "-m", "my own change")
        before = self.head()
        result = self.apply()
        self.assertEqual(result["result"], "skipped_diverged")
        self.assertEqual(self.head(), before)
        self.assertEqual(self.pip.calls, [])

    def test_merge_blocked_by_an_untracked_file_changes_nothing(self):
        write(os.path.join(self.root, "new_feature.py"), "# my own file with the same name\n")
        result = self.apply()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(read(os.path.join(self.root, "new_feature.py")), "# my own file with the same name\n")
        self.assertEqual(self.pip.calls, [])

    def test_a_failed_rollback_is_reported_loudly(self):
        def git_that_cannot_reset(args, cwd):
            if args[0] == "reset":
                return completed(1, "", "fatal: cannot lock ref")
            return updater.run_git(args, cwd)

        self.tests = Recorder(returncode=1, output="boom")
        result = self.apply(git=git_that_cannot_reset)
        self.assertEqual(result["result"], "rollback_failed")
        self.assertIn(self.v100_sha, result["message"])

    def test_busy_runner_skips_the_update(self):
        def busy(root, wait_seconds):
            raise updater.UpdateBusy("A run is in progress")

        with mock.patch.object(updater, "_update_lock", busy):
            result = self.apply()
        self.assertEqual(result["result"], "skipped_busy")
        self.assertEqual(self.head(), self.v100_sha)

    def test_running_pipeline_holding_the_real_runner_lock_blocks_the_update(self):
        try:
            from core.locking import file_lock
        except ImportError:
            self.skipTest("core.locking not available")
        with file_lock(os.path.join(self.root, "data", ".runner.lock")):
            result = self.apply()
        self.assertEqual(result["result"], "skipped_busy")
        self.assertIn("update", result["message"])
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(self.pip.calls, [])
        self.assertEqual(updater.read_status(self.root)["last_result"], "skipped_busy")

    def test_lock_follows_a_database_moved_with_automations_db_path(self):
        try:
            from core.locking import file_lock
        except ImportError:
            self.skipTest("core.locking not available")
        import core.db as db

        elsewhere = os.path.join(self.tmp.name, "db-elsewhere")
        with mock.patch.object(updater, "ROOT_DIR", self.root), mock.patch.object(db, "DB_DIR", elsewhere), \
                file_lock(os.path.join(elsewhere, ".runner.lock")):
            result = self.apply()
        self.assertEqual(result["result"], "skipped_busy")

    def test_specific_tag_must_look_like_a_release(self):
        with self.assertRaises(ValueError):
            self.apply(tag="--upload-pack=evil")


class TestScheduledRun(GitRepoTestCase):
    def test_off_does_nothing_at_all(self):
        result = updater.scheduled_run({"updates": {"mode": "off"}}, root=self.root, pip=self.pip, tests=self.tests)
        self.assertEqual(result["result"], "none")
        self.assertFalse(os.path.exists(updater.status_path(self.root)))
        self.assertEqual(self.head(), self.v100_sha)

    def test_notify_checks_and_records_but_never_installs(self):
        result = updater.scheduled_run({"updates": {"mode": "notify"}}, root=self.root,
                                       pip=self.pip, tests=self.tests)
        self.assertEqual(result["result"], "none")
        self.assertIn("v1.1.0", result["message"])
        self.assertTrue(updater.read_status(self.root)["available"])
        self.assertEqual(self.head(), self.v100_sha)
        self.assertEqual(self.pip.calls, [])

    def test_missing_profile_behaves_like_notify(self):
        result = updater.scheduled_run(None, root=self.root, pip=self.pip, tests=self.tests)
        self.assertEqual(result["mode"], "notify")
        self.assertEqual(self.head(), self.v100_sha)

    def test_auto_installs_the_new_release(self):
        result = updater.scheduled_run({"updates": {"mode": "auto"}}, root=self.root,
                                       pip=self.pip, tests=self.tests)
        self.assertEqual(result["result"], "updated", result["message"])
        self.assertEqual(self.head(), self.v110_sha)
        status = updater.read_status(self.root)
        self.assertEqual((status["mode"], status["last_result"]), ("auto", "updated"))
        self.assertEqual(updater.digest_lines({"updates": {"mode": "auto"}}, root=self.root), ["Updated to v1.1.0"])

    def test_auto_with_nothing_new_installs_nothing(self):
        sh_git(self.root, "fetch", "-q", "--tags", "origin")
        sh_git(self.root, "merge", "-q", "--ff-only", self.v110_sha)
        result = updater.scheduled_run({"updates": {"mode": "auto"}}, root=self.root,
                                       pip=self.pip, tests=self.tests)
        self.assertEqual(result["result"], "none")
        self.assertEqual(self.pip.calls, [])


# ---------------------------------------------------------------------------------------
# Daily digest integration
# ---------------------------------------------------------------------------------------

class TestDigestIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def send_digest(self, status: dict | None, profile_updates: dict | None = None) -> str:
        from bots import digest

        if status is not None:
            updater.write_status(status, self.tmp.name)
        sent = self.sent

        class FakeEngine:
            def __init__(self, profile):
                pass

            def send_real_email(self, to, subject, html, text):
                sent.append({"to": to, "subject": subject, "text": text})
                return True

        profile = {"sender": {"product_name": "Acme"}, "sdr": {"alert_email": "me@acme.test"}}
        if profile_updates is not None:
            profile["updates"] = profile_updates
        with mock.patch.object(updater, "ROOT_DIR", self.tmp.name), \
                mock.patch.object(digest, "EmailMarketingEngine", FakeEngine), \
                mock.patch.object(digest, "pipeline_stats", lambda p: {"sent_today": 2, "hot_leads": []}), \
                mock.patch.object(digest, "render_report", lambda stats, product, profile: "FUNNEL REPORT"), \
                mock.patch.object(digest, "log_event", lambda *a, **k: None), \
                mock.patch.object(digest, "load_env_file", lambda: None):
            result = digest.send_daily_digest(profile, force=True)
        self.assertEqual(result["status"], "sent")
        return self.sent[-1]["text"]

    def test_digest_mentions_an_available_update(self):
        text = self.send_digest({"available": True, "latest": "1.1.0", "current": "1.0.0", "last_result": "none"})
        self.assertIn("FUNNEL REPORT", text)
        self.assertIn("Update available: v1.1.0 — run `sdr update`", text)

    def test_digest_is_unchanged_without_an_update(self):
        text = self.send_digest(None)
        self.assertEqual(text, "FUNNEL REPORT")

    def test_digest_respects_updates_off(self):
        text = self.send_digest({"available": True, "latest": "1.1.0"}, profile_updates={"mode": "off"})
        self.assertNotIn("Update available", text)


class TestChangelogFile(unittest.TestCase):
    def test_changelog_has_the_current_release_and_notes_parse(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        text = read(os.path.join(root, "CHANGELOG.md"))
        version = read(os.path.join(root, "VERSION")).strip()
        self.assertIn(f"## [{version}]", text)
        self.assertIn("Keep a Changelog", text)
        self.assertIn("Automated SDR", updater.changelog_notes(text, current="0.0.0"))


if __name__ == "__main__":
    unittest.main()
