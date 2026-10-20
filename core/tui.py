"""Terminal UI for `sdr setup` and friends: pretty when it can be, plain when it must be.

Three ways the same prompts get answered:

* **Fancy** (a real terminal with rich + questionary installed): ASCII banner, coloured
  steps, arrow-key menus, hidden password entry.
* **Plain** (no TTY, `SDR_PLAIN=1`, `TERM=dumb`, or the libraries are missing/broken):
  numbered menus and `input()`, ASCII only, so it works in any shell, CI log or Windows
  console. If questionary fails mid-run (some Windows terminals), we fall back to plain
  prompts instead of crashing.
* **Answers** (AI agents and scripts): a flat `{"section.key": value}` dict, usually from
  `load_answers("setup-answers.toml")`. Every prompt takes a `key`; a present answer is
  validated and used without asking. Without a TTY, a missing required answer raises
  `MissingAnswer` (or, with `collect_missing=True`, is recorded so the wizard can report
  every missing key at once via `raise_missing()`).

Passwords never live in the answers file: `password("email.password", ...)` reads the
answer `email.password_env`, which names an environment variable holding the secret.

Validation and answers-file parsing live in core/tui_validation.py (pure functions) and
are re-exported here, so `from core.tui import valid_email, load_answers` works.

Windows-safe: no termios/fcntl; rich and questionary are imported lazily and optional.
"""

from __future__ import annotations

import builtins
import getpass
import os
import re
import sys
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from core.product import BRAND_COLOR
from core.tui_output import ACCENT, BANNER_ART, BANNER_TITLE, TerminalOutput  # noqa: F401 (re-exported)
from core.tui_stdin import stdin_watchable
from core.tui_validation import (  # re-exported: part of this module's public API
    GENERIC_INVALID,
    choice_title,
    dedupe,
    flatten_answers,
    load_answers,
    match_choice,
    normalize_choices,
    normalize_hex_color,
    normalize_hhmm,
    parse_list,
    run_validator,
    unknown_answers,
    valid_email,
    valid_hex_color,
    valid_hhmm,
    valid_url,
    validator,
)

__all__ = [
    "UI", "MissingAnswer", "InvalidAnswer", "MissingAnswers", "Cancelled",
    "valid_email", "valid_url", "valid_hex_color", "valid_hhmm", "validator",
    "normalize_hex_color", "normalize_hhmm", "parse_list", "flatten_answers", "load_answers", "unknown_answers",
    "BANNER_ART",
]

# Invalid input is re-asked, but not forever: a script feeding the same bad line to a
# TTY would otherwise spin. Ten tries is far more patience than any human needs.
MAX_ATTEMPTS = 10
REQUIRED_MSG = "This one is required."

_YES = {"y", "yes", "true", "1", "on"}
_NO = {"n", "no", "false", "0", "off"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")



# ── errors ───────────────────────────────────────────────────────────────────


class MissingAnswer(Exception):
    """A required answer isn't in the answers file and there's no terminal to ask on.

    `key` is exactly what to add to the answers file ("section.key"); `label` is the
    human question, so the wizard can print a to-do list the user or agent understands.
    """

    default_reason = "no answer given"

    def __init__(self, key: str, label: str, reason: str = ""):
        super().__init__(key, label, reason)
        self.key = key
        self.label = label
        self.reason = reason or self.default_reason

    def __str__(self) -> str:
        return f"{self.key} ({self.label}): {self.reason}"


class InvalidAnswer(MissingAnswer):
    """An answer was given but can't be used. Subclass of MissingAnswer on purpose: a
    wrong answer means the valid one is still missing, and callers need one except clause."""

    default_reason = "that answer isn't valid"


class MissingAnswers(Exception):
    """Every missing/invalid answer from a `collect_missing=True` run, reported together."""

    def __init__(self, errors: Sequence[MissingAnswer]):
        super().__init__(errors)
        self.errors = [*errors]

    def __str__(self) -> str:
        count = len(self.errors)
        noun = "answer is" if count == 1 else "answers are"
        lines = [f"{count} setup {noun} missing or invalid:"]
        lines += [f"  - {err}" for err in self.errors]
        lines.append("Add them to your answers file and run the command again.")
        return "\n".join(lines)


class Cancelled(KeyboardInterrupt):
    """The person pressed Ctrl-C / Ctrl-D at a prompt. A KeyboardInterrupt subclass so a
    single `except KeyboardInterrupt` in the CLI handles both."""


# ── lazy optional imports ────────────────────────────────────────────────────


def _import_rich() -> SimpleNamespace | None:
    """rich pieces we use, or None when rich is missing or broken (plain mode then)."""
    try:
        from rich import box
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.table import Table
        from rich.text import Text
    except Exception:  # noqa: BLE001 - any import failure just means "no fancy output"
        return None
    return SimpleNamespace(box=box, Console=Console, Group=Group, Panel=Panel, Rule=Rule, Table=Table, Text=Text)


def _import_questionary() -> Any:
    """The questionary module, or None. Imported lazily: it pulls in prompt_toolkit,
    which is slow to import and can't run without a real console."""
    try:
        import questionary
    except Exception:  # noqa: BLE001
        return None
    return questionary


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# ── the UI ───────────────────────────────────────────────────────────────────


class UI(TerminalOutput):
    """Prompts and messages for the setup wizard and CLI. See the module docstring.

    `answers`: flat {"section.key": value} dict (see `load_answers`).
    `plain`: force plain (True) or fancy (False); None = auto-detect.
    Keyword-only extras exist for tests and embedding: `interactive` (override TTY
    detection), `collect_missing`, `console` (a rich Console), `environ`, `out`,
    `input_fn`, `getpass_fn`.
    """

    def __init__(self, answers: Mapping[str, Any] | None = None, plain: bool | None = None, *,
                 interactive: bool | None = None, collect_missing: bool = False, console: Any = None,
                 environ: Mapping[str, str] | None = None, out: Any = None,
                 input_fn: Callable[[str], str] | None = None, getpass_fn: Callable[[str], str] | None = None):
        self.answers = {k: v for k, v in (answers or {}).items() if v is not None}
        self.environ = os.environ if environ is None else environ
        self.interactive = _isatty(sys.stdin) if interactive is None else bool(interactive)
        self.collect_missing = collect_missing
        self.missing: list[MissingAnswer] = []
        self._out = out
        self._input_fn = input_fn
        self._getpass_fn = getpass_fn
        self._qstyle: Any = None
        self._rich = None
        self.console = None
        self._questionary = None
        self.plain = self._decide_plain(plain)
        if not self.plain:
            self._rich = _import_rich()
            self.console = console or self._rich.Console(highlight=False)
            self._questionary = _import_questionary()
            if self._questionary is not None and interactive is None and not stdin_watchable():
                self._questionary = None   # arrow keys can't be read here; typed prompts still can

    def _decide_plain(self, plain: bool | None) -> bool:
        if plain is True:
            return True
        if plain is None:
            forced = str(self.environ.get("SDR_PLAIN", "")).strip().lower() in _YES
            dumb = str(self.environ.get("TERM", "")).lower() == "dumb"
            if forced or dumb or not (self.interactive and _isatty(sys.stdout)):
                return True
        return _import_rich() is None or _import_questionary() is None

    def has_answer(self, key: str) -> bool:
        return key in self.answers

    def raise_missing(self) -> None:
        """After a `collect_missing=True` pass: raise MissingAnswers if anything was missing."""
        if self.missing:
            raise MissingAnswers(self.missing)

    # ── output (the rest lives in core/tui_output.py) ──

    def pause_for(self, msg: str) -> None:
        """Tell the person to do something outside the terminal (e.g. create an app
        password), then wait for Enter. Without a TTY there's nobody to wait for."""
        self.info(msg)
        if self.interactive:
            self._read(None, lambda: self._raw_input("  Press Enter to continue... "))

    # ── input plumbing ──

    def _raw_input(self, prompt: str) -> str:
        return (self._input_fn or builtins.input)(prompt)

    def _raw_getpass(self, prompt: str) -> str:
        return (self._getpass_fn or getpass.getpass)(prompt)

    def _style(self, q: Any) -> Any:
        if self._qstyle is None:
            try:
                self._qstyle = q.Style([
                    ("qmark", f"fg:{BRAND_COLOR} bold"), ("question", "bold"),
                    ("answer", f"fg:{BRAND_COLOR} bold"), ("pointer", f"fg:{BRAND_COLOR} bold"),
                    ("highlighted", f"fg:{BRAND_COLOR} bold"), ("selected", f"fg:{BRAND_COLOR}"),
                    ("instruction", "fg:#7F8A9E"), ("separator", "fg:#7F8A9E"),
                ])
            except Exception:  # noqa: BLE001 - an unstyled prompt is fine
                self._qstyle = None
        return self._qstyle

    def _read(self, fancy: Callable[[Any], Any] | None, plain: Callable[[], Any]) -> Any:
        """One raw answer: questionary when available, else the plain reader. Ctrl-C/Ctrl-D
        become Cancelled; any other questionary failure permanently switches to plain."""
        q = self._questionary
        if q is not None and fancy is not None:
            try:
                return fancy(q)
            except (KeyboardInterrupt, EOFError):
                raise Cancelled() from None
            except Exception:  # noqa: BLE001 - e.g. prompt_toolkit NoConsoleScreenBufferError on Windows
                self._questionary = None
                self.info("Switching to simple prompts (arrow-key menus aren't available in this terminal).")
        try:
            return plain()
        except (KeyboardInterrupt, EOFError):
            raise Cancelled() from None

    def _loop(self, key: str, label: str, read: Callable[[], Any], convert: Callable[[Any], Any]) -> Any:
        for _ in range(MAX_ATTEMPTS):
            raw = read()
            try:
                return convert(raw)
            except ValueError as exc:
                self._retry(str(exc))
        raise InvalidAnswer(key, label, "no valid answer after several tries")

    def _problem(self, err: MissingAnswer, placeholder: Any) -> Any:
        """Raise, or (collect mode) record once per key and carry on with a placeholder."""
        if not self.collect_missing:
            raise err
        if all(existing.key != err.key for existing in self.missing):
            self.missing.append(err)
        return placeholder

    def _from_answers(self, key: str, label: str, convert: Callable[[Any], Any], placeholder: Any,
                      shown: Callable[[Any], str] = str) -> tuple[bool, Any]:
        """(True, value) when the answers dict settles this prompt, else (False, None)."""
        if key not in self.answers:
            return False, None
        try:
            value = convert(self.answers[key])
        except ValueError as exc:
            if self.interactive:
                self.warn(f"Your answers file has an invalid {key}: {exc} Please answer it here.")
                return False, None
            return True, self._problem(InvalidAnswer(key, label, str(exc)), placeholder)
        self._echo_answer(label, shown(value))
        return True, value

    @staticmethod
    def _plain_label(label: str, default: Any = None, required: bool = True) -> str:
        if default not in (None, "", [], ()):
            shown = str(default).replace("\n", " / ")
            return f"{label} [{shown}]: "
        if not required:
            return f"{label} (optional): "
        return f"{label}: "

    # ── prompts ──

    def _resolve(self, key: str, label: str, convert: Callable[[Any], Any], *, needed: bool, placeholder: Any,
                 offline: Callable[[], Any], shown: Callable[[Any], str] = str, hints: Sequence[str | None] = (),
                 fancy: Callable[[Any], Any] | None = None, plain: Callable[[], Any] | None = None,
                 typed: Callable[[Any], Any] | None = None, ask: Callable[[], Any] | None = None) -> Any:
        """The flow every prompt shares: a valid answer wins; with no TTY use the default, or
        report the key when `needed`; otherwise print hints and ask until the input converts
        (`typed` may be more lenient than `convert`, e.g. menu numbers)."""
        found, value = self._from_answers(key, label, convert, placeholder, shown)
        if found:
            return value
        if not self.interactive:
            return self._problem(MissingAnswer(key, label), placeholder) if needed else offline()
        for hint in hints:
            self._hint(hint)
        if ask is not None:
            return ask()
        return self._loop(key, label, lambda: self._read(fancy, plain), typed or convert)

    def text(self, key: str, label: str, default: str | None = None, required: bool = True,
             validate: Callable[[str], Any] | None = None, hint: str | None = None, *,
             normalize: Callable[[str], str] | None = None) -> str:
        """One line of text. `normalize` runs before `validate` (e.g. normalize_hex_color)."""
        def convert(raw: Any) -> str:
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise ValueError("Expected a single piece of text.")
            value = str(raw).strip()
            if not value and default not in (None, ""):
                value = str(default).strip()
            if value and normalize is not None:
                value = normalize(value)
            if not value:
                if required:
                    raise ValueError(REQUIRED_MSG)
                return ""
            run_validator(validate, value)
            return value

        # A multi-line default (e.g. a sign-off) can't be pre-filled in a one-line editor.
        multiline = isinstance(default, str) and "\n" in default
        keep = ("Press Enter to keep: " + default.replace("\n", " / ")) if multiline and self._questionary else None
        question = label if required or default else f"{label} (optional)"
        prefill = "" if default is None or multiline else str(default)
        return self._resolve(
            key, label, convert, needed=required and default in (None, ""),
            placeholder="" if default is None else str(default), offline=lambda: convert(""), hints=(hint, keep),
            fancy=lambda q: q.text(question, default=prefill, validate=self._q_check(convert), qmark="?",
                                   style=self._style(q)).unsafe_ask(),
            plain=lambda: self._raw_input(self._plain_label(label, default, required)))

    @staticmethod
    def _q_check(convert: Callable[[Any], Any]) -> Callable[[Any], bool | str]:
        """Adapt a converter to questionary's validate contract (True or an error message)."""
        def check(raw: Any) -> bool | str:
            try:
                convert(raw)
            except ValueError as exc:
                return str(exc) or GENERIC_INVALID
            return True
        return check

    def confirm(self, key: str, label: str, default: bool = True, *, required: bool = False,
                hint: str | None = None) -> bool:
        """Yes/no. Without a TTY an unanswered confirm uses `default` unless `required`."""
        def convert(raw: Any) -> bool:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, int) and raw in (0, 1):
                return bool(raw)
            text = str(raw).strip().lower() if isinstance(raw, str) else None
            if text == "":
                return default
            if text in _YES or text in _NO:
                return text in _YES
            raise ValueError("Please answer yes or no.")

        suffix = "[Y/n]" if default else "[y/N]"
        return self._resolve(
            key, label, convert, needed=required, placeholder=default, offline=lambda: default,
            shown=lambda v: "yes" if v else "no", hints=(hint,),
            fancy=lambda q: q.confirm(label, default=default, qmark="?", style=self._style(q)).unsafe_ask(),
            plain=lambda: self._raw_input(f"{label} {suffix}: "))

    def _make_choice(self, q: Any, value: Any, title: str, desc: str, **extra: Any) -> Any:
        try:
            return q.Choice(title=title, value=value, description=desc or None, **extra)
        except TypeError:  # questionary without Choice(description=...)
            return q.Choice(title=f"{title} - {desc}" if desc else title, value=value, **extra)

    def _print_options(self, label: str, options: list[tuple[Any, str, str]]) -> None:
        self._write(label)
        for number, (_value, title, desc) in enumerate(options, 1):
            self._write(f"  {number}) {title}" + (f" - {desc}" if desc else ""))

    def select(self, key: str, label: str, choices: Sequence[tuple], default: Any = None, *,
               hint: str | None = None) -> Any:
        """Pick one of (value, label, description) choices; returns the value. Typed menus
        also accept the option number; answers must name the value (or its label)."""
        options = normalize_choices(choices)
        values = [value for value, _t, _d in options]
        if default is not None and default not in values:
            raise ValueError(f"default {default!r} is not one of the choices")

        def convert(raw: Any, allow_index: bool = False) -> Any:
            if isinstance(raw, str) and not raw.strip():
                if default is not None:
                    return default
                raise ValueError("Please pick one of the options.")
            return match_choice(options, raw, allow_index)

        def fancy(q: Any) -> Any:
            q_choices = [self._make_choice(q, v, t, d) for v, t, d in options]
            pointed = q_choices[values.index(default)] if default is not None else None
            return q.select(label, choices=q_choices, default=pointed, qmark="?", style=self._style(q)).unsafe_ask()

        def plain() -> str:
            self._print_options(label, options)
            marker = f" [{values.index(default) + 1}]" if default is not None else ""
            return self._raw_input(f"Choose 1-{len(options)}{marker}: ")

        return self._resolve(key, label, convert, needed=default is None,
                             placeholder=values[0] if default is None else default, offline=lambda: default,
                             shown=lambda v: choice_title(options, v), hints=(hint,), fancy=fancy, plain=plain,
                             typed=lambda raw: convert(raw, allow_index=True))

    def checkbox(self, key: str, label: str, choices: Sequence[tuple], default: Sequence[Any] | None = None, *,
                 required: bool = True, hint: str | None = None) -> list[Any]:
        """Pick several (e.g. send days). Returns values in the order of `choices`."""
        options = normalize_choices(choices)
        chosen = [*default] if default is not None else None

        def convert(raw: Any, allow_index: bool = False) -> list[Any]:
            if isinstance(raw, str):
                tokens: list[Any] = [t.strip() for t in re.split(r"[,;\r\n]+", raw) if t.strip()]
            elif isinstance(raw, (list, tuple)):
                tokens = [*raw]
            else:
                raise ValueError("Expected a list of options.")
            picked = [match_choice(options, token, allow_index) for token in tokens]
            result = [value for value, _t, _d in options if value in picked]
            if not result and chosen is not None:
                return [*chosen]
            if not result and required:
                raise ValueError("Pick at least one.")
            return result

        def fancy(q: Any) -> Any:
            q_choices = [self._make_choice(q, v, t, d, checked=bool(chosen) and v in chosen) for v, t, d in options]
            return q.checkbox(label, choices=q_choices, qmark="?", style=self._style(q),
                              validate=lambda picked: bool(picked) or not required or "Pick at least one.").unsafe_ask()

        def plain() -> str:
            self._print_options(label, options)
            keep = ", ".join(choice_title(options, v) for v in chosen or [])
            marker = f" [{keep}]" if keep else ""
            return self._raw_input(f"Pick one or more, e.g. 1,3{marker}: ")

        return self._resolve(key, label, convert, needed=chosen is None and required, placeholder=chosen or [],
                             offline=lambda: chosen or [], hints=(hint,), fancy=fancy, plain=plain,
                             shown=lambda vs: ", ".join(choice_title(options, v) for v in vs),
                             typed=lambda raw: convert(raw, allow_index=True))

    def password(self, key: str, label: str, *, required: bool = True, confirm: bool = False,
                 hint: str | None = None) -> str:
        """Hidden entry. Answers mode reads answers[key + "_env"] -> os.environ[name];
        a raw password in the answers is refused. The value is never printed.
        Naming an env var that turns out to be unset is reported even when not `required`:
        whoever wrote the answers clearly meant to provide it."""
        env_key = f"{key}_env"
        if key in self.answers:
            err = InvalidAnswer(key, label, f"passwords can't go in the answers file; set {env_key} "
                                            "to the name of an environment variable that holds it")
            if not self.interactive:
                return self._problem(err, "")
            self.warn(str(err))
        from_env = self._password_from_env(env_key, label)
        if from_env is not None:
            return from_env
        if not self.interactive:  # env_key is absent here; _password_from_env handled it otherwise
            if not required:
                return ""
            return self._problem(MissingAnswer(env_key, label, "name the environment variable that holds it"), "")
        self._hint(hint)
        return self._ask_password(key, label, required, confirm)

    def _password_from_env(self, env_key: str, label: str) -> str | None:
        """The secret from the environment variable named in the answers, or None (after
        warning, or raising/recording when nobody can be asked)."""
        if env_key not in self.answers:
            return None
        name = self.answers[env_key]
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name.strip()):
            err = InvalidAnswer(env_key, label, "must be the NAME of an environment variable, like SDR_EMAIL_PASSWORD")
        else:
            name = name.strip()
            value = str(self.environ.get(name, "")).strip()
            if value:
                self._echo_answer(label, f"(from environment variable {name})")
                return value
            err = MissingAnswer(env_key, label, f"the environment variable {name} is empty or not set. Set it in "
                                                f"your terminal first (macOS/Linux: export {name}=...; Windows "
                                                f"PowerShell: $env:{name} = '...')")
        if not self.interactive:
            return self._problem(err, "")
        self.warn(f"{err.reason} - please type it instead.")
        return None

    def _ask_password(self, key: str, label: str, required: bool, confirm: bool) -> str:
        def convert(raw: Any) -> str:
            value = str(raw or "").strip()
            if required and not value:
                raise ValueError(REQUIRED_MSG)
            return value

        def reader(prompt: str) -> Callable[[], Any]:
            return lambda: self._read(
                lambda q: q.password(prompt, validate=self._q_check(convert), qmark="?",
                                     style=self._style(q)).unsafe_ask(),
                lambda: self._raw_getpass(f"{prompt} (hidden): "))

        for _ in range(MAX_ATTEMPTS):
            value = self._loop(key, label, reader(label), convert)
            if not confirm or not value:
                return value
            if str(reader("Type it again")() or "").strip() == value:
                return value
            self._retry("Those didn't match. Let's try again.")
        raise InvalidAnswer(key, label, "the two entries never matched")

    def list(self, key: str, label: str, example: str, default: Sequence[str] | None = None, *,
             required: bool = True, validate: Callable[[str], Any] | None = None,
             normalize: Callable[[str], str] | None = None, split_commas: bool = True,
             hint: str | None = None) -> list[str]:
        """Several values. Interactive: one per line, empty line to finish (";" also splits).
        Answers: a TOML list, or a string split like `parse_list`. Use split_commas=False
        for values that contain commas, such as "Austin, TX"."""
        fallback = [*default] if default is not None else None

        def item(raw: str) -> str:
            value = normalize(raw) if normalize is not None else raw
            try:
                run_validator(validate, value)
            except ValueError as exc:
                raise ValueError(f"{raw}: {exc}") from None
            return value

        def convert(raw: Any) -> list[str]:
            if isinstance(raw, str):
                pieces = parse_list(raw, split_commas)
            elif isinstance(raw, (list, tuple)) and all(isinstance(x, (str, int, float)) for x in raw):
                pieces = [str(x).strip() for x in raw if str(x).strip()]
            else:
                raise ValueError("Expected a list of text values.")
            items = dedupe(item(piece) for piece in pieces)
            if not items:
                if fallback is not None:
                    return [*fallback]
                if required:
                    raise ValueError("Add at least one.")
            return items

        return self._resolve(key, label, convert, needed=fallback is None and required, placeholder=fallback or [],
                             offline=lambda: fallback or [], shown="; ".join,
                             ask=lambda: self._ask_list(key, label, example, fallback, required, split_commas,
                                                        item, hint))

    def _ask_list(self, key: str, label: str, example: str, fallback: list[str] | None, required: bool,
                  split_commas: bool, item: Callable[[str], str], hint: str | None) -> list[str]:
        def take_line(line: str, items: list[str]) -> tuple[bool, list[str]]:
            """(done, items after this line). Raises ValueError for a line to re-ask."""
            text = str(line or "").strip()
            if not text:
                if items:
                    return True, items
                if fallback is not None:
                    return True, [*fallback]
                if not required:
                    return True, []
                raise ValueError("Add at least one.")
            return False, dedupe([*items, *(item(piece) for piece in parse_list(text, split_commas))])

        self._emit(label, lambda: self._rich.Text.assemble(("? ", f"bold {BRAND_COLOR}"), (label, "bold")))
        self._hint(hint)
        self._hint(f"One per line, e.g. {example}. Press Enter on an empty line when done.")
        if fallback:
            self._hint("Press Enter now to keep: " + "; ".join(fallback))
        items: list[str] = []
        failures = 0
        while failures < MAX_ATTEMPTS:
            snapshot = [*items]
            line = self._read(
                lambda q: q.text("", qmark="  " + self._sym("›", ">"), style=self._style(q),
                                 validate=self._q_check(lambda s: take_line(s, snapshot))).unsafe_ask(),
                lambda: self._raw_input("  > "))
            try:
                done, items = take_line(line, items)
            except ValueError as exc:
                failures += 1
                self._retry(str(exc))
                continue
            if done:
                return items
        raise InvalidAnswer(key, label, "no valid answer after several tries")
