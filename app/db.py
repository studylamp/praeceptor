"""SQLite connection management and schema.

One connection per operation (SQLite + FastAPI's threadpool make a shared
connection unsafe). WAL mode lets the admin read logs while a kid is chatting.
Schema is created idempotently on startup.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL,
    birth_year         INTEGER,               -- with birth_month, derives the age on demand
    birth_month        INTEGER,               -- 1–12; NULL (either) = age unknown
    pin_hash           TEXT    NOT NULL,
    daily_message_cap  INTEGER,            -- NULL = no cap
    daily_token_cap    INTEGER,            -- NULL = no cap
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subjects (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id         INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    grade_level        TEXT,
    curriculum_name    TEXT,
    style              TEXT,
    answer_policy      TEXT,
    gate_scope         TEXT,
    curriculum_context TEXT,
    tutor_model        TEXT    NOT NULL DEFAULT 'anthropic/claude-sonnet-5',
    tools_enabled      INTEGER NOT NULL DEFAULT 0,   -- 1 = tutor may use code/compute tools
    multi_chat_enabled INTEGER NOT NULL DEFAULT 0,   -- 1 = student may keep several chats (see conversations)
    framing_supplement TEXT,                          -- optional per-subject worldview/framing note
    active             INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    is_test     INTEGER NOT NULL DEFAULT 0,   -- 1 = admin chat-test thread, kept apart from the real ones
    title       TEXT,                         -- student-chosen chat name; NULL = date-based fallback
    archived    INTEGER NOT NULL DEFAULT 0,   -- 1 = hidden from the student's picker (still admin-visible)
    started_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- A subject with multi_chat_enabled may hold MANY real threads per (student, subject);
-- single-chat subjects keep one via get_or_create_conversation. Only the admin chat-test
-- thread stays unique per (student, subject) — enforced by idx_conversations_one_test,
-- created in init_db AFTER migrations (it references is_test, which a legacy DB gains
-- only via _migrate_conversations_is_test, so it can't be created in this script).

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT    NOT NULL,   -- 'student' | 'tutor'
    content          TEXT    NOT NULL,
    blocked          INTEGER NOT NULL DEFAULT 0,
    gate_verdict     TEXT,               -- on_subject | other_subject | off_topic
    gate_reason      TEXT,
    token_count      INTEGER,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage (
    student_id     INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    date           TEXT    NOT NULL,     -- YYYY-MM-DD
    message_count  INTEGER NOT NULL DEFAULT 0,
    token_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (student_id, date)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_subjects_student ON subjects(student_id);
CREATE INDEX IF NOT EXISTS idx_conversations_student_subject ON conversations(student_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
"""


def get_conn() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Connection scope: commits on clean exit, rolls back on error, always closes."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Schema version stamped into `PRAGMA user_version`. Bump this when you add a migration
# below that must run in order (see _stamp_schema_version). The existing idempotent
# CREATE-IF-NOT-EXISTS / _migrate_add_column steps don't need a version and run every time.
CURRENT_SCHEMA_VERSION = 1


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
    _migrate_conversations_is_test()
    _migrate_conversations_multi_chat()
    _migrate_add_column("conversations", "title", "TEXT")
    _migrate_add_column("conversations", "archived", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("subjects", "multi_chat_enabled", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("subjects", "tools_enabled", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column("subjects", "framing_supplement", "TEXT")
    _migrate_add_column("students", "birth_year", "INTEGER")
    _migrate_add_column("students", "birth_month", "INTEGER")
    _migrate_students_birthdate_backfill()
    with db() as conn:
        # One admin chat-test thread per (student, subject) — the real-thread UNIQUE is
        # gone (multi-chat), so test-thread uniqueness lives in this partial index. Created
        # here (not in SCHEMA) because it references is_test, which a legacy DB gains only
        # via _migrate_conversations_is_test above.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_one_test "
                     "ON conversations(student_id, subject_id) WHERE is_test = 1")
    _stamp_schema_version()


def _stamp_schema_version() -> None:
    """Establish a `PRAGMA user_version` baseline so future migrations can gate on it.

    Every schema change so far is applied idempotently above (CREATE TABLE IF NOT EXISTS,
    _migrate_add_column, the conversations rebuild) — safe to re-run, no ordering needed,
    so nothing here changes the schema yet. This just stamps the current version so the
    FIRST migration that DOES need ordering has a version to gate on, e.g.:

        if version < 2:
            conn.execute("ALTER TABLE ...")   # the real change
            version = 2
        conn.execute(f"PRAGMA user_version = {int(version)}")

    PRAGMA user_version can't be bound as a parameter, so the (int-forced) value is
    interpolated directly — never from untrusted input.
    """
    with db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # --- future ordered migrations go here, each advancing `version` ---
        if version < CURRENT_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {int(CURRENT_SCHEMA_VERSION)}")


def _migrate_add_column(table: str, column: str, decl: str) -> None:
    """Idempotently add a column to a table on a DB created before it existed. A plain
    ADD COLUMN (no UNIQUE/constraint rebuild needed) so this is safe and atomic; a
    fresh DB already has it via SCHEMA, so this no-ops there."""
    with db() as conn:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols or column in cols:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_students_birthdate_backfill() -> None:
    """One-time backfill of birth_year/birth_month from the legacy `age` column on a DB
    created before students stored a birthdate. Chooses birth_year/month so the derived
    age EXACTLY equals the old stored age at migration time (birth_year = this year − age,
    birth_month = this month → today is not before the birth month, so no off-by-one); the
    parent can then correct the month. No-ops on a fresh DB (no `age` column) or once the
    legacy values are retired.

    The dead `age` column is left in place (SQLite DROP COLUMN needs a table rebuild), but
    its VALUES are cleared once consumed so this pass is genuinely one-shot: otherwise, if a
    parent later cleared a student's birthdate, the next startup would resurrect it from the
    stale age. After this runs, `age` is all-NULL and the WHERE below can never re-match."""
    from datetime import datetime

    with db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)").fetchall()}
        if "age" not in cols or "birth_year" not in cols:
            return  # nothing legacy to backfill, or columns not added yet
        now = datetime.now()
        conn.execute(
            "UPDATE students SET birth_year = ? - age, birth_month = ? "
            "WHERE age IS NOT NULL AND birth_year IS NULL AND birth_month IS NULL",
            (now.year, now.month),
        )
        # Retire the consumed legacy values so a later birthdate clear can't be re-backfilled.
        conn.execute("UPDATE students SET age = NULL WHERE age IS NOT NULL")


def _migrate_conversations_is_test() -> None:
    """Add conversations.is_test (and widen the UNIQUE to include it) on a DB created
    before the admin chat test got its own thread. A fresh DB already has the new
    shape via SCHEMA, so this is a no-op there. SQLite can't ALTER a UNIQUE
    constraint in place, so the table is rebuilt (ids preserved, so messages' FK
    stays valid); foreign keys are disabled only for the structural swap."""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")  # a hot backup may hold the DB briefly
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if not cols or "is_test" in cols:
            return  # no table yet (fresh DB handled by SCHEMA), or already migrated
        seq = _autoincrement_seq(conn)
        conn.isolation_level = None  # take manual control of the transaction
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        # Defensive: clear any half-built table from an externally-interrupted run so a
        # retry starts clean (this function's own failures roll back atomically).
        conn.execute("DROP TABLE IF EXISTS conversations_new")
        conn.execute(
            """
            CREATE TABLE conversations_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                is_test     INTEGER NOT NULL DEFAULT 0,
                started_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (student_id, subject_id, is_test)
            )
            """
        )
        conn.execute(
            "INSERT INTO conversations_new (id, student_id, subject_id, is_test, started_at) "
            "SELECT id, student_id, subject_id, 0, started_at FROM conversations"
        )
        conn.execute("DROP TABLE conversations")
        conn.execute("ALTER TABLE conversations_new RENAME TO conversations")
        # The index lived on the old table; recreate it on the rebuilt one.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_student_subject "
                     "ON conversations(student_id, subject_id)")
        _restore_autoincrement_seq(conn, seq)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"conversations migration left dangling rows: {violations}")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()


def _autoincrement_seq(conn: sqlite3.Connection):
    """conversations' AUTOINCREMENT high-water mark, or None if it has never issued an
    id. Captured before a table rebuild: DROP TABLE deletes the sqlite_sequence row, and
    the copy-insert recreates it at only max(id) — so if the newest conversation had
    been deleted, the next insert would REUSE its id, and a stale admin bookmark for
    /admin/conversations/{id} could land on a different student's chat."""
    has_seq = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone()
    if not has_seq:
        return None
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'conversations'").fetchone()
    return row[0] if row else None


def _restore_autoincrement_seq(conn: sqlite3.Connection, seq) -> None:
    """Carry the captured high-water mark over to the rebuilt table (never lowering
    it), inside the rebuild's transaction. No-op when there was nothing to carry."""
    if seq is None:
        return
    if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = 'conversations'").fetchone():
        conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) "
                     "WHERE name = 'conversations'", (seq,))
    else:  # the rebuilt table copied zero rows, so the insert never recreated the row
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('conversations', ?)",
                     (seq,))


def _migrate_conversations_multi_chat() -> None:
    """Drop the table-level UNIQUE (student_id, subject_id, is_test) on a DB created
    before subjects could hold multiple real chats, adding the title/archived columns in
    the same rebuild. Test-thread uniqueness moves to the partial index
    idx_conversations_one_test (created in init_db, after this runs). A fresh DB already
    has the new shape via SCHEMA — detected by the table SQL carrying no UNIQUE — so this
    no-ops there. Runs AFTER _migrate_conversations_is_test, so is_test always exists
    here. Same rebuild recipe as that migration (ids preserved → messages' FK stays
    valid); foreign keys are disabled only for the structural swap."""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000;")  # a hot backup may hold the DB briefly
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        # Belt & braces: a table that already has `title` is the multi-chat shape — never
        # rebuild it (the copy below carries only the legacy columns, so a false-positive
        # re-run would wipe every chat's title/archived flag).
        if not cols or "title" in cols:
            return  # no table yet (fresh DB handled by SCHEMA), or already migrated
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'conversations'"
        ).fetchone()
        if row is None or "UNIQUE" not in (row["sql"] or ""):
            return
        seq = _autoincrement_seq(conn)
        conn.isolation_level = None  # take manual control of the transaction
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        # Defensive: clear any half-built table from an externally-interrupted run so a
        # retry starts clean (this function's own failures roll back atomically).
        conn.execute("DROP TABLE IF EXISTS conversations_new")
        conn.execute(
            """
            CREATE TABLE conversations_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                is_test     INTEGER NOT NULL DEFAULT 0,
                title       TEXT,
                archived    INTEGER NOT NULL DEFAULT 0,
                started_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO conversations_new (id, student_id, subject_id, is_test, started_at) "
            "SELECT id, student_id, subject_id, is_test, started_at FROM conversations"
        )
        conn.execute("DROP TABLE conversations")
        conn.execute("ALTER TABLE conversations_new RENAME TO conversations")
        # Indexes lived on the old table; recreate them on the rebuilt one. (init_db also
        # creates idx_conversations_one_test, but a mid-migration crash must not leave the
        # table without its test-thread uniqueness, so it's part of this transaction too.)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_student_subject "
                     "ON conversations(student_id, subject_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_one_test "
                     "ON conversations(student_id, subject_id) WHERE is_test = 1")
        _restore_autoincrement_seq(conn, seq)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            conn.execute("ROLLBACK")
            raise RuntimeError(
                f"conversations multi-chat migration left dangling rows: {violations}")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()
