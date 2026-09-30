"""End-to-end check of the one-line install, the way a new user runs it (macOS / Linux CI).

Runs `cat install.sh | bash` in a pseudo-terminal, so setup reaches the keyboard through
/dev/tty exactly as under `curl ... | bash`, then:
  1. waits for setup's first question (it must wait, not cancel: the v1.2.2 bug),
  2. types a bad website and expects the validation message (keys really arrive),
  3. presses Ctrl-C and expects the clean "Setup cancelled" exit.

The installer only installs release tags, so this script tags a throwaway clone of the checked-out
code (v99.99.99) and installs from that: CI tests the pull request's code, never the last release.
Nothing is sent and nothing leaves the machine beyond `pip install`.

Usage: python scripts/install_smoke.py
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRST_QUESTION = "website"
INSTALL_TIMEOUT = 600          # pip on a cold runner
STEP_TIMEOUT = 30
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


class Terminal:
    """A child process in a pseudo-terminal, answering the cursor-position query a real one would."""

    def __init__(self, command: list[str], env: dict):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:   # child
            os.execvpe(command[0], command, env)
        self.output = ""

    def expect(self, needle: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if needle.lower() in ANSI.sub("", self.output).lower():
                return True
            ready, _, _ = select.select([self.fd], [], [], 0.5)
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            if b"\x1b[6n" in data:
                os.write(self.fd, b"\x1b[1;1R")
            self.output += data.decode("utf-8", "replace")
        return needle.lower() in ANSI.sub("", self.output).lower()

    def type(self, keys: str) -> None:
        os.write(self.fd, keys.encode())

    def still_running_after(self, seconds: float) -> bool:
        self.expect("\x00never\x00", seconds)
        finished, _ = os.waitpid(self.pid, os.WNOHANG)
        return finished == 0

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def fail(message: str, term: Terminal | None = None) -> int:
    print(f"FAIL: {message}")
    if term is not None:
        print("---- terminal output ----")
        print(ANSI.sub("", term.output)[-4000:])
    return 1


def main() -> int:
    work = tempfile.mkdtemp(prefix="sdr-install-smoke-")
    source = os.path.join(work, "source")
    subprocess.run(["git", "clone", "--quiet", REPO, source], check=True)
    subprocess.run(["git", "-C", source, "tag", "v99.99.99"], check=True)
    env = {**os.environ, "SDR_REPO": source, "SDR_HOME": os.path.join(work, "app"),
           "SDR_BIN_DIR": os.path.join(work, "bin"), "TERM": "xterm-256color"}
    env.pop("SDR_NO_SETUP", None)
    installer = os.path.join(REPO, "install.sh")
    term = Terminal(["/bin/bash", "-c", f'cat "{installer}" | bash'], env)
    try:
        if not term.expect(FIRST_QUESTION, INSTALL_TIMEOUT):
            return fail("setup's first question never appeared", term)
        if not term.still_running_after(3):
            return fail("setup quit at the first question instead of waiting for an answer", term)
        term.type("not a website\r")
        if not term.expect("Please enter a website", STEP_TIMEOUT):
            return fail("typed keys didn't reach setup", term)
        term.type("\x03")
        if not term.expect("Setup cancelled", STEP_TIMEOUT):
            return fail("Ctrl-C didn't give the clean cancel message", term)
        print("OK: installed from a release tag, setup waited, read keys, and cancelled cleanly")
        return 0
    finally:
        term.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
