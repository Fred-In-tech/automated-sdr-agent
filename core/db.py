import sqlite3
import os
import re
import json
import stat
from datetime import datetime, timezone

# Each install keeps its own private SQLite database (git-ignored).
# Set AUTOMATIONS_DB_PATH to use a different file.
DB_PATH = os.getenv("AUTOMATIONS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "automations.db"
)
DB_DIR = os.path.dirname(DB_PATH)

# Lead data is personal data. .env and update backups were already owner-only, but the database,
# the CSV export and the cron log took the process umask (0644 on most systems), so on a shared
# computer any other account could read every prospect's email and reply. These helpers close
# that gap. Windows ignores the mode bits: there the user folder's ACLs protect the files.
PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


def make_private(path: str, mode: int = PRIVATE_FILE) -> None:
    """Best-effort chmod: a missing file, or one owned by someone else, is not an error."""
    if os.name == "nt":
        return
    try:
        if stat.S_IMODE(os.stat(path).st_mode) != mode:
            os.chmod(path, mode)
    except OSError:
        pass


def ensure_private_dir(path: str) -> None:
    """Create `path` owner-only if it doesn't exist yet. An existing folder is left alone: with
    AUTOMATIONS_DB_PATH it may be one the user shares with other programs."""
    if os.path.isdir(path):
        return
    os.makedirs(path, mode=PRIVATE_DIR, exist_ok=True)
    make_private(path, PRIVATE_DIR)  # the umask filters makedirs' mode


def open_private(path: str, **kwargs):
    """Open a text file for writing that is owner-only from the moment it exists (an older,
    world-readable copy is tightened too). `kwargs` go to the text wrapper: encoding, newline."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, PRIVATE_FILE)
    try:
        make_private(path)
        return os.fdopen(fd, "w", **kwargs)
    except BaseException:
        os.close(fd)
        raise


def get_connection():
    ensure_private_dir(DB_DIR)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    make_private(DB_PATH)  # SQLite creates it with the umask; its journals copy the database's mode
    conn.row_factory = sqlite3.Row
    return conn


# Columns added after the first release. init_db() adds any that are missing,
# so existing databases upgrade in place without losing data.
LEAD_COLUMNS = {
    "first_name": "TEXT",           # real first name if we found one, else NULL ("Hi there")
    "website": "TEXT",
    "fit_score": "INTEGER",         # 0-100 ICP fit, see core/qualify.py
    "fit_reasons": "TEXT",          # JSON list of human-readable reasons
    "sequence_step": "INTEGER DEFAULT 0",  # how many emails of the sequence were sent
    "last_contacted_at": "TEXT",
    "next_touch_at": "TEXT",        # when the next follow-up is due (NULL = none scheduled)
    "thread_subject": "TEXT",       # subject of the first email, follow-ups reply in-thread
    "thread_message_id": "TEXT",
    "opener": "TEXT",               # AI-written personal first line ({{opener}}), NULL = profile default
    "variant": "TEXT",              # subject-line A/B test variant ("A", "B", ...)
    "signature_variant": "TEXT",    # signature A/B test: "logo" or "plain"
}
EMAIL_LOG_COLUMNS = {
    "step": "INTEGER DEFAULT 1",
    "message_id": "TEXT",
}

# Pipeline stages a lead moves through
ACTIVE_STATUSES = ("new", "contacted")
SUPPRESSED_STATUSES = ("unsubscribed", "not_interested", "bounced", "invalid_email", "disqualified")


# Table/column names and types can't be bound as SQL parameters, so the schema migration only
# accepts plain identifiers and simple column types (they are code constants; this keeps it so).
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_COLUMN_TYPE = re.compile(r"^(TEXT|INTEGER|REAL|BLOB|NUMERIC)( DEFAULT (-?[0-9]+|NULL|'[A-Za-z0-9_ -]*'))?$")


def _add_missing_columns(cursor, table: str, columns: dict) -> None:
    unsafe = [x for x in (table, *columns) if not _SQL_IDENTIFIER.match(x)]
    unsafe += [t for t in columns.values() if not _SQL_COLUMN_TYPE.match(t)]
    if unsafe:
        raise ValueError(f"Refusing unsafe schema identifiers/types: {unsafe}")
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Leads Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            title TEXT,
            company TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            category TEXT,
            location TEXT,
            enriched_info TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT NOT NULL
        )
    """)
    
    # 2. Email Campaigns Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            template_body TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            sent_count INTEGER DEFAULT 0,
            opened_count INTEGER DEFAULT 0,
            clicked_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # 3. Email Logs Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            lead_id INTEGER,
            lead_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'sent',
            sent_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES email_campaigns(id),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)

    # 4. Social Posts Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT 'X/Twitter',
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT DEFAULT 'scheduled',
            likes INTEGER DEFAULT 0,
            retweets INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # 5. Bot Execution Logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_name TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    # 6. Inbound replies the SDR has processed (also prevents handling one twice)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inbound_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            lead_id INTEGER,
            from_email TEXT NOT NULL,
            subject TEXT,
            excerpt TEXT,
            intent TEXT,
            action TEXT,
            received_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        )
    """)

    _add_missing_columns(cursor, "leads", LEAD_COLUMNS)
    _add_missing_columns(cursor, "email_logs", EMAIL_LOG_COLUMNS)

    conn.commit()
    conn.close()

def log_event(bot_name: str, action: str, status: str, details: str = ""):
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        INSERT INTO bot_logs (bot_name, action, status, details, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (bot_name, action, status, details, now))
    conn.commit()
    conn.close()

def get_recent_logs(limit: int = 50):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM bot_logs ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def get_db_stats():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM leads")
    total_leads = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM leads WHERE sequence_step > 0 OR status NOT IN ('new', 'disqualified', 'invalid_email')")
    contacted_leads = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM email_campaigns")
    total_campaigns = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM email_logs")
    total_emails_sent = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM social_posts")
    total_social_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM social_posts WHERE status = 'published'")
    published_social_posts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM bot_logs")
    total_bot_runs = cursor.fetchone()[0]

    conn.close()

    return {
        "total_leads": total_leads,
        "contacted_leads": contacted_leads,
        "total_campaigns": total_campaigns,
        "total_emails_sent": total_emails_sent,
        "total_social_posts": total_social_posts,
        "published_social_posts": published_social_posts,
        "total_bot_runs": total_bot_runs
    }

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
