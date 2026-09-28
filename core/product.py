"""Product identity — the one place the name, version and repo live."""

import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PRODUCT_NAME = "Automated SDR"
AUTHOR = "Fred"
DISPLAY_NAME = f"{PRODUCT_NAME} by {AUTHOR}"
TAGLINE = "Your AI sales rep: finds your ideal clients, emails them, and handles the replies."
CLI_NAME = "sdr"
REPO_SLUG = "Fred-In-tech/automated-sdr-agent"
REPO_URL = f"https://github.com/{REPO_SLUG}"
BRAND_COLOR = "#3B82F6"


def version() -> str:
    try:
        with open(os.path.join(ROOT_DIR, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"
