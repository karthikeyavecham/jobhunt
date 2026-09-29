"""Nightly housekeeping: database backups and WAL checkpointing.

The database on the Pi is the only copy of the application tracker, the
scores, and the Slack tokens — and it lives on an SD card, the least durable
storage there is. A card failure in September would erase the pipeline in the
middle of interview season, so this is the cheapest insurance in the project.

Backups use SQLite's own backup API rather than copying the file: a copy taken
mid-write under WAL can be inconsistent, while Connection.backup() produces a
consistent snapshot without blocking the writers.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import connect

log = logging.getLogger(__name__)

BACKUP_DIR = Path(__file__).parent / "backups"
#: Nightly, keep a week. The deploy script also pulls the newest one to the
#: Mac, so a dead SD card costs at most one day of tracker changes.
KEEP_BACKUPS = 7

_PREFIX = "jobhunt-"
_SUFFIX = ".db"


def backup_database(conn=None, backup_dir=None, keep=KEEP_BACKUPS, now=None):
    """Write a consistent snapshot and rotate old ones. Returns the new path."""
    backup_dir = Path(backup_dir or BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / "{0}{1}{2}".format(_PREFIX, stamp, _SUFFIX)

    owns_conn = conn is None
    source = conn or connect()
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    except Exception:
        # A half-written backup is worse than none: it looks like insurance.
        target.unlink(missing_ok=True)
        raise
    finally:
        if owns_conn:
            source.close()

    removed = rotate_backups(backup_dir, keep)
    log.info("backup written: %s (%.1f MB); %d old backup(s) removed",
             target.name, target.stat().st_size / 1e6, removed)
    return target


def rotate_backups(backup_dir=None, keep=KEEP_BACKUPS):
    """Delete the oldest backups beyond `keep`. Returns how many were removed.

    Only files matching our own naming pattern are touched — a stray file in
    the directory must never be deleted by rotation.
    """
    backup_dir = Path(backup_dir or BACKUP_DIR)
    if not backup_dir.exists():
        return 0
    ours = sorted(
        p for p in backup_dir.iterdir()
        if p.name.startswith(_PREFIX) and p.name.endswith(_SUFFIX)
    )
    excess = ours[:-keep] if keep > 0 else ours
    for path in excess:
        path.unlink()
    return len(excess)


def checkpoint_wal(conn=None):
    """Fold the WAL back into the main file so it cannot grow unbounded."""
    owns_conn = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        log.info("wal_checkpoint(TRUNCATE) -> busy=%s log=%s checkpointed=%s",
                 row[0], row[1], row[2])
        return row
    finally:
        if owns_conn:
            conn.close()


def run_housekeeping(conn_factory=connect):
    """The nightly job: backup, then checkpoint. Each step is isolated —
    a failed backup must not prevent the checkpoint, and vice versa."""
    summary = {"backup": None, "checkpoint": False, "errors": []}
    conn = conn_factory()
    try:
        try:
            summary["backup"] = str(backup_database(conn))
        except Exception as exc:
            log.exception("nightly backup failed")
            summary["errors"].append("backup: {0!r}".format(exc))
        try:
            checkpoint_wal(conn)
            summary["checkpoint"] = True
        except Exception as exc:
            log.exception("wal checkpoint failed")
            summary["errors"].append("checkpoint: {0!r}".format(exc))
    finally:
        conn.close()
    return summary
