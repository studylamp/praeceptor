"""First-run behavior.

A fresh install starts EMPTY — no students or subjects (decided 2026-07-01): a deployed
instance must not ship with placeholder or author-shaped records, so the parent creates
every student and subject in the admin console (the dashboard renders an empty state).
There is therefore no auto-seed; `db.init_db()` creates the schema and nothing else.

Only `DEFAULT_PIN` remains: the parent sets each student's PIN at creation, but the admin
console still flags any student whose PIN equals this well-known weak value (dashboard
banner + per-student badge) so a placeholder PIN can't quietly ship into real use.
"""

# Well-known weak PIN the admin console warns about (see routers/admin._is_default_pin).
DEFAULT_PIN = "1234"


def seed_if_empty() -> bool:
    """Retained as a no-op so any external caller/import stays valid. First run is empty
    by design — see the module docstring. Always returns False (nothing seeded)."""
    return False
