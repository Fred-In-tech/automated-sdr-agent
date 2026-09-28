"""Checks that the email login in .env works (sending + inbox). Sends nothing.

Usage: python3 check_email_login.py

A thin wrapper around core.email_checks.check_login, reading the same .env settings the SDR
uses when it sends and reads replies (core.config.mail_server, no guessed host), so a pass here
means a pass there.
"""

import os
import sys

from core.config import EmailSettingsError, load_env_file, mail_server
from core.email_checks import check_login


def login_settings() -> dict:
    """The login the SDR will use, from the environment (.env already loaded)."""
    smtp_host, smtp_port = mail_server("smtp")
    imap_host, imap_port = mail_server("imap")
    return {
        "address": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASS", ""),
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "imap_host": imap_host,
        "imap_port": imap_port,
    }


def main(check=check_login) -> bool:
    """Print what is being checked and the result; True when both logins work."""
    load_env_file()
    try:
        settings = login_settings()
    except EmailSettingsError as exc:
        print(f"\n{exc}")
        return False
    print(f" Email      : {settings['address'] or '[NOT SET]'}")
    print(f" Sending    : {settings['smtp_host']}:{settings['smtp_port']}")
    print(f" Inbox      : {settings['imap_host']}:{settings['imap_port']}")
    if not settings["address"] or not settings["password"]:
        print("\nNo email login in .env yet. Run `sdr setup` (or `python3 cli.py setup`).")
        return False

    result = check(**settings)
    print()
    print(f" Sending (SMTP) : {'OK' if result['smtp'] else 'FAILED'}")
    print(f" Inbox (IMAP)   : {'OK' if result['imap'] else 'FAILED'}")
    for message in result["errors"]:
        print(f"\n - {message}")
    ok = bool(result["smtp"] and result["imap"])
    if ok:
        print("\nBoth logins work. Nothing was sent.")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
