# Changelog

All notable changes to Automated SDR by Fred are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Each release is a git tag `vX.Y.Z`. `sdr update` only ever moves an installed copy
forward to one of those tags, and it shows you the notes below before it does.

## [Unreleased]

## [1.4.1] - 2026-10-20

### Security

- **dnspython 2.9.0**, which fixes a reported issue where a crafted DNS answer could make the
  library use excessive CPU. The tool uses it to check that each lead's domain can receive email,
  so it talks to outside DNS servers. `sdr update` installs it for you.

### Changed

- **soupsieve 3.0.3** (used by beautifulsoup4 when reading websites), with its matching fixes.

## [1.4.0] - 2026-10-20

### Added

- **Follow-ups tab: what happens after someone says "interested".** Every lead who replies with
  interest or a question becomes a deal with one next step and a due date ("Reply to Sarah and
  suggest a call", due tomorrow), saved in the same step as the reply, so nobody slips through.
  - **Today list:** overdue first, then due today, then deals with no next step, then upcoming.
  - **Done** asks what happens next (with a date and stage), or closes the deal as **Won** (with
    its value) or **Lost**, so every open deal always has a next step.
  - **Snooze** to tomorrow, +3 days or next Monday; a **Stale** badge after 5 days in one stage.
  - **Reply** opens your own mail app. The tab never sends anything.
  - **Pipeline board** (Replied, Meeting, Proposal, Won, Lost) with counts and values, once you
    have 3 or more deals.
  - **Numbers:** due now, open deals and their value, won deals, and **revenue per 100 first
    emails**, the number that shows whether the outreach pays.
- The daily digest email lists follow-ups that are overdue or due today.
- `sdr followups` shows the same Today list in the terminal.

## [1.3.2] - 2026-10-20

### Removed

- **The Social Bot (X) tab and the social post drafter** (`sdr social`, `runner.py --task social`
  and the `[social]` settings). It only drafted placeholder posts and never posted anything, so it
  added clutter without helping anyone get clients. Old schedule entries that still run the
  `social` task now log a short note instead of failing. Existing `[social]` settings in your
  profile are simply ignored, and old drafted posts stay in your database untouched.

## [1.3.1] - 2026-10-19

### Fixed

- **Test connection failed with "LM Studio sent back an answer in an unexpected format"** when the
  AI address was typed the way LM Studio shows it (`http://127.0.0.1:1234`). That API lives under
  `/v1`, which is now added automatically to any address without a path, so the address you copy
  from LM Studio, llama.cpp, vLLM or Jan just works. Settings you already saved start working
  after `sdr update`, with nothing to change.
- When an AI server answers with an error, its own message is now shown (for example "Unexpected
  endpoint" or "model not loaded") instead of "unexpected format".

## [1.3.0] - 2026-10-19

### Added

- **Bring your own AI.** Connect an AI on your own computer (Ollama, LM Studio, llama.cpp, vLLM or
  Jan: free, and nothing leaves the computer) or a hosted provider with your own key (OpenAI,
  Anthropic Claude, Google Gemini, Groq, OpenRouter, Mistral, Azure OpenAI, or any
  OpenAI-compatible address). Set it up with `sdr setup --section ai` or the dashboard's
  **Settings > AI**. Setup finds a running Ollama or LM Studio and lists its models for you.
- **Four AI jobs, each with its own switch:** a personal first line for each lead (from their
  website), sorting replies, **drafting answers to leads' questions** from the facts you write
  (prices, what's included, FAQ), and **one line on why each lead fits** (hover the Fit score on the
  Leads tab). Every job falls back to the built-in rules when AI is off, slow or wrong: AI never
  stops your emails.
- **You approve AI drafts first, if you want.** "Show me first" drafts wait on a new **Needs you**
  card on the dashboard (or `sdr drafts` in the terminal): edit, approve or skip. A first email waits
  for its approved first line; approved answers go out with the next run. Answers default to
  "Show me first".
- **Test connection** button in Settings > AI and `sdr ai test`: lists the models, asks one tiny
  question, and explains what to fix in plain words ("Ollama isn't running", "model not found:
  ollama pull …", "key rejected").
- `sdr doctor` shows your AI and what data leaves the computer.
- A monthly budget for hosted AI: AI pauses for the month when the estimated spend reaches it.

### Security and privacy

- "AI on this computer" refuses any address on the internet, and hosted AI must use https.
- Your AI key is stored in `.env` (owner-only) as `AI_API_KEY`, never in `profile.toml`, never
  shown again, and removed from every error message.
- Every AI draft is checked before use: no links, no email addresses, no placeholders, no stock
  "AI" phrases, no prices that aren't in your facts. Website text and replies are treated as data,
  never as instructions. AI text in emails is escaped, so it can't add HTML.
- Unsubscribes are always decided by the built-in rules first, never by the AI.
- AI text is refused when it contains a web address (even without "https"), a phone number,
  payment or account wording ("invoice", "verify", "password"...), or a discount, percentage,
  amount or guarantee that isn't in your facts. This stops a lead's website from steering the AI
  into writing something you'd never send.
- "AI on this computer" checks where an address really points, so it can't be tricked into
  reaching the internet. Redirects are never followed, and the AI key setting can't point at
  your mailbox password.
- Approved answers are checked again right before sending, and never go to someone who has
  unsubscribed, bounced or is on your do-not-contact list, including in the same inbox check.

### Reliability

- A lead whose first line is waiting for review never holds up the others: it doesn't use a
  sending slot, and a first line nobody reviews within 3 days falls back to your standard one.
- One bad draft can't stop the inbox check, and a draft is always saved together with the lead's
  new status.

### Changed

- AI no longer needs the `anthropic` package: it talks to every provider directly, with nothing
  extra to install. Profiles that used Claude before (`[ai] enabled = true` with
  `ANTHROPIC_API_KEY`) keep working as "Hosted AI: Anthropic".
- Works with "thinking" models and slower computers: local AI gets 120 seconds by default (the
  first answer includes loading the model), reasoning written inline is removed, and a model that
  thinks without answering gets a clear message instead of failing silently.

## [1.2.5] - 2026-10-12

### Changed

- **Newer versions of the libraries Automated SDR is built on.** The minimums are now rich 15.0
  (the terminal colours and boxes), dnspython 2.8 (checking that a lead's domain can receive
  email), beautifulsoup4 4.15 (reading websites), idna 3.20 (international website names) and
  pygments 2.21. Nothing changes in how you use it. `sdr update` installs them for you.

## [1.2.4] - 2026-10-12

### Fixed

- **Emails could say "a a" or contradict themselves, depending on how you typed your offer.**
  The emails write "a" before your offer, so an offer typed as "a free planning call" became
  "there's a a free planning call waiting for you, no call needed". Now:
  - "a" is only added when your offer needs it, and becomes "an" before a vowel ("an intro
    session"). Offers that start with a number or "our" ("3 free months", "our starter plan")
    get no "a" at all.
  - "Start your …" buttons no longer read "Start your a free planning call".
  - "no call needed" is left out when your offer is itself a call, meeting, consultation or demo.
- **This also fixes existing setups.** The wording is fixed when each email is written, so your
  saved email copy keeps working as it is, and you don't need to run setup again. Run
  `sdr preview` to see your emails.

## [1.2.3] - 2026-09-30

### Changed

- **Newer, patched libraries.** The minimum versions are now requests 2.34.2, questionary 2.1.1,
  urllib3 2.8.0, certifi 2026.7.22 (the newest list of trusted certificates, used when your
  mailbox connection is checked) and soupsieve 2.10. `sdr update` installs them for you.

### Added (for contributors)

- **Every release now tests the real one-line install.** CI runs `curl | bash` style installs on
  macOS and Linux in a pseudo-terminal and answers setup's first question, so a problem like the
  one fixed in 1.2.2 is caught before release instead of by a user. Run it yourself with
  `python scripts/install_smoke.py`.
- Bug report and feature request forms, a pull request checklist, a Code of Conduct and a
  CODEOWNERS file. Contributors are reminded never to paste passwords, `.env` files or real
  lead data, and security problems go to a private advisory instead of a public issue.
- `main` is now protected: changes arrive through pull requests that pass CI.
- CI uses the current `actions/checkout` and `actions/setup-python` (v7).

## [1.2.2] - 2026-09-29

### Fixed

- **On a Mac, the one-line installer's setup stopped at the first question with "Setup
  cancelled. Nothing was saved."** before anyone had typed anything. The installer runs
  `curl ... | bash`, so it starts setup with the keyboard attached as `/dev/tty`. macOS can't
  watch that device for key presses the way the arrow-key menus need to, and the menu library
  reports this exactly as it reports Ctrl-D, so setup took it as "cancel". Setup now reads the
  keyboard from the terminal's real device instead. If that isn't possible, it switches to
  simple typed prompts rather than stopping. Recent releases of the menu library made this show
  up; older ones failed differently and fell back to simple prompts on their own.
- The "Setup cancelled" message now tells you how to start again (`sdr setup`).
- Tests now also run on macOS with Python 3.14, the version the python.org installer gives new Mac users.

If setup stopped like this for you, your install is fine. Run `sdr update`, then `sdr setup`.

## [1.2.1] - 2026-09-29

### Fixed

- **Two-line settings lost their line break on the Settings page.** A sign-off such as
  "Jamie" with the business name on the line below, or a postal address on several lines,
  was shown in a one-line box, and saving joined the lines ("JamieAcme"). These settings now
  get a multi-line box, and any setting that already has more than one line keeps it.
- **The "Saved" confirmation vanished instantly on the Settings page**, so a save that worked
  looked like it had done nothing. The result now stays under the Save button.

## [1.2.0] - 2026-09-29

### Changed

- **Sales-style emails no longer contain any link.** The first email, the follow-ups and the
  "not now" check-in end with "reply and I'll send you the link". A cold email without links
  reads like a person wrote it and is less likely to be filtered. When someone replies, they
  get the welcome email with the button. Marketing-style emails keep their buttons.
  This applies to new setups; an existing profile keeps its own wording
  (edit `[outreach]` in `config/profile.toml`, or run setup again to get the new wording).

### Fixed

- Short replies such as "send it", "send the link" or "send over" are now read as interested,
  so the person gets the welcome email. "Don't send" is read as not interested.

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
- **A Settings page on the dashboard.** Every setting from `sdr setup` is now a form with a Save
  button: your business, ideal clients, email style, mailbox, replies, schedule, dashboard
  password and updates. The mailbox login is checked before it's saved, and passwords are never
  shown again. It replaces the old "Email Setup" tab.
- **Finish setup in your browser.** After the first two steps, `sdr setup` asks whether to
  finish in the browser or in the terminal. The browser opens the Settings page with a
  checklist of what's left. Nothing can be sent until you connect your mailbox there.
- The Settings page and setup both remind you that `sdr dashboard` reopens the dashboard.
- **`sdr resume`** and a **Resume sending** button on the dashboard, to start sending again
  after a bounce-rate pause.

### Fixed

- **Signing in to a password-protected dashboard failed with "403 Forbidden"** in Chrome, Edge
  and Safari. If you set a dashboard password in 1.0.0, update to sign in again.

### Changed

- The bounce-rate pause now trips above **10%** of recent first emails (it was 5%). Change it
  with `[outreach] max_bounce_rate` in your profile. An existing profile keeps its own number.
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

[Unreleased]: https://github.com/Fred-In-tech/automated-sdr-agent/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/Fred-In-tech/automated-sdr-agent/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Fred-In-tech/automated-sdr-agent/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Fred-In-tech/automated-sdr-agent/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Fred-In-tech/automated-sdr-agent/releases/tag/v1.0.0
