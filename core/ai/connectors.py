"""Talking to the owner's AI over plain HTTP: no SDKs, no extra installs.

Three adapters cover every provider in core/ai/providers.py:
- "openai": the OpenAI Chat Completions format, spoken by OpenAI, Gemini, Groq, OpenRouter,
  Mistral, Azure, LM Studio, llama.cpp, vLLM, Jan and most other servers.
- "anthropic": Claude's Messages API (its OpenAI-compatible layer ignores JSON mode).
- "ollama": Ollama's native /api/chat, whose `format` gives small local models the most
  reliable JSON.

When a JSON answer is wanted, the OpenAI adapter tries the strongest structured-output mode first
and steps down (json_schema -> json_object -> instructions only) when a server rejects it. Every
failure becomes an AIError with a plain-English fix, and API keys are scrubbed from all messages.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable

from core.ai.providers import AIConfig, url_problem

ANTHROPIC_VERSION = "2023-06-01"
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
MAX_ERROR_TEXT = 300
Opener = Callable[..., object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it could carry the API key to another server, or take a
    "local only" request off this computer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "Redirects are not followed", headers, fp)


default_opener = urllib.request.build_opener(_NoRedirect())


class AIError(Exception):
    """A plain-English reason the AI call failed (already free of secrets)."""


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


def redact(text: str, secrets: Iterable[str | None]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[hidden]")
    return text


def extract_json(text: str) -> dict | None:
    """The first complete {...} object in a model's answer (models sometimes wrap it in prose)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:end + 1])
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def _schema_instructions(schema: dict) -> str:
    return ("\n\nAnswer with only a JSON object that matches this JSON schema, and nothing else:\n"
            + json.dumps(schema))


# ── HTTP ────────────────────────────────────────────────────────────────────


def _headers(cfg: AIConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "automated-sdr"}
    if cfg.api_key and cfg.auth == "bearer":
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    elif cfg.api_key and cfg.auth == "api-key":
        headers["api-key"] = cfg.api_key
    elif cfg.api_key and cfg.auth == "x-api-key":
        headers["x-api-key"] = cfg.api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    return headers


def _clean(text: str) -> str:
    """Remove reasoning that some local models write inline (<think>...</think>)."""
    return THINK_BLOCK.sub("", text or "").strip()


def _out_of_room(cfg: AIConfig) -> AIError:
    return AIError(f"{cfg.model} used its whole answer length on thinking and wrote no answer. "
                   "Pick a non-thinking model, or turn thinking off for it in the AI app.")


def _explain_http(cfg: AIConfig, code: int, detail: str) -> str:
    lowered = detail.lower()
    if "failed to load model" in lowered or "insufficient system resources" in lowered or "out of memory" in lowered:
        return (f"{cfg.label} couldn't load '{cfg.model}' (not enough memory). Pick a smaller model, "
                "or close other models in the app first.")
    if code in (301, 302, 303, 307, 308):
        return (f"{cfg.label} tried to redirect to another address. Redirects aren't followed, to keep your key "
                "and data where you sent them. Check the address in Settings > AI.")
    if code in (401, 403):
        return f"{cfg.label} rejected the key ({code}). Check the AI key in Settings > AI."
    if code == 404 and ("model" in detail.lower() or cfg.adapter == "ollama"):
        fix = f" For Ollama: ollama pull {cfg.model}." if cfg.adapter == "ollama" else ""
        return f"Model '{cfg.model}' was not found at {cfg.label}.{fix}"
    if code == 404:
        return f"{cfg.base_url} doesn't look like an AI endpoint (404). Check the address in Settings > AI."
    if code == 429:
        return f"{cfg.label} says too many requests or no credit left (429). Try again later or check your plan."
    return f"{cfg.label} returned an error ({code}): {detail[:MAX_ERROR_TEXT]}"


def _request(cfg: AIConfig, method: str, url: str, body: dict | None, opener: Opener | None) -> dict:
    opener = opener or default_opener.open
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=_headers(cfg), method=method)
    secrets = (cfg.api_key,)
    try:
        with opener(request, timeout=cfg.timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode("utf-8", "replace"), secrets) if exc.fp else ""
        raise _HTTPFailure(exc.code, detail, redact(_explain_http(cfg, exc.code, detail), secrets)) from None
    except (socket.timeout, TimeoutError):
        raise AIError(f"{cfg.label} took longer than {cfg.timeout:g} seconds to answer.") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AIError(f"{cfg.label} took longer than {cfg.timeout:g} seconds to answer.") from None
        if cfg.is_local:
            raise AIError(f"{cfg.label} isn't running at {cfg.base_url}. Start it and try again.") from None
        raise AIError(f"Couldn't reach {cfg.label} at {cfg.base_url}. Check the internet connection.") from None
    except OSError as exc:
        raise AIError(redact(f"Couldn't reach {cfg.label}: {exc}", secrets)) from None
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise AIError(f"{cfg.label} sent back something that isn't JSON. Is the address right?") from None
    if not isinstance(value, dict):
        raise AIError(f"{cfg.label} sent back an unexpected answer.")
    return value


class _HTTPFailure(AIError):
    def __init__(self, code: int, detail: str, message: str):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _check(cfg: AIConfig) -> None:
    problem = url_problem(cfg)
    if problem:
        raise AIError(problem)
    if cfg.mode == "hosted" and not cfg.is_local and not cfg.api_key:
        raise AIError(f"No API key is saved for {cfg.label}. Add it in Settings > AI.")
    if not cfg.model:
        raise AIError(f"No model is chosen for {cfg.label}. Pick one in Settings > AI.")


def _ollama_root(cfg: AIConfig) -> str:
    base = cfg.base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


# ── adapters ────────────────────────────────────────────────────────────────


def _chat_openai(cfg, system, user, schema, max_tokens, opener) -> Reply:
    url = f"{cfg.base_url}/chat/completions"
    formats: list[dict | None] = [None]
    if schema:
        formats = [{"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}},
                   {"type": "json_object"}, None]
    for index, response_format in enumerate(formats):
        prompt = system + (_schema_instructions(schema) if schema and response_format is None else "")
        body = {"model": cfg.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}]}
        if response_format:
            body["response_format"] = response_format
        try:
            data = _request(cfg, "POST", url, body, opener)
        except _HTTPFailure as exc:
            unsupported = exc.code in (400, 422) and index < len(formats) - 1
            if unsupported:
                continue          # this server doesn't do that JSON mode: step down the ladder
            raise
        try:
            choice = data["choices"][0]
            text = _clean(choice["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise AIError(f"{cfg.label} sent back an answer in an unexpected format.") from None
        if not text and choice.get("finish_reason") == "length":
            raise _out_of_room(cfg)
        usage = data.get("usage") or {}
        return Reply(text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
    raise AIError(f"{cfg.label} rejected every request format.")


def _chat_anthropic(cfg, system, user, schema, max_tokens, opener) -> Reply:
    body = {"model": cfg.model, "max_tokens": max_tokens,
            "system": system + (_schema_instructions(schema) if schema else ""),
            "messages": [{"role": "user", "content": user}]}
    data = _request(cfg, "POST", f"{cfg.base_url}/messages", body, opener)
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise AIError(f"{cfg.label} sent back an answer in an unexpected format.")
    text = _clean("".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"))
    if not text and data.get("stop_reason") == "max_tokens":
        raise _out_of_room(cfg)
    usage = data.get("usage") or {}
    return Reply(text, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))


def _chat_ollama(cfg, system, user, schema, max_tokens, opener) -> Reply:
    body = {"model": cfg.model, "stream": False, "options": {"num_predict": max_tokens},
            "messages": [{"role": "system", "content": system + (_schema_instructions(schema) if schema else "")},
                         {"role": "user", "content": user}]}
    if schema:
        body["format"] = schema
    data = _request(cfg, "POST", f"{_ollama_root(cfg)}/api/chat", body, opener)
    message = data.get("message")
    if not isinstance(message, dict):
        raise AIError(f"{cfg.label} sent back an answer in an unexpected format.")
    text = _clean(str(message.get("content") or ""))
    if not text and data.get("done_reason") == "length":
        raise _out_of_room(cfg)
    return Reply(text, int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0))


_ADAPTERS = {"openai": _chat_openai, "anthropic": _chat_anthropic, "ollama": _chat_ollama}


def chat(cfg: AIConfig, system: str, user: str, *, schema: dict | None = None, max_tokens: int = 400,
         opener: Opener | None = None) -> Reply:
    """One question to the owner's AI. Raises AIError with a fix-it message on any failure."""
    _check(cfg)
    return _ADAPTERS[cfg.adapter](cfg, system, user, schema, max_tokens, opener)


def list_models(cfg: AIConfig, *, opener: Opener | None = None) -> list[str]:
    """The models this AI offers (installed models for local servers)."""
    problem = url_problem(cfg)
    if problem:
        raise AIError(problem)
    if cfg.adapter == "ollama":
        data = _request(cfg, "GET", f"{_ollama_root(cfg)}/api/tags", None, opener)
        return [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]
    data = _request(cfg, "GET", f"{cfg.base_url}/models", None, opener)
    return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
