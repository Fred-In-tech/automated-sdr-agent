#!/bin/bash
# Runs a task for cron — installed by `sdr schedule on` (see core/scheduler.py).
# Windows uses run_task.cmd instead.
#   run_cron_pipeline.sh            full SDR pipeline (inbox -> leads -> emails -> digest)
#   run_cron_pipeline.sh inbox      quick reply check only (sends no cold email)
#   run_cron_pipeline.sh update     check for a new version (cli.py update --scheduled)
TASK="${1:-pipeline}"
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"
# Lead data is personal data: the log (sent-email lines, runner output), the database and the
# CSV export this run creates must be readable by this user only. Python inherits the umask.
umask 077
mkdir -p "$DIR/data"
LOG="$DIR/data/cron.log"

# Keep the log small: rotate at 5 MB (one previous copy kept as cron.log.1)
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5000000 ]; then
    mv "$LOG" "$LOG.1"
fi

# cron has a bare PATH, so try the usual install locations and use the first
# Python (3.11+) that has this project's requirements installed.
# Override with PYTHON=/path/to/python in the crontab line if needed.
has_requirements() {
    [ -x "$1" ] && "$1" -c "import tomllib, requests, bs4, dns.resolver" >/dev/null 2>&1
}

if [ -z "$PYTHON" ]; then
    for candidate in \
        "$DIR/.venv/bin/python" \
        $(ls -r /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null) \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        "$(command -v python3)"; do
        if has_requirements "$candidate"; then
            PYTHON="$candidate"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "=== $(date) — ERROR: no Python 3.11+ with requirements found. Run: pip install -r requirements.txt ===" >> "$LOG"
    exit 1
fi

echo "=== $(date) — running $TASK with $PYTHON ===" >> "$LOG"
if [ "$TASK" = "update" ]; then
    "$PYTHON" cli.py update --scheduled >> "$LOG" 2>&1
else
    "$PYTHON" runner.py --task "$TASK" >> "$LOG" 2>&1
fi
