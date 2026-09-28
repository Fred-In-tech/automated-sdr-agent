"""Every setup question in one place: labels, hints, types and defaults.

The wizard (core/setup_steps.py) reads labels and hints from here, and `sdr setup
--print-questions` prints this list as JSON, so an AI agent helping someone set up always sees
exactly what the wizard would ask — and the keys to put in setup-answers.toml. Keys are
"section.key", the same as the answers file (see AGENTS.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.email_checks import PROVIDERS
from core.setup_profile import (ALL_DAYS, DEFAULT_DAILY_LIMIT, DEFAULT_LEADS_PER_RUN, DEFAULT_REPLY_CHECK,
                                DEFAULT_RUN_TIMES, DEFAULT_SEND_DAYS)

# The seven visible steps, and the `--section` names that re-run them.
STEP_TITLES = ["Your business", "Your ideal clients", "Email style", "Your email account", "Replies",
               "Schedule", "Security & updates"]
SECTIONS = {  # section -> step number (1-based)
    "brand": 1, "audience": 2, "style": 3, "email": 4, "replies": 5, "schedule": 6, "security": 7, "updates": 7,
}
SECTION_ALIASES = {
    "business": "brand", "website": "brand", "clients": "audience", "targeting": "audience",
    "design": "style", "account": "email", "login": "email", "mailbox": "email", "reply": "replies",
    "password": "security", "dashboard": "security", "update": "updates",
}
SECTION_LABELS = {
    "brand": "Your business", "audience": "Your ideal clients", "style": "Email style",
    "email": "Your email account", "replies": "Replies", "schedule": "Schedule",
    "security": "Dashboard password", "updates": "Updates",
}
SECTION_HELP = {
    "brand": "website, business name, logo and colours, offer, mailing address",
    "audience": "ideal client, pitch, job titles, cities, search engine",
    "style": "sales (plain) or marketing (branded) emails, signature logo, welcome email",
    "email": "your mailbox login, sender name, sign-off, alerts",
    "replies": "automatic replies to interested leads",
    "schedule": "run times, send days, emails per day, reply checks, leads per run",
    "security": "dashboard password",
    "updates": "update checks: notify, auto or off",
}

SEARCH_CHOICES = (
    ("brave", "Brave Search (recommended)", "Most reliable. Needs a free API key from https://brave.com/search/api/"),
    ("duckduckgo", "DuckDuckGo", "No key needed; may slow down or stop if you search a lot"),
    ("auto", "Decide for me", "Brave when you have a key, otherwise DuckDuckGo"),
    ("bing", "Bing (not recommended)", "Against Bing's rules for bots; only if you accept that"),
)
KIND_CHOICES = (
    ("sales", "Sales outreach: plain and personal (recommended)",
     "Looks typed by a person, lands in the Primary tab, gets more replies"),
    ("marketing", "Marketing: branded HTML",
     "Logo, brand colours and a button; looks designed, often lands in Promotions"),
)
UPDATE_CHOICES = (
    ("notify", "Tell me when there's an update (recommended)", "You decide when to install: `sdr update`"),
    ("auto", "Install updates automatically", "Weekly, with a backup; rolls back if anything fails"),
    ("off", "Don't check for updates", ""),
)
DAY_PRESETS = (
    ("weekdays", "Weekdays (Mon-Fri)", ""),
    ("everyday", "Every day", ""),
    ("custom", "Let me pick the days", ""),
)
DAY_CHOICES = tuple((day, day, "") for day in ALL_DAYS)
PROVIDER_CHOICES = tuple((key, info["label"], "") for key, info in PROVIDERS.items())


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    type: str  # text | url | email | color | int | bool | choice | list | time_list | days | env_var
    help: str = ""
    default: Any = None
    required: bool = False
    choices: tuple = ()

    def as_json(self) -> dict:
        choices = [{"value": value, "label": label} for value, label, _desc in self.choices] or None
        return {"key": self.key, "label": self.label, "type": self.type, "default": self.default,
                "choices": choices, "required": self.required, "help": self.help}


QUESTIONS = (
    # 1. Your business
    Question("business.website", "Your website", "url",
             "We read your business name, logo and colours from it.", required=True),
    Question("business.name", "Business name", "text", "Detected from your website when left out."),
    Question("business.tagline", "Short tagline", "text",
             "Shown in branded emails. Detected from your website when left out."),
    Question("business.offer", "What do you offer new clients?", "text",
             'For example "free 14-day trial" or "free 30-minute consultation".', required=True),
    Question("business.signup_url", "Link for the 'try it' button", "url",
             "Your signup or booking page. Leave out to use your website.", default="(your website)"),
    Question("business.postal_address", "Business mailing address", "text",
             "Required by law in cold email (US CAN-SPAM, UK/EU rules). It goes in small print at the bottom.",
             required=True),
    Question("business.allow_no_address", "Send without a mailing address (not recommended)", "bool",
             "Only if you truly have none: cold email without an address breaks the law in many countries.",
             default=False),
    # 2. Your ideal clients
    Question("audience.ideal_client", "Who is your ideal client? (1-3 words)", "text",
             'For example "wedding photographer" or "dental clinic".', required=True),
    Question("business.pitch", 'Finish the sentence: "<your business> helps <ideal clients> ..."', "text",
             "One line on what you do for them. It goes in your first email.", required=True),
    Question("audience.job_titles", "Job titles or business types to search for", "list",
             "One per line. Leave out to search for your ideal client.", default="[ideal_client]"),
    Question("audience.cities", "Cities to search in", "list",
             'One per line. Use "City, ST" for US cities, e.g. "Austin, TX".', required=True),
    Question("audience.search_engine", "How should we search the web for leads?", "choice",
             "Brave needs a free key (https://brave.com/search/api/). Bing breaks Bing's rules for bots.",
             default="auto", choices=SEARCH_CHOICES),
    Question("audience.brave_api_key_env", "Brave Search API key", "env_var",
             "Only for search_engine = \"brave\": the NAME of an environment variable holding the key "
             "(stored in .env as BRAVE_API_KEY)."),
    # 3. Email style
    Question("style.kind", "How should your emails look?", "choice",
             "Sales emails look typed by a person (Primary tab). Marketing emails are branded (often Promotions).",
             default="sales", required=True, choices=KIND_CHOICES),
    Question("style.signature_logo_test", "Test a small logo in your signature?", "bool",
             "Sales style only: half your leads see your logo next to your name; `sdr report` shows which "
             "gets more replies.", default=True),
    Question("style.brand_color", "Brand colour", "color", "Like #3B82F6. Detected from your website."),
    Question("style.logo_url", "Logo image link (https)", "url",
             "A square image, like 192x192 PNG. Detected from your website."),
    Question("replies.branded_welcome", 'Send a branded welcome email when someone says "send me the link"?',
             "bool", "It's a reply they asked for, so it stays in their main inbox.", default=True),
    # 4. Your email account
    Question("email.address", "Email address you'll send from", "email",
             "Replies come back to this mailbox.", required=True),
    Question("email.provider", "Who provides that mailbox?", "choice",
             "Pick \"other\" to type the server names yourself.", required=True, choices=PROVIDER_CHOICES),
    Question("email.password_env", "Mailbox password or app password", "env_var",
             "The NAME of an environment variable holding it (never the password itself). Gmail and "
             "Google Workspace need an App Password: https://myaccount.google.com/apppasswords",
             required=True),
    Question("email.smtp_host", "Sending server (SMTP)", "text", 'Only for provider "other".'),
    Question("email.smtp_port", "Sending port", "int", 'Only for provider "other": 465 (SSL) or 587.', default=465),
    Question("email.imap_host", "Inbox server (IMAP)", "text", 'Only for provider "other".'),
    Question("email.imap_port", "Inbox port", "int", 'Only for provider "other".', default=993),
    Question("email.skip_login_check", "Skip the login check", "bool",
             "Save the settings without signing in to the mailbox first.", default=False),
    Question("email.sign_off", "Your first name (how you sign your emails)", "text", "", required=True),
    Question("email.from_name", "Name people see in their inbox", "text",
             'For example "Jamie at Acme".', required=True),
    Question("email.alias", "Send from an alias of this mailbox", "email",
             "Optional, e.g. jamie@ while logging in as hello@. It must deliver to the same mailbox."),
    Question("email.alert_email", "Where should hot-lead alerts go?", "email",
             "Optional. Leave out to use your sending inbox."),
    # 5. Replies
    Question("replies.enabled", "Answer interested leads automatically?", "bool",
             "Replies are sorted into interested / question / not now; you're alerted either way.", default=True),
    # 6. Schedule
    Question("schedule.run_times", "What times should it run each day?", "time_list",
             "24-hour times, this computer's clock.", default=list(DEFAULT_RUN_TIMES)),
    Question("schedule.send_days", "Which days should it send?", "days", "", default=list(DEFAULT_SEND_DAYS),
             choices=DAY_CHOICES),
    Question("schedule.daily_send_limit", "Most emails to send per day", "int",
             "Keep it low for the first 2 weeks so your address builds a good reputation.",
             default=DEFAULT_DAILY_LIMIT),
    Question("schedule.reply_check_minutes", "Check for replies every how many minutes?", "int",
             "0 = only at the full runs.", default=DEFAULT_REPLY_CHECK),
    Question("audience.leads_per_run", "New leads to find each run", "int", "", default=DEFAULT_LEADS_PER_RUN),
    Question("schedule.install", "Turn the schedule on now?", "bool",
             "It then runs by itself and emails real prospects. Off unless you say so: turn it on later "
             "with `sdr schedule on` (and `sdr schedule off` stops it). A schedule that is already "
             "running is updated with the new settings either way.", default=False),
    # 7. Security & updates
    Question("security.dashboard_password_env", "Dashboard password", "env_var",
             "Optional, recommended: the NAME of an environment variable holding a password "
             "(8+ characters) for your local dashboard. Only a hash is stored."),
    Question("updates.mode", "How should updates work?", "choice", "", default="notify", choices=UPDATE_CHOICES),
    # After saving (answers files only; the interactive wizard asks)
    Question("finish.send_sample", "Send a sample email to yourself now?", "bool", "", default=False),
    Question("finish.open_dashboard", "Open your dashboard now?", "bool", "", default=False),
    Question("finish.find_leads", "Find your first leads now?", "bool", "", default=False),
)

BY_KEY = {q.key: q for q in QUESTIONS}

# Prompts the wizard asks that aren't setup questions in their own right (menus, "keep it?"
# confirmations, the login-failure menu...). Answers files may answer them, so the unknown-key
# check must not reject them; `--print-questions` leaves them out because they need no answer.
EXTRA_ANSWER_KEYS = frozenset({
    "setup.existing", "setup.section", "finish.save",
    "business.use_detected",
    "audience.keep_brave_key", "audience.accept_bing", "audience.cities_ok",
    "email.keep_password", "email.login_failed", "email.code", "email.more_options",
    "schedule.use_suggested", "schedule.days",
    "security.keep_password", "security.protect",
})


def q(key: str) -> Question:
    return BY_KEY[key]


def known_answer_keys() -> frozenset[str]:
    """Every key an answers file may contain: the questions, their secret-bearing twins
    (`email.password` is refused with a clear message, not as an unknown key) and the
    extra prompts."""
    secrets = {key[:-len("_env")] for key in BY_KEY if key.endswith("_env")}
    return frozenset(BY_KEY) | secrets | EXTRA_ANSWER_KEYS


def normalize_section(name: str | None) -> str | None:
    """Section name as typed ("Business", "email ") -> canonical name, or None if unknown."""
    text = (name or "").strip().lower()
    text = SECTION_ALIASES.get(text, text)
    return text if text in SECTIONS else None


def questions_json() -> str:
    """What `sdr setup --print-questions` prints."""
    return json.dumps([question.as_json() for question in QUESTIONS], indent=2, ensure_ascii=False)
