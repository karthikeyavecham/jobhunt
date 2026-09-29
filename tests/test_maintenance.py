"""Backups and housekeeping — the insurance that must not silently rot."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobhunt import maintenance
from jobhunt import db as db_module


class MaintenanceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        db_module.init_db()
        self.backup_dir = Path(self._tmp.name) / "backups"

    def tearDown(self):
        db_module.DB_PATH = self._orig
        self._tmp.cleanup()


class TestBackup(MaintenanceTestCase):
    def test_backup_is_a_valid_database_with_the_same_content(self):
        conn = db_module.connect()
        conn.execute(
            "INSERT INTO search_terms (term, enabled, match_scope, created_at) "
            "VALUES ('Backup Probe', 1, 'title', '2026-08-07T00:00:00+00:00')")
        conn.commit()
        expected = conn.execute("SELECT COUNT(*) FROM search_terms").fetchone()[0]

        path = maintenance.backup_database(conn, self.backup_dir)
        conn.close()

        self.assertTrue(path.exists())
        copy = sqlite3.connect(str(path))
        try:
            self.assertEqual(
                copy.execute("SELECT COUNT(*) FROM search_terms").fetchone()[0],
                expected)
            self.assertEqual(
                copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            copy.close()

    def test_backup_filename_carries_a_timestamp(self):
        when = datetime(2026, 8, 7, 3, 30, tzinfo=timezone.utc)
        path = maintenance.backup_database(backup_dir=self.backup_dir, now=when)
        self.assertEqual(path.name, "jobhunt-20260807-033000.db")

    def test_rotation_keeps_only_the_newest(self):
        for day in range(1, 10):
            (self.backup_dir / "jobhunt-2026080{0}-033000.db".format(day)).parent.mkdir(
                parents=True, exist_ok=True)
            (self.backup_dir / "jobhunt-2026080{0}-033000.db".format(day)).touch()

        removed = maintenance.rotate_backups(self.backup_dir, keep=7)

        self.assertEqual(removed, 2)
        remaining = sorted(p.name for p in self.backup_dir.iterdir())
        self.assertEqual(len(remaining), 7)
        self.assertNotIn("jobhunt-20260801-033000.db", remaining)
        self.assertIn("jobhunt-20260809-033000.db", remaining)

    def test_rotation_never_touches_files_it_did_not_create(self):
        self.backup_dir.mkdir(parents=True)
        stray = self.backup_dir / "important-notes.db"
        stray.touch()
        for day in range(1, 10):
            (self.backup_dir / "jobhunt-2026080{0}-000000.db".format(day)).touch()

        maintenance.rotate_backups(self.backup_dir, keep=2)

        self.assertTrue(stray.exists(), "rotation must only delete its own files")

    def test_a_failed_backup_leaves_no_half_written_file(self):
        """A truncated backup that looks like insurance is worse than none."""
        class Broken(object):
            def backup(self, dest):
                raise sqlite3.OperationalError("disk I/O error")

        with self.assertRaises(sqlite3.OperationalError):
            maintenance.backup_database(Broken(), self.backup_dir)
        leftovers = list(self.backup_dir.glob("jobhunt-*.db"))
        self.assertEqual(leftovers, [])


class TestCheckpoint(MaintenanceTestCase):
    def test_checkpoint_runs_and_reports(self):
        conn = db_module.connect()
        try:
            row = maintenance.checkpoint_wal(conn)
            self.assertEqual(row[0], 0, "checkpoint must not report busy")
        finally:
            conn.close()


class TestHousekeeping(MaintenanceTestCase):
    def test_runs_both_steps(self):
        maintenance.BACKUP_DIR, orig = self.backup_dir, maintenance.BACKUP_DIR
        try:
            summary = maintenance.run_housekeeping(db_module.connect)
        finally:
            maintenance.BACKUP_DIR = orig
        self.assertIsNotNone(summary["backup"])
        self.assertTrue(summary["checkpoint"])
        self.assertEqual(summary["errors"], [])

    def test_a_failed_backup_does_not_stop_the_checkpoint(self):
        maintenance.BACKUP_DIR, orig = self.backup_dir, maintenance.BACKUP_DIR
        real_backup = maintenance.backup_database
        maintenance.backup_database = lambda *a, **k: (_ for _ in ()).throw(
            OSError("disk full"))
        try:
            summary = maintenance.run_housekeeping(db_module.connect)
        finally:
            maintenance.backup_database = real_backup
            maintenance.BACKUP_DIR = orig
        self.assertIsNone(summary["backup"])
        self.assertTrue(summary["checkpoint"], "checkpoint must still run")
        self.assertEqual(len(summary["errors"]), 1)


class TestSchedulerWiring(unittest.TestCase):
    def test_housekeeping_job_is_scheduled(self):
        from jobhunt import scheduler

        s = scheduler.Scheduler(conn_factory=db_module.connect)
        # Isolate from the real database path.
        tmp = tempfile.TemporaryDirectory()
        orig = db_module.DB_PATH
        db_module.DB_PATH = Path(tmp.name) / "t.db"
        db_module.init_db()
        try:
            s.start()
            ids = {j["id"] for j in s.jobs()}
            self.assertIn(scheduler.HOUSEKEEPING_JOB_ID, ids)
            self.assertIn(scheduler.FETCH_JOB_ID, ids)
            self.assertIn(scheduler.REPORT_JOB_ID, ids)
        finally:
            s.shutdown()
            db_module.DB_PATH = orig
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
