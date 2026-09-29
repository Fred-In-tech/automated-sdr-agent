"""Offline tests for the dashboard's password, sessions, CSRF protection and security headers.

A real dashboard server runs in a thread on an ephemeral 127.0.0.1 port. The database,
profile and update-status file are temporary, the real .env is never read, and background
bot runs are replaced by a mock, so nothing is sent and nothing outside the temp dir changes.
"""

import base64
import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import core.config as config
import core.db as db
from core import dashboard_auth as auth
from core.config import EXAMPLE_PROFILE_PATH
from dashboard import server

PASSWORD = "correct horse battery"
FAST_ITERATIONS = 1_000  # real hashes use 600k iterations; the server reads the count from the hash
POLL = 0.02  # serve_forever poll interval: keeps shutdown() (and so each test) fast
EXTERNAL_RESOURCE_RE = re.compile(r"<(?:link|script|iframe)\b[^>]*\b(?:href|src)=[\"']https?://", re.IGNORECASE)


class FakeClock:
    """A controllable time source so expiry/lockout tests never sleep."""

    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def external_resources(page: str) -> list[str]:
    """Stylesheets, scripts and frames a page would fetch from another site (a favicon or logo
    <link rel="icon"> from the business's own website is allowed)."""
    return [tag for tag in EXTERNAL_RESOURCE_RE.findall(page) if 'rel="icon"' not in tag]


def write_profile(directory: str) -> str:
    """The example profile plus a homepage, a logo and a custom brand colour."""
    with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("[sender]\n", '[sender]\nwebsite = "https://acme.test"\n', 1)
    text = text.replace('\nlogo_url = ""', '\nlogo_url = "https://acme.test/logo.png"', 1)
    text = text.replace('brand_color = "#3B82F6"', 'brand_color = "#FF5500"', 1)
    path = os.path.join(directory, "profile.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── Password hashing ─────────────────────────────────────────────────────────


class TestPasswordHashing(unittest.TestCase):
    def test_hash_is_pbkdf2_sha256_with_600k_iterations_and_16_byte_salt(self):
        stored = auth.hash_password("s3cret-pass")
        algorithm, iterations, salt_b64, hash_b64 = stored.split("$")
        self.assertEqual(algorithm, "pbkdf2_sha256")
        self.assertEqual(int(iterations), 600_000)
        self.assertEqual(len(base64.b64decode(salt_b64)), 16)
        self.assertEqual(len(base64.b64decode(hash_b64)), 32)

    def test_same_password_gets_a_fresh_salt_every_time(self):
        first = auth.hash_password(PASSWORD, iterations=FAST_ITERATIONS)
        second = auth.hash_password(PASSWORD, iterations=FAST_ITERATIONS)
        self.assertNotEqual(first, second)

    def test_verify_accepts_only_the_right_password(self):
        stored = auth.hash_password(PASSWORD, iterations=FAST_ITERATIONS)
        self.assertTrue(auth.verify_password(PASSWORD, stored))
        self.assertFalse(auth.verify_password(PASSWORD + " ", stored))
        self.assertFalse(auth.verify_password("", stored))

    def test_verify_handles_unicode_passwords(self):
        stored = auth.hash_password("café-☕-密码", iterations=FAST_ITERATIONS)
        self.assertTrue(auth.verify_password("café-☕-密码", stored))
        self.assertFalse(auth.verify_password("cafe-☕-密码", stored))

    def test_verify_rejects_malformed_or_foreign_hashes_without_raising(self):
        good = auth.hash_password(PASSWORD, iterations=FAST_ITERATIONS)
        _algo, _iters, salt, digest = good.split("$")
        for stored in (None, "", "plain-text", f"md5$1000${salt}${digest}",
                       f"pbkdf2_sha256$many${salt}${digest}", f"pbkdf2_sha256$0${salt}${digest}",
                       f"pbkdf2_sha256$-5${salt}${digest}", "pbkdf2_sha256$1000$!!!$???",
                       f"pbkdf2_sha256$1000${salt}${digest}$extra", f"pbkdf2_sha256$1000${salt}$"):
            with self.subTest(stored=stored):
                self.assertFalse(auth.verify_password(PASSWORD, stored))
                self.assertFalse(auth.is_valid_hash(stored))
        self.assertTrue(auth.is_valid_hash(good))

    def test_empty_password_cannot_be_hashed(self):
        with self.assertRaises(ValueError):
            auth.hash_password("")

    def test_password_problem_flags_short_passwords(self):
        self.assertIsNotNone(auth.password_problem("short"))
        self.assertIsNotNone(auth.password_problem("   "))
        self.assertIsNone(auth.password_problem("long enough pass"))

    def test_configured_password_hash_reads_the_env_file(self):
        stored = auth.hash_password(PASSWORD, iterations=FAST_ITERATIONS)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(auth.ENV_KEY, None)
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"SMTP_USER=me@acme.test\n{auth.ENV_KEY}={stored}\n")
            self.assertEqual(auth.configured_password_hash(env_path), stored)

    def test_configured_password_hash_is_none_when_not_set(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(auth.ENV_KEY, None)
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"{auth.ENV_KEY}=\n")
            self.assertIsNone(auth.configured_password_hash(env_path))
            self.assertIsNone(auth.configured_password_hash(os.path.join(tmp, "missing.env")))


# ── Sessions ─────────────────────────────────────────────────────────────────


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = auth.SessionStore(clock=self.clock)

    def test_sessions_last_12_hours_by_default(self):
        self.assertEqual(auth.SESSION_TTL_SECONDS, 12 * 3600)

    def test_new_session_has_unguessable_distinct_tokens(self):
        session = self.store.create()
        self.assertGreaterEqual(len(session.token), 43)  # token_urlsafe(32)
        self.assertGreaterEqual(len(session.csrf_token), 43)
        self.assertNotEqual(session.token, session.csrf_token)
        self.assertNotEqual(session.token, self.store.create().token)

    def test_session_is_valid_until_it_expires(self):
        session = self.store.create()
        self.clock.advance(auth.SESSION_TTL_SECONDS - 1)
        self.assertEqual(self.store.get(session.token), session)
        self.clock.advance(2)
        self.assertIsNone(self.store.get(session.token))
        self.assertEqual(len(self.store), 0)  # expired sessions are purged

    def test_unknown_or_missing_token_is_not_a_session(self):
        self.store.create()
        for token in (None, "", "not-a-real-token"):
            self.assertIsNone(self.store.get(token))

    def test_revoke_ends_the_session(self):
        session = self.store.create()
        self.store.revoke(session.token)
        self.store.revoke("unknown")  # harmless
        self.assertIsNone(self.store.get(session.token))

    def test_csrf_token_must_match_its_own_session(self):
        a, b = self.store.create(), self.store.create()
        self.assertTrue(self.store.check_csrf(a.token, a.csrf_token))
        self.assertFalse(self.store.check_csrf(a.token, b.csrf_token))
        self.assertFalse(self.store.check_csrf(a.token, ""))
        self.assertFalse(self.store.check_csrf(a.token, None))
        self.assertFalse(self.store.check_csrf("unknown", a.csrf_token))

    def test_oldest_sessions_are_dropped_past_the_cap(self):
        store = auth.SessionStore(clock=self.clock, max_sessions=2)
        first = store.create()
        self.clock.advance(1)
        second = store.create()
        self.clock.advance(1)
        third = store.create()
        self.assertIsNone(store.get(first.token))
        self.assertIsNotNone(store.get(second.token))
        self.assertIsNotNone(store.get(third.token))
        self.assertEqual(len(store), 2)


# ── Login rate limit ─────────────────────────────────────────────────────────


class TestLoginLimiter(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = auth.LoginLimiter(clock=self.clock)

    def test_four_failures_do_not_lock(self):
        for _ in range(4):
            self.limiter.record_failure()
        self.assertFalse(self.limiter.is_locked())
        self.assertEqual(self.limiter.retry_after(), 0)

    def test_fifth_failure_locks_login_for_60_seconds(self):
        for _ in range(5):
            self.limiter.record_failure()
        self.assertTrue(self.limiter.is_locked())
        self.assertEqual(self.limiter.retry_after(), 60)
        self.clock.advance(59)
        self.assertTrue(self.limiter.is_locked())
        self.clock.advance(1)
        self.assertFalse(self.limiter.is_locked())

    def test_count_starts_over_after_a_lockout(self):
        for _ in range(5):
            self.limiter.record_failure()
        self.clock.advance(60)
        for _ in range(4):
            self.limiter.record_failure()
        self.assertFalse(self.limiter.is_locked())

    def test_success_resets_failures(self):
        for _ in range(4):
            self.limiter.record_failure()
        self.limiter.record_success()
        for _ in range(4):
            self.limiter.record_failure()
        self.assertFalse(self.limiter.is_locked())


# ── The live server ──────────────────────────────────────────────────────────


class DashboardServerCase(unittest.TestCase):
    """Starts a dashboard on an ephemeral port. Subclasses set `password` (None = open mode)."""

    password: str | None = PASSWORD

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.run_task = mock.Mock()
        self.status_path = os.path.join(self.tmp.name, "update_status.json")
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(config, "PROFILE_PATH", write_profile(self.tmp.name)),
            mock.patch.object(server, "UPDATE_STATUS_PATH", self.status_path),
            mock.patch.object(server, "_run_task_safely", self.run_task),
        ]
        for p in self.patches:
            p.start()
        db.init_db()
        stored = auth.hash_password(self.password, iterations=FAST_ITERATIONS) if self.password else None
        self.httpd = server.make_server(
            0, password_hash=stored,
            sessions=auth.SessionStore(clock=self.clock), limiter=auth.LoginLimiter(clock=self.clock))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": POLL}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # -- HTTP helpers --------------------------------------------------------

    def request(self, method: str, path: str, body: str | bytes | None = None,
                headers: dict | None = None) -> tuple[int, http.client.HTTPMessage, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers={"Host": f"localhost:{self.port}", **(headers or {})})
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def get(self, path: str, headers: dict | None = None):
        return self.request("GET", path, headers=headers)

    def get_json(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        status, _headers, body = self.get(path, headers)
        return status, json.loads(body)

    def post_form(self, path: str, fields: dict, headers: dict | None = None):
        return self.request("POST", path, urllib.parse.urlencode(fields),
                            {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def post_json(self, path: str, data, headers: dict | None = None):
        return self.request("POST", path, json.dumps(data), {"Content-Type": "application/json", **(headers or {})})

    @staticmethod
    def cookie(token: str) -> dict:
        return {"Cookie": f"{server.SESSION_COOKIE}={token}"}

    def login(self, password: str = PASSWORD) -> str:
        status, headers, _body = self.post_form("/login", {"password": password})
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def csrf_for(self, token: str) -> str:
        status, data = self.get_json("/api/session", self.cookie(token))
        self.assertEqual(status, 200)
        return data["csrf_token"]


class TestPasswordProtectedDashboard(DashboardServerCase):
    def test_pages_redirect_to_login_without_a_session(self):
        for path in ("/", "/index.html"):
            status, headers, _ = self.get(path)
            self.assertEqual((status, headers["Location"]), (303, "/login"))

    def test_api_answers_401_without_a_session(self):
        for path in ("/api/stats", "/api/leads", "/api/profile", "/api/session", "/api/update-status"):
            with self.subTest(path=path):
                status, data = self.get_json(path)
                self.assertEqual(status, 401)
                self.assertEqual(data["error"], "login_required")

    def test_login_page_is_branded_from_the_profile(self):
        status, headers, body = self.get("/login")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Acme Scheduling", page)
        self.assertIn("https://acme.test/logo.png", page)
        self.assertIn("#FF5500", page)
        self.assertIn('type="password"', page)
        self.assertIn('action="/login"', page)

    def test_login_page_loads_nothing_from_other_sites(self):
        """Shown before you sign in, so it must not fetch fonts or scripts from anywhere: the only
        outside resource is the business's own logo from its own website."""
        _status, _headers, body = self.get("/login")
        page = body.decode("utf-8")
        self.assertEqual(external_resources(page), [])
        self.assertNotIn("fonts.g", page)
        self.assertIn("system-ui", page)
        self.assertEqual(set(re.findall(r"https?://[^\"'\s<>]+", page)), {"https://acme.test/logo.png"})

    def test_wrong_password_is_rejected_without_a_cookie(self):
        status, headers, body = self.post_form("/login", {"password": "nope"})
        self.assertEqual(status, 401)
        self.assertIsNone(headers["Set-Cookie"])
        self.assertIn("Wrong password", body.decode("utf-8"))

    def test_right_password_sets_a_strict_http_only_session_cookie(self):
        status, headers, _ = self.post_form("/login", {"password": PASSWORD})
        self.assertEqual((status, headers["Location"]), (303, "/"))
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"{server.SESSION_COOKIE}="))
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, cookie)
        self.assertGreaterEqual(len(cookie.split(";")[0].split("=", 1)[1]), 43)

    def test_session_cookie_unlocks_pages_and_api(self):
        token = self.login()
        status, _headers, body = self.get("/", self.cookie(token))
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body)
        status, data = self.get_json("/api/stats", self.cookie(token))
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)

    def test_json_login_returns_the_csrf_token(self):
        status, headers, body = self.post_json("/login", {"password": PASSWORD})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["csrf_token"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])

    def test_json_login_with_wrong_password_is_401(self):
        status, _headers, body = self.post_json("/login", {"password": "nope"})
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["ok"])

    def test_api_session_reports_the_csrf_token_for_this_session(self):
        token = self.login()
        status, data = self.get_json("/api/session", self.cookie(token))
        self.assertEqual(status, 200)
        self.assertTrue(data["auth_required"])
        self.assertTrue(data["logged_in"])
        self.assertTrue(data["csrf_token"])

    def test_post_api_requires_the_matching_csrf_token(self):
        token = self.login()
        status, _h, _b = self.post_json("/api/run-bot", {"task": "leadgen"}, self.cookie(token))
        self.assertEqual(status, 403)
        status, _h, _b = self.post_json("/api/run-bot", {"task": "leadgen"},
                                        {**self.cookie(token), "X-CSRF-Token": "forged"})
        self.assertEqual(status, 403)
        self.run_task.assert_not_called()

        csrf = self.csrf_for(token)
        status, _h, body = self.post_json("/api/run-bot", {"task": "leadgen"},
                                          {**self.cookie(token), "X-CSRF-Token": csrf})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.assertTrue(wait_until(lambda: self.run_task.called))
        self.run_task.assert_called_once_with("leadgen")

    def test_csrf_token_from_another_session_is_rejected(self):
        mine, theirs = self.login(), self.login()
        status, _h, _b = self.post_json("/api/run-bot", {"task": "leadgen"},
                                        {**self.cookie(mine), "X-CSRF-Token": self.csrf_for(theirs)})
        self.assertEqual(status, 403)
        self.run_task.assert_not_called()

    def test_post_api_without_a_session_is_401(self):
        status, _h, _b = self.post_json("/api/run-bot", {"task": "leadgen"})
        self.assertEqual(status, 401)
        self.run_task.assert_not_called()

    def test_five_wrong_passwords_lock_login_for_a_minute(self):
        for _ in range(4):
            status, _h, _b = self.post_form("/login", {"password": "nope"})
            self.assertEqual(status, 401)
        status, _h, _b = self.post_form("/login", {"password": "nope"})
        self.assertEqual(status, 429)
        status, headers, body = self.post_form("/login", {"password": PASSWORD})  # right, but locked
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "60")
        self.assertIsNone(headers["Set-Cookie"])
        self.assertIn("Too many attempts", body.decode("utf-8"))
        self.clock.advance(60)
        status, _h, _b = self.post_form("/login", {"password": PASSWORD})
        self.assertEqual(status, 303)

    def test_logout_ends_the_session(self):
        token = self.login()
        status, headers, _ = self.get("/logout", self.cookie(token))
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        status, _data = self.get_json("/api/stats", self.cookie(token))
        self.assertEqual(status, 401)

    def test_session_expires_after_12_hours(self):
        token = self.login()
        self.clock.advance(auth.SESSION_TTL_SECONDS + 1)
        status, _data = self.get_json("/api/stats", self.cookie(token))
        self.assertEqual(status, 401)

    def test_login_page_sends_signed_in_users_home(self):
        token = self.login()
        status, headers, _ = self.get("/login", self.cookie(token))
        self.assertEqual((status, headers["Location"]), (303, "/"))

    def test_forged_session_cookie_is_not_a_session(self):
        status, _data = self.get_json("/api/stats", self.cookie("forged-token"))
        self.assertEqual(status, 401)
        status, _data = self.get_json("/api/stats", {"Cookie": 'junk="unterminated; ;;'})
        self.assertEqual(status, 401)

    def test_login_from_another_website_is_refused(self):
        status, headers, _ = self.post_form("/login", {"password": PASSWORD}, {"Origin": "https://evil.test"})
        self.assertEqual(status, 403)
        self.assertIsNone(headers["Set-Cookie"])

    def test_login_from_another_local_port_is_refused(self):
        status, _h, _b = self.post_form("/login", {"password": PASSWORD}, {"Origin": "http://localhost:1"})
        self.assertEqual(status, 403)

    def test_same_origin_login_is_accepted(self):
        status, _h, _b = self.post_form("/login", {"password": PASSWORD},
                                        {"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 303)

    def test_dns_rebinding_host_is_refused(self):
        status, _h, _b = self.get("/login", {"Host": f"evil.test:{self.port}"})
        self.assertEqual(status, 403)

    def test_oversized_login_body_is_rejected(self):
        status, _h, _b = self.post_form("/login", {"password": "x" * (server.MAX_LOGIN_BODY_BYTES + 10)})
        self.assertEqual(status, 413)

    def test_login_form_with_too_many_fields_is_a_bad_request(self):
        fields = {f"field{i}": "x" for i in range(20)}
        status, headers, _ = self.post_form("/login", {"password": PASSWORD, **fields})
        self.assertEqual(status, 400)
        self.assertIsNone(headers["Set-Cookie"])

    def test_json_lockout_says_when_to_retry(self):
        for _ in range(4):
            self.post_json("/login", {"password": "nope"})
        status, headers, body = self.post_json("/login", {"password": "nope"})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["retry_after"], 60)
        self.assertEqual(headers["Retry-After"], "60")

    def test_unsupported_login_content_type_is_rejected(self):
        status, _h, _b = self.request("POST", "/login", "password=x", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


class TestSecurityHeaders(DashboardServerCase):
    CSP_DIRECTIVES = (
        "default-src 'self'", "img-src 'self' https: data:", "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'",
    )

    def assert_security_headers(self, headers):
        csp = headers["Content-Security-Policy"] or ""
        for directive in self.CSP_DIRECTIVES:
            self.assertIn(directive, csp)
        self.assertNotIn("fonts.", csp)  # no third-party fonts: default-src 'self' covers font-src
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_every_kind_of_response_carries_security_headers(self):
        token = self.login()
        for path, headers in (("/login", {}), ("/", {}), ("/api/stats", {}), ("/nope", self.cookie(token)),
                              ("/", self.cookie(token)), ("/api/pipeline", self.cookie(token))):
            with self.subTest(path=path, signed_in=bool(headers)):
                _status, response_headers, _body = self.get(path, headers)
                self.assert_security_headers(response_headers)

    def test_forbidden_responses_carry_security_headers_too(self):
        status, headers, _ = self.get("/login", {"Host": "evil.test"})
        self.assertEqual(status, 403)
        self.assert_security_headers(headers)

    def test_api_responses_are_never_cached(self):
        token = self.login()
        for headers in ({}, self.cookie(token)):
            _status, response_headers, _ = self.get("/api/session", headers)
            self.assertEqual(response_headers["Cache-Control"], "no-store")

    def test_server_header_does_not_reveal_the_python_version(self):
        _status, headers, _ = self.get("/login")
        self.assertTrue(headers["Server"].startswith(server.SERVER_NAME))
        self.assertNotIn("Python", headers["Server"])


class TestOpenDashboard(DashboardServerCase):
    """No DASHBOARD_PASSWORD_HASH: behaves as before (localhost-only + Host/Origin checks)."""

    password = None

    def test_pages_and_api_work_without_logging_in(self):
        status, _headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body)
        status, _data = self.get_json("/api/stats")
        self.assertEqual(status, 200)

    def test_session_endpoint_says_no_password_is_set(self):
        status, data = self.get_json("/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(data["auth_required"])
        self.assertEqual(data["csrf_token"], "")

    def test_run_bot_needs_no_csrf_token(self):
        status, _h, body = self.post_json("/api/run-bot", {"task": "pipeline"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["task"], "pipeline")
        self.assertTrue(wait_until(lambda: self.run_task.called))
        self.run_task.assert_called_once_with("pipeline")

    def test_run_bot_still_requires_json(self):
        status, _h, _b = self.request("POST", "/api/run-bot", "task=pipeline",
                                      {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 403)
        self.run_task.assert_not_called()

    def test_unknown_task_is_rejected(self):
        status, _h, body = self.post_json("/api/run-bot", {"task": "rm -rf"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["success"])
        self.run_task.assert_not_called()

    def test_cross_site_post_is_still_blocked(self):
        status, _h, _b = self.post_json("/api/run-bot", {"task": "pipeline"}, {"Origin": "https://evil.test"})
        self.assertEqual(status, 403)
        self.run_task.assert_not_called()

    def test_login_page_just_goes_home(self):
        status, headers, _ = self.get("/login")
        self.assertEqual((status, headers["Location"]), (303, "/"))

    def test_security_headers_are_still_sent(self):
        _status, headers, _ = self.get("/")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


class TestDashboardApi(DashboardServerCase):
    password = None

    def test_update_status_is_empty_when_never_checked(self):
        self.assertEqual(self.get_json("/api/update-status"), (200, {}))

    def test_update_status_returns_what_the_updater_wrote(self):
        payload = {"checked_at": "2026-09-26T08:30:00+00:00", "current": "1.0.0", "latest": "1.1.0",
                   "available": True, "mode": "notify", "notes": "- Faster", "last_result": "none"}
        with open(self.status_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        self.assertEqual(self.get_json("/api/update-status"), (200, payload))

    def test_update_status_ignores_a_broken_file(self):
        for content in ("{not json", "[1, 2]", ""):
            with self.subTest(content=content):
                with open(self.status_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.assertEqual(self.get_json("/api/update-status"), (200, {}))

    def test_profile_includes_the_website_and_brand_design(self):
        status, data = self.get_json("/api/profile")
        self.assertEqual(status, 200)
        self.assertTrue(data["configured"])
        self.assertEqual(data["website"], "https://acme.test")
        self.assertEqual(data["design"]["logo_url"], "https://acme.test/logo.png")
        self.assertEqual(data["design"]["brand_color"], "#FF5500")

    def test_profile_website_falls_back_to_the_product_url(self):
        with open(EXAMPLE_PROFILE_PATH, "r", encoding="utf-8") as f:
            example = f.read()
        with open(config.PROFILE_PATH, "w", encoding="utf-8") as f:
            f.write(example)
        status, data = self.get_json("/api/profile")
        self.assertEqual(status, 200)
        self.assertEqual(data["website"], data["product_url"])


class TestImportLeads(DashboardServerCase):
    password = None
    CSV = "email,first_name,company\nsam@acme.test,Sam,Acme\nnot-an-email,,\n"

    def setUp(self):
        super().setUp()
        from core import lead_import
        self.extra = [mock.patch.object(lead_import, "verify_lead_email", lambda email: (True, "ok")),
                      mock.patch.object(lead_import, "load_do_not_contact", lambda: set())]
        for p in self.extra:
            p.start()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        super().tearDown()

    def leads(self) -> list:
        return self.get_json("/api/leads")[1]["leads"]

    def test_check_first_then_import(self):
        status, _h, body = self.post_json("/api/import-leads", {"csv": self.CSV, "dry_run": True})
        preview = json.loads(body)
        self.assertEqual((status, preview["imported"], preview["invalid"]), (200, 1, 1))
        self.assertEqual(self.leads(), [])
        status, _h, body = self.post_json("/api/import-leads", {"csv": self.CSV})
        self.assertEqual((status, json.loads(body)["imported"]), (200, 1))
        leads = self.leads()
        self.assertEqual([(l["email"], l["status"], l["first_name"]) for l in leads], [("sam@acme.test", "new", "Sam")])
        self.run_task.assert_not_called()          # importing never starts a send
        status, _h, body = self.post_json("/api/import-leads", {"csv": self.CSV})
        self.assertEqual(json.loads(body)["duplicates"], 1)

    def test_bad_requests_get_a_plain_message(self):
        for payload, fragment in (({}, "Choose a CSV"), ({"csv": 5}, "Choose a CSV"),
                                  ({"csv": "name\nSam\n"}, 'No "email" column')):
            status, _h, body = self.post_json("/api/import-leads", payload)
            self.assertEqual(status, 400)
            self.assertIn(fragment, json.loads(body)["message"])

    def test_oversized_files_are_refused(self):
        status, _h, _b = self.post_json("/api/import-leads", {"csv": "email\n" + "x" * (2 * 1024 * 1024)})
        self.assertEqual(status, 413)

    def test_only_json_posts_are_accepted(self):
        status, _h, _b = self.request("POST", "/api/import-leads", "csv=email",
                                      {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 403)

    def test_template_download(self):
        status, headers, body = self.get("/api/import-template")
        self.assertEqual(status, 200)
        self.assertIn("leads-template.csv", headers["Content-Disposition"])
        self.assertTrue(body.startswith(b"email,first_name"))


class TestImportNeedsSignIn(DashboardServerCase):
    def test_import_needs_a_session_and_csrf_token(self):
        status, _h, _b = self.post_json("/api/import-leads", {"csv": "email\nsam@acme.test\n"})
        self.assertEqual(status, 401)
        token = self.login()
        status, _h, _b = self.post_json("/api/import-leads", {"csv": "email\nsam@acme.test\n"}, self.cookie(token))
        self.assertEqual(status, 403)


class TestDashboardPage(unittest.TestCase):
    """Contract between index.html and the server (no browser needed)."""

    @classmethod
    def setUpClass(cls):
        with open(server.INDEX_PATH, "r", encoding="utf-8") as f:
            cls.page = f.read()

    def test_page_gets_its_csrf_token_and_sends_it_on_posts(self):
        self.assertIn("/api/session", self.page)
        self.assertIn("'X-CSRF-Token'", self.page)

    def test_every_api_call_goes_through_the_csrf_aware_wrapper(self):
        raw_calls = [line for line in self.page.splitlines() if "fetch('/api" in line or 'fetch("/api' in line]
        self.assertEqual(raw_calls, [])

    def test_page_sends_expired_sessions_back_to_login(self):
        self.assertIn("res.status === 401", self.page)
        self.assertIn("'/login'", self.page)

    def test_page_shows_update_pill_and_logout_link(self):
        self.assertIn("/api/update-status", self.page)
        self.assertIn('id="update-pill"', self.page)
        self.assertIn('href="/logout"', self.page)

    def test_page_loads_nothing_from_other_sites(self):
        """SECURITY.md lists everything the tool connects to; Google Fonts isn't on it (and every
        page open would send the user's IP to Google), so the page uses the system font stack."""
        self.assertEqual(external_resources(self.page), [])
        self.assertNotIn("fonts.g", self.page)
        self.assertNotIn("'Inter'", self.page)
        self.assertIn("system-ui", self.page)
        self.assertNotIn("<script src=", self.page)  # script-src is 'self' + inline only


# ── Recognising our own dashboard (`sdr dashboard` port reuse) ──────────────


class TestInstanceEndpoint(DashboardServerCase):
    """`sdr dashboard` proves a port is this install's dashboard with a nonce challenge, before
    any login. See tests/test_dashboard_start.py for the probing side."""

    def test_proof_is_answered_before_login_and_never_reveals_the_token(self):
        nonce = auth.new_nonce()
        status, headers, body = self.get(f"/api/instance?nonce={nonce}")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["proof"], auth.instance_proof(self.httpd.instance_token, nonce))
        self.assertNotIn(self.httpd.instance_token, body.decode("utf-8"))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_each_nonce_gets_its_own_answer(self):
        first, second = auth.new_nonce(), auth.new_nonce()
        _s, _h, body_a = self.get(f"/api/instance?nonce={first}")
        _s, _h, body_b = self.get(f"/api/instance?nonce={second}")
        self.assertNotEqual(json.loads(body_a)["proof"], json.loads(body_b)["proof"])

    def test_a_missing_or_malformed_nonce_is_a_bad_request(self):
        for query in ("", "?nonce=", "?nonce=short", "?nonce=" + "x" * 200, "?nonce=bad%20chars!",
                      "?" + "&".join(f"a{i}=1" for i in range(20)) + "&nonce=" + auth.new_nonce()):
            with self.subTest(query=query):
                status, _headers, _body = self.get("/api/instance" + query)
                self.assertEqual(status, 400)

    def test_probe_still_needs_a_local_host_header(self):
        status, _h, _b = self.get(f"/api/instance?nonce={auth.new_nonce()}", {"Host": "evil.test"})
        self.assertEqual(status, 403)

    def test_probe_cannot_be_posted_to_without_a_session(self):
        status, _h, _b = self.post_json(f"/api/instance?nonce={auth.new_nonce()}", {})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
