#!/bin/bash
# Opens the dashboard in your browser, starting it first if it isn't running (same as `sdr dashboard`).
# It stays running in this terminal: press Ctrl+C to stop it.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif [ -x "$DIR/.venv/bin/python" ]; then
    PY="$DIR/.venv/bin/python"
else
    PY="$(command -v python3)"
fi
if [ -z "$PY" ]; then
    echo "Python 3.11+ not found. Re-run the installer, or create $DIR/.venv" >&2
    exit 1
fi

exec "$PY" "$DIR/cli.py" dashboard "$@"
