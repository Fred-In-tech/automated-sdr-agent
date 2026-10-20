# Follow-ups tab (v1.4.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** A Follow-ups tab that turns every interested reply into a deal with one dated next step.
**Architecture:** `core/followups.py` over a new `deals` table; inbox hook; dashboard mixin + JS; digest + CLI.
**Tech Stack:** Python 3.11 stdlib, SQLite, vanilla JS, pytest. Spec: `docs/superpowers/specs/2026-10-20-follow-ups-design.md`.

- [ ] Task 1: `deals` table + `core/followups.py` rules, tests `tests/test_followups.py` (create/dedupe, today ordering, complete with next step / won / lost, snooze incl. next Monday, update validation, stale, summary incl. revenue per 100 first emails, digest lines). Commit.
- [ ] Task 2: inbox hook (`interested`/`question` → `ensure_deal` in the same transaction, incl. AI review path), tests in `tests/test_followups_inbox.py`. Commit.
- [ ] Task 3: dashboard routes + `followups.js` + tab, tests `tests/test_dashboard_followups.py` (list, actions, validation, auth/CSRF, script served, no innerHTML). Browser check. Commit.
- [ ] Task 4: digest section + `sdr followups`, tests; README/AGENTS command tables. Commit.
- [ ] Task 5: CHANGELOG 1.4.0, VERSION, full suite, smoke, bandit, PR, CI, merge, tag, release, update live.
