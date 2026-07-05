"""Query helpers (raw SQL over sqlite3). Thin, explicit, no ORM.

All functions open their own short-lived connection via db(). Rows come back as
sqlite3.Row (dict-like). Datetimes are stored/compared as UTC text via SQLite's
datetime('now'); the `date` used for caps is YYYY-MM-DD.
"""

from typing import Optional

from app.db import db

# Column allowlists for the dynamic-update helpers below. Keys passed to
# update_*/create_subject MUST be a subset of these — this blocks both SQL
# identifier injection and mass-assignment of columns a caller shouldn't touch.
_STUDENT_COLS = frozenset(
    {"name", "birth_year", "birth_month", "pin_hash", "daily_message_cap", "daily_token_cap"}
)
_SUBJECT_COLS = frozenset(
    {"name", "grade_level", "curriculum_name", "style", "answer_policy",
     "gate_scope", "curriculum_context", "tutor_model", "tools_enabled",
     "multi_chat_enabled", "framing_supplement", "active"}
)


def _check_cols(fields, allowed) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"disallowed column(s): {sorted(bad)}")


# ----------------------------- students -----------------------------

def create_student(
    name: str,
    birth_year: Optional[int],
    birth_month: Optional[int],
    pin_hash: str,
    daily_message_cap: Optional[int] = None,
    daily_token_cap: Optional[int] = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO students (name, birth_year, birth_month, pin_hash, "
            "daily_message_cap, daily_token_cap) VALUES (?, ?, ?, ?, ?, ?)",
            (name, birth_year, birth_month, pin_hash, daily_message_cap, daily_token_cap),
        )
        return int(cur.lastrowid)


def get_student(student_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()


def list_students():
    with db() as conn:
        return conn.execute("SELECT * FROM students ORDER BY name").fetchall()


def update_student(student_id: int, **fields) -> None:
    if not fields:
        return
    _check_cols(fields, _STUDENT_COLS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE students SET {cols} WHERE id = ?", (*fields.values(), student_id))


def delete_student(student_id: int) -> None:
    """Remove a student and (via ON DELETE CASCADE) their subjects, conversations,
    messages, and usage. Destructive — the admin UI confirms first."""
    with db() as conn:
        conn.execute("DELETE FROM students WHERE id = ?", (student_id,))


# ----------------------------- subjects -----------------------------

def create_subject(student_id: int, name: str, **fields) -> int:
    _check_cols(fields, _SUBJECT_COLS)
    cols = ["student_id", "name", *fields.keys()]
    placeholders = ", ".join("?" for _ in cols)
    with db() as conn:
        cur = conn.execute(
            f"INSERT INTO subjects ({', '.join(cols)}) VALUES ({placeholders})",
            (student_id, name, *fields.values()),
        )
        return int(cur.lastrowid)


def get_subject(subject_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()


def list_subjects(student_id: int, active_only: bool = True):
    sql = "SELECT * FROM subjects WHERE student_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY name"
    with db() as conn:
        return conn.execute(sql, (student_id,)).fetchall()


def update_subject(subject_id: int, **fields) -> None:
    if not fields:
        return
    _check_cols(fields, _SUBJECT_COLS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE subjects SET {cols} WHERE id = ?", (*fields.values(), subject_id))


def delete_subject(subject_id: int) -> None:
    """Remove a subject and (via cascade) its conversations + messages. Destructive —
    prefer deactivating (active=0), which preserves the logs; the admin UI confirms."""
    with db() as conn:
        conn.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))


# --------------------------- conversations --------------------------

def list_conversations(student_id: Optional[int] = None):
    """Every REAL conversation with its student/subject names and roll-up counts, newest
    activity first (empty conversations last). Powers the admin log viewer. Admin chat-test
    threads (is_test=1) are excluded — they have their own review path (list_test_conversations)
    and share student/subject names with the real threads, which is confusing here."""
    sql = """
        SELECT c.id, c.student_id, c.subject_id, c.title, c.archived, c.started_at,
               st.name AS student_name, su.name AS subject_name, su.active AS subject_active,
               COUNT(m.id) AS message_count,
               SUM(CASE WHEN m.blocked = 1 THEN 1 ELSE 0 END) AS blocked_count,
               MAX(m.created_at) AS last_at
        FROM conversations c
        JOIN students st ON st.id = c.student_id
        JOIN subjects su ON su.id = c.subject_id
        LEFT JOIN messages m ON m.conversation_id = c.id
        WHERE c.is_test = 0
    """
    params: tuple = ()
    if student_id is not None:
        sql += " AND c.student_id = ?"
        params = (student_id,)
    sql += " GROUP BY c.id ORDER BY last_at DESC"  # SQLite sorts NULL last under DESC
    with db() as conn:
        return conn.execute(sql, params).fetchall()


def get_conversation(conversation_id: int):
    """A single conversation with its student/subject names, for the transcript view."""
    with db() as conn:
        return conn.execute(
            """
            SELECT c.id, c.student_id, c.subject_id, c.is_test, c.title, c.archived,
                   c.started_at,
                   st.name AS student_name, st.birth_year, st.birth_month,
                   su.name AS subject_name, su.active AS subject_active
            FROM conversations c
            JOIN students st ON st.id = c.student_id
            JOIN subjects su ON su.id = c.subject_id
            WHERE c.id = ?
            """,
            (conversation_id,),
        ).fetchone()


def get_or_create_conversation(student_id: int, subject_id: int, is_test: bool = False) -> int:
    """The student's CURRENT thread for a subject, created on first use. A multi-chat
    subject can hold several real threads; this returns the most recently ACTIVE one
    (un-archived preferred), which is both the single-chat behavior (one thread ever
    exists) and the landing chat when a parent turns multi-chat back off. The admin
    chat test passes is_test=True to get its SEPARATE, unique-per-subject thread that
    never mixes with the student's real history (see clear_test_conversations)."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT c.id FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.student_id = ? AND c.subject_id = ? AND c.is_test = ?
            GROUP BY c.id
            ORDER BY c.archived ASC, COALESCE(MAX(m.created_at), c.started_at) DESC, c.id DESC
            LIMIT 1
            """,
            (student_id, subject_id, int(is_test)),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO conversations (student_id, subject_id, is_test) VALUES (?, ?, ?)",
            (student_id, subject_id, int(is_test)),
        )
        return int(cur.lastrowid)


def list_subject_chats(student_id: int, subject_id: int):
    """All REAL chats for one (student, subject) with roll-up counts, most recently
    active first and un-archived before archived — the student's chat switcher, whose
    first row matches what get_or_create_conversation would pick. Counts are display
    hints and include blocked turns (they're still activity)."""
    with db() as conn:
        return conn.execute(
            """
            SELECT c.id, c.title, c.archived, c.started_at,
                   COUNT(m.id) AS message_count,
                   MAX(m.created_at) AS last_at
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.student_id = ? AND c.subject_id = ? AND c.is_test = 0
            GROUP BY c.id
            ORDER BY c.archived ASC, COALESCE(MAX(m.created_at), c.started_at) DESC, c.id DESC
            """,
            (student_id, subject_id),
        ).fetchall()


def create_chat(student_id: int, subject_id: int, title: Optional[str]) -> int:
    """Start a new REAL chat for a multi-chat subject. If an UNNAMED, un-archived,
    EMPTY chat already exists (e.g. "New chat" tapped twice, or the auto-created first
    thread was never used), it's reused — titled and its started_at refreshed so the
    date-based fallback name stays honest — instead of stacking blank threads. A chat
    the student explicitly TITLED or ARCHIVED is never reused, even empty: silently
    overwriting a student-typed title would erase it from the parent's review, and
    un-archiving would undo a deliberate action. Callers that can run while a turn is
    streaming must hold the per-student lock — an in-flight first exchange persists
    only at finalize, so its chat still LOOKS empty until then (see routers.student)."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT c.id FROM conversations c
            WHERE c.student_id = ? AND c.subject_id = ? AND c.is_test = 0
              AND c.title IS NULL AND c.archived = 0
              AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
            ORDER BY c.id DESC LIMIT 1
            """,
            (student_id, subject_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE conversations SET title = ?, started_at = datetime('now') "
                "WHERE id = ?",
                (title, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO conversations (student_id, subject_id, is_test, title) "
            "VALUES (?, ?, 0, ?)",
            (student_id, subject_id, title),
        )
        return int(cur.lastrowid)


def rename_conversation(conversation_id: int, title: Optional[str]) -> None:
    with db() as conn:
        conn.execute("UPDATE conversations SET title = ? WHERE id = ?",
                     (title, conversation_id))


def set_conversation_archived(conversation_id: int, archived: bool) -> None:
    """Archive = hidden from the student's picker only; the chat stays fully visible
    (and continuable), and the admin console always sees it."""
    with db() as conn:
        conn.execute("UPDATE conversations SET archived = ? WHERE id = ?",
                     (int(archived), conversation_id))


def delete_conversation(conversation_id: int) -> None:
    """Remove ONE chat thread and (via cascade) its messages. Destructive — the admin
    UI confirms first; students have no delete path (oversight is the point)."""
    with db() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def list_test_conversations():
    """Admin chat-test threads (is_test=1) with student/subject names, oldest first.
    For oversight/debugging; real student conversations are excluded."""
    with db() as conn:
        return conn.execute(
            """
            SELECT c.id, c.student_id, c.subject_id, c.started_at,
                   st.name AS student_name, su.name AS subject_name
            FROM conversations c
            JOIN students st ON st.id = c.student_id
            JOIN subjects su ON su.id = c.subject_id
            WHERE c.is_test = 1
            ORDER BY c.id
            """
        ).fetchall()


def clear_subject_conversation(subject_id: int) -> None:
    """Drop ALL of the student's REAL chats for one subject (messages cascade), giving a
    clean slate — e.g. when the student starts a new chapter. The subject and its
    settings are kept; the next message reopens a fresh thread via get_or_create_conversation.
    The admin chat-test thread (is_test=1) is untouched — it has its own clear path."""
    with db() as conn:
        conn.execute(
            "DELETE FROM conversations WHERE subject_id = ? AND is_test = 0",
            (subject_id,),
        )


def clear_test_conversations() -> None:
    """Wipe ALL admin chat-test threads (and their messages, via cascade). Backs the
    chat test's "New conversation" button; never touches real student conversations."""
    with db() as conn:
        conn.execute("DELETE FROM conversations WHERE is_test = 1")


# ----------------------------- messages -----------------------------

def add_message(
    conversation_id: int,
    role: str,
    content: str,
    blocked: bool = False,
    gate_verdict: Optional[str] = None,
    gate_reason: Optional[str] = None,
    token_count: Optional[int] = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, blocked, gate_verdict, "
            "gate_reason, token_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, int(blocked), gate_verdict, gate_reason, token_count),
        )
        return int(cur.lastrowid)


def get_messages(conversation_id: int, include_blocked: bool = True):
    sql = "SELECT * FROM messages WHERE conversation_id = ?"
    if not include_blocked:
        sql += " AND blocked = 0"
    sql += " ORDER BY id"
    with db() as conn:
        return conn.execute(sql, (conversation_id,)).fetchall()


# The student view shows only the real dialogue: tutor replies + on-subject student
# turns. Kept as SQL (not a Python post-filter) so LIMIT and the has_more probe count
# only shown rows. Mirrors pipeline._history_for_tutor's visibility rule exactly.
_VISIBLE_MESSAGE_FILTER = (
    " AND blocked = 0 AND (role = 'tutor' OR gate_verdict IS NULL "
    "OR gate_verdict = 'on_subject')"
)


def get_messages_page(conversation_id: int, before_id: Optional[int] = None,
                      limit: Optional[int] = None, visible_only: bool = False):
    """A page of a conversation's messages for lazy "load earlier" display, returned
    oldest-first with a `has_more` flag. Display-only paging — the tutor's context is
    rebuilt separately (pipeline._history_for_tutor) and is not affected by this.

    `before_id` is a cursor: return only messages older than it (id < before_id); None
    starts from the newest. `limit` bounds the page (None = the whole thread, e.g. the
    no-JS "show all" fallback), and one extra row is fetched to compute `has_more`.
    `visible_only` applies the STUDENT visibility filter (drop blocked turns and
    non-on_subject student turns); the admin transcript passes False to see every turn.
    """
    sql = "SELECT * FROM messages WHERE conversation_id = ?"
    params: list = [conversation_id]
    if visible_only:
        sql += _VISIBLE_MESSAGE_FILTER
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    if limit is not None and limit < 1:
        limit = 1  # a non-positive window (misconfigured knob) would otherwise render an
                   # empty page yet still flag has_more — clamp so paging stays coherent
    if limit is None:
        # Whole thread, already oldest-first.
        sql += " ORDER BY id ASC"
        with db() as conn:
            return conn.execute(sql, tuple(params)).fetchall(), False
    # Newest `limit` (+1 to detect older messages), then flip to oldest-first for display.
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit + 1)
    with db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    has_more = len(rows) > limit
    rows = list(rows[:limit])
    rows.reverse()
    return rows, has_more


# ------------------------------- usage ------------------------------

def get_usage(student_id: int, date: str):
    with db() as conn:
        return conn.execute(
            "SELECT message_count, token_count FROM usage WHERE student_id = ? AND date = ?",
            (student_id, date),
        ).fetchone()


def increment_usage(student_id: int, date: str, messages: int = 0, tokens: int = 0) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO usage (student_id, date, message_count, token_count) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(student_id, date) DO UPDATE SET "
            "message_count = message_count + excluded.message_count, "
            "token_count = token_count + excluded.token_count",
            (student_id, date, messages, tokens),
        )


def reset_usage(student_id: int, date: str) -> None:
    """Wipe a student's usage counters for `date` (the admin "reset today's usage"
    action): the daily message/token caps start counting from zero again. The audit
    trail lives in conversations/messages, not here, so deleting the row loses
    nothing."""
    with db() as conn:
        conn.execute("DELETE FROM usage WHERE student_id = ? AND date = ?",
                     (student_id, date))


# ----------------------------- settings -----------------------------
# Simple key/value store for app-wide admin settings (e.g. the global educational
# framing). Values are plain text; callers are responsible for their meaning.

# Key for the parent's optional global educational/worldview framing (tutor-only).
FRAMING_SETTING_KEY = "educational_framing"

# Key for the app-wide display/cap timezone (an IANA name, e.g. "America/Chicago").
# Empty/unset → the server's local zone. See app/clock.py.
TIMEZONE_SETTING_KEY = "app_timezone"


def get_setting(key: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: Optional[str]) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
