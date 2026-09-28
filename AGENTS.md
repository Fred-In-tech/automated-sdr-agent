# AGENTS.md: Automated SDR by Fred

Instructions for AI coding agents (Claude Code, Cursor, Codex, Copilot, Windsurf and friends)
helping someone **install, set up, change or develop** this project. People should start with
README.md.

Automated SDR is a free, open-source sales development rep that runs on the user's own computer.
It finds businesses that match their ideal client, emails them a short follow-up sequence from
the user's own mailbox, reads the replies, and flags the interested leads. It is written in
Python 3.11+ (standard library first), stores everything in SQLite, and has no server. All
personal data stays on the machine, in `config/profile.toml`, `.env` and `data/`.

## Safety rules (read these first)

1. **Never read, print, `cat`, `grep`, open or paste `.env`**, or any password, app password,
   token or API key (including the Brave Search key). Never put a secret in a file, in a command
   line, or in chat. A secret only reaches the tool in one of two ways: the user types it into
   `sdr setup`, or the user sets an environment variable in their own terminal (see below).
2. **Never commit or push secrets or personal data.** That means `.env`, `config/profile.toml`,
   `config/do_not_contact.txt`, every answers file (`setup-answers.toml`, `schedule-answers.toml`
   or any other `*answers*.toml`) and everything in `data/` (the leads database, logs, backups).
   They are gitignored, so never force-add them.
3. **Never send email** except one sample to the user's own address when they ask
   (`sdr test-email`). `sdr run`, `sdr email`, `sdr pipeline`, `sdr run-all` and
   `sdr schedule on` email real prospects, `sdr inbox` can send auto-replies, and `sdr leadgen`
   searches the web. Run them only when the user explicitly asks for that.
4. **Never disable or weaken compliance checks** unless the user explicitly asks and understands
   the legal risk (CAN-SPAM, CASL, GDPR/PECR). The checks are: the mailing address, the
   unsubscribe line, the do-not-contact list, the daily send limit, the bounce pause and
   robots.txt. Only choose the Bing search engine if the user explicitly accepts that it breaks
   Bing's rules.
5. **Don't touch the crontab or Windows Task Scheduler directly.** Use `sdr schedule on|off`,
   which manages only its own entries. From an answers file the schedule stays **off** unless it
   says `[schedule] install = true`. Still write `install` explicitly, so the user's decision is
   on record.
6. **Tests are offline.** Never scrape, send or log in to a mailbox to "try something". After any
   code change, run `python3 -m pytest tests/ -q` (on Windows, `py -3 -m pytest tests/ -q`) and
   keep it green.

## Prerequisites

- **Python 3.11 or newer**, and **git**.
- macOS or Linux with bash, or Windows with PowerShell 5.1+ (Windows 10/11 already have it).
- A mailbox to send from. Gmail and Google Workspace need an **App Password**, which requires
  2-Step Verification (https://myaccount.google.com/apppasswords). Hostinger, Zoho and any
  SMTP/IMAP provider also work. Microsoft is retiring password logins for SMTP/IMAP, so Outlook /
  Microsoft 365 may refuse even the right password.
- Optional: a free Brave Search API key (https://brave.com/search/api/) for the most reliable
  lead search. Without one, DuckDuckGo is used.

## Install

| System | Command |
|---|---|
| macOS / Linux | `curl -fsSL https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.sh \| bash` |
| Windows (PowerShell) | `irm https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.ps1 \| iex` |

The installer puts the code in `~/automated-sdr` (Windows: `%USERPROFILE%\automated-sdr`) and
creates a private `.venv`. It adds the `sdr` command to `~/.local/bin` (Windows:
`%USERPROFILE%\.automated-sdr\bin`, which it also adds to the user PATH) and then starts
`sdr setup`. It never uses sudo or admin rights. If git or Python is missing, it prints the
install command and stops.

**When you (the agent) run the installer**, you usually have no interactive terminal, so skip
the wizard and configure it with an answers file instead:

```bash
curl -fsSL https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.sh | SDR_NO_SETUP=1 bash
```

```powershell
$env:SDR_NO_SETUP = '1'; irm https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.ps1 | iex; $LASTEXITCODE
```

Installer options (environment variables): `SDR_HOME` (install folder), `SDR_REPO` (git URL),
`SDR_PYTHON` (interpreter), `SDR_BIN_DIR` (macOS/Linux only; where `sdr` is linked) and
`SDR_NO_SETUP=1`. If a new terminal says `sdr: command not found`, follow the PATH hint the
installer printed, or call the launcher directly: `~/automated-sdr/bin/sdr`
(Windows: `%USERPROFILE%\automated-sdr\bin\sdr.cmd`). On old Windows versions where `irm` fails
with a TLS error, run
`[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12`
first.

To work from a clone as a contributor: `python3 -m venv .venv`, then
`.venv/bin/pip install -r requirements.txt pytest`, then `bin/sdr setup`. Any command also works
as `python3 cli.py <command>`.

## Setting it up for the user

### 1. Ask the user these questions

Keep it short and friendly, and ask in groups. Start with their website: setup reads their
business name, tagline, logo and brand colours from it, so they can skip those questions. Anything
marked (optional) can be left out. `sdr setup --print-questions` prints the exact current list as
JSON, with keys, types, defaults and choices; if it ever disagrees with this page, the command is
right.

**Business** `[business]`
- Website: `website`
- Business name: `name` (optional; detected from the website)
- Short tagline for branded emails: `tagline` (optional; detected from the website)
- What they offer new clients, e.g. "free 14-day trial": `offer`
- Link for the "try it" button, e.g. a signup or booking page: `signup_url` (optional; defaults
  to the website)
- How they finish the sentence "*<business>* helps *<ideal clients>* …", e.g. "book more
  weddings with less admin": `pitch`. It becomes the key line of the first email.
- Business mailing address: `postal_address`. The law requires one in cold email. Only if they
  truly have none, set `allow_no_address = true`, and tell them about the risk first.

**Audience** `[audience]`
- Their ideal client in 1-3 words, e.g. "wedding photographer": `ideal_client`
- The job titles or business types to search for: `job_titles` (optional list; defaults to the
  ideal client)
- The cities to search in: `cities` (a list; use "Austin, TX" style for US cities)
- How to search the web for leads: `search_engine`. Explain the choice in one line each:
  - `"brave"`: Brave Search, recommended. The most reliable, but needs a free API key from
    https://brave.com/search/api/. Also set `brave_api_key_env` (below).
  - `"duckduckgo"`: no key needed, but it may slow down or stop for a run if they search a lot.
  - `"auto"` (default): Brave when a key is saved, otherwise DuckDuckGo. Never Bing.
  - `"bing"`: not recommended. Bing's robots.txt forbids bots on its search pages, so it breaks
    Bing's rules and may get blocked. Only if the user explicitly accepts that.
- Brave key: `brave_api_key_env` is the *name* of an environment variable holding the key
  (e.g. `"SDR_BRAVE_API_KEY"`). Setup saves the key to `.env` as `BRAVE_API_KEY`. Needed only
  with `search_engine = "brave"` when no key is saved yet.
- How many new leads to look for each run: `leads_per_run` (optional, default 4)

**Email style** `[style]`
- `kind`: `"sales"` is a plain, personal email that lands in the Primary tab (recommended for cold
  outreach). `"marketing"` is a branded email with logo, colours and a button that often lands in
  Promotions.
- Whether to A/B test a small logo in the signature: `signature_logo_test` (optional, default
  true; sales style only)
- Brand colour and logo URL, if the detected ones are wrong: `brand_color` (like `"#3B82F6"`),
  `logo_url` (an https image link) (both optional)

**Email account** `[email]`
- The address that logs in and sends: `address`
- Provider: `provider`, one of `gmail`, `google_workspace`, `outlook`, `hostinger`, `zoho` or
  `other`. For `other` you also need `smtp_host`, `smtp_port` (default 465), `imap_host` and
  `imap_port` (default 993).
- **Don't ask for the password.** Set `password_env` to the *name* of an environment variable
  (e.g. `"SDR_EMAIL_PASSWORD"`) that the user fills in themselves (step 3).
- Their first name, used to sign emails: `sign_off`
- The sender name people see, e.g. "Jamie at Acme": `from_name`
- Send from an alias of that mailbox: `alias` (optional; it must deliver to the same mailbox)
- Where hot-lead alerts and the daily digest go: `alert_email` (optional; defaults to the address)
- Save without testing the login: `skip_login_check` (optional, default false). Leave it off: by
  default setup signs in once (sending nothing) and refuses to save a login that doesn't work.

**Replies** `[replies]`
- Answer interested leads automatically: `enabled` (optional, default true)
- Send a branded welcome email when someone says "send me the link": `branded_welcome`
  (optional, default true)

**Schedule** `[schedule]`
- Times to run each day: `run_times` (optional, default `["09:00", "14:00"]`, this computer's
  clock)
- Days to send: `send_days` (optional, default Monday to Friday, `["Mon", "Tue", "Wed", "Thu", "Fri"]`)
- Maximum emails a day: `daily_send_limit` (optional, default 10; keep it there for the first two
  weeks, then raise it gradually)
- How often to check for replies, in minutes: `reply_check_minutes` (optional, default 45; 0 = only
  at the full runs)
- Turn the schedule on now: `install` (default `false`). Only an explicit `install = true` starts a
  live schedule, and a live schedule emails real people, so ask the user and write their answer
  down either way.

**Dashboard password** `[security]`
- `dashboard_password_env`: the *name* of an environment variable that holds a password (8+
  characters) for the local dashboard (optional, recommended). Only a hash is saved.

**Updates** `[updates]`
- `mode`: `"notify"` (default; says "Update available"), `"auto"` (installs tagged releases
  with backup, tests and rollback) or `"off"`.

**After saving** `[finish]` (all optional and false by default in answers mode)
- `send_sample`: email the first message of the sequence to the user's own address. Only with
  the user's OK.
- `find_leads`: search the web for the first leads right away (emails nobody).
- `open_dashboard`: from an answers file this only prints how to start it; `--answers` never
  starts the dashboard itself. The user runs `sdr dashboard` in their own terminal (it keeps
  running until Ctrl+C).

### 2. Write `setup-answers.toml` in the install folder

Any file whose name contains `answers` and ends in `.toml` is gitignored (`setup-answers.toml`,
`schedule-answers.toml`, ...), so keep to that naming. It must never contain a password or key:
setup refuses answers files with keys like `password` or `api_key` in them.

```toml
[business]
website = "https://acme.com"
offer = "free 14-day trial"
pitch = "book more weddings with less admin"
postal_address = "123 Main St, Austin, TX 78701, USA"

[audience]
ideal_client = "wedding photographer"
job_titles = ["wedding photographer", "portrait photographer"]
cities = ["Austin, TX", "Denver, CO"]
search_engine = "brave"
brave_api_key_env = "SDR_BRAVE_API_KEY"

[style]
kind = "sales"

[email]
provider = "gmail"
address = "jamie@acme.com"
password_env = "SDR_EMAIL_PASSWORD"
from_name = "Jamie at Acme"
sign_off = "Jamie"

[schedule]
install = false

[security]
dashboard_password_env = "SDR_DASHBOARD_PASSWORD"

[updates]
mode = "notify"
```

Without a Brave key, use `search_engine = "duckduckgo"` (or `"auto"`) and leave out
`brave_api_key_env`.

### 3. The user adds the passwords in their own terminal, then runs setup

Environment variables set in the user's terminal are invisible to you, and that is the point.
Ask the user to run this (nothing is echoed, and nothing ends up in shell history). Leave out the
Brave line if they didn't choose Brave:

macOS / Linux (bash or zsh):
```bash
cd ~/automated-sdr
printf 'Email app password: ';  read -rs SDR_EMAIL_PASSWORD;     echo; export SDR_EMAIL_PASSWORD
printf 'Dashboard password: ';  read -rs SDR_DASHBOARD_PASSWORD; echo; export SDR_DASHBOARD_PASSWORD
printf 'Brave Search key: ';    read -rs SDR_BRAVE_API_KEY;      echo; export SDR_BRAVE_API_KEY
sdr setup --answers setup-answers.toml
```

Windows (PowerShell):
```powershell
cd $env:USERPROFILE\automated-sdr
$env:SDR_EMAIL_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host 'Email app password' -AsSecureString)).Password
$env:SDR_DASHBOARD_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host 'Dashboard password' -AsSecureString)).Password
$env:SDR_BRAVE_API_KEY = [Net.NetworkCredential]::new('', (Read-Host 'Brave Search key' -AsSecureString)).Password
sdr setup --answers setup-answers.toml
```

With `--answers`, setup never prompts. It reads the website, checks the mailbox login (it signs in
and sends nothing), writes `config/profile.toml` and `.env` (owner-only), and exits with:

| Exit code | Meaning |
|---|---|
| `0` | Saved |
| `1` | Not saved, e.g. the email login was refused. The output says why |
| `2` | Answers missing or invalid. **All** of them are listed at once, including any password variable that isn't set. Fix the file and run it again |

Another way: run `sdr setup --answers setup-answers.toml` without the email password, then have
the user run `sdr setup --section email` (and `sdr setup --section security` for the dashboard
password) and type the password into the wizard themselves.

Running `--answers` again on an install that is already set up **rewrites the whole profile**,
including any hand-edited email wording. The old profile is first copied to
`data/backups/profile-<timestamp>.toml`. To change one part, use `--section` (next section).

### 4. Check it together

1. `sdr doctor` checks Python, the profile, the email login (signs in, sends nothing), lead
   search, the schedule, the dashboard password and updates, and says what to fix. Add
   `--offline` to skip the login, or `--online` to also test the Brave key with one search.
2. `sdr preview` shows every email in the sequence and the web searches. It sends nothing.
3. `sdr test-email` sends one sample to the user's own address. Run it only after they say yes.
4. `sdr dashboard` opens the local dashboard in the browser. It keeps running until Ctrl+C, so let
   the user run it in their own terminal.
5. `sdr schedule on` starts automatic runs. Run it only when the user explicitly says to start.

## Changing settings later

- Re-run one part of the wizard with `sdr setup --section <name>`. The names are `brand`,
  `audience`, `style`, `email`, `replies`, `schedule`, `security` and `updates`. It rewrites only
  the settings that part owns; hand-edited email copy, comments and other sections are kept.
- Without a terminal, combine it with a small answers file that holds only that table, named
  `<section>-answers.toml` so it stays gitignored:
  `sdr setup --section schedule --answers schedule-answers.toml`. Unanswered questions keep their
  current values.
- Or edit `config/profile.toml` directly. Every option is documented in
  `config/profile.example.toml`, and README.md has a configuration reference. Afterwards run
  `sdr preview` and `sdr doctor --offline`.
- After changing `[schedule]` by hand, run `sdr schedule on` again to apply it
  (`sdr setup --section schedule` does this for you).
- Passwords and keys live in `.env`. Change them with `sdr setup --section email`,
  `sdr setup --section audience` (Brave key) or `sdr dashboard --set-password`, never by editing
  `.env` yourself.

## Commands

| Command | What it does |
|---|---|
| `sdr` | Home menu: status plus the most common actions. Without a terminal it prints the status and the command list, then exits |
| `sdr setup` | Setup wizard. Also takes `--answers FILE`, `--section NAME` and `--print-questions` |
| `sdr preview` | Shows the email sequence (to a made-up lead) and the searches. Sends nothing |
| `sdr test-email` | Sends one sample email to the user's own address. Options: `--to ADDR`, `--step N`, `--reply interested\|question\|not_now`, `--signature plain\|logo` |
| `sdr dashboard` | Opens the local dashboard (localhost only; password-protected if set). Runs until Ctrl+C. Options: `--set-password`, `--no-open`, `--port N` |
| `sdr run` | Runs the full SDR pipeline once now: replies, new leads, emails, digest. **Emails real prospects** |
| `sdr report` | Pipeline report: leads, emails, replies, A/B tests, hot leads (`sdr status` is the same) |
| `sdr schedule on` / `sdr schedule off` / `sdr schedule status` | Turns automatic daily runs, reply checks and the weekly update check on or off (cron on macOS/Linux, Task Scheduler on Windows), or shows them |
| `sdr update` | Installs the latest tagged release, with a backup, then runs the tests; rolls back if anything fails. `--check` only checks; `--force` updates over local edits (saved as a patch first; ask the user); `--scheduled` is what the weekly cron / Task Scheduler entry runs, not something to type by hand |
| `sdr doctor` | Checks the whole install and explains how to fix problems. `--online` also tests the Brave key; `--offline` skips the mailbox login |
| `sdr open` | Prints where the files are and offers to open them: `sdr open folder\|profile\|example\|logs` |
| `sdr version` | Prints the version (`sdr --version` works too) |
| `sdr requalify` | Re-scores older leads (`--dry-run` to preview) |
| `sdr export` | Exports leads to `data/leads_export.csv` |
| `sdr digest` | Emails the user their pipeline digest now (`--force` sends it even if it already went out today) |
| `sdr leadgen` | Finds new leads only (`--count N`). Searches the web |
| `sdr email` | Emails leads that are due only (`--limit N`). **Emails real prospects** |
| `sdr inbox` | Handles replies, bounces and unsubscribes only. Can send auto-replies |
| `sdr pipeline` / `sdr run-all` | Same as `sdr run` (`run-all` also drafts social posts when `[social]` is on) |
| `sdr social` | Drafts a social post (`--topic`). Never posts anything |

Run `sdr <command> --help` for the exact options. The last group prints its result as JSON.

## Project map

```
cli.py                  the `sdr` command (argparse); `python3 cli.py <command>` works too
runner.py               scheduled entry point: runner.py --task pipeline|inbox|leadgen|email|social|all
core/
  product.py            product name, version (reads VERSION), repo URL, brand colour
  config.py             loads/validates config/profile.toml and .env
  setup_wizard.py       `sdr setup`: runs the steps, saves, offers the first actions
  setup_steps.py        steps 1-3 and 5-7;  setup_email.py  step 4 (mailbox login + code check)
  setup_questions.py    every question: label, key, type, default (`--print-questions`)
  setup_profile.py      sales and marketing templates; answers -> profile.toml
  setup_toml.py         TOML/.env writers (a --section edits only the keys it owns)
  setup_sample.py       the sample email (`sdr test-email`)
  cli_home.py           `sdr` home menu;  cli_doctor.py  `sdr doctor`
  tui.py tui_validation.py   terminal UI (banner, prompts, plain fallback, answers files)
  brand_detect.py       reads name/logo/colours from the user's website
  email_checks.py       mail provider presets, SMTP/IMAP login check, verification code
  search.py             lead search: Brave API, DuckDuckGo, Bing (opt-in); throttling and backoff
  robots.py             robots.txt rules shared by search and the lead finder
  scheduler.py          cron / Task Scheduler entries;  locking.py  cross-platform run lock
  updater.py            release check, update with backup + test + rollback
  dashboard_auth.py     dashboard password hashing, sessions, CSRF, login rate limit
  db.py                 SQLite (data/automations.db)
  qualify.py intent.py outreach_rules.py email_design.py email_verifier.py ai.py report.py notifications.py
bots/                   leadgen_pipeline.py (+ lead_store.py), email_marketing.py, inbox_listener.py, digest.py, social_bot.py
dashboard/              server.py (localhost-only web server) + index.html
config/                 profile.example.toml (documented template); profile.toml and do_not_contact.txt are the user's own (gitignored)
data/                   the user's database, logs, backups, update_status.json (gitignored)
tests/                  offline pytest suite
install.sh install.ps1  one-command installers;  bin/sdr, bin/sdr.cmd  launchers
run_cron_pipeline.sh run_task.cmd   what the schedule runs (macOS/Linux, Windows)
VERSION CHANGELOG.md    release version and notes
```

## Changing the code

- Stick to the standard library first, with small functions and docstrings that explain *why*.
  Use type hints in the `str | None` style. No `print` in library code, only in CLI/TUI output.
- Python 3.11 compatibility: no backslashes and no reused quote characters inside f-string
  expressions.
- Windows-safe: no top-level `fcntl`/`termios` imports, use `pathlib`/`os.path`, and never
  `shell=True`.
- Keep files under 800 lines; split a module before it gets there.
- Tests live in `tests/test_<area>.py` and must stay offline: mock the network, SMTP, IMAP,
  subprocess and crontab.
- New setup questions go in `core/setup_questions.py` (then `--print-questions`, this page and
  README.md pick them up). New CLI commands must be added to the command tables here and in
  README.md; `tests/test_packaging.py` and `tests/test_docs.py` check that.
- Security scans (CI runs both):
  `pip install bandit pip-audit && bandit -q -r core bots dashboard cli.py runner.py -ll && pip-audit -r requirements.txt`
- To make a release, bump `VERSION`, add a `CHANGELOG.md` section, and tag `vX.Y.Z` on `main`.
  Installed copies update only to tags.
