"""Local web dashboard for the SDR agent: pipeline stats, leads, emails and one-click runs.

Security model (why each layer exists):
- Listens on 127.0.0.1 only, and checks Host/Origin so other websites (and DNS-rebinding
  tricks) can't drive it from your browser.
- Optional password (DASHBOARD_PASSWORD_HASH in .env, set by `sdr setup`): every page and
  API call then needs the `sdr_session` cookie, and every POST also needs that session's
  CSRF token in X-CSRF-Token. Wrong passwords are rate-limited.
- Strict security headers (CSP, no framing, no sniffing, no referrer) on every response,
  and API responses are never cached. The pages fetch nothing from other sites.
- `sdr dashboard` only reuses a running dashboard that proves it belongs to this install
  (a nonce challenge against the token in data/dashboard.json), so the browser, and the
  password typed into it, never go to some other program that took port 8080.

Start it with `sdr dashboard` (see start_dashboard) or `python3 dashboard/server.py`.
"""

import argparse
import http.client
import http.server
import json
import os
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable, Iterable

# Adjust sys.path to import core and bots
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.db import init_db, get_db_stats, get_recent_logs, get_connection, log_event
from core.config import ProfileError, load_profile
from core.outreach_rules import resume_sending
from core.dashboard_auth import (LoginLimiter, Session, SessionStore, configured_password_hash,
                                 ensure_instance_token, instance_proof, instance_token, is_nonce,
                                 is_valid_hash, new_nonce, proof_matches, record_instance_port,
                                 verify_password)
from core.product import DISPLAY_NAME, version
from core.report import pipeline_stats
from dashboard.login_page import render_login_page
from runner import TASKS, run_task
from bots.leadgen_pipeline import export_leads_to_csv

PORT = 8080
HOST = "127.0.0.1"  # this computer only — never expose the dashboard to the network
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1")
DEFAULT_PORTS = tuple(range(8080, 8089))  # where `sdr dashboard` looks for / starts a dashboard
SERVER_NAME = "AutomatedSDR"  # Server header (no Python version); not proof of anything, see _is_dashboard
SESSION_COOKIE = "sdr_session"
AUTH_PATHS = ("/login", "/logout")
INSTANCE_PATH = "/api/instance"  # answered before login: how `sdr dashboard` recognises its own server
MAX_LOGIN_BODY_BYTES = 4 * 1024
MAX_API_BODY_BYTES = 64 * 1024
IMPORT_PATH = "/api/import-leads"
MAX_IMPORT_BODY_BYTES = 2 * 1024 * 1024   # a 1 MB CSV, JSON-escaped
DRAIN_LIMIT_BYTES = 4 * 1024 * 1024  # read (and drop) oversized bodies up to this, so the error reply isn't lost
PROBE_TIMEOUT_SECONDS = 0.5
MAX_PROBE_BYTES = 4 * 1024  # a probe answer is a short JSON object; whatever else answers isn't ours
UPDATE_STATUS_PATH = os.path.join(BASE_DIR, "data", "update_status.json")
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
SETTINGS_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.js")

CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",  # also covers fonts: the pages use the system font stack, nothing hosted elsewhere
    "img-src 'self' https: data:",  # brand logos live on the user's own website
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self' 'unsafe-inline'",  # the dashboard is a single self-contained page
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
))
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": with it browsers send `Origin: null` when the sign-in form is posted,
    # and the same-origin check then refuses the owner's own login. "same-origin" still tells
    # other websites nothing.
    "Referrer-Policy": "same-origin",
}

_FROM_ENV = object()  # make_server default: read DASHBOARD_PASSWORD_HASH from env/.env


class DashboardUnavailable(RuntimeError):
    """Every dashboard port is taken by something else."""


def _run_task_safely(task: str) -> None:
    try:
        run_task(task)
    except ProfileError as e:
        print(f"[Dashboard] {e}")


def _load_profile_or_none() -> dict | None:
    try:
        return load_profile()
    except ProfileError:
        return None


def read_update_status(path: str | None = None) -> dict:
    """data/update_status.json as written by the updater, or {} if missing/unreadable."""
    try:
        with open(path or UPDATE_STATUS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _split_host(value: str) -> tuple[str | None, int | None]:
    """(hostname, port) from a Host header value; (None, None) if it's malformed."""
    try:
        parts = urllib.parse.urlsplit("//" + value)
        return parts.hostname, parts.port
    except ValueError:
        return None, None


def _cookie_values(header: str | None, name: str) -> list[str]:
    """Every value of cookie `name`. Parsed by hand because other localhost apps share the
    cookie jar, and http.cookies gives up on the whole header at their first odd cookie."""
    values = []
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            values.append(value.strip().strip('"'))
    return values


def _session_cookie(token: str, max_age: int) -> str:
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"


CLEAR_SESSION_COOKIE = _session_cookie("", 0)


# ── Request handling ─────────────────────────────────────────────────────────


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    timeout = 30  # a stalled client can't pin a worker thread forever
    session: Session | None = None
    body: bytes = b""

    GET_ROUTES = {
        "/": "_index",
        "/index.html": "_index",
        "/api/session": "_api_session",
        "/api/profile": "_api_profile",
        "/api/pipeline": "_api_pipeline",
        "/api/stats": "_api_stats",
        "/api/leads": "_api_leads",
        "/api/emails": "_api_emails",
        "/api/social": "_api_social",
        "/api/logs": "_api_logs",
        "/api/update-status": "_api_update_status",
        "/api/export-csv": "_api_export_csv",
        "/api/import-template": "_api_import_template",
        "/api/settings": "_api_settings",
        "/settings.js": "_settings_script",
    }

    def log_message(self, format, *args):
        # Silence standard HTTP access logging to keep console clean
        pass

    def version_string(self) -> str:
        return SERVER_NAME  # no Python version in the Server header

    # -- responses ------------------------------------------------------------

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        if self._request_path().startswith(("/api/",) + AUTH_PATHS):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_body(self, body: bytes, content_type: str, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status: int = 200, headers: dict | None = None):
        self._send_body(json.dumps(data).encode("utf-8"), "application/json", status, headers)

    def _send_html(self, page: str, status: int = 200, headers: dict | None = None):
        self._send_body(page.encode("utf-8"), "text/html; charset=utf-8", status, headers)

    def _redirect(self, location: str, headers: dict | None = None):
        self._send_body(b"", "text/plain; charset=utf-8", 303, {"Location": location, **(headers or {})})

    # -- request helpers ------------------------------------------------------

    def _request_path(self) -> str:
        return urllib.parse.urlparse(getattr(self, "path", "") or "").path

    def _content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    def _is_local_request(self) -> bool:
        """Blocks other websites (and DNS-rebinding tricks) from driving the dashboard.

        Host must be localhost/127.0.0.1. A browser-sent Origin must be this very dashboard,
        port included: other apps on localhost count as "same-site" for cookies otherwise."""
        host, port = _split_host(self.headers.get("Host") or "")
        if host not in LOCAL_HOSTNAMES:
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
            return parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTNAMES and parsed.port == port
        except ValueError:
            return False

    def _read_body(self, limit: int) -> bytes | None:
        """The request body, or None after answering 400/413 (callers then just return).
        Oversized bodies are drained first so the client reliably sees the error reply."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.send_error(400, "Bad Content-Length")
            return None
        if length > limit:
            if length <= DRAIN_LIMIT_BYTES:
                self.rfile.read(length)
            self.send_error(413, "Request too large")
            return None
        return self.rfile.read(length) if length else b""

    def _current_session(self) -> Session | None:
        for token in _cookie_values(self.headers.get("Cookie"), SESSION_COOKIE):
            session = self.server.sessions.get(token)
            if session:
                return session
        return None

    # -- dispatch -------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self._request_path()
        if method == "POST":
            limit = (MAX_LOGIN_BODY_BYTES if path in AUTH_PATHS
                     else MAX_IMPORT_BODY_BYTES if path == IMPORT_PATH else MAX_API_BODY_BYTES)
            body = self._read_body(limit)
            if body is None:
                return
            self.body = body
        if not self._is_local_request():
            self.send_error(403, "Forbidden")
            return
        if path == "/login":
            self._login_page() if method == "GET" else self._login_submit()
            return
        if path == "/logout":
            self._logout()
            return
        if path == INSTANCE_PATH and method == "GET":
            self._api_instance()
            return
        self.session = self._current_session()
        if self.server.password_required and self.session is None:
            self._require_login(path)
            return
        if method == "POST":
            self._handle_post(path)
            return
        handler = self.GET_ROUTES.get(path)
        if handler is None:
            self.send_error(404, "File Not Found")
            return
        getattr(self, handler)()

    def _require_login(self, path: str) -> None:
        if path.startswith("/api/"):
            self._send_json({"error": "login_required", "login_url": "/login"}, 401)
        else:
            self._redirect("/login")

    def _api_instance(self) -> None:
        """Lets `sdr dashboard` recognise this install's own dashboard before anyone is signed in:
        the caller's fresh nonce is answered with HMAC(install token, nonce). The token itself
        never leaves the process and the answer fits no other nonce, so a spoofing server
        learns nothing it can reuse (core.dashboard_auth explains the threat)."""
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, max_num_fields=5)
        except ValueError:
            query = {}
        nonce = query.get("nonce", [""])[0]
        if not self.server.instance_token or not is_nonce(nonce):
            self._send_json({"error": "bad_nonce"}, 400)
            return
        self._send_json({"name": SERVER_NAME, "proof": instance_proof(self.server.instance_token, nonce)})

    # -- login / logout -------------------------------------------------------

    def _login_page(self) -> None:
        if not self.server.password_required or self._current_session():
            self._redirect("/")
            return
        self._send_html(render_login_page(_load_profile_or_none()))

    def _login_password(self) -> str | None:
        """The submitted password ("" if absent), or None after answering 400/415."""
        content_type = self._content_type()
        text = self.body.decode("utf-8", errors="replace")
        if content_type == "application/x-www-form-urlencoded":
            try:  # the login form has one field; a flood of them isn't a login attempt
                return urllib.parse.parse_qs(text, max_num_fields=10).get("password", [""])[0]
            except ValueError:
                self.send_error(400, "Bad form")
                return None
        if content_type == "application/json":
            try:
                data = json.loads(text or "{}")
            except ValueError:
                return ""
            value = data.get("password") if isinstance(data, dict) else ""
            return value if isinstance(value, str) else ""
        self.send_error(415, "Send the password as a form or JSON")
        return None

    def _login_submit(self) -> None:
        wants_json = self._content_type() == "application/json"
        if not self.server.password_required:
            if wants_json:
                self._send_json({"ok": True, "auth_required": False, "csrf_token": ""})
            else:
                self._redirect("/")
            return
        password = self._login_password()
        if password is None:
            return
        limiter = self.server.limiter
        if limiter.is_locked():
            self._login_refused(wants_json, locked=True)
            return
        if verify_password(password, self.server.password_hash):
            limiter.record_success()
            self._login_succeeded(wants_json)
            return
        limiter.record_failure()
        self._login_refused(wants_json, locked=limiter.is_locked())

    def _login_succeeded(self, wants_json: bool) -> None:
        session = self.server.sessions.create()
        cookie = {"Set-Cookie": _session_cookie(session.token, int(session.expires_at - session.created_at))}
        if wants_json:
            self._send_json({"ok": True, "auth_required": True, "csrf_token": session.csrf_token}, 200, cookie)
        else:
            self._redirect("/", cookie)

    def _login_refused(self, wants_json: bool, locked: bool) -> None:
        retry_after = self.server.limiter.retry_after() if locked else 0
        if locked:
            status, headers = 429, {"Retry-After": str(retry_after)}
            message = f"Too many attempts. Try again in {retry_after} seconds."
        else:
            status, headers, message = 401, {}, "Wrong password. Try again."
        if wants_json:
            self._send_json({"ok": False, "error": message, "retry_after": retry_after}, status, headers)
        else:
            self._send_html(render_login_page(_load_profile_or_none(), message), status, headers)

    def _logout(self) -> None:
        for token in _cookie_values(self.headers.get("Cookie"), SESSION_COOKIE):
            self.server.sessions.revoke(token)
        cookie = {"Set-Cookie": CLEAR_SESSION_COOKIE}
        if self.command == "POST" and self._content_type() == "application/json":
            self._send_json({"ok": True}, 200, cookie)
        else:
            self._redirect("/login" if self.server.password_required else "/", cookie)

    # -- POST /api/* ----------------------------------------------------------

    def _handle_post(self, path: str) -> None:
        if self._content_type() != "application/json":
            self.send_error(403, "Forbidden")
            return
        if self.server.password_required and not self.server.sessions.check_csrf(
                self.session.token, self.headers.get("X-CSRF-Token")):
            self._send_json({"success": False, "error": "csrf",
                             "message": "Your session changed. Reload the page and try again."}, 403)
            return
        if path == "/api/run-bot":
            self._api_run_bot()
            return
        if path == IMPORT_PATH:
            self._api_import_leads()
            return
        if path == "/api/settings":
            self._api_save_settings()
            return
        if path == "/api/resume-sending":
            self._api_resume_sending()
            return
        self.send_error(404, "Endpoint Not Found")

    def _api_import_leads(self) -> None:
        """Import leads from CSV text the page read from a file. Saves leads; sends nothing."""
        from core.lead_import import LeadImportError, import_csv_text
        try:
            payload = json.loads(self.body.decode("utf-8"))
            text, dry_run = payload["csv"], bool(payload.get("dry_run"))
            if not isinstance(text, str):
                raise ValueError
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            self._send_json({"success": False, "message": "Choose a CSV file to import."}, 400)
            return
        try:
            summary = import_csv_text(text, load_profile(), dry_run=dry_run)
        except (LeadImportError, ProfileError) as e:
            self._send_json({"success": False, "message": str(e)}, 400)
            return
        result = summary.as_dict()
        if not dry_run:
            log_event("Dashboard", "ImportLeads", "success",
                      f"Imported {result['imported']} leads ({result['skipped']} skipped).")
        self._send_json({"success": True, **result})

    def _api_run_bot(self) -> None:
        try:
            payload = json.loads(self.body.decode("utf-8")) if self.body else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        task = payload.get("task", "all") if isinstance(payload, dict) else "all"
        if task not in TASKS:
            self._send_json({"success": False, "message": f"Unknown task '{task}'"}, 400)
            return
        print(f"[Dashboard HTTP] Triggered task in background thread: '{task}'")
        # Execute task in background thread so the HTTP request returns immediately
        threading.Thread(target=_run_task_safely, args=(task,), daemon=True).start()
        self._send_json({"success": True, "message": f"Task '{task}' started in background", "task": task})

    # -- GET routes -----------------------------------------------------------

    def _index(self) -> None:
        try:
            with open(INDEX_PATH, "rb") as f:
                content = f.read()
        except OSError:
            self.send_error(404, "File Not Found")
            return
        self._send_body(content, "text/html; charset=utf-8")

    def _api_session(self) -> None:
        session = self.session
        self._send_json({
            "auth_required": self.server.password_required,
            "logged_in": session is not None,
            "csrf_token": session.csrf_token if session else "",
            "version": version(),
        })

    def _api_profile(self) -> None:
        try:
            profile = load_profile()
        except ProfileError as e:
            self._send_json({"configured": False, "error": str(e)})
            return
        sender, design = profile["sender"], profile.get("email_design", {})
        product_url = sender["product_url"]
        self._send_json({
            "configured": True,
            "product_name": sender["product_name"],
            "product_url": product_url,
            "website": sender.get("website") or product_url,
            "ideal_client": profile["targeting"]["ideal_client"],
            "cities": profile["targeting"]["cities"],
            "design": {
                "logo_url": design.get("logo_url") or design.get("signature_logo_url", ""),
                "brand_color": design.get("brand_color", ""),
                "heading_color": design.get("heading_color", ""),
                "text_color": design.get("text_color", ""),
                "background": design.get("background", ""),
                "tagline": design.get("tagline", ""),
            },
        })

    def _api_pipeline(self) -> None:
        self._send_json(pipeline_stats(_load_profile_or_none()))

    def _api_resume_sending(self) -> None:
        try:
            resumed = resume_sending(load_profile())
        except ProfileError as e:
            self._send_json({"success": False, "message": str(e)}, 400)
            return
        self._send_json({"success": True, "resumed": resumed})

    def _api_stats(self) -> None:
        self._send_json(get_db_stats())

    def _query(self, sql: str) -> list[dict]:
        conn = get_connection()
        try:
            return [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def _api_leads(self) -> None:
        # Real prospects first (best fit on top); rejected sites last
        rows = self._query("""SELECT * FROM leads ORDER BY status IN ('disqualified', 'invalid_email'),
                              COALESCE(fit_score, 0) DESC, id DESC LIMIT 100""")
        for row in rows:
            if row.get("enriched_info"):
                try:
                    row["enriched_info"] = json.loads(row["enriched_info"])
                except ValueError:
                    pass
            try:
                row["fit_reasons_list"] = json.loads(row.get("fit_reasons") or "[]")
            except ValueError:
                row["fit_reasons_list"] = []
        self._send_json({"leads": rows})

    def _api_emails(self) -> None:
        self._send_json({
            "campaigns": self._query("SELECT * FROM email_campaigns ORDER BY id DESC"),
            "logs": self._query("SELECT * FROM email_logs ORDER BY id DESC LIMIT 50"),
        })

    def _api_social(self) -> None:
        self._send_json({"posts": self._query("SELECT * FROM social_posts ORDER BY id DESC LIMIT 50")})

    def _api_logs(self) -> None:
        self._send_json({"logs": get_recent_logs(50)})

    def _api_update_status(self) -> None:
        self._send_json(read_update_status())

    def _api_settings(self) -> None:
        from core.settings_api import read_settings
        self._send_json(read_settings())

    def _settings_script(self) -> None:
        try:
            with open(SETTINGS_SCRIPT_PATH, "rb") as f:
                self._send_body(f.read(), "text/javascript; charset=utf-8")
        except OSError:
            self.send_error(404, "File Not Found")

    def _api_save_settings(self) -> None:
        """Save one section of the Settings page (the same code as `sdr setup --section`)."""
        from core.settings_api import save_section
        try:
            payload = json.loads(self.body.decode("utf-8"))
            section, values = payload["section"], payload["values"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            self._send_json({"success": False, "messages": ["Nothing to save."]}, 400)
            return
        result = save_section(section, values)
        if result.pop("password_changed", False):
            # Protect the running dashboard at once, instead of asking for a restart.
            from core import config as core_config
            from core.setup_toml import read_env_file
            # read from the file: an environment variable would still hold the old value
            new_hash = read_env_file(core_config.ENV_PATH).get("DASHBOARD_PASSWORD_HASH")
            if new_hash:
                self.server.password_hash = new_hash
                result["sign_in_again"] = True
        if result["success"]:
            log_event("Dashboard", "SaveSettings", "success", f"Saved the {section} settings.")
        self._send_json(result, 200 if result["success"] else 400)

    def _api_import_template(self) -> None:
        from core.lead_import import TEMPLATE_CSV
        self._send_body(TEMPLATE_CSV.encode("utf-8"), "text/csv", 200,
                        {"Content-Disposition": 'attachment; filename="leads-template.csv"'})

    def _api_export_csv(self) -> None:
        csv_path = export_leads_to_csv()
        if not os.path.exists(csv_path):
            self._send_json({"error": "Export failed"}, 500)
            return
        with open(csv_path, "rb") as f:
            content = f.read()
        self._send_body(content, "text/csv", 200,
                        {"Content-Disposition": 'attachment; filename="leads_export.csv"'})


class DashboardServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded HTTP server that carries the auth state its handlers share."""

    # On Windows SO_REUSEADDR lets a second process steal a port that's already in use,
    # so only enable it where it means "reuse a port in TIME_WAIT".
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def __init__(self, address: tuple[str, int], password_hash: str | None = None,
                 sessions: SessionStore | None = None, limiter: LoginLimiter | None = None,
                 instance_token: str | None = None):
        self.password_hash = password_hash
        # `is None`, not `or`: an empty SessionStore is falsy (it has __len__)
        self.sessions = sessions if sessions is not None else SessionStore()
        self.limiter = limiter if limiter is not None else LoginLimiter()
        self.instance_token = instance_token  # what /api/instance proves we hold; see core.dashboard_auth
        super().__init__(address, DashboardHandler)

    @property
    def password_required(self) -> bool:
        return self.password_hash is not None


def make_server(port: int = PORT, host: str = HOST, password_hash=_FROM_ENV,
                sessions: SessionStore | None = None, limiter: LoginLimiter | None = None,
                instance_token: str | None = None) -> DashboardServer:
    """A bound (not yet serving) dashboard. By default the password comes from
    DASHBOARD_PASSWORD_HASH (env or .env); pass None for no password. The instance token
    defaults to this install's (data/dashboard.json, created if needed). Raises OSError if
    the port is taken."""
    if password_hash is _FROM_ENV:
        password_hash = configured_password_hash()
    if instance_token is None:
        instance_token = ensure_instance_token()
    return DashboardServer((host, port), password_hash, sessions, limiter, instance_token)


# ── Starting it (`sdr dashboard`) ────────────────────────────────────────────


def _url(port: int) -> str:
    return f"http://localhost:{port}"


def _is_dashboard(port: int, token: str | None) -> bool:
    """True only if the server on `port` proves it holds this install's token (see
    core.dashboard_auth). A `Server: AutomatedSDR` header is not proof: any local program
    (another account's install on a shared computer, a phishing page) can send one, and we
    would open the browser, and its password prompt, on it."""
    if not token:
        return False
    nonce = new_nonce()
    conn = http.client.HTTPConnection(HOST, port, timeout=PROBE_TIMEOUT_SECONDS)
    try:
        conn.request("GET", f"{INSTANCE_PATH}?nonce={nonce}", headers={"Host": f"localhost:{port}"})
        response = conn.getresponse()
        if response.status != 200:
            return False
        data = json.loads(response.read(MAX_PROBE_BYTES).decode("utf-8", errors="replace"))
        return isinstance(data, dict) and proof_matches(token, nonce, data.get("proof"))
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        conn.close()


def find_running_dashboard(ports: Iterable[int] = DEFAULT_PORTS, token: str | None = None) -> int | None:
    """The first port in `ports` where this install's own dashboard is running, else None.
    `token` defaults to the one in data/dashboard.json; with none (no dashboard has started
    here yet) nothing is recognised and `sdr dashboard` starts one."""
    if token is None:
        token = instance_token()
    return next((port for port in ports if port and _is_dashboard(port, token)), None)


def _bind_first_free(ports: list[int], password_hash: str | None, token: str) -> DashboardServer:
    for port in ports:
        try:
            return make_server(port, password_hash=password_hash, instance_token=token)
        except OSError:
            continue
    raise DashboardUnavailable(
        f"Couldn't start the dashboard: ports {', '.join(map(str, ports))} are all in use. "
        "Close the app using them, or pick another port.")


def _print_port_taken(wanted: int, bound: int) -> None:
    """Said instead of opening whatever is on the wanted port: it didn't prove it's our dashboard."""
    if wanted and bound != wanted:
        print(f"\n  Port {wanted} is used by another program, so the dashboard uses port {bound}.")


def _open(opener: Callable[[str], object], url: str) -> None:
    try:
        opener(url)
    except webbrowser.Error:
        pass  # headless machine: the URL is printed anyway


def _print_started(url: str, password_hash: str | None, background: bool) -> None:
    print(f"\n  {DISPLAY_NAME} dashboard: {url}")
    if password_hash is None:
        print("  No password set: anyone using this computer can open it. "
              "Add one with `sdr setup --section security`.")
    elif not is_valid_hash(password_hash):
        print("  DASHBOARD_PASSWORD_HASH in .env isn't valid, so nobody can sign in. "
              "Set a new password with `sdr setup --section security`.")
    else:
        print("  Password protected.")
    if not background:
        print("  Press Ctrl+C to stop.\n")


def start_dashboard(port: int = PORT, open_browser: bool = True, background: bool = False, *,
                    ports: Iterable[int] | None = None,
                    opener: Callable[[str], object] = webbrowser.open, page: str = "") -> dict:
    """Open the dashboard, starting it first unless this install's dashboard is already running
    on 8080-8088 (anything else on those ports is skipped, not opened).

    background=False blocks until Ctrl+C (what `sdr dashboard` wants). background=True serves
    from a daemon thread and returns at once; it stops when this process exits, or call
    result["server"].shutdown(). Returns {"url", "port", "already_running", "server"}.
    `page` is the tab the browser opens on ("#settings"). `ports`/`opener` exist for tests.
    Raises DashboardUnavailable if every port is taken.
    """
    candidates = list(ports) if ports is not None else [port] + [p for p in DEFAULT_PORTS if p != port]
    token = ensure_instance_token()
    running = find_running_dashboard(candidates, token)
    if running:
        url = _url(running)
        print(f"\n  The dashboard is already running: {url}\n")
        if open_browser:
            _open(opener, url + page)
        return {"url": url, "port": running, "already_running": True, "server": None}

    init_db()
    password_hash = configured_password_hash()
    httpd = _bind_first_free(candidates, password_hash, token)
    bound_port = httpd.server_address[1]
    record_instance_port(bound_port)
    _print_port_taken(candidates[0], bound_port)
    url = _url(bound_port)
    _print_started(url, password_hash, background)
    result = {"url": url, "port": bound_port, "already_running": False, "server": None}
    if background:
        threading.Thread(target=httpd.serve_forever, name="sdr-dashboard", daemon=True).start()
        if open_browser:
            _open(opener, url + page)
        return {**result, "server": httpd}
    if open_browser:
        _open(opener, url + page)  # the socket is already listening, so the browser just queues
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
    finally:
        httpd.server_close()
    return result


def start_server(port: int = PORT) -> None:
    """Blocking start without opening a browser (launch_app.sh, `python3 dashboard/server.py`)."""
    start_dashboard(port, open_browser=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=f"{DISPLAY_NAME} dashboard (this computer only)")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to use (default {PORT})")
    parser.add_argument("--open", action="store_true", help="Open it in your browser")
    args = parser.parse_args(argv)
    try:
        start_dashboard(args.port, open_browser=args.open)
    except DashboardUnavailable as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
