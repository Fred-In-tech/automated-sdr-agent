# Bring your own AI (v1.3.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let owners connect any hosted or local LLM, use it for four jobs with review-first or automatic control, and never let AI block outreach.

**Architecture:** `core/ai.py` becomes the package `core/ai/` (providers, connectors, jobs, drafts). Three stdlib `urllib` adapters (OpenAI-compatible, Anthropic Messages, Ollama native). Jobs validate every answer and fall back to the existing rules. A new `ai_drafts` table backs a review queue shown on the dashboard and in `sdr drafts`. AI becomes a setup/Settings section `ai`.

**Tech Stack:** Python 3.11 stdlib (`urllib.request`, `json`, `ipaddress`), SQLite, existing dashboard (`http.server` + vanilla JS), pytest. Spec: `docs/superpowers/specs/2026-10-19-bring-your-own-ai-design.md`.

**Conventions (from AGENTS.md):** tests offline (patch `urllib.request.urlopen`), files < 800 lines, Python 3.11 f-strings, no `print` in library code except existing CLI-facing modules, run `python3 -m pytest tests/ -q` after every task, commit with the Fred-In-tech no-reply identity.

---

### Task 1: Provider presets, safety checks, config loading

**Files:** Create `core/ai/__init__.py` (temporary re-export of old API), `core/ai/providers.py`; Move `core/ai.py` → `core/ai/legacy.py` (deleted in Task 3); Test `tests/test_ai_providers.py`.

Interface:
```python
@dataclass(frozen=True)
class Preset: name: str; label: str; adapter: str  # "openai" | "anthropic" | "ollama"
              base_url: str; auth: str             # "bearer" | "api-key" | "x-api-key" | "none"
              local: bool; needs_key: bool
PRESETS: dict[str, Preset]   # openai, anthropic, gemini, groq, openrouter, mistral, azure,
                             # ollama, lmstudio, llamacpp, vllm, jan, custom
MODES = ("off", "local_only", "hosted"); JOB_NAMES = ("openers", "reply_intent", "answer_questions", "lead_fit")
@dataclass(frozen=True)
class AIConfig: mode; provider; adapter; base_url; model; api_key (str|None, never logged);
                auth; timeout: float; monthly_budget_usd: float; jobs: dict[str, str]
def load_ai_config(profile: dict, environ=os.environ) -> AIConfig     # handles legacy [ai] enabled/model + ANTHROPIC_API_KEY
def is_private_host(url: str) -> bool                                  # loopback, RFC1918, link-local, *.local, localhost
def url_problem(cfg: AIConfig) -> str | None                           # plain-English reason the URL is not allowed, or None
def data_notice(cfg: AIConfig) -> str                                  # "nothing leaves this computer" / "... are sent to <label>"
```
Job values: openers off|review|auto (default auto), reply_intent off|auto (auto), answer_questions off|review|auto (review), lead_fit off|auto (auto). Invalid values fall back to the default. API key: `environ[cfg api_key_env or "AI_API_KEY"]`; legacy anthropic uses `ANTHROPIC_API_KEY`.

- [ ] Step 1: tests — presets cover all 13 names; `load_ai_config({})` → mode off; legacy `{"ai": {"enabled": True, "model": "claude-x"}}` + env ANTHROPIC_API_KEY → hosted/anthropic/model claude-x; legacy enabled without key → off; base_url override; `is_private_host` true for 127.0.0.1, localhost, 10.x, 192.168.x, 172.16.x, ::1, mac.local, false for api.openai.com and 8.8.8.8; `url_problem` rejects public host in local_only, http public host in hosted, accepts http://localhost:11434 in both; `data_notice` wording; invalid job value → default.
- [ ] Step 2: run, expect ImportError/FAIL. Step 3: implement. Step 4: PASS. Step 5: full suite, commit `feat(ai): provider presets, safety checks and config loading`.

### Task 2: Connectors (HTTP adapters, JSON ladder, redaction)

**Files:** Create `core/ai/connectors.py`; Test `tests/test_ai_connectors.py`.

Interface:
```python
class AIError(Exception)                 # message already redacted, plain English
@dataclass
class Reply: text: str; input_tokens: int = 0; output_tokens: int = 0
def chat(cfg, system: str, user: str, *, schema: dict | None = None, max_tokens: int = 400,
         opener=urllib.request.urlopen) -> Reply
def list_models(cfg, *, opener=urllib.request.urlopen) -> list[str]
def extract_json(text: str) -> dict | None      # first {...} block
def redact(text: str, secrets: Iterable[str]) -> str
```
Adapter rules: openai → POST `{base}/chat/completions` body `{model, messages:[system,user], max_tokens}`; with schema add `response_format={"type":"json_schema","json_schema":{"name":"answer","schema":schema,"strict":True}}`; on HTTP 400 mentioning response_format retry once with `{"type":"json_object"}`, then without (prompt-only). Auth header per preset (bearer / `api-key`). anthropic → POST `{base}/messages` headers `x-api-key`, `anthropic-version: 2023-06-01`, body `{model,max_tokens,system,messages}`; text = joined `content[].text`. ollama → POST `{host}/api/chat` body `{model, messages, stream:false, format: schema or omitted}`; text = `message.content`; base_url may be `http://localhost:11434` or `.../v1` (strip `/v1`). Usage tokens from `usage` / `prompt_eval_count`+`eval_count`. Timeouts: `cfg.timeout`. Errors → AIError with fixes: connection refused on local preset → "<label> isn't running at <url>. Start it and try again."; 401/403 → "The AI provider rejected the key (401)."; 404 with model → "Model '<m>' not found. For Ollama: ollama pull <m>."; timeout → "The AI took longer than N seconds." `url_problem` checked first.

- [ ] Step 1: tests with a `FakeOpener` recording requests and returning canned bytes: request URL/headers/body per adapter (openai bearer, azure api-key, anthropic x-api-key, ollama native, `/v1` stripping), schema → response_format, 400 fallback ladder, token usage parsing, list_models for `/models` (data[].id) and `/api/tags` (models[].name), error mapping (URLError refused, HTTPError 401/404, socket.timeout), key never in AIError text, `extract_json`, local_only refusal before any request.
- [ ] Steps 2–5 as Task 1; commit `feat(ai): stdlib connectors for OpenAI-compatible, Anthropic and Ollama`.

### Task 3: Jobs, validators, circuit breaker, budget; retire old module

**Files:** Create `core/ai/jobs.py`; rewrite `core/ai/__init__.py`; delete `core/ai/legacy.py`; Modify `tests/test_ai.py` (old tests now target the new API); Test `tests/test_ai_jobs.py`.

Interface:
```python
class AIRun:   # one per pipeline run; caches config, counts failures, tracks tokens
    def __init__(self, profile, *, environ=os.environ, chat_fn=connectors.chat, usage_store=None)
    def enabled(self, job: str) -> bool        # mode != off, job != off, breaker closed, budget ok, url ok
    def job_mode(self, job: str) -> str        # off | review | auto
    def ask(self, job, system, user, *, schema=None, max_tokens=400) -> str | None  # None on any failure; 2 consecutive failures open the breaker
def write_opener(run, company, category, page_text) -> str | None
def classify_reply(run, text, opt_out_words) -> str            # rules first for unsubscribe; validates INTENTS
def answer_question(run, profile, lead, reply_text) -> str | None
def explain_fit(run, profile, company, page_text, rule_reasons) -> str | None
def is_valid_opener(text) -> bool                               # existing rules + ai_phrase_problem
def ai_phrase_problem(text) -> str | None                       # stock phrases list
def is_valid_answer(text, profile) -> bool                      # <=120 words, no URLs/emails, no "$"/price unless in profile facts, no {{ }}
```
Budget: `usage_store` = `core.ai.usage` helpers on `bot_logs`? — use a small table-free approach: append `{month, input_tokens, output_tokens}` to `data/ai_usage.json` (owner-only via `core.db.open_private`); cost estimate uses a per-provider $/1M table (hosted only; local = 0); `monthly_budget_usd == 0` = no cap. `core/ai/__init__.py` exports: `AIRun, write_opener, classify_reply, answer_question, explain_fit, is_valid_opener, ai_phrase_problem, load_ai_config, data_notice`, plus back-compat `ai_enabled(profile)` (True when mode != off and url ok) and `classify_reply_with_ai(profile, text, opt_out_words)`.

- [ ] Step 1: tests with a fake `chat_fn`: opener accepted/rejected (AI phrases, URLs, NONE), intent valid/invalid/unsubscribe-wins, answer validation (too long, URL, invented price), fit line trimmed to one line ≤ 160 chars, breaker opens after 2 failures and stays open, job off → None without calling, budget reached → disabled, local provider never counts cost, `ai_enabled` back-compat. Port existing `tests/test_ai.py` assertions to the new functions.
- [ ] Steps 2–5; commit `feat(ai): four AI jobs with validation, breaker and budget`.

### Task 4: Review queue storage

**Files:** Create `core/ai/drafts.py`; Modify `core/db.py` (CREATE TABLE ai_drafts); Test `tests/test_ai_drafts.py`.

```sql
CREATE TABLE IF NOT EXISTS ai_drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL,
  kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  context TEXT DEFAULT '{}', created_at TEXT NOT NULL, decided_at TEXT)
```
```python
def add_draft(lead_id, kind, body, context=None) -> int        # replaces an older pending draft of the same kind for that lead
def pending(limit=50) -> list[dict]                             # joined with lead name/company/email, oldest first, with "stale" (> 2 h)
def decide(draft_id, action, body=None) -> dict                 # approve | skip | edit (body required, re-validated by caller)
def approved_for(lead_id, kind) -> dict | None                  # approved/edited and not yet sent
def mark_sent(draft_id) -> None
def has_pending(lead_id, kind) -> bool
```
- [ ] Tests on a temp DB (conftest guard already isolates DB): add/replace, pending ordering + stale flag, decide transitions and invalid action, approved_for/mark_sent, has_pending. Steps 2–5; commit `feat(ai): review queue storage`.

### Task 5: Wire the jobs into the pipeline

**Files:** Modify `bots/leadgen_pipeline.py` (~lines 205–235), `bots/inbox_listener.py` (`_load_classifier`, `_act`), `bots/email_marketing.py` (first-email opener use), `core/db.py` (column `leads.ai_fit TEXT`); Tests `tests/test_ai_pipeline.py`.

- leadgen: create one `AIRun` per pipeline (`LeadGenPipeline.__init__` → `self.ai = AIRun(profile)`; module function gets `ai` param defaulting to None → `AIRun(profile)`). Qualified lead: opener mode auto → `write_opener` stored as `opener`; mode review → `opener=None` and, after the lead is saved, `add_draft(lead_id, "opener", text)`. `lead_fit` auto → `explain_fit` stored in `ai_fit`.
- email_marketing: before sending step 1 to a lead, if `has_pending(lead_id,"opener")` skip this lead this run (log "waiting for your review"); if `approved_for(lead_id,"opener")` use its body as `opener`, then `mark_sent`.
- inbox: `_load_classifier` uses `AIRun(...).enabled("reply_intent")`. In `_act`, for intent `question` and first reply: answer mode auto → `answer_question` text used as the reply body (plain text, sign-off appended by template rendering of `{{sign_off}}`), fallback to `build_reply`; mode review → `add_draft(lead_id,"answer",text,{subject, message_id})`, no reply sent, action "drafted an answer for your review". Approved answers are sent by `InboxListener.send_approved_answers()` called at the start of `check_inbox_and_auto_reply`.
- [ ] Tests: each branch with fake AIRun/drafts and the existing fakes for SMTP; existing pipeline/inbox tests stay green. Steps 2–5; commit `feat(ai): use AI jobs in lead finding, sending and replies`.

### Task 6: Setup + Settings section `ai`

**Files:** Modify `core/setup_questions.py` (SECTIONS `ai`: 7, labels/help, Questions `ai.mode` choice, `ai.provider` choice from PRESETS, `ai.base_url` text optional, `ai.model` text optional, `ai.api_key_env` secret-env, `ai.monthly_budget_usd` int, `ai.openers`/`ai.reply_intent`/`ai.answer_questions`/`ai.lead_fit` choices), `core/setup_profile.py` (SECTION_KEYS["ai"], managed_values, profile template `[ai]` + `[ai.jobs]`, state_from_profile), `core/setup_steps.py` (`step_ai`: ask mode; when local, probe `http://localhost:11434/api/tags` and `http://localhost:1234/v1/models` via `list_models` with 2 s timeout and offer the found provider/models; save key to `.env` as `AI_API_KEY`), `core/setup_wizard.py` (FULL_STEPS + SECTION_STEPS), `core/settings_api.py` (SECTION_FIELDS["ai"], SECRETS["ai.api_key_env"] = ("AI_API_KEY", "AI API key")), Tests `tests/test_setup_ai.py`, update `tests/test_settings_api.py`.
- [ ] Tests: answers-mode `--section ai` writes `[ai]` keys and key to .env (owner-only), never into profile; legacy profile round-trips; Settings read shows `is_set` for the key and never the value; local probe offered when fake list_models succeeds; `--print-questions` includes ai keys. Steps 2–5; commit `feat(ai): AI section in setup and Settings`.

### Task 7: CLI and doctor

**Files:** Create `core/cli_ai.py`; Modify `cli.py` (commands `ai` with subcommand `test`, `drafts`), `core/cli_doctor.py` (AI line), README.md + AGENTS.md command tables; Tests `tests/test_cli_ai.py`.
- `sdr ai test`: prints mode, provider, data notice, models found, one test classification with latency + tier, exit 0/1 with the AIError fix text.
- `sdr drafts`: lists pending drafts; interactive approve/edit/skip via UI; non-TTY prints list and how to use the dashboard.
- doctor: `OK: AI: <provider> <model> (local only — nothing leaves this computer)` / `AI: off` / warning with fix; `--online` runs list_models.
- [ ] Tests with fake connectors/UI; docs tests pass. Steps 2–5; commit `feat(ai): sdr ai test, sdr drafts, doctor status`.

### Task 8: Dashboard

**Files:** Modify `dashboard/server.py` (routes `GET /api/ai/drafts`, `POST /api/ai/drafts/<id>`, `POST /api/ai/test`, `GET /ai.js`), Create `dashboard/ai.js` (Needs-you card on Overview: list, Approve/Edit/Skip, stale highlight; Settings AI section "Test connection" button rendering the result; textContent only), Modify `dashboard/index.html` (container + script tag); Tests in `tests/test_dashboard.py` (`TestAIDrafts`, `TestAITest`).
- Edit re-validates with `is_valid_opener` / `is_valid_answer`; CSRF + session like other POSTs; `/api/ai/test` never returns the key.
- [ ] Steps: tests → implement → manual browser check against demo profile with a fake local server (`python3 -m http.server`-style stub serving `/v1/models` + `/v1/chat/completions`) → commit `feat(ai): Needs-you review card and AI test button`.

### Task 9: Docs, version, release

**Files:** `config/profile.example.toml` (`[ai]`, `[ai.jobs]` documented), README (AI section: local vs hosted, privacy line, Ollama quick start), AGENTS.md (answers keys, commands, project map `core/ai/`), SECURITY.md (what data leaves the machine), CHANGELOG `[1.3.0]`, VERSION 1.3.0.
- [ ] Full suite + `python scripts/install_smoke.py` + bandit locally if available; push branch, PR, wait for 11 checks, rebase-merge via API, tag v1.3.0, release notes from CHANGELOG, delete branch, `sdr update` on the live folder (wait if a run is in progress).
