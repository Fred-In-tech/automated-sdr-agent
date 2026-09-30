"""Make sure the arrow-key prompts can actually read the keyboard.

The one-line installer runs `curl ... | bash`, so bash's stdin is the download. It starts setup
with `sdr setup </dev/tty` to reach the keyboard. On macOS that breaks prompt_toolkit (which
questionary is built on): it waits for keys with kqueue, and kqueue can't watch the /dev/tty
alias. prompt_toolkit reports that as EOFError, exactly what Ctrl-D raises, so setup used to stop
at its first question with "Setup cancelled" before anyone typed a thing.

The terminal's real device (e.g. /dev/ttys003) can be watched, and stdout/stderr usually point
at it, so we read the keyboard from there instead. If that isn't possible either, the caller
falls back to simple typed prompts, which read /dev/tty without trouble.
"""

from __future__ import annotations

import os
import selectors

CONTROLLING_TTY = "/dev/tty"


def _is_windows() -> bool:
    return os.name == "nt"


def _pollable(fd: int) -> bool:
    """Can an event loop wait for keys on this fd? (What prompt_toolkit needs.)"""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
    except (OSError, ValueError):
        return False
    return True


def _swap_in(device: str) -> bool:
    """Put `device` on fd 0 when it can be watched. Returns whether it did."""
    try:
        fd = os.open(device, os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return False
    try:
        if not _pollable(fd):
            return False
        os.dup2(fd, 0)   # sys.stdin keeps using fd 0, so everything reads the new device
        return True
    finally:
        os.close(fd)


def stdin_watchable() -> bool:
    """True when arrow-key prompts can read stdin, after repairing it if needed.

    Only touches fd 0 when it is a terminal nobody can watch (the /dev/tty case above).
    Windows consoles work differently and are left alone."""
    if _is_windows() or not os.isatty(0) or _pollable(0):
        return True
    for fd in (1, 2):
        try:
            device = os.ttyname(fd)
        except OSError:
            continue
        if device != CONTROLLING_TTY and _swap_in(device):
            return True
    return False
