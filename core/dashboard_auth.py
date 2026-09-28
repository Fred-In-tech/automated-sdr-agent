"""Password, sessions and login rate-limiting for the local dashboard.

The dashboard only listens on 127.0.0.1, but anything else running on this computer (another
user account, a browser tab on a hostile site, a compromised dev tool) can still reach it. A
password turns "anyone on this machine" into "only you". Stdlib only, so it works everywhere
the rest of the tool does.

The password itself is never stored: `sdr setup` saves a salted PBKDF2 hash in .env as
DASHBOARD_PASSWORD_HASH (format `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`).
Sessions live in memory only, so restarting the dashboard signs everyone out — acceptable for
a single-user local tool, and it means no session secret ever touches the disk.
"""

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from core import config, db

ENV_KEY = "DASHBOARD_PASSWORD_HASH"
ALGORITHM = "pbkdf2_sha256"
# OWASP's 2023+ recommendation for PBKDF2-HMAC-SHA256. ~0.2-0.5 s per check on a laptop,
# which is invisible for one login and makes offline guessing of a leaked .env expensive.
ITERATIONS = 600_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8

SESSION_TTL_SECONDS = 12 * 3600  # a working day; after that, sign in again
MAX_SESSIONS = 50                # bounds memory if something keeps logging in
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 60

Clock = Callable[[], float]


# ── Password hashing ─────────────────────────────────────────────────────────


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    """Salted PBKDF2-SHA256 hash of `password`, in the DASHBOARD_PASSWORD_HASH format.

    `iterations` is only lowered by tests; the count is stored in the hash so verification
    keeps working if the default is raised in a later release.
    """
    if not password:
        raise ValueError("The dashboard password can't be empty.")
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def _parse_hash(stored: str | None) -> tuple[int, bytes, bytes] | None:
    """(iterations, salt, digest) from a stored hash, or None if it isn't one of ours."""
    if not isinstance(stored, str):
        return None
    parts = stored.strip().split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return None
    try:
        iterations = int(parts[1])
        salt = base64.b64decode(parts[2], validate=True)
        digest = base64.b64decode(parts[3], validate=True)
    except (ValueError, binascii.Error):
        return None
    if iterations < 1 or not salt or not digest:
        return None
    return iterations, salt, digest


def is_valid_hash(stored: str | None) -> bool:
    """True if `stored` is a well-formed DASHBOARD_PASSWORD_HASH (lets `sdr doctor` warn early)."""
    return _parse_hash(stored) is not None


def verify_password(password: str, stored: str | None) -> bool:
    """Check a login attempt. Never raises: a malformed hash simply matches nothing
    (fail closed), and the comparison is constant-time so timing leaks nothing."""
    parsed = _parse_hash(stored)
    if parsed is None or not isinstance(password, str):
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    """Why a new dashboard password isn't acceptable, or None if it's fine (for the setup wizard)."""
    if len((password or "").strip()) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    return None


def configured_password_hash(env_path: str | None = None) -> str | None:
    """DASHBOARD_PASSWORD_HASH from the environment or .env, or None when no password is set.

    Returned as-is even if malformed: the server then fails closed (nothing can log in)
    instead of silently running without a password the user thinks they set.
    """
    if env_path:
        config.load_env_file(env_path)
    else:
        config.load_env_file()
    return os.environ.get(ENV_KEY, "").strip() or None


# ── Instance token: which port is *our* dashboard? ──────────────────────────
#
# `sdr dashboard` reuses a dashboard that is already running instead of starting a second one.
# It used to trust a `Server: AutomatedSDR` header, which any local program can send: on a shared
# computer another account's install on port 8080 would get your browser, and then your password.
# Now each install has a random token in data/dashboard.json (owner-only), and a running
# dashboard proves it holds the same token by answering a fresh nonce with HMAC(token, nonce).
# The token never travels and an answer fits no other nonce, so there is nothing to replay.

INSTANCE_FILE = "dashboard.json"
INSTANCE_TOKEN_BYTES = 32
NONCE_BYTES = 16
_MIN_TOKEN_LENGTH = 32
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def instance_path() -> str:
    """data/dashboard.json, next to the database (so it moves with AUTOMATIONS_DB_PATH)."""
    return os.path.join(db.DB_DIR, INSTANCE_FILE)


def _read_instance(path: str) -> dict:
    """{"token", "port"} from the file, or {} if it is missing, unreadable or not ours."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or len(token) < _MIN_TOKEN_LENGTH:
        return {}
    port = data.get("port")
    return {"token": token, "port": port if isinstance(port, int) and not isinstance(port, bool) else None}


def _write_instance(path: str, data: dict) -> None:
    """Best effort: without the file the dashboard still runs, a later `sdr dashboard` just
    can't recognise it and starts another one on the next port."""
    try:
        db.ensure_private_dir(os.path.dirname(os.path.abspath(path)))
        with db.open_private(path, encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def instance_token(path: str | None = None) -> str | None:
    """This install's token, or None when no dashboard has started here yet. Never creates it."""
    return _read_instance(path or instance_path()).get("token")


def ensure_instance_token(path: str | None = None) -> str:
    """This install's token, created (owner-only) the first time a dashboard starts."""
    path = path or instance_path()
    token = instance_token(path)
    if token:
        return token
    token = secrets.token_urlsafe(INSTANCE_TOKEN_BYTES)
    _write_instance(path, {"token": token, "port": None})
    return token


def record_instance_port(port: int, path: str | None = None) -> None:
    """Note which port the dashboard is on (informational: the token is what gets checked)."""
    path = path or instance_path()
    data = _read_instance(path)
    if data:
        _write_instance(path, {**data, "port": port})


def new_nonce() -> str:
    return secrets.token_urlsafe(NONCE_BYTES)


def is_nonce(value) -> bool:
    return isinstance(value, str) and _NONCE_RE.match(value) is not None


def instance_proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def proof_matches(token: str | None, nonce: str, proof) -> bool:
    """True only if `proof` is HMAC(token, nonce): the server that sent it holds our token."""
    if not token or not is_nonce(nonce) or not isinstance(proof, str):
        return False
    return hmac.compare_digest(instance_proof(token, nonce), proof)


# ── Sessions ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Session:
    token: str        # value of the sdr_session cookie (HttpOnly, so page scripts never see it)
    csrf_token: str   # sent back by the page in X-CSRF-Token on every POST
    created_at: float
    expires_at: float


class SessionStore:
    """In-memory sessions for one dashboard process. Thread-safe (the server is threaded)."""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS, max_sessions: int = MAX_SESSIONS,
                 clock: Clock = time.time):
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._sessions)

    def _purge_expired(self) -> None:
        now = self._clock()
        self._sessions = {t: s for t, s in self._sessions.items() if s.expires_at > now}

    def create(self) -> Session:
        now = self._clock()
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now + self._ttl)
        with self._lock:
            self._purge_expired()
            newest_first = sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)
            kept = newest_first[:max(self._max - 1, 0)]  # make room: the oldest sign-ins go first
            self._sessions = {s.token: s for s in kept} | {session.token: session}
        return session

    def get(self, token: str | None) -> Session | None:
        """The live session for this cookie value, or None (missing, unknown or expired)."""
        if not token:
            return None
        with self._lock:
            self._purge_expired()
            return self._sessions.get(token)

    def revoke(self, token: str | None) -> None:
        with self._lock:
            self._sessions = {t: s for t, s in self._sessions.items() if t != token}

    def check_csrf(self, token: str | None, csrf_token: str | None) -> bool:
        """True only if `csrf_token` belongs to the session identified by `token`."""
        session = self.get(token)
        if session is None or not csrf_token:
            return False
        return hmac.compare_digest(session.csrf_token.encode("utf-8"), csrf_token.encode("utf-8"))


# ── Login rate limit ─────────────────────────────────────────────────────────


class LoginLimiter:
    """After 5 wrong passwords, refuse all logins for 60 s, then allow 5 more tries.

    Per process rather than per IP: every client is 127.0.0.1 anyway. Combined with the
    PBKDF2 cost this caps online guessing at a handful of tries per minute.
    """

    def __init__(self, max_failures: int = MAX_LOGIN_FAILURES, lockout_seconds: float = LOCKOUT_SECONDS,
                 clock: Clock = time.monotonic):
        self._max_failures = max_failures
        self._lockout = lockout_seconds
        self._clock = clock
        self._failures = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def retry_after(self) -> int:
        """Whole seconds until login is allowed again (0 = allowed now)."""
        with self._lock:
            return max(0, math.ceil(self._locked_until - self._clock()))

    def is_locked(self) -> bool:
        return self.retry_after() > 0

    def record_failure(self) -> None:
        with self._lock:
            if self._locked_until and self._clock() >= self._locked_until:
                self._failures, self._locked_until = 0, 0.0  # lockout served: start over
            self._failures += 1
            if self._failures >= self._max_failures:
                self._locked_until = self._clock() + self._lockout

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._locked_until = 0, 0.0
