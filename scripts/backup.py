#!/usr/bin/env python3
"""Hot backup / restore for Praeceptor's SQLite database.

Backups use SQLite's **online-backup API**, so they take a *consistent* snapshot of
a LIVE database without stopping the app: WAL mode lets this reader see a coherent
point-in-time view while the tutor keeps writing. That is the key difference from the
volume-`tar` recipe in the README, which copies the raw -db/-wal/-shm files and therefore
needs the container stopped to avoid a torn copy. (`--sql` dumps are frozen through the
same API first — `iterdump()` alone is NOT transactionally consistent on a live DB.)

Usage (from the repo root):

    uv run python scripts/backup.py                    # backup -> .backups/praeceptor-<ts>.db
    uv run python scripts/backup.py --sql              # SQL text dump (.sql) instead of a binary .db
    uv run python scripts/backup.py --out FILE         # backup to a specific path
    uv run python scripts/backup.py --keep 10          # keep the 10 newest snapshots (default 5; 0 = all)
    uv run python scripts/backup.py --list             # list existing backups
    uv run python scripts/backup.py --restore          # restore the newest backup (stop the app first)
    uv run python scripts/backup.py --restore FILE     # restore a specific backup
    uv run python scripts/backup.py --db PATH ...      # target a non-default database

Inside Docker, backup still needs no downtime; snapshots land on the data volume
(BACKUP_DIR=/app/data/.backups, set in the image) and can be copied out to the host:

    docker compose exec praeceptor python scripts/backup.py
    mkdir -p .backups && docker cp praeceptor:/app/data/.backups/. ./.backups/

RESTORE overwrites the live database, so stop the app/container first; the script prompts
for confirmation (skip with -y) and snapshots the current DB into the backups directory
beforehand, so it's undoable. On Docker, `docker compose run` gives you a throwaway
container on the same volume while the app itself stays stopped:

    docker compose stop praeceptor
    docker compose run --rm --no-deps praeceptor python scripts/backup.py --restore
    docker compose start praeceptor
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SQLITE_MAGIC = b"SQLite format 3\x00"
# Backups this script writes are named praeceptor-<ts>; the pre-restore safety copies use a
# distinct prefix so "restore the newest" never accidentally grabs one of those.
BACKUP_PREFIX = "praeceptor-"
# Retention only ever deletes files matching this EXACT shape (praeceptor-YYYYMMDD-HHMMSS.db
# or .sql) — the precise names do_backup writes. A stricter test than the BACKUP_PREFIX one
# used for restore-selection, because prune unlinks: a live DB named praeceptor-1.db, a
# hand-named praeceptor-final.db, etc. must never be eligible for deletion.
_SNAPSHOT_RE = re.compile(r"^praeceptor-\d{8}-\d{6}\.(?:db|sql)$")


def _app_settings():
    """The app's Settings if importable (respects DB_PATH/BACKUP_DIR *and* a local .env);
    None — with a loud note — otherwise, so a bare `python` run can't silently target the
    wrong database just because e.g. python-dotenv isn't installed."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from app.config import settings

        return settings
    except Exception as e:
        print(
            f"note: app config unavailable ({e.__class__.__name__}: {e}) — falling back to "
            "the DB_PATH/BACKUP_DIR env vars and defaults; a .env file is NOT consulted. "
            "Run via `uv run python scripts/backup.py` to use the app's own config.",
            file=sys.stderr,
        )
        return None


_SETTINGS = _app_settings()
DEFAULT_BACKUP_DIR = Path(
    _SETTINGS.backup_dir if _SETTINGS else (os.getenv("BACKUP_DIR") or REPO_ROOT / ".backups")
)


def _default_db_path() -> Path:
    return Path(
        _SETTINGS.db_path if _SETTINGS else (os.getenv("DB_PATH") or REPO_ROOT / "data" / "praeceptor.db")
    )


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_sqlite_file(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(16) == SQLITE_MAGIC


def _quick_check(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        ok = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        sys.exit(f"error: {path} failed integrity check: {ok}")


def _snapshot_to(src_path: Path, dest_path: Path) -> None:
    """Consistent point-in-time copy of a (possibly live) database via the online-backup
    API, verified with quick_check. Unlike a byte copy, this folds in any -wal sidecar
    (a plain copy of a WAL database silently drops un-checkpointed transactions) and
    chokes cleanly on a file that isn't really a database."""
    dest_path.unlink(missing_ok=True)
    # A generous busy timeout so a mid-write app doesn't fail the snapshot.
    src = sqlite3.connect(str(src_path), timeout=30)
    try:
        src.execute("PRAGMA busy_timeout = 30000")
        dest = sqlite3.connect(str(dest_path))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    _quick_check(dest_path)


def do_backup(db_path: Path, out_path: Path, as_sql: bool) -> Path:
    """Take a consistent snapshot of a (possibly live) DB. Returns the written path."""
    if not db_path.is_file():
        sys.exit(f"error: database not found: {db_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and atomically rename, so an interrupted run never leaves a
    # partial file that looks like a valid backup.
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        if as_sql:
            # iterdump() does NOT open a read transaction (it SELECTs each table
            # separately in autocommit), so dumping the live file directly could
            # interleave a concurrent commit between tables. Freeze a consistent
            # binary snapshot first, then dump the frozen copy.
            snap = out_path.with_name(out_path.name + ".snap.part")
            try:
                _snapshot_to(db_path, snap)
                src = sqlite3.connect(str(snap))
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        for line in src.iterdump():
                            f.write(line + "\n")
                finally:
                    src.close()
            finally:
                snap.unlink(missing_ok=True)
        else:
            _snapshot_to(db_path, tmp)
        os.replace(tmp, out_path)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace
    print(f"backed up {db_path} -> {out_path} ({_human(out_path.stat().st_size)})")
    return out_path


def do_restore(db_path: Path, in_path: Path, assume_yes: bool) -> None:
    """Overwrite the live DB with a backup. Stop the app first (this reminds you)."""
    if not in_path.is_file():
        sys.exit(f"error: backup file not found: {in_path}")
    if db_path.exists() and in_path.resolve() == db_path.resolve():
        sys.exit("error: the backup file and the live database are the same file")

    print(f"About to RESTORE:\n  from: {in_path}\n  into: {db_path}   (this OVERWRITES current data)")
    print("Stop the app/container first - restoring under a running app corrupts the database.")
    if not assume_yes:
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            sys.exit("aborted")

    # Safety net: snapshot the current DB before clobbering it, so a bad restore is undoable.
    if db_path.exists():
        safety = do_backup(db_path, DEFAULT_BACKUP_DIR / f"pre-restore-{_stamp()}.db", as_sql=False)
        print(f"(current database saved to {safety} before restoring)")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Stage the restored DB NEXT TO the target (same filesystem, so os.replace is atomic),
    # verify it, and only then swap it in — the live file is never written in place, so an
    # interrupted restore can't leave it torn.
    staged = db_path.with_name(db_path.name + ".part")
    try:
        # Dispatch on CONTENT, not extension — a SQL dump misnamed .db (or vice versa)
        # restores correctly instead of producing a "successful" garbage restore.
        if _is_sqlite_file(in_path):
            _snapshot_to(in_path, staged)
        else:
            # Assume a SQL text dump; materialize it into the staging DB, then verify.
            staged.unlink(missing_ok=True)
            conn = sqlite3.connect(str(staged))
            try:
                conn.executescript(in_path.read_text(encoding="utf-8"))
                conn.commit()
            finally:
                conn.close()
            _quick_check(staged)
        # Drop the target's OLD WAL/SHM sidecars BEFORE the swap — left behind, SQLite
        # would replay stale WAL frames onto the restored file and corrupt it. If this is
        # interrupted between unlink and replace, the old DB is intact minus its WAL, and
        # the pre-restore snapshot above already preserves that content.
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        os.replace(staged, db_path)
    except sqlite3.DatabaseError as e:
        sys.exit(f"error: {in_path} does not restore cleanly ({e})")
    except UnicodeDecodeError:
        sys.exit(f"error: {in_path} is neither a SQLite database nor a SQL text dump")
    except PermissionError:
        sys.exit(
            f"error: {db_path} is locked — the app is probably still running. Stop it and "
            "retry. (The current database has not been replaced.)"
        )
    finally:
        staged.unlink(missing_ok=True)

    print(f"restored {in_path} -> {db_path}")


def _list_backups() -> list[Path]:
    if not DEFAULT_BACKUP_DIR.exists():
        return []
    files = [p for p in DEFAULT_BACKUP_DIR.iterdir() if p.suffix in (".db", ".sql") and p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _newest_backup() -> Path | None:
    """The most recent backup this script wrote (ignores pre-restore safety copies).
    Newest by the timestamp IN THE NAME, not mtime — a backup copied in from another
    machine gets a fresh mtime, which must not make it outrank yesterday's real one."""
    ours = [p for p in _list_backups() if p.name.startswith(BACKUP_PREFIX)]
    return max(ours, key=lambda p: p.name, default=None)


def _prune_backups(directory: Path, keep: int, protect: tuple[Path, ...] = ()) -> None:
    """Keep only the `keep` newest timestamped snapshots (praeceptor-YYYYMMDD-HHMMSS.db/.sql)
    in `directory`; delete the rest. Deliberately conservative because it unlinks files:
      - only the EXACT names do_backup writes are eligible (see _SNAPSHOT_RE) — the live DB,
        pre-restore safety copies, .part temps, and any hand-named file are never matched;
      - nothing in `protect` (the just-written snapshot and the live DB path) is ever
        deleted, so a backward clock/DST/NTP skew that sorts the new file low can't evict it.
    Snapshots count toward `keep` even while protected, so the common case keeps exactly N."""
    protected = {p.resolve() for p in protect}
    snaps = sorted(
        (p for p in directory.iterdir() if p.is_file() and _SNAPSHOT_RE.match(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    stale = [p for p in snaps[keep:] if p.resolve() not in protected]
    for p in stale:
        p.unlink(missing_ok=True)
    if stale:
        print(f"pruned {len(stale)} old backup(s), keeping the {keep} newest in {directory}")


def do_list() -> None:
    backups = _list_backups()
    if not backups:
        print(f"no backups in {DEFAULT_BACKUP_DIR}")
        return
    print(f"backups in {DEFAULT_BACKUP_DIR} (newest first):")
    for p in backups:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {mtime}  {_human(p.stat().st_size):>9}  {p.name}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Hot backup / restore for Praeceptor's SQLite DB. Backup is the default action.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--restore", nargs="?", const="", metavar="FILE",
        help="restore the DB from FILE (default: the newest backup in the backups dir)",
    )
    ap.add_argument("--list", action="store_true", help="list existing backups and exit")
    ap.add_argument("--sql", action="store_true", help="backup as a SQL text dump (.sql) instead of a binary .db")
    ap.add_argument("--out", metavar="FILE", help="backup destination (default: <backups dir>/praeceptor-<timestamp>.db)")
    ap.add_argument("--keep", type=int, default=5, metavar="N",
                    help="after a default-location backup, keep only the N newest snapshots in "
                         "the backups dir, deleting older ones (default: 5; 0 = keep all; binary "
                         "and --sql snapshots share the budget). Ignored when --out is given.")
    ap.add_argument("--db", metavar="PATH", help="database path (default: from DB_PATH / app config)")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the restore confirmation prompt")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else _default_db_path()

    if args.list:
        do_list()
        return

    if args.restore is not None:
        target = Path(args.restore) if args.restore else _newest_backup()
        if not target:
            sys.exit(f"error: no --restore file given and no backup found in {DEFAULT_BACKUP_DIR}")
        do_restore(db_path, Path(target), args.yes)
        return

    if args.out:
        # Explicit destination: the user manages that location, so don't auto-prune it.
        do_backup(db_path, Path(args.out), args.sql)
    else:
        ext = "sql" if args.sql else "db"
        out = do_backup(db_path, DEFAULT_BACKUP_DIR / f"{BACKUP_PREFIX}{_stamp()}.{ext}", args.sql)
        if args.keep > 0:
            _prune_backups(DEFAULT_BACKUP_DIR, args.keep, protect=(out, db_path))


if __name__ == "__main__":
    main()
