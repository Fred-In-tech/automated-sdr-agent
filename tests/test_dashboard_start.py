"""Offline tests for `sdr dashboard` finding or starting the dashboard (dashboard.server.start_dashboard).

Everything runs on ephemeral 127.0.0.1 ports with a temp data folder; the browser opener is a
mock, so nothing is opened and nothing outside the temp dir changes. Split out of
tests/test_dashboard.py, which covers the running server itself.
"""

import http.server
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

import core.db as db
from core import dashboard_auth as auth
from dashboard import server

POLL = 0.02  # serve_forever poll interval: keeps shutdown() (and so each test) fast


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OtherAppHandler(http.server.BaseHTTPRequestHandler):
    """Some unrelated local web app that happens to use one of our ports."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"hi")

    def log_message(self, *args):
        pass


class _SpoofHandler(http.server.BaseHTTPRequestHandler):
    """A local server (another user's install, or a phishing page) that claims to be our dashboard:
    it sends our Server header and answers the probe endpoint, but can't know this install's token."""

    def version_string(self) -> str:
        return server.SERVER_NAME

    def do_GET(self):
        body = json.dumps({"name": server.SERVER_NAME, "proof": "0" * 64}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ── Instance token (data/dashboard.json) ─────────────────────────────────────


class TestInstanceToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "data", "dashboard.json")

    def test_token_is_created_once_and_reused(self):
        token = auth.ensure_instance_token(self.path)
        self.assertGreaterEqual(len(token), 43)  # token_urlsafe(32)
        self.assertEqual(auth.ensure_instance_token(self.path), token)
        self.assertEqual(auth.instance_token(self.path), token)

    def test_reading_never_creates_the_file(self):
        self.assertIsNone(auth.instance_token(self.path))
        self.assertFalse(os.path.exists(self.path))

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_file_and_folder_are_owner_only(self):
        self.addCleanup(os.umask, os.umask(0o022))
        auth.ensure_instance_token(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)

    def test_broken_or_foreign_files_are_replaced(self):
        os.makedirs(os.path.dirname(self.path))
        for content in ("{not json", "[1, 2]", '{"token": 5}', '{"token": "short"}', ""):
            with self.subTest(content=content):
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.assertIsNone(auth.instance_token(self.path))
                self.assertGreaterEqual(len(auth.ensure_instance_token(self.path)), 43)

    def test_port_is_recorded_next_to_the_token(self):
        token = auth.ensure_instance_token(self.path)
        auth.record_instance_port(8083, self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"token": token, "port": 8083})
        auth.record_instance_port(8080, os.path.join(self.tmp.name, "nowhere.json"))  # no token: no file

    def test_proof_is_bound_to_the_token_and_the_nonce(self):
        token, other = auth.ensure_instance_token(self.path), "another-installs-token-" + "x" * 30
        nonce, later = auth.new_nonce(), auth.new_nonce()
        self.assertNotEqual(nonce, later)
        self.assertTrue(auth.proof_matches(token, nonce, auth.instance_proof(token, nonce)))
        self.assertFalse(auth.proof_matches(other, nonce, auth.instance_proof(token, nonce)))
        self.assertFalse(auth.proof_matches(token, later, auth.instance_proof(token, nonce)))  # no replay
        for proof in ("", None, 12, "0" * 64):
            self.assertFalse(auth.proof_matches(token, nonce, proof))
        self.assertFalse(auth.proof_matches(None, nonce, auth.instance_proof(token, nonce)))

    def test_nonce_must_be_a_url_safe_token_of_sensible_length(self):
        self.assertTrue(auth.is_nonce(auth.new_nonce()))
        for bad in ("", "short", "x" * 200, "has space", "bad!chars", None, 42):
            self.assertFalse(auth.is_nonce(bad))


# ── start_dashboard ──────────────────────────────────────────────────────────


class TestStartDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.opener = mock.Mock()
        self.servers = []
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(server, "configured_password_hash", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def serve(self, httpd):
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": POLL}, daemon=True).start()
        self.servers.append(httpd)
        return httpd.server_address[1]

    def start(self, port: int, **kwargs) -> tuple[dict, str]:
        with redirect_stdout(io.StringIO()) as out:
            result = server.start_dashboard(port, opener=self.opener, **kwargs)
        if result["server"] is not None:
            self.servers.append(result["server"])
        return result, out.getvalue()

    def test_finds_a_dashboard_that_is_already_running(self):
        port = self.serve(server.make_server(0, password_hash=None))
        self.assertEqual(server.find_running_dashboard([free_port(), port]), port)

    def test_ignores_other_web_apps_and_closed_ports(self):
        other = self.serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OtherAppHandler))
        self.assertIsNone(server.find_running_dashboard([other, free_port()]))

    def test_opens_the_running_dashboard_instead_of_starting_another(self):
        port = self.serve(server.make_server(0, password_hash=None))
        result, _out = self.start(port, ports=[port])
        self.assertTrue(result["already_running"])
        self.assertEqual(result["url"], f"http://localhost:{port}")
        self.assertIsNone(result["server"])
        self.opener.assert_called_once_with(result["url"])

    def test_a_server_that_only_spoofs_our_server_header_is_never_opened(self):
        """Another user's install (or a phishing page) on 8080 must not receive our password."""
        spoof = self.serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SpoofHandler))
        auth.ensure_instance_token()  # this install has a token; the spoof can't know it
        self.assertIsNone(server.find_running_dashboard([spoof]))
        result, out = self.start(spoof, ports=[spoof, 0], background=True)
        self.assertFalse(result["already_running"])
        self.assertNotEqual(result["port"], spoof)
        self.opener.assert_called_once_with(result["url"])  # ours, never the spoof's
        self.assertIn(f"Port {spoof} is used by another program", out)
        self.assertIn(f"port {result['port']}", out)

    def test_another_installs_dashboard_is_not_reused(self):
        """Two installs on one computer each have their own data/dashboard.json token."""
        auth.ensure_instance_token()
        other_install = tempfile.TemporaryDirectory()
        self.addCleanup(other_install.cleanup)
        with mock.patch.object(db, "DB_DIR", other_install.name):
            port = self.serve(server.make_server(0, password_hash=None))
        self.assertIsNone(server.find_running_dashboard([port]))
        self.assertEqual(server.find_running_dashboard([port], token=auth.instance_token(
            os.path.join(other_install.name, auth.INSTANCE_FILE))), port)

    def test_without_a_token_nothing_is_recognised(self):
        port = self.serve(server.make_server(0, password_hash=None))
        os.remove(os.path.join(self.tmp.name, auth.INSTANCE_FILE))
        self.assertIsNone(server.find_running_dashboard([port]))

    def test_instance_file_is_private_and_records_the_port(self):
        result, _out = self.start(0, ports=[0], background=True)
        path = os.path.join(self.tmp.name, auth.INSTANCE_FILE)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["port"], result["port"])
        self.assertEqual(data["token"], result["server"].instance_token)
        self.assertNotIn(data["token"], result["url"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_no_port_message_when_the_wanted_port_is_free(self):
        _result, out = self.start(0, ports=[0], background=True)
        self.assertNotIn("used by another program", out)

    def test_background_mode_serves_from_a_thread_and_opens_the_browser(self):
        result, _out = self.start(0, ports=[0], background=True)
        self.assertFalse(result["already_running"])
        self.opener.assert_called_once_with(result["url"])
        self.assertEqual(server.find_running_dashboard([result["port"]]), result["port"])

    def test_open_browser_false_does_not_open_anything(self):
        self.start(0, open_browser=False, ports=[0], background=True)
        self.opener.assert_not_called()

    def test_foreground_mode_stops_cleanly_on_ctrl_c(self):
        with mock.patch.object(server.DashboardServer, "serve_forever", side_effect=KeyboardInterrupt):
            result, out = self.start(0, ports=[0])
        self.assertIsNone(result["server"])
        self.assertIn(result["url"], out)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", result["port"]), timeout=1).close()

    def test_all_ports_busy_raises_a_clear_error(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            with mock.patch.object(server, "PROBE_TIMEOUT_SECONDS", 0.1), redirect_stdout(io.StringIO()):
                with self.assertRaises(server.DashboardUnavailable):
                    server.start_dashboard(0, ports=[blocker.getsockname()[1]], opener=self.opener)
        finally:
            blocker.close()
        self.opener.assert_not_called()

    def test_start_server_keeps_working_as_a_blocking_entry_point(self):
        with mock.patch.object(server, "start_dashboard") as start:
            server.start_server(8090)
        start.assert_called_once_with(8090, open_browser=False)


if __name__ == "__main__":
    unittest.main()
