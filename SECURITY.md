# Security Policy

Automated SDR by Fred runs on your own computer, logs in to your mailbox and keeps a database of
the people it contacts. This page explains how it protects that data and how to report a
problem.

## Supported versions

| Version | Security fixes |
|---|---|
| 1.x (latest release) | Yes |
| Older than 1.0 | No |

Fixes ship in the newest release only. Run `sdr update` to get it; see
[Update integrity](#update-integrity) for how updates are applied.

## Reporting a vulnerability

**Please don't open a public issue for security problems.** Report them privately through
GitHub's security advisories instead:

1. Go to https://github.com/Fred-In-tech/automated-sdr-agent/security/advisories/new, or
   open the repository's **Security** tab and click **Report a vulnerability**.
2. Include the version (the `VERSION` file), your operating system, the steps to reproduce it,
   and what an attacker could do with it.
3. **Never include your `.env`, passwords or real lead data.** Use made-up values instead.

You should get a reply within 3 business days. We aim to release a fix for serious issues within
30 days, and we're happy to credit you in the advisory and `CHANGELOG.md` if you'd like.

## What the tool stores (all on your computer)

| Where | What |
|---|---|
| `config/profile.toml` | Your business details, targeting and email wording |
| `.env` | Your mailbox login (SMTP/IMAP), the dashboard password *hash*, and optional API keys (Brave Search, Anthropic, Telegram/Discord) |
| `data/automations.db` | Leads (business names, public business emails, websites), the emails sent, and replies |
| `config/do_not_contact.txt` | Addresses and domains that must never be emailed |
| `data/*.log`, `data/update_status.json` | Run logs and the last update check |
| `data/backups/<timestamp>/` | Copies of the database, profile, `.env` and do-not-contact list, taken before each update (the last 5 are kept) |
| `data/dashboard.json` | A random token so `sdr dashboard` recognises its own running dashboard (never sent anywhere) |

On macOS and Linux the `data/` folder, the database, the CSV export and the run logs are created
readable by your user account only, like `.env`, so other accounts on a shared computer can't
read your leads.

Nothing is sent to the author, and there's no telemetry or analytics. The tool only connects to:

- the search engine you chose: the Brave Search API (with your key), DuckDuckGo, or Bing if you
  opt in. Search requests contain only search terms: your job titles and cities, or the name of
  a business it's looking up;
- the websites of the businesses it researches (and Thumbtack listings, if you set categories),
  respecting robots.txt;
- your mail server;
- GitHub, with a `git fetch` to check for updates;
- the Anthropic API and Telegram/Discord webhooks, only if you turn them on.

The dashboard is served from your own computer and loads no third-party fonts, scripts or styles,
so opening it (including its login page) tells nobody else. The only remote content it shows is
your own logo, if you set `logo_url`.

You're the data controller for the leads you collect. Keep the computer's disk encrypted
(FileVault, BitLocker or LUKS) and delete `data/` when you stop using the tool.

## Secrets handling

- `.env` is created with owner-only permissions (`600`) on macOS and Linux. On Windows it
  inherits your user profile's permissions.
- `.env`, `config/profile.toml`, `config/do_not_contact.txt`, `data/` and every answers file
  (`setup-answers.toml` or any other `*answers*.toml`) are gitignored. Never commit them. If a
  password ever ends up in git or anywhere public, **revoke it** (delete the app password) and
  create a new one. Deleting the commit isn't enough.
- Use an **app password** (Gmail, Google Workspace, Outlook) rather than your main password. You
  can revoke it at any time without affecting anything else.
- Answers files for automated setup never contain passwords, only the *names* of environment
  variables that hold them.
- The tool never prints or logs passwords, and AI agents working on this repo are told never to
  read `.env` (see [AGENTS.md](AGENTS.md)).

## Dashboard

- It listens on `127.0.0.1` only and rejects requests whose `Host` or `Origin` header isn't
  local. That blocks other websites and DNS-rebinding tricks from driving it.
- You can set a password, which is strongly recommended on a shared computer. It's stored only
  as a PBKDF2-SHA256 hash (600,000 iterations, random salt) in `.env`
  (`DASHBOARD_PASSWORD_HASH`). Sessions use an `HttpOnly`, `SameSite=Strict` cookie that expires
  after 12 hours. Every change needs a CSRF token, and 5 wrong passwords lock logins for 60
  seconds.
- Every response carries a strict Content-Security-Policy, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`. API responses are never
  cached.
- It's built for one person on one computer. Don't expose it to the internet with port
  forwarding, tunnels or reverse proxies.

## Update integrity

- Updates come only from **release tags (`vX.Y.Z`) on the repository you installed from**
  (`origin`), and they're applied as a **fast-forward** merge. Nobody can rewrite history under
  you, and if you've changed tracked files yourself the update stops rather than overwriting
  them.
- Before updating, the tool backs up your database, profile, `.env` and do-not-contact list to
  `data/backups/`. Afterwards it reinstalls the requirements and runs the test suite. If anything
  fails, it rolls back to the previous version automatically. Your data is never deleted.
- The default mode is **notify**: the digest and the dashboard say "Update available". Read
  `CHANGELOG.md` and the changes (`git log -p v1.0.0..v1.1.0`) before running `sdr update`.
  Automatic updates are opt-in (`[updates] mode = "auto"`).
- Release tags aren't signed yet. If you need stronger guarantees, set `mode = "off"`, pin a
  commit you've reviewed, and update by hand.
- The installers are plain scripts. To read one before running it:
  `curl -fsSL https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.sh -o install.sh`,
  then `less install.sh` and `bash install.sh`. Neither installer uses sudo or admin rights.

## Responsible use

Cold email is regulated: CAN-SPAM in the US, CASL in Canada, GDPR and PECR in the EU and UK, and
other laws elsewhere. You're responsible for following the rules where you and your recipients
are. The tool helps with built-in safeguards:

- Every email includes your postal address and an unsubscribe line, and opt-outs are honoured
  forever.
- It respects your do-not-contact list and a daily send limit, sends during business hours only,
  and pauses automatically if bounces spike.
- It reads only pages that a site's robots.txt allows, and only collects business contact
  addresses published on business websites.

Don't use it to send bulk unsolicited email, to email consumers, to harvest personal addresses,
or with these safeguards removed. Pull requests that weaken them won't be accepted.

## Automated checks

Every push (to any branch or release tag) and every pull request runs the offline test suite on
macOS, Linux and Windows, plus `bandit` (static security analysis, medium severity and above),
`pip-audit` (known-vulnerable dependencies) and `shellcheck` on the installer scripts. Dependabot
proposes dependency and GitHub Actions updates every week.
