"""Email login checks and the "prove this mailbox is yours" verification code.

Setup uses this module to answer two questions before anything is sent to a lead:

1. Does the login work? `check_login` signs in to the sending (SMTP) and inbox (IMAP) servers
   exactly the way the SDR does later, sends nothing, and turns the cryptic server errors
   ("535 5.7.8", "[AUTHENTICATIONFAILED]", gaierror) into sentences a non-technical owner can
   act on, with the right provider hint (e.g. Gmail's App Password link).
2. Can it really send, and does the owner read that inbox? `send_verification_code` emails a
   6-digit code through the real sending path (EmailMarketingEngine.send_real_email); the owner
   types it back into the setup, which proves delivery end to end.

Everything network-facing takes an injectable factory, so tests run offline. Passwords are
never logged, printed or echoed back in error messages.
"""

import hmac
import html
import imaplib
import re
import secrets
import smtplib
import socket
import ssl
from email.utils import make_msgid

from core.product import CLI_NAME, DISPLAY_NAME

LOGIN_TIMEOUT_SECONDS = 15
MX_LOOKUP_SECONDS = 4
SMTP_SSL_PORT = 465
CODE_LENGTH = 6
MAX_DETAIL_CHARS = 200

GMAIL_APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"

PROVIDERS = {
    "gmail": {
        "label": "Gmail",
        "smtp_host": "smtp.gmail.com", "smtp_port": 465,
        "imap_host": "imap.gmail.com", "imap_port": 993,
        "password_label": "Gmail App Password (16 letters)",
        "help": ("Gmail needs an App Password, not your normal password. Turn on 2-Step Verification, "
                 f"then create one at {GMAIL_APP_PASSWORD_URL} (name it \"Automated SDR\")."),
    },
    "google_workspace": {
        "label": "Google Workspace (Gmail on your own domain)",
        "smtp_host": "smtp.gmail.com", "smtp_port": 465,
        "imap_host": "imap.gmail.com", "imap_port": 993,
        "password_label": "Google App Password (16 letters)",
        "help": ("Google Workspace needs an App Password: turn on 2-Step Verification for this account, "
                 f"then create one at {GMAIL_APP_PASSWORD_URL}. If that page says it isn't available, "
                 "your Workspace admin has to allow 2-Step Verification (and IMAP) first."),
    },
    "outlook": {
        "label": "Outlook / Microsoft 365",
        "smtp_host": "smtp.office365.com", "smtp_port": 587,
        "imap_host": "outlook.office365.com", "imap_port": 993,
        "password_label": "Mailbox password (or app password)",
        "help": ("Personal Outlook.com, Hotmail, Live and MSN addresses can't be used: Microsoft removed "
                 "password sign-in for them, so pick another provider (Gmail, Google Workspace, Zoho or "
                 "Hostinger). A Microsoft 365 work mailbox: use its password, or an app password with two-step "
                 "verification; if the right password is refused, an admin must enable \"Authenticated SMTP\" "
                 "for the mailbox."),
    },
    "hostinger": {
        "label": "Hostinger Email",
        "smtp_host": "smtp.hostinger.com", "smtp_port": 465,
        "imap_host": "imap.hostinger.com", "imap_port": 993,
        "password_label": "Mailbox password",
        "help": ("Use the password of the mailbox itself (set in hPanel → Emails), "
                 "not your Hostinger account password."),
    },
    "zoho": {
        "label": "Zoho Mail",
        "smtp_host": "smtp.zoho.com", "smtp_port": 465,
        "imap_host": "imap.zoho.com", "imap_port": 993,
        "password_label": "Mailbox password (or app-specific password)",
        "help": ("Turn on IMAP access in Zoho Mail (Settings → Mail Accounts → IMAP). With two-factor "
                 "sign-in, create an app-specific password. Accounts hosted in the EU, India or Australia "
                 "use smtp.zoho.eu / .in / .com.au — pick \"Other\" and enter those instead."),
    },
    "other": {
        "label": "Other (I'll enter the server names)",
        "smtp_host": "", "smtp_port": 465,
        "imap_host": "", "imap_port": 993,
        "password_label": "Mailbox password",
        "help": ("Search \"<your provider> SMTP IMAP settings\". Sending uses port 465 (SSL) or 587 "
                 "(STARTTLS); the inbox uses port 993."),
    },
}

# Personal-address domains we can recognise without a DNS lookup.
FREEMAIL_PROVIDERS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook", "msn.com": "outlook",
    "hotmail.co.uk": "outlook", "outlook.co.uk": "outlook",
    "zoho.com": "zoho", "zohomail.com": "zoho",
}
# Microsoft's consumer mailboxes also live on country domains (hotmail.fr, live.co.uk, ...).
_MICROSOFT_COUNTRY_DOMAINS = {
    "hotmail": ("fr", "de", "it", "es", "nl", "be", "ca", "co.jp", "com.br", "com.au", "com.mx"),
    "live": ("co.uk", "fr", "de", "it", "nl", "ca", "com.au", "com.mx", "jp"),
    "outlook": ("fr", "de", "es", "it", "jp", "com.br", "com.au", "ie", "in"),
}
FREEMAIL_PROVIDERS.update({f"{name}.{tld}": "outlook"
                           for name, tlds in _MICROSOFT_COUNTRY_DOMAINS.items() for tld in tlds})
FREEMAIL_PROVIDERS.update({"windowslive.com": "outlook", "passport.com": "outlook"})

# Business domains: who handles their mail is visible in the MX records.
MX_PROVIDERS = (
    ("google.com", "google_workspace"), ("googlemail.com", "google_workspace"),
    ("outlook.com", "outlook"), ("hostinger", "hostinger"), ("zoho.", "zoho"),
)

# IMAP has no numeric codes, so an IMAP auth failure is recognised by its wording.
IMAP_AUTH_FAILURE_PATTERNS = ("authenticationfailed", "invalid credentials", "authentication failed",
                              "login failed", "application-specific password", "invalidsecondfactor",
                              "logondenied")
GMAIL_HINT_PATTERNS = ("application-specific password", "invalidsecondfactor", "apppasswords")
MICROSOFT_HINT_PATTERNS = ("smtpclientauthentication is disabled", "basic authentication is disabled", "5.7.139")
IMAP_DISABLED_PATTERNS = ("not enabled for imap", "imap access is disabled", "imap is disabled")
# An issuer the client doesn't know: the signature of an empty (or incomplete) trust store.
UNTRUSTED_ISSUER_PATTERNS = ("unable to get local issuer certificate", "self-signed certificate",
                             "self signed certificate")
# Microsoft removed password sign-in for these mailboxes, so no password can ever work there.
PERSONAL_MICROSOFT_DOMAINS = frozenset(domain for domain, provider in FREEMAIL_PROVIDERS.items()
                                       if provider == "outlook")

HINTS = {
    "gmail": ("Gmail / Google Workspace needs an App Password, not your normal password: turn on "
              f"2-Step Verification, then create one at {GMAIL_APP_PASSWORD_URL}"),
    "microsoft": ("Microsoft 365 / Outlook: password logins (SMTP AUTH) may be turned off for this mailbox. "
                  "An admin can enable \"Authenticated SMTP\" for it in the Microsoft 365 admin center, "
                  "or use a mailbox from another provider."),
    "microsoft_personal": ("Personal Outlook.com, Hotmail, Live and MSN mailboxes can't be used: Microsoft removed "
                           "password sign-in for them, so no password will work. Send from another mailbox "
                           "instead (Gmail with an App Password, Google Workspace, Zoho or Hostinger): "
                           f"`{CLI_NAME} setup --section email`."),
    "zoho": ("Zoho: turn on IMAP access in Zoho Mail settings, and with two-factor sign-in use an "
             "app-specific password. EU accounts use smtp.zoho.eu / imap.zoho.eu."),
    "generic": ("Use the full email address as the username and the mailbox's own password "
                "(not your website or hosting-panel password)."),
}

APP_PASSWORD_RE = re.compile(r"^[a-z]{4}( ?[a-z]{4}){3}$")


class EmailCheckError(Exception):
    """Raised when the verification email can't be sent (the message is safe to show)."""


# ── Provider helpers ────────────────────────────────────────────────────────

def provider_settings(provider: str, smtp_host: str | None = None, smtp_port: int | str | None = None,
                      imap_host: str | None = None, imap_port: int | str | None = None) -> dict:
    """Server settings for a provider key, with optional overrides (needed for "other").

    Raises ValueError for an unknown provider so a typo in an answers file fails loudly
    instead of silently trying the wrong servers."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown email provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}")
    settings = dict(PROVIDERS[provider])
    overrides = {"smtp_host": smtp_host, "smtp_port": smtp_port, "imap_host": imap_host, "imap_port": imap_port}
    for key, value in overrides.items():
        if value not in (None, ""):
            settings[key] = value.strip() if isinstance(value, str) and key.endswith("host") else value
    settings["smtp_port"] = int(settings["smtp_port"])
    settings["imap_port"] = int(settings["imap_port"])
    return settings


def _default_mx_lookup(domain: str) -> list[str]:
    """MX hostnames for a domain, or [] on any failure (DNS down, no dnspython, no records)."""
    try:
        import dns.resolver  # optional dependency; imported lazily so this module always loads
        answers = dns.resolver.resolve(domain, "MX", lifetime=MX_LOOKUP_SECONDS)
        return [str(record.exchange).rstrip(".").lower() for record in answers]
    except Exception:
        return []


def _domain(address: str | None) -> str:
    """The part after the last "@", lower-cased; "" when there isn't one."""
    address = address or ""
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""


def is_personal_microsoft(address: str | None) -> bool:
    """True for Outlook.com / Hotmail / Live / MSN addresses. Microsoft removed password sign-in
    for those mailboxes, so no password can ever work there and "ask your admin" is the wrong
    advice: a sole trader on hotmail.com has no admin. Setup can use this to lead with "change
    the provider" instead of a password retry."""
    return _domain(address) in PERSONAL_MICROSOFT_DOMAINS


def guess_provider(address: str, mx_lookup=None) -> str:
    """Best guess of the provider key for an address, used to preselect the setup menu.

    Personal domains (gmail.com, outlook.com…) are known; business domains are recognised from
    their MX records. Always returns a key of PROVIDERS ("other" when unsure) and never raises.
    `mx_lookup(domain) -> list[str]` is injectable for tests."""
    domain = _domain(address)
    if not domain:
        return "other"
    if domain in FREEMAIL_PROVIDERS:
        return FREEMAIL_PROVIDERS[domain]
    try:
        mx_hosts = (mx_lookup or _default_mx_lookup)(domain) or []
    except Exception:
        return "other"
    for mx_host in mx_hosts:
        for marker, provider in MX_PROVIDERS:
            if marker in mx_host.lower():
                return provider
    return "other"


def clean_app_password(provider: str, password: str) -> str:
    """Google shows App Passwords as "abcd efgh ijkl mnop"; people paste them with the spaces.
    Strip the spaces only when it is unmistakably an App Password, never for other passwords."""
    candidate = (password or "").strip()
    if provider in ("gmail", "google_workspace") and APP_PASSWORD_RE.match(candidate):
        return candidate.replace(" ", "")
    return password


# ── TLS ─────────────────────────────────────────────────────────────────────

def _certifi_bundle() -> str | None:
    """Path of certifi's CA bundle, or None when certifi can't be used (it is in requirements.txt,
    but the SDR must still start, and verify with the system store, without it)."""
    try:
        import certifi
        return certifi.where()
    except Exception:  # noqa: BLE001 - not installed, or a broken package: fall back, don't crash
        return None


def tls_context() -> ssl.SSLContext:
    """The one certificate-verifying TLS context for every SMTP/IMAP connection the SDR makes:
    the login check, sending and the inbox listener all use it, so a passing check means the
    real runs verify exactly the same way.

    It starts from the system trust store and adds certifi's Mozilla roots. Why both: python.org's
    macOS installer ships a Python whose OpenSSL trusts nothing until "Install Certificates.command"
    is run, so ssl.create_default_context() alone rejects every mail server ("unable to get local
    issuer certificate") while the requests-based features, which use certifi, keep working and
    nothing points at the real cause. Keeping the system store means a company CA or a self-hosted
    mail server this computer already trusts still works."""
    context = ssl.create_default_context()
    bundle = _certifi_bundle()
    if bundle:
        try:
            context.load_verify_locations(cafile=bundle)
        except (OSError, ssl.SSLError):
            pass  # a missing or damaged bundle must not take the system store down with it
    return context


def trust_store_is_empty() -> bool:
    """True when Python trusts no certificate authority at all, so no secure connection can ever
    be verified; used to explain a certificate failure by its real cause (see tls_context)."""
    try:
        return tls_context().cert_store_stats().get("x509_ca", 0) == 0
    except Exception:  # noqa: BLE001 - a failed probe must not invent a diagnosis
        return False


# ── Login check ─────────────────────────────────────────────────────────────

def _default_smtp_factory(host: str, port: int):
    """Connect the way EmailMarketingEngine.send_via_smtp does: SSL on 465, STARTTLS otherwise.

    STARTTLS is mandatory: if the server doesn't offer it, starttls() raises and we never send
    the password over an unencrypted connection."""
    context = tls_context()
    if port == SMTP_SSL_PORT:
        return smtplib.SMTP_SSL(host, port, context=context, timeout=LOGIN_TIMEOUT_SECONDS)
    server = smtplib.SMTP(host, port, timeout=LOGIN_TIMEOUT_SECONDS)
    try:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    except BaseException:
        server.close()
        raise
    return server


def _default_imap_factory(host: str, port: int):
    """IMAP over SSL, like the inbox listener — so a passing check means the listener works too."""
    return imaplib.IMAP4_SSL(host, port, ssl_context=tls_context(), timeout=LOGIN_TIMEOUT_SECONDS)


def _close(connection, method: str) -> None:
    """Say goodbye to the server. The check already has its answer, so a failed goodbye
    (server already hung up) must not turn a working login into a reported failure."""
    try:
        getattr(connection, method)()
    except Exception:
        pass


def _scrub(text: str, password: str) -> str:
    """Server messages never should, but might, echo the password: mask it, and keep it short."""
    if password:
        text = text.replace(password, "***")
    text = " ".join(text.split())
    return text[:MAX_DETAIL_CHARS]


def _detail(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPResponseException):
        message = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {message}"
    return str(exc) or type(exc).__name__


def _is_auth_failure(exc: BaseException, text: str) -> bool:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return True
    return isinstance(exc, imaplib.IMAP4.error) and any(p in text for p in IMAP_AUTH_FAILURE_PATTERNS)


def _hint_for(host: str, text: str, auth_failed: bool, address: str = "") -> str | None:
    """Which provider-specific advice applies. Returned as a key of HINTS (deduplicated later).

    The address is checked before the host: a hotmail.com owner who picked the wrong server
    still needs to hear that the mailbox itself can't sign in, not Gmail's App Password advice."""
    host = host.lower()
    microsoft_said_no = any(p in text for p in MICROSOFT_HINT_PATTERNS)
    if is_personal_microsoft(address) and (auth_failed or microsoft_said_no):
        return "microsoft_personal"
    if any(p in text for p in GMAIL_HINT_PATTERNS) or (auth_failed and ("gmail" in host or "google" in host)):
        return "gmail"
    if microsoft_said_no or (auth_failed and ("office365" in host or "outlook" in host)):
        return "microsoft"
    if auth_failed and "zoho" in host:
        return "zoho"
    return "generic" if auth_failed else None


def _explain_certificate(label: str, host: str, text: str) -> str:
    """Three different causes hide behind one SSLCertVerificationError; each needs different advice."""
    if any(p in text for p in UNTRUSTED_ISSUER_PATTERNS):
        if trust_store_is_empty():
            return (f"{label}: this Python trusts no security certificates at all, so it can't check {host}'s. "
                    "Run \"Install Certificates.command\" from your Python folder (Applications > Python 3.x on "
                    "a Mac), or `pip install certifi` in the project's .venv, then try again.")
        return (f"{label}: {host}'s security certificate isn't trusted by this computer. Use your email "
                "provider's server name (e.g. smtp.gmail.com), not a self-hosted or mistyped one.")
    return (f"{label}: {host}'s security certificate doesn't match its name. Use your email provider's "
            "server name (e.g. smtp.gmail.com), not your own website's.")


def _explain(label: str, exc: BaseException, host: str, port: int, password: str,
             address: str = "") -> tuple[str, str | None]:
    """(friendly message, hint key) for one failed login. Order matters: the specific
    exception types are subclasses of the generic ones (e.g. gaierror is an OSError)."""
    detail = _scrub(_detail(exc), password)
    text = detail.lower()
    auth_failed = _is_auth_failure(exc, text)
    hint = _hint_for(host, text, auth_failed, address)
    if auth_failed:
        return f"{label}: {host} didn't accept the email address and password.", hint
    if isinstance(exc, imaplib.IMAP4.error) and any(p in text for p in IMAP_DISABLED_PATTERNS):
        return f"{label}: IMAP (inbox access) is turned off for this mailbox — turn it on in your email settings.", hint
    if isinstance(exc, UnicodeEncodeError):
        return (f"{label}: the email address or password contains characters (accents, emoji…) the mail server "
                "can't accept. Use an app password or change the mailbox password."), hint
    if isinstance(exc, socket.gaierror):
        return (f"{label}: can't find the server \"{host}\". Check the spelling and your internet "
                "connection."), hint
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return (f"{label}: {host}:{port} didn't answer in time. The port may be wrong or blocked by your "
                "network — sending usually uses 465 or 587, the inbox 993."), hint
    if isinstance(exc, ConnectionRefusedError):
        return f"{label}: {host} refused the connection on port {port}. Check the port number.", hint
    if isinstance(exc, ssl.SSLCertVerificationError):
        return _explain_certificate(label, host, text), hint
    if isinstance(exc, ssl.SSLError):
        return (f"{label}: the secure connection to {host}:{port} failed. Port 465 uses SSL and 587 uses "
                "STARTTLS — check you picked the right port."), hint
    if isinstance(exc, smtplib.SMTPNotSupportedError) and "starttls" in text:
        return (f"{label}: {host}:{port} doesn't offer an encrypted connection, so your password wasn't sent. "
                "Try port 465 or 587."), hint
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return f"{label}: {host} closed the connection. Check the port (465 or 587).", hint
    if isinstance(exc, (smtplib.SMTPException, imaplib.IMAP4.error)):
        return f"{label}: the server said: {detail}", hint
    if isinstance(exc, OSError):
        return f"{label}: couldn't connect to {host}:{port} ({detail}).", hint
    return f"{label}: unexpected error ({type(exc).__name__}: {detail}).", hint


def _port(value) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _check_one(label: str, host: str, port_value, address: str, password: str, factory,
               login_close: str) -> tuple[bool, str | None, str | None]:
    """Log in to one server. Returns (ok, error message, hint key)."""
    host = (host or "").strip()
    port = _port(port_value)
    if not host:
        return False, f"{label}: no server name set.", None
    if port is None:
        return False, f"{label}: the port must be a number like 465, 587 or 993 (got {port_value!r}).", None
    connection = None
    try:
        connection = factory(host, port)
        connection.login(address, password)
        return True, None, None
    except Exception as exc:  # every failure becomes a sentence; nothing escapes to the wizard
        message, hint = _explain(label, exc, host, port, password, address)
        return False, message, hint
    finally:
        if connection is not None:
            _close(connection, login_close)


def check_login(address: str, password: str, smtp_host: str, smtp_port: int | str, imap_host: str,
                imap_port: int | str, smtp_factory=None, imap_factory=None) -> dict:
    """Sign in to the sending and inbox servers. Sends nothing, never raises.

    Returns {"smtp": bool, "imap": bool, "errors": [friendly strings]} — per-server messages
    first, then each applicable provider hint once (e.g. the Gmail App Password link).
    Factories are `factory(host, port) -> connection` with .login(user, pw) and
    .quit() (SMTP) / .logout() (IMAP); the default ones connect exactly like the SDR does."""
    address = (address or "").strip()
    if not address or not password:
        missing = "email address" if not address else "password"
        return {"smtp": False, "imap": False, "errors": [f"No {missing} given — nothing was checked."]}

    smtp_ok, smtp_error, smtp_hint = _check_one("Sending (SMTP)", smtp_host, smtp_port, address, password,
                                                smtp_factory or _default_smtp_factory, "quit")
    imap_ok, imap_error, imap_hint = _check_one("Inbox (IMAP)", imap_host, imap_port, address, password,
                                                imap_factory or _default_imap_factory, "logout")
    errors = [e for e in (smtp_error, imap_error) if e]
    hints = [h for h in (smtp_hint, imap_hint) if h]
    if "generic" in hints and len(set(hints)) > 1:
        hints = [h for h in hints if h != "generic"]  # a provider-specific hint says it better
    errors += [HINTS[h] for h in dict.fromkeys(hints)]
    return {"smtp": smtp_ok, "imap": imap_ok, "errors": errors}


# ── Verification code ───────────────────────────────────────────────────────

def make_code() -> str:
    """A 6-digit code from the OS's secure random source (never `random`)."""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def code_matches(expected: str, typed: str) -> bool:
    """True when the typed code matches. Forgiving about spaces/dashes ("123 456"), and
    compared in constant time so the check leaks nothing about how close a guess was."""
    digits = re.sub(r"\D", "", typed or "")
    if not expected or len(digits) != len(expected):
        return False
    return hmac.compare_digest(digits.encode(), expected.encode())


def verification_message(code: str, product: str = DISPLAY_NAME) -> tuple[str, str, str]:
    """(subject, text, html) of the verification email. Short and plain on purpose: it goes to
    the owner's own inbox and should look like the login codes they already receive."""
    subject = f"{code} is your {product} verification code"
    text = (f"Your verification code is: {code}\n\n"
            f"Type it into the {product} setup in your terminal to confirm this mailbox can send email.\n\n"
            "Didn't start a setup? You can ignore this email — nothing else will be sent.")
    safe_code, safe_product = html.escape(code), html.escape(product)
    html_body = (
        '<div style="font-family:-apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Arial, sans-serif; '
        'font-size:15px; color:#1B2B4D; line-height:1.6;">'
        "<p>Your verification code is:</p>"
        '<p style="font-family:Menlo, Consolas, monospace; font-size:28px; font-weight:700; '
        f'letter-spacing:6px; color:#0F1729; margin:8px 0 16px 0;">{safe_code}</p>'
        f"<p>Type it into the {safe_product} setup in your terminal to confirm this mailbox can send email.</p>"
        '<p style="color:#6B7A90; font-size:13px;">Didn\'t start a setup? You can ignore this email — '
        "nothing else will be sent.</p></div>"
    )
    return subject, text, html_body


def send_verification_code(engine, to: str, product: str = DISPLAY_NAME) -> str:
    """Email a fresh code to `to` through the engine's real sending path; return the code.

    `engine` is an EmailMarketingEngine (or anything with the same
    `send_real_email(to, subject, html, text, extra_headers)`), so a code that arrives proves
    the exact login, server and From address the SDR will use. Raises EmailCheckError when the
    send fails, because setup must not continue as if the mailbox were verified."""
    to = (to or "").strip()
    if "@" not in to:
        raise EmailCheckError("A valid email address is needed to send the verification code to.")
    code = make_code()
    subject, text, html_body = verification_message(code, product)
    sender = getattr(engine, "from_email", "") or to
    headers = {"Message-ID": make_msgid(domain=sender.rsplit("@", 1)[-1] or "localhost")}
    try:
        sent = engine.send_real_email(to, subject, html_body, text, headers)
    except Exception as exc:
        raise EmailCheckError(f"Couldn't send the verification email: {type(exc).__name__}") from exc
    if not sent:
        raise EmailCheckError("Couldn't send the verification email. Check the email login "
                              "(run the login check again) and try once more.")
    return code
