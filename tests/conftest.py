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
