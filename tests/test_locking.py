"""Offline tests for core/locking.py and the runner's run lock.

The lock keeps a scheduled pipeline, the dashboard and `sdr update` from running over each other,
so it must work across threads and processes on every OS. The Windows backend is exercised on any
OS with a fake msvcrt module; nothing here runs a bot or touches the real schedule.
"""

import ast
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core import locking
from core.locking import AlreadyLocked, file_lock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestFileLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "nested", "run.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def hold_in_thread(self, path: str):
        held, release = threading.Event(), threading.Event()

        def hold():
            with file_lock(path):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(held.wait(5))
        return worker, release

    def test_creates_the_folder_and_lock_file(self):
        with file_lock(self.path):
            self.assertTrue(os.path.exists(self.path))

    def test_second_holder_fails_fast_without_waiting(self):
        worker, release = self.hold_in_thread(self.path)
        try:
            started = time.monotonic()
            with self.assertRaises(AlreadyLocked):
                with file_lock(self.path, wait_seconds=0):
                    pass
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            worker.join()

    def test_waits_for_the_holder_to_finish(self):
        worker, release = self.hold_in_thread(self.path)
        threading.Timer(0.3, release.set).start()
        started = time.monotonic()
        with file_lock(self.path, wait_seconds=5, poll_seconds=0.05):
            waited = time.monotonic() - started
        worker.join()
        self.assertGreater(waited, 0.2)

    def test_gives_up_after_wait_seconds(self):
        worker, release = self.hold_in_thread(self.path)
        try:
            started = time.monotonic()
            with self.assertRaises(AlreadyLocked):
                with file_lock(self.path, wait_seconds=0.3, poll_seconds=0.05):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
        finally:
            release.set()
            worker.join()

    def test_lock_is_released_when_the_block_raises(self):
        with self.assertRaises(RuntimeError):
            with file_lock(self.path):
                raise RuntimeError("boom")
        with file_lock(self.path):  # would raise AlreadyLocked if still held
            pass

    def test_error_message_names_the_lock_file(self):
        worker, release = self.hold_in_thread(self.path)
        try:
            with self.assertRaises(AlreadyLocked) as ctx:
                with file_lock(self.path):
                    pass
            self.assertEqual(ctx.exception.path, self.path)
            self.assertIn("run.lock", str(ctx.exception))
        finally:
            release.set()
            worker.join()

    def test_lock_holds_across_processes(self):
        """Cron and the dashboard are separate processes: the lock must work between them."""
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from core.locking import file_lock\n"
            "with file_lock(sys.argv[2]):\n"
            "    print('locked', flush=True); sys.stdin.read()\n"
        )
        child = subprocess.Popen([sys.executable, "-c", code, REPO_ROOT, self.path],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(AlreadyLocked):
                with file_lock(self.path):
                    pass
        finally:
            child.stdin.close()
            child.wait(10)
            child.stdout.close()
        with file_lock(self.path):  # free again once the other process exits
            pass


class FakeMsvcrt:
    """Mimics msvcrt.locking byte-range locks (per open handle), keyed by the file's identity."""

    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        self.held = {}  # (dev, inode, offset) -> fd

    def locking(self, fd, mode, nbytes):
        stat = os.fstat(fd)
        key = (stat.st_dev, stat.st_ino, os.lseek(fd, 0, os.SEEK_CUR))
        assert nbytes == 1
        if mode == self.LK_NBLCK:
            if key in self.held:
                raise PermissionError(13, "Permission denied")
            self.held[key] = fd
        elif mode == self.LK_UNLCK:
            self.held.pop(key, None)


class TestWindowsLockBackend(unittest.TestCase):
    """Runs the msvcrt code path on any OS with a fake msvcrt module."""

    def test_windows_backend_locks_and_unlocks(self):
        fake = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"msvcrt": fake}), \
                mock.patch.object(locking, "_platform_functions",
                                  lambda: (locking._try_lock_windows, locking._unlock_windows)):
            path = os.path.join(tmp, "win.lock")
            with file_lock(path):
                self.assertEqual(len(fake.held), 1)
                with self.assertRaises(AlreadyLocked):
                    with file_lock(path):
                        pass
            self.assertEqual(fake.held, {})
            with file_lock(path):  # re-acquirable after release
                pass


class TestNoPosixOnlyImports(unittest.TestCase):
    """fcntl/termios don't exist on Windows: importing them at module level would crash there."""

    def test_modules_do_not_import_fcntl_at_top_level(self):
        for rel in ("core/locking.py", "core/scheduler.py", "core/scheduler_helper.py", "runner.py"):
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertNotIn(name.split(".")[0], ("fcntl", "termios", "msvcrt"), rel)


class TestRunnerLock(unittest.TestCase):
    def test_runner_uses_core_locking(self):
        import runner
        self.assertTrue(issubclass(runner.AlreadyRunning, AlreadyLocked))
        self.assertEqual(runner.LOCK_WAIT_SECONDS["inbox"], 0)
        self.assertEqual(runner.DEFAULT_LOCK_WAIT_SECONDS, 600)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner, "DB_DIR", tmp):
            with runner.run_lock():
                self.assertTrue(os.path.exists(os.path.join(tmp, ".runner.lock")))
                with self.assertRaises(runner.AlreadyRunning) as ctx:
                    with runner.run_lock(0):
                        pass
                self.assertIn("Another run is in progress", str(ctx.exception))

    def test_run_task_reports_skipped_when_busy(self):
        import runner
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner, "DB_DIR", tmp), \
                mock.patch.object(runner, "log_event"), mock.patch.object(runner, "_run_task") as run, \
                redirect_stdout(io.StringIO()):
            with runner.run_lock():
                result = runner.run_task("inbox")
        run.assert_not_called()
        self.assertEqual(result["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
