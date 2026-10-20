# Deliverability & lead quality (v1.5.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Checkbox steps.

**Goal:** see `docs/superpowers/specs/2026-10-20-deliverability-design.md`.
**Architecture:** `core/deliverability.py` (DNS checks, cache, cap, sample verdict), `core/lead_review.py`, `core/funnel.py`; hooks in qualify/leadgen, email_marketing, setup_sample/cli test-email, doctor, report/digest, dashboard (mixin + `insights.js`, Needs-you card).
**Tech Stack:** Python 3.11 stdlib + dnspython (already a dependency), SQLite, vanilla JS, pytest.

- [ ] T1 `core/deliverability.py`: provider detection, SPF/DKIM/DMARC/MX checks with injected resolver, consumer domains, sending-domain note, status, cache file, daily refresh, cap function; `parse_authentication_results`. Tests `tests/test_deliverability.py`. Commit.
- [ ] T2 Sample proof: fetch delivered sample by Message-ID over IMAP after `sdr test-email`, store verdict, DKIM override. Tests. Commit.
- [ ] T3 Cap in `EmailMarketingEngine` (refresh at most daily, cap at 5 on problem, log). Tests. Commit.
- [ ] T4 Lead review band: `auto_send_score` question/profile, `review` status in qualify/leadgen, `core/lead_review.py` (list/approve/reject/expire), exclude from sending, `sdr review`. Tests. Commit.
- [ ] T5 `core/funnel.py` + report/digest lines. Tests. Commit.
- [ ] T6 Doctor checks; dashboard routes (`/api/insights`, `/api/deliverability/check`, `/api/lead-review`), `insights.js` (Overview banner, Deliverability card, funnel card), Needs-you card shows lead reviews. Tests + browser check. Commit.
- [ ] T7 Docs (README section, AGENTS keys/commands, example profile, SECURITY note on DNS lookups), CHANGELOG 1.5.0, VERSION; suite, bandit, smoke; PR, CI, merge, tag, release, update live.
