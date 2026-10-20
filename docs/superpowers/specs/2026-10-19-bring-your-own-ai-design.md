# Bring your own AI (v1.3.0): design

Status: approved in conversation on 2026-10-19. First of three planned releases
(v1.3 AI, v1.4 deliverability & lead quality, v1.5 UI/workflow redesign).

## Why

Research (Reddit/HN second-hand via review sites, G2/Capterra, vendor docs, October 2026):

- People want AI help but dislike unreviewed "AI slop": obvious AI phrasing, fake familiarity,
  replies nobody checked. Human-reviewed beats fully automatic.
- Replies go cold fast; questions ("how much is it?") deserve a real, quick answer.
- Our own baseline: about 80% of found leads were off-target. Owners need to see *why* a lead fits.
- Owners want choice and privacy: their own provider and key, or a local model with nothing
  leaving the machine. Today the tool only supports Anthropic Claude via the `anthropic` SDK.

## Goals

1. Any AI: hosted (OpenAI, Anthropic, Google Gemini, Groq, OpenRouter, Mistral, Azure OpenAI, any
   OpenAI-compatible URL) and local (Ollama, LM Studio, llama.cpp server, vLLM, Jan).
2. Four AI jobs, each switchable on/off and each with "review first" or "send automatically":
   personal first lines, reply intent, drafted answers to questions, lead-fit explanation.
3. A review queue on the dashboard ("Needs you") for drafts waiting on the owner.
4. AI never blocks outreach: any failure falls back to today's rule-based behaviour.
5. No new dependencies. Remove the optional `anthropic` SDK requirement.

## Non-goals (this release)

Tool calling / agents, RAG or vector search, multi-model routing, fine-tuning, multichannel,
AI-written full emails (sequence copy stays owner-approved templates), the v1.4/v1.5 items.

## Approach

Raw HTTP via `urllib` (stdlib), three adapters:

| Adapter | Used for | Endpoint |
|---|---|---|
| `openai_compat` | OpenAI, Gemini (`/v1beta/openai/`), Groq, OpenRouter, Mistral, Azure v1, LM Studio, llama.cpp, vLLM, Jan, custom | `POST {base}/chat/completions`, `GET {base}/models` |
| `anthropic` | Claude | `POST https://api.anthropic.com/v1/messages` (`x-api-key`, `anthropic-version`) |
| `ollama` | Ollama | native `POST /api/chat` (`format` = JSON schema), `GET /api/tags` |

Rejected: per-provider SDKs (dependency weight, churn); LiteLLM (heavy, and PyPI releases
1.82.7/1.82.8 were backdoored in March 2026 — unacceptable next to mailbox passwords).

## Configuration

```toml
[ai]
mode = "off"                 # off | local_only | hosted
provider = "ollama"          # preset name, or "custom"
base_url = ""                # empty = the provider preset's URL
model = ""
api_key_env = ""             # name of an env var; setup saves the key to .env as AI_API_KEY
timeout_seconds = 60
monthly_budget_usd = 0       # 0 = no cap (hosted only; estimated from token usage)

[ai.jobs]
openers = "auto"             # off | review | auto
reply_intent = "auto"        # off | auto   (labels only, never sends)
answer_questions = "review"  # off | review | auto
lead_fit = "auto"            # off | auto   (display only)
```

- Presets map provider -> (adapter, base_url, auth style, models path, local?). Azure uses the
  `api-key` header; Anthropic `x-api-key`; others `Authorization: Bearer`.
- Secrets: the key lives in `.env` (`AI_API_KEY`, owner-only), never in `profile.toml`; it is
  write-only on the Settings page like the mailbox password. Keys are redacted from all errors/logs.
- Backward compatibility: an old profile with `[ai] enabled = true` + `model` and
  `ANTHROPIC_API_KEY` in the environment/.env maps to `mode = "hosted"`, `provider = "anthropic"`.
  `enabled = false` maps to `mode = "off"`.

## Safety rules

- `local_only` refuses any base URL whose host is not loopback or a private (RFC 1918 / link-local
  / `.local`) address. `hosted` requires `https://` unless the host is loopback/private.
- The UI and `sdr doctor` show one plain line about data leaving the machine
  (hosted: "lead name, company, website snippet and reply text are sent to <provider>";
  local: "nothing leaves this computer").
- Website text and reply text are untrusted data: system prompts say so; outputs are validated.
- Circuit breaker: after 2 consecutive failures in a run, AI is skipped for the rest of that run
  (one log line). Connect timeout 5 s; read timeout `timeout_seconds`.
- Validation per job: intent must be one of `INTENTS`; opener must pass the existing
  `is_valid_opener` plus a new AI-phrase check (stock phrases such as "hope this finds you well",
  "I came across your amazing", "I noticed you"); answers must be under 120 words, contain no URLs
  (sales style stays link-free), no prices or promises unless present in the owner's profile
  facts, and must not claim to be human-written facts the profile doesn't contain.
- Structured output ladder: native schema (Anthropic structured output / Ollama `format` /
  `response_format: json_schema`) -> `json_object` -> prompt-only with first `{...}` extraction.
  The working tier is detected by "Test connection" and remembered for the run.
- Temperature is not sent by default (some providers reject non-default values).
- Budget: hosted runs track tokens from the response `usage`; when the month's estimated spend
  reaches `monthly_budget_usd`, AI pauses until next month (rules take over) and the digest says so.

## The four jobs

| Job | Input | Output | Review | Fallback |
|---|---|---|---|---|
| Openers | company, category, website text | one sentence | `review`: draft queued; first email waits for it (other leads continue). `auto`: used directly | default opener |
| Reply intent | reply text | one of `INTENTS` | never sends | rule classifier |
| Answer questions | reply text, profile facts (name, pitch, offer, tagline, sign-off) | short reply body | `review`: queued; `auto`: sent through the existing auto-reply path | existing `question` template |
| Lead fit | profile ideal client, lead website text, rule score | one line "why it fits / doesn't" | display only | rule `fit_reasons` |

Profile facts given to the AI are the owner's own settings only; prompts instruct it to answer
only from them and to say "I'll check and get back to you" otherwise.

## Review queue

New table `ai_drafts(id, lead_id, kind, body, status, created_at, decided_at, context_json)`,
`kind` in (`opener`, `answer`), `status` in (`pending`, `approved`, `edited`, `skipped`, `sent`).

- Dashboard card "Needs you" (Overview tab) lists pending drafts with Approve / Edit / Skip;
  drafts older than 2 hours are highlighted. Endpoints `GET /api/ai/drafts`,
  `POST /api/ai/drafts/<id>` (`approve` / `skip` / `edit` with body), CSRF-protected like others.
- The next run sends approved/edited drafts; skipped drafts fall back to the template.
- CLI: `sdr drafts` lists pending drafts and approves/skips them interactively (plain fallback
  without a TTY prints the list).

## Setup, Settings, CLI

- Settings page: new **AI** section (mode, provider, base URL, model, key, budget, the four job
  switches) and a **Test connection** button calling `POST /api/ai/test`.
- Setup wizard: one optional question after the email step — "Use AI? Local / Hosted / Not now"
  (default Not now); detects a running Ollama (`localhost:11434`) or LM Studio (`localhost:1234`)
  and offers it. Answers-file keys `ai.*` documented in AGENTS.md and README.
- `sdr ai test` runs the same check as the button; `sdr doctor` reports AI status, mode, provider,
  model reachability (online only) and the data-leaves-machine line.
- Test connection: URL vs mode check -> list models (offer as picker) -> one tiny classification
  with schema -> report latency, valid JSON y/n, structured tier -> plain-English fix on failure
  (e.g. "Ollama isn't running: open the Ollama app or run `ollama serve`", "model not pulled:
  `ollama pull <model>`", "key rejected (401)").

## Code layout

```
core/ai/__init__.py      public API: ai_status(), write_opener(), classify_reply(),
                         answer_question(), explain_fit() (keeps old import paths working)
core/ai/providers.py     presets, mode/URL safety checks, config loading + legacy migration
core/ai/connectors.py    the three adapters (urllib), JSON ladder, timeouts, redaction
core/ai/jobs.py          prompts, validators, AI-phrase check, fallbacks, circuit breaker, budget
core/ai/drafts.py        ai_drafts table access and the review-queue rules
core/cli_ai.py           `sdr ai test`, `sdr drafts`
dashboard/ai.js          Needs-you card + AI settings test button (textContent only)
```

Touch points: `bots/leadgen_pipeline.py` (openers, lead fit), `bots/inbox_listener.py`
(intent, answers), `bots/email_marketing.py` (wait for/consume approved opener drafts),
`core/settings_api.py` (AI section), `core/setup_*` (question), `dashboard/server.py`
(routes), `core/db.py` (table), `cli.py`, docs. Each new file stays well under 400 lines.

## Testing

All offline: `urllib.request.urlopen` is replaced by a fake that returns canned provider
responses per adapter (OpenAI-shape, Anthropic-shape, Ollama-shape, errors, timeouts, bad JSON).
Covered: presets and URL safety, legacy config migration, each adapter's request shape and
parsing, JSON ladder, circuit breaker, budget cap, each job's validator and fallback, AI-phrase
check, drafts lifecycle and API endpoints (CSRF), Settings section save, setup question in
answers mode, `sdr ai test` messages, docs/packaging tests for new commands. No test calls a real
AI or the network.

## Release

v1.3.0 with CHANGELOG notes; README, AGENTS.md, SECURITY.md (what data leaves the machine),
`config/profile.example.toml` updated. Protected `main`: PR + 11 CI checks, then tag/release.
