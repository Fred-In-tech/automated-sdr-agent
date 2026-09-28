"""Small TOML writer + a surgical profile updater, and the .env writer, for `sdr setup`.

Why not a TOML library: tomllib (stdlib) only reads, and a writer dependency would reformat
the user's file and drop their comments. `sdr setup --section <name>` must change only the keys
that step owns and leave everything else — hand-written email copy, comments, sections we don't
know about — exactly as it was. So `update_profile_text` edits the text in place:

- it finds each `[table]` and each `key = value` in it (a value may span lines: multi-line
  strings and arrays), using tomllib itself to know where a value ends, so it never mistakes a
  line inside an email body for a table header;
- replaces the managed keys (keeping a trailing `# comment` on single-line values), inserts
  missing keys after the table's last key, and appends missing tables at the end;
- re-parses the result and checks every managed value came out as intended, so a bug here can
  never silently write a broken profile.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from typing import Any, Mapping

__all__ = [
    "REMOVE", "toml_str", "toml_multiline", "toml_list", "toml_value", "toml_key",
    "update_profile_text", "read_env_file", "update_env_file", "parse_env_value", "format_env_value",
    "write_private_file", "write_text_atomic",
]


class _Remove:
    """Sentinel: delete this key (used as a value in `update_profile_text` updates)."""

    def __repr__(self) -> str:
        return "REMOVE"


REMOVE = _Remove()

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_KEY_PART = r"(?:[A-Za-z0-9_-]+|\"(?:[^\"\\]|\\.)*\"|'[^']*')"
_KEY_RE = re.compile(r"^\s*(" + _KEY_PART + r"(?:\s*\.\s*" + _KEY_PART + r")*)\s*=\s*")
_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*(\]\]?)\s*(?:#.*)?$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # everything but \t and \n


# ── writing values ───────────────────────────────────────────────────────────


def toml_str(value: str) -> str:
    """A one-line TOML basic string. JSON escaping is TOML-compatible, except DEL (U+007F),
    which TOML forbids raw and JSON leaves alone."""
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007F")


def toml_multiline(value: str) -> str:
    """A multi-line basic string ("\"\"\"...\"\"\""), readable in the file. Backslashes and runs
    of three quotes are escaped; control characters other than tab/newline become \\uXXXX;
    a leading newline is protected (TOML trims the first newline after the opening quotes)."""
    text = str(value).replace("\r\n", "\n").replace("\\", "\\\\")
    text = _CONTROL_RE.sub(lambda m: f"\\u{ord(m.group(0)):04X}", text)
    text = text.replace('"""', '""\\"')
    if text.endswith('"'):
        # `...""""` is legal but easy to misread; escape the final quote instead.
        text = text[:-1] + '\\"'
    return '"""' + ("\n" if text.startswith("\n") else "") + text + '"""'


def toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else toml_str(key)


def toml_value(value: Any) -> str:
    """Any value we write: bool, int, float, str (multi-line when it contains a newline),
    list, or dict (as an inline table, e.g. a `button = { text = "...", url = "..." }`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return toml_multiline(value) if "\n" in value else toml_str(value)
    if isinstance(value, (list, tuple)):
        return toml_list(value)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{toml_key(str(k))} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"Can't write {type(value).__name__} to TOML")


def toml_list(values) -> str:
    return "[" + ", ".join(toml_value(v) for v in values) + "]"


# ── reading the structure of an existing file ────────────────────────────────


def _unquote(part: str) -> str:
    part = part.strip()
    if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
        return part[1:-1]
    return part


def _normalize_name(raw: str) -> str:
    """`auto_reply . buttons` / `"sender"` -> `auto_reply.buttons` / `sender`."""
    parts = re.findall(_KEY_PART, raw)
    return ".".join(_unquote(p) for p in parts) if parts else raw.strip()


def _parses(chunk: str) -> bool:
    try:
        tomllib.loads(chunk)
    except tomllib.TOMLDecodeError:
        return False
    return True


def _value_end(lines: list[str], start: int) -> int:
    """Index of the last line of the key/value that starts at `start`: the first line where
    the chunk parses on its own. The whole file is valid (checked first), so the first complete
    parse is the real end — a valid value can't be followed by a stray continuation line."""
    for end in range(start, len(lines)):
        if _parses("\n".join(lines[start:end + 1])):
            return end
    return start


def _scan(lines: list[str]) -> list[tuple]:
    """[("header", line, name, is_array) | ("key", first, last, name)] in file order."""
    entries: list[tuple] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        header = _HEADER_RE.match(lines[i]) if stripped.startswith("[") else None
        if header:
            entries.append(("header", i, _normalize_name(header.group(2)), header.group(1) == "[["))
            i += 1
            continue
        key = _KEY_RE.match(lines[i])
        end = _value_end(lines, i)
        if key:
            entries.append(("key", i, end, _normalize_name(key.group(1))))
        i = end + 1
    return entries


def _table_span(entries: list[tuple], total: int, table: str) -> tuple[int, int, list[tuple]] | None:
    """(header line, end line exclusive, key entries) of the first `[table]`, or None."""
    for index, entry in enumerate(entries):
        if entry[0] == "header" and entry[2] == table and not entry[3]:
            end = next((e[1] for e in entries[index + 1:] if e[0] == "header"), total)
            keys = [e for e in entries[index + 1:] if e[0] == "key" and e[1] < end]
            return entry[1], end, keys
    return None


def _inline_comment(line: str, value_start: int) -> tuple[str, str]:
    """(whitespace, "# comment") after a single-line value, or ("", "")."""
    for match in re.finditer("#", line):
        pos = match.start()
        if pos <= value_start:
            continue
        if _parses(line[:pos]):
            before = line[:pos]
            gap = before[len(before.rstrip()):] or "  "
            return gap, line[pos:].rstrip()
    return "", ""


def _render_line(original: str | None, key: str, value: Any) -> list[str]:
    rendered = toml_value(value)
    if original is None:
        return f"{toml_key(key)} = {rendered}".split("\n")
    match = _KEY_RE.match(original)
    prefix = original[:match.end()] if match else f"{toml_key(key)} = "
    gap, comment = _inline_comment(original, len(prefix))
    if comment and "\n" not in rendered:
        return [f"{prefix}{rendered}{gap}{comment}"]
    return f"{prefix}{rendered}".split("\n")


def _update_table(lines: list[str], table: str, values: Mapping[str, Any]) -> list[str]:
    entries = _scan(lines)
    span = _table_span(entries, len(lines), table)
    if span is None:
        additions = [f"{toml_key(k)} = {toml_value(v)}" for k, v in values.items() if v is not REMOVE]
        if not additions:
            return lines
        tail = [*lines]
        while tail and not tail[-1].strip():
            tail.pop()
        header = "[" + ".".join(toml_key(part) for part in table.split(".")) + "]"
        return [*tail, "", header, *"\n".join(additions).split("\n"), ""]

    header_line, _end, keys = span
    by_name = {entry[3]: entry for entry in keys}
    edits: list[tuple[int, int, list[str]]] = []  # (first, last, replacement) — last < first = insert
    insert_after = keys[-1][2] if keys else header_line
    inserts: list[str] = []
    for key, value in values.items():
        entry = by_name.get(key)
        if entry is None:
            if value is not REMOVE:
                inserts += _render_line(None, key, value)
            continue
        first, last = entry[1], entry[2]
        replacement = [] if value is REMOVE else _render_line(lines[first] if first == last else
                                                              lines[first].split("=", 1)[0] + "= ", key, value)
        edits.append((first, last, replacement))
    if inserts:
        edits.append((insert_after + 1, insert_after, inserts))
    result = [*lines]
    for first, last, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result[first:last + 1] = replacement
    return result


def _lookup(data: dict, table: str, key: str) -> tuple[bool, Any]:
    node: Any = data
    for part in table.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    if not isinstance(node, dict) or key not in node:
        return False, None
    return True, node[key]


def _same(actual: Any, wanted: Any) -> bool:
    """Read-back check. Tuples are written as arrays; bool must stay bool (True == 1 in Python)."""
    if isinstance(wanted, tuple):
        wanted = [*wanted]
    if isinstance(wanted, bool) or isinstance(actual, bool):
        return type(actual) is type(wanted) and actual == wanted
    return actual == wanted


def update_profile_text(text: str, updates: Mapping[str, Mapping[str, Any]]) -> str:
    """Return `text` with only the given `{table: {key: value}}` changed (see the module doc).

    A value of REMOVE deletes the key. Raises ValueError when the existing text isn't valid
    TOML or the edit can't be made safely (the caller then leaves the file untouched)."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"config/profile.toml has a formatting error: {exc}") from None
    lines = text.replace("\r\n", "\n").split("\n")
    for table, values in updates.items():
        if values:
            lines = _update_table(lines, table, values)
    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    try:
        data = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Couldn't update the profile safely ({exc}). Nothing was changed.") from None
    for table, values in updates.items():
        for key, value in values.items():
            found, actual = _lookup(data, table, key)
            ok = (not found) if value is REMOVE else (found and _same(actual, value))
            if not ok:
                raise ValueError(f"Couldn't update [{table}] {key} safely. Nothing was changed; "
                                 "edit config/profile.toml by hand instead.")
    return new_text


# ── files ────────────────────────────────────────────────────────────────────


def write_text_atomic(path: str, text: str, mode: int | None = None) -> None:
    """Write via a temp file + rename, so a crash never leaves a half-written profile.
    With `mode`, the temp file is created with those permissions (no readable window)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass  # Windows: the file is in the user's own folder; ACLs protect it


def write_private_file(path: str, text: str) -> None:
    """Owner-only (0600) file, for secrets."""
    write_text_atomic(path, text, mode=0o600)


def parse_env_value(raw: str) -> str:
    """The value of a `KEY=raw` .env line. Only a *matching* pair of outer quotes is removed
    (`"..."` also unescapes \\" and \\\\; `'...'` is literal), so a password that merely starts
    or ends with a quote character survives. Anything else is taken as written, minus the
    surrounding whitespace. `format_env_value` is the inverse."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return re.sub(r"\\([\\\"])", r"\1", inner) if text[0] == '"' else inner
    return text


def format_env_value(value: str) -> str:
    """How a value is written after `KEY=`: as is when the readers would take it back
    unchanged, else double-quoted with \\ and " escaped (leading/trailing quote characters or
    whitespace are exactly what a bare value can't carry)."""
    text = str(value)
    if text == text.strip() and text[:1] not in "\"'" and text[-1:] not in "\"'":
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_env_file(path: str) -> dict[str, str]:
    """KEY=value pairs from a .env file, parsed like core.config.load_env_file, but without
    touching os.environ. Used only to know what's already set (never printed)."""
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    values[key.strip()] = parse_env_value(val)
    except OSError:
        pass
    return values


def update_env_file(values: Mapping[str, Any], path: str | None = None) -> None:
    """Set keys in .env, keeping every other line (comments included) as it was.

    The file is written atomically with owner-only permissions (0600). Values can't contain
    line breaks: a newline would smuggle an extra KEY=value line into the file. Values that a
    bare `KEY=value` line can't carry are quoted (see `format_env_value`)."""
    if path is None:
        from core import config
        path = config.ENV_PATH
    for key, value in values.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(key)):
            raise ValueError(f"Not a valid .env key: {key!r}")
        if any(ch in str(value) for ch in "\r\n\x00"):
            raise ValueError(f"The value for {key} can't contain line breaks.")
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    remaining = {k: format_env_value(v) for k, v in values.items()}
    updated = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    write_private_file(path, "\n".join(updated) + "\n")
