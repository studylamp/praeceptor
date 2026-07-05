"""The app-wide display/cap timezone.

The parent picks a zone in the admin console (stored in the `settings` k/v table); a
single zone drives BOTH things that depend on the time of day — the daily-cap reset
boundary (`pipeline._today`) and how stored timestamps render (`templating._localdt`).
The two are deliberately tied so a transcript's shown times line up with the day a
message was charged against.

Unset → the server's own local zone, which is the historical behavior, so nothing
shifts until a zone is chosen. Stored message timestamps stay UTC (SQLite
`datetime('now')`); only the *interpretation* of "today" and the *rendering* of those
UTC timestamps use this zone.

The resolved zone is cached and invalidated on save (`invalidate`), so a change takes
effect on the next request without a DB read per timestamp. With multiple app processes
a change lands per-process on its next cache miss; the LAN/docker-compose deployment is
single-process.
"""

import sys
import threading
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from app import models

# Sentinel: distinguishes "cache not populated yet" from a cached value of None
# (None is a real, meaningful result → "unset, use the server's local zone").
_NOT_LOADED = object()
_tz_cache = _NOT_LOADED
# Guards the load-and-store in get_app_tz so a save's invalidate() can't be lost to a
# read that was mid-flight — without it, a reader that fetched the OLD value could store
# it back AFTER invalidate() cleared the cache, stranding the stale zone until the next
# save. The hot path (an already-populated cache) reads without the lock: a plain
# reference read/assignment is atomic under the GIL.
_lock = threading.Lock()


def _resolve(name: str) -> ZoneInfo | None:
    """Turn a stored setting value into a ZoneInfo, or None for unset/invalid."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # A bad stored value (hand-edited DB, or tzdata missing) must never break
        # rendering or the cap check — fall back to server-local and warn.
        print(f"WARNING [praeceptor]: unknown app timezone {name!r} — using the "
              "server's local zone. Set a valid zone in admin → Settings.",
              file=sys.stderr)
        return None


def get_app_tz() -> ZoneInfo | None:
    """The configured ZoneInfo, or None when unset (meaning the server's local zone).
    `datetime.now(None)` / `dt.astimezone(None)` fall back to server-local, so callers
    can pass this straight through without special-casing None."""
    global _tz_cache
    cached = _tz_cache
    if cached is not _NOT_LOADED:
        return cached
    with _lock:
        if _tz_cache is _NOT_LOADED:
            name = (models.get_setting(models.TIMEZONE_SETTING_KEY) or "").strip()
            _tz_cache = _resolve(name)
        return _tz_cache


def invalidate() -> None:
    """Drop the cached zone so the next lookup re-reads the setting. Call after saving."""
    global _tz_cache
    with _lock:
        _tz_cache = _NOT_LOADED


def today_str() -> str:
    """Current calendar date (YYYY-MM-DD) in the configured zone — the daily-cap basis."""
    return datetime.now(get_app_tz()).strftime("%Y-%m-%d")


def to_local(dt: datetime) -> datetime:
    """Convert an aware datetime to the configured zone (None → server-local)."""
    return dt.astimezone(get_app_tz())


@lru_cache(maxsize=1)
def available_zone_names() -> list[str]:
    """Sorted IANA zone names for the admin picker and save-time validation. Needs the
    `tzdata` package on systems without a system zone database (e.g. Windows)."""
    return sorted(available_timezones())
