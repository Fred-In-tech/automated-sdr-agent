"""Suite-wide guard: tests never open the real lead database in data/.

Individual tests usually point core.db at their own temp file; this catches the ones that
forget. Without it a test can pass on the author's machine only because data/automations.db
happens to exist there (and quietly read or change real leads), then fail on a fresh checkout.
RUN_LIVE_TESTS=1 turns the guard off for the opt-in live tests.
"""

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _throwaway_database():
    if os.getenv("RUN_LIVE_TESTS") == "1":
        yield
        return
    import core.db

    with tempfile.TemporaryDirectory() as tmp:
        patch = pytest.MonkeyPatch()
        patch.setattr(core.db, "DB_DIR", tmp)
        patch.setattr(core.db, "DB_PATH", os.path.join(tmp, "automations.db"))
        try:
            yield
        finally:
            patch.undo()


@pytest.fixture(autouse=True, scope="session")
def _no_real_dns_or_deliverability_file():
    """Deliverability checks never query real DNS or write data/deliverability.json in tests:
    lookups report "DNS unavailable" (status unknown, no cap) unless a test injects a resolver."""
    if os.getenv("RUN_LIVE_TESTS") == "1":
        yield
        return
    import core.deliverability as dv

    def offline(name, rtype):
        raise dv.DNSUnavailable("tests are offline")

    with tempfile.TemporaryDirectory() as tmp:
        patch = pytest.MonkeyPatch()
        patch.setattr(dv, "_resolve", offline)
        patch.setattr(dv, "_default_path", lambda: os.path.join(tmp, "deliverability.json"))
        patch.setattr(dv.refresh_if_stale, "__kwdefaults__", {**dv.refresh_if_stale.__kwdefaults__, "resolve": offline})
        patch.setattr(dv.run_checks, "__kwdefaults__", {**dv.run_checks.__kwdefaults__, "resolve": offline})
        try:
            yield
        finally:
            patch.undo()
