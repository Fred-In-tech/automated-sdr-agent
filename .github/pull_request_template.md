## What this changes and why

<!-- One or two sentences. Link the issue it fixes, e.g. "Fixes #12". -->

## How you tested it

<!-- Commands you ran and what you saw. -->

## Checklist

- [ ] `python3 -m pytest tests/ -q` passes, and new behaviour has tests
- [ ] The tests stay offline: no real email, network, scraping or crontab
- [ ] No passwords, keys, `.env`, `config/profile.toml`, real leads or real email addresses anywhere in the change
- [ ] Compliance features untouched (unsubscribe line, postal address, do-not-contact list, send limits, bounce pause, robots.txt)
- [ ] Docs updated if a command, option or setup question changed (`README.md`, `AGENTS.md`)
- [ ] User-visible changes noted under `## [Unreleased]` in `CHANGELOG.md`
- [ ] New dependencies (if any) are explained and have a version floor in `requirements.txt`
