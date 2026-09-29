# Changelog

All notable changes to Automated SDR by Fred are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Each release is a git tag `vX.Y.Z`. `sdr update` only ever moves an installed copy
forward to one of those tags, and it shows you the notes below before it does.

## [Unreleased]

## [1.1.0] - 2026-09-28

### Added

- **Import your own leads.** Click **Import Leads** on the dashboard's Leads tab and choose a
  CSV file from Excel, Google Sheets or your CRM, or run `sdr import leads.csv` in Terminal or
  PowerShell. Only an `email` column is needed; first name, company, title, website and
  location are picked up when present. The file is checked first, and you see what will be
  imported and what is skipped, and why. Importing sends nothing: the leads join your next run.
- Imported leads pass the same safeguards as found ones. Anyone already in your leads
  (including people who unsubscribed or bounced) and anyone on your do-not-contact list is
  skipped, and each domain is checked for a working mail server.
- **`sdr resume`** and a **Resume sending** button on the dashboard, to start sending again
  after a bounce-rate pause.

### Changed

- After a bounce-rate pause, the message now points to `sdr resume` instead of suggesting a
  higher bounce limit. Resuming keeps the limit where it is, so another bad batch pauses
  sending again.

## [1.0.0] - 2026-09-28

First public release of **Automated SDR by Fred**: an AI sales rep that runs on your own
computer. It finds your ideal clients, emails them, and handles the replies.

### Added

- **One-command install** on macOS, Linux and Windows, followed by a short guided setup in
  the terminal (arrow-key menus, with a plain-text fallback). An AI coding agent can run the
  same setup for you from an answers file (`sdr setup --answers`, see AGENTS.md).
- **Brand detection**: give setup your website and it picks up your business name, tagline,
  logo and brand colours for your emails and dashboard.
- **Prospecting and qualification**: searches the web for the job titles and cities you
  choose, reads each business's website (respecting robots.txt), and gives every lead a
  0-100 fit score with reasons. Poor fits are never emailed.
- **Three lead search engines**, picked in setup or with `[targeting] search_engine`:
  - **Brave Search** (recommended): the official API with a free key, saved in `.env` as
    `BRAVE_API_KEY`. `sdr doctor --online` checks the key with one test search.
  - **DuckDuckGo**: needs no key, and is the default when there's no Brave key
    (`search_engine = "auto"`).
  - **Bing**: opt-in only. Its robots.txt forbids bots on search pages, so every run logs a
    warning that this breaks Bing's rules.

  Each engine is throttled politely. A rate limit, a captcha or a rejected key stops that
  engine for the rest of the run with a plain-English note in the activity log, and it tries
  again next run.
- **Follow-up sequences**: a first email and up to three follow-ups in the same thread, best
  fits first, within a daily cap, business hours and your send days. Every email carries your
  mailing address and an unsubscribe line. Sales or marketing style.
- **Inbox handling**: each reply is sorted into interested, question, not now, not
  interested, unsubscribe, wrong person or other. Interested leads get a branded welcome
  reply and you get a hot-lead alert. Opt-outs are never emailed again, and out-of-office
  replies and newsletters are ignored.
- **Sender-reputation safeguards**: automatic pause when bounces spike, a do-not-contact
  list, and a login check of your email account during setup.
- **Experiments and tracking**: subject-line and signature A/B tests, UTM-tagged links, and a
  polite check-in for leads who said "not now".
- **Reporting**: a daily digest email, `sdr report`, and a local dashboard in your own brand
  colours, protected by a password.
- **The `sdr` command**: a home menu with your status and the common actions, plus
  `sdr preview` (every email, sends nothing), `sdr test-email` (a real sample to yourself),
  `sdr doctor` (a health check that says how to fix problems) and `sdr open`.
- **Scheduling**: `sdr schedule on|off|status` sets up the daily runs and the reply check
  with cron (macOS/Linux) or Task Scheduler (Windows).
- **Updates**: `sdr update` checks for a new release, backs up your data, moves to the
  release, reinstalls requirements and runs the self-tests. If anything fails it rolls back
  on its own. Updates only notify you by default; automatic updates are optional.
- **Optional AI**: with a Claude API key, a personal first line for each lead and more
  accurate reading of replies.

### Security

- Passwords and keys stay in `.env`, which is never committed.
- The dashboard only listens on your own computer and supports a password (PBKDF2-hashed),
  sessions, CSRF protection and strict security headers.
- Updates only fast-forward to release tags from `origin`. They never merge over your own
  commits or edited files.
- Every lead-search request checks robots.txt first. The only exception is Bing's search page,
  and only when you choose Bing yourself.
- Mail logins are always checked with certificate verification (with a bundled trust store,
  so python.org installs on macOS work too), and a login is never sent to a guessed mail
  server: a missing host stops with a message saying how to fix it.
- The lead database, CSV export and logs are readable by your user account only, and CSV
  cells that look like spreadsheet formulas are neutralised.
- `sdr dashboard` only reuses a running dashboard that proves it belongs to this install.
- The Brave Search key is never sent along on a redirect.
- Installers install the newest release tag, not unreleased code.

[Unreleased]: https://github.com/Fred-In-tech/automated-sdr-agent/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Fred-In-tech/automated-sdr-agent/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Fred-In-tech/automated-sdr-agent/releases/tag/v1.0.0
