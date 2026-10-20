# Follow-ups tab (v1.4.0): design

Status: chosen by the owner on 2026-10-20 to replace the removed Social Bot tab.

## Why
Research (Zapier vendor survey of 400+ US B2B sales/marketing managers; Close, Pipedrive, Apollo,
Streak product docs): qualified leads get dropped after the first reply (92% say monthly; 42% of
reps skip the 2nd/3rd follow-up; 47% want automatic reminders). This tool finds, emails and
classifies replies, then the lead returns to the owner's memory. Follow-ups closes that gap.

## Scope
- One open **deal** per lead: stage `replied | meeting | proposal | won | lost`, optional value,
  one **next step** (text + due date), notes, timestamps. One next step per deal (Pipedrive's model)
  instead of a separate task list: every open deal always has exactly one next action.
- **Automatic:** an `interested` or `question` reply opens a deal at `replied` with the next step
  "Reply to <name> and suggest a call", due tomorrow (the owner's local date). Never duplicated.
- **Today view:** overdue, then due today, then open deals with no next step, then upcoming.
  Done (asks for the next step, or Won / Lost), Snooze (tomorrow, 3 days, next Monday), edit the
  next step, stage, value and notes. A "Stale" badge after 5 days in the same stage.
- **Stage board** with counts and values, shown once there are 3+ deals.
- **Numbers:** open deals, pipeline value, won value, and revenue per 100 first emails.
- **Daily digest:** a "Follow-ups due" section (overdue + today).
- **CLI:** `sdr followups` prints the Today list.
- **Never sends anything.** "Reply" opens the owner's mail app (mailto:) with the lead's address.

## Non-goals
Calendar/meeting sync, multiple tasks per deal, teams/assignees, email sending from the tab.

## Design
- `core/followups.py`: table access and rules (`ensure_deal`, `today`, `complete`, `snooze`,
  `update`, `summary`, `digest_lines`), dates in the owner's local time, all SQL parameterised.
- `core/db.py`: table `deals` (UNIQUE open deal per lead enforced in code).
- `bots/inbox_listener.py`: `ensure_deal(lead_id, cursor=...)` in the reply transaction.
- `dashboard/followup_routes.py` (mixin, same auth/CSRF) + `dashboard/followups.js`
  (textContent only) + a tab in `index.html`. `GET /api/followups`, `POST /api/followups`.
- `bots/digest.py`: due section. `cli.py` + `core/cli_followups.py`: `sdr followups`.

## Testing
Offline unit tests for every rule and date edge (overdue, today, next Monday, stale), the reply
hook (one deal per lead, also in review mode), API (auth, CSRF, validation, 404), digest section,
CLI output; docs tables updated; browser check on the demo profile.
