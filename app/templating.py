"""Shared Jinja2 templates instance. Autoescaping is on by default for .html, so
student/tutor/admin text is HTML-escaped — never use `| safe` on user content."""

from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

from app import clock
from app.ages import calc_age
from app.config import BASE_DIR, settings
from app.render import render_tutor_markdown

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
# `| tutor_md` renders a tutor reply's markdown to sanitized, safe HTML.
templates.env.filters["tutor_md"] = render_tutor_markdown
# Optional source-code link for the admin footer; empty string when unset (no link).
templates.env.globals["project_url"] = settings.project_url
# Build/version string for the admin footer + /healthz — a `git describe` value like
# "v0.1.0", "v0.1.0-3-gabc1234", a bare "abc1234", or "dev" (unset / plain build).
templates.env.globals["app_version"] = settings.app_version


def _age_of(row) -> int | None:
    """`| age_of` — a student/conversation row's derived age from birth_month/year,
    or None (rendered as "—") when either is unset or the columns aren't present."""
    try:
        return calc_age(row["birth_year"], row["birth_month"])
    except (KeyError, IndexError, TypeError):
        return None


templates.env.filters["age_of"] = _age_of
# `current_year()` — callable so a long-running server stays current across a year roll.
templates.env.globals["current_year"] = lambda: datetime.now(clock.get_app_tz()).year


def _localdt(value: str | None) -> str:
    """Format a stored UTC timestamp (SQLite `datetime('now')` → "YYYY-MM-DD HH:MM:SS")
    in the configured display zone (server-local when unset), so the admin's transcript
    times line up with the date basis the daily caps use. Falls back to the raw value if
    unparseable."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return value
    # Portable format (no %-d/%-I, which fail on Windows): e.g. "Jun 05, 2026 02:30 PM".
    return clock.to_local(dt).strftime("%b %d, %Y %I:%M %p")


# `| localdt` shows a stored UTC timestamp in the configured display zone.
templates.env.filters["localdt"] = _localdt
