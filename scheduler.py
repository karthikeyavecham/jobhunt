"""The unattended cycle: fetch → score → notify, every few hours.

One design decision matters more than the rest: **the notification window gates
notifying, not fetching**. Overnight the app still fetches and scores, so
nothing is missed and the morning run has no backlog to chew through — but no
cards are posted until the window opens. `jobs.notified_at` makes that work for
free: a job scored at 03:00 simply stays unnotified until a run inside the
window picks it up.

APScheduler runs in-process. Waitress is thread-based rather than
process-based, so exactly one scheduler instance exists — this would need
rethinking under multi-worker Gunicorn, which is a reason not to switch.
"""

import logging
import threading
from datetime import datetime, time as dtime

from . import config, ingest, scoring, slackbot
from .db import connect
from .util import DEFAULT_TIMEZONE, get_timezone   # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 3
DEFAULT_NOTIFY_FROM = "08:00"
DEFAULT_NOTIFY_UNTIL = "22:00"
DEFAULT_REPORT_AT = "18:00"

FETCH_JOB_ID = "jobhunt-cycle"
REPORT_JOB_ID = "jobhunt-report"
HOUSEKEEPING_JOB_ID = "jobhunt-housekeeping"
#: 03:30 sits between the 03:00-ish cycle finishing and the morning window.
HOUSEKEEPING_AT = "03:30"

#: Guards against a slow cycle overlapping the next tick.
_cycle_lock = threading.Lock()


def parse_hhmm(value, fallback):
    """'08:00' -> time(8, 0). Falls back rather than raising on bad config."""
    try:
        hours, minutes = str(value).strip().split(":")
        return dtime(int(hours), int(minutes))
    except (ValueError, AttributeError):
        log.warning("could not parse time %r; using %s", value, fallback)
        hours, minutes = fallback.split(":")
        return dtime(int(hours), int(minutes))


def in_notify_window(settings, now=None):
    """Is it currently inside the window when cards may be posted?

    Windows that wrap past midnight (22:00–06:00) are supported by inverting
    the comparison rather than being silently treated as empty.
    """
    tz = get_timezone(settings.get("schedule.timezone", DEFAULT_TIMEZONE))
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    current = now.astimezone(tz).time()

    start = parse_hhmm(settings.get("slack.notify_from", DEFAULT_NOTIFY_FROM),
                       DEFAULT_NOTIFY_FROM)
    end = parse_hhmm(settings.get("slack.notify_until", DEFAULT_NOTIFY_UNTIL),
                     DEFAULT_NOTIFY_UNTIL)

    if start == end:
        return True                      # a zero-width window means "always"
    if start < end:
        return start <= current <= end
    return current >= start or current <= end   # wraps midnight


class CycleResult(object):
    def __init__(self):
        self.fetched = 0
        self.new_jobs = 0
        self.scored = 0
        self.score_failed = 0
        self.cards_posted = 0
        self.rolled_up = 0
        self.notified_skipped = False
        self.errors = []

    def summary(self):
        parts = [
            "{0} found".format(self.fetched),
            "{0} new".format(self.new_jobs),
            "{0} scored".format(self.scored),
        ]
        if self.notified_skipped:
            parts.append("notifications held (outside window)")
        else:
            parts.append("{0} cards".format(self.cards_posted))
        if self.score_failed:
            parts.append("{0} scoring failures".format(self.score_failed))
        if self.errors:
            parts.append("{0} source errors".format(len(self.errors)))
        return ", ".join(parts)


def run_cycle(trigger="manual", conn=None, force_notify=False):
    """One full pass. Each stage is isolated so a failure degrades rather than aborts."""
    if not _cycle_lock.acquire(blocking=False):
        log.warning("a cycle is already running; skipping this %s trigger", trigger)
        result = CycleResult()
        result.errors.append({"stage": "lock", "error": "previous cycle still running"})
        return result

    owns_conn = conn is None
    conn = conn or connect()
    result = CycleResult()
    try:
        settings = config.load_settings(conn)

        # 1. Fetch
        try:
            fetch = ingest.run_fetch(trigger=trigger, conn=conn)
            result.fetched = fetch.found
            result.new_jobs = fetch.inserted
            result.errors.extend(fetch.errors)
        except Exception as exc:
            log.exception("fetch stage failed")
            result.errors.append({"stage": "fetch", "error": repr(exc)})

        # 2. Score. A provider outage aborts scoring but must not stop the
        #    cycle — already-scored jobs can still be notified.
        if settings.get("scoring.enabled", True):
            try:
                provider = config.build_provider(conn)
                run = scoring.score_pending(
                    conn, provider,
                    limit=settings.get("scoring.batch_limit", 40),
                    max_chars=settings.get("scoring.max_description_chars", 3500),
                    concurrency=settings.get("scoring.concurrency", 1),
                )
                result.scored = run.scored
                result.score_failed = run.failed
                if run.aborted_reason:
                    result.errors.append({"stage": "score", "error": run.aborted_reason})
            except Exception as exc:
                log.exception("scoring stage failed")
                result.errors.append({"stage": "score", "error": repr(exc)})

        # 3. Notify, only inside the window.
        if settings.get("slack.enabled", True):
            if force_notify or in_notify_window(settings):
                try:
                    posted, rolled = slackbot.post_new_cards(conn)
                    result.cards_posted = posted
                    result.rolled_up = rolled
                except slackbot.SlackNotConfigured as exc:
                    log.info("Slack not configured: %s", exc)
                except Exception as exc:
                    log.exception("notify stage failed")
                    result.errors.append({"stage": "notify", "error": repr(exc)})
            else:
                result.notified_skipped = True
                log.info("outside the notification window; holding cards")

        log.info("cycle (%s): %s", trigger, result.summary())
    finally:
        if owns_conn:
            conn.close()
        _cycle_lock.release()
    return result


def _run_housekeeping_job():
    """Imported lazily so the scheduler module has no import-time cycle."""
    from . import maintenance

    return maintenance.run_housekeeping()


def run_daily_report(trigger="schedule", conn=None):
    """Post the end-of-day summary."""
    owns_conn = conn is None
    conn = conn or connect()
    try:
        from . import reports

        return reports.post_daily_report(conn)
    except Exception:
        log.exception("daily report failed")
        return None
    finally:
        if owns_conn:
            conn.close()


class Scheduler(object):
    """Wraps APScheduler so the schedule can be changed without a restart."""

    def __init__(self, conn_factory=connect):
        self.conn_factory = conn_factory
        self._scheduler = None

    def start(self):
        from apscheduler.schedulers.background import BackgroundScheduler

        conn = self.conn_factory()
        try:
            settings = config.load_settings(conn)
        finally:
            conn.close()

        tz = get_timezone(settings.get("schedule.timezone", DEFAULT_TIMEZONE))
        self._scheduler = BackgroundScheduler(timezone=tz)
        self._scheduler.start()
        self.reschedule(settings)
        return self._scheduler

    def reschedule(self, settings=None):
        """Apply the current settings. Safe to call while running."""
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        if self._scheduler is None:
            return
        if settings is None:
            conn = self.conn_factory()
            try:
                settings = config.load_settings(conn)
            finally:
                conn.close()

        tz = get_timezone(settings.get("schedule.timezone", DEFAULT_TIMEZONE))
        hours = int(settings.get("schedule.interval_hours", DEFAULT_INTERVAL_HOURS))
        enabled = settings.get("schedule.enabled", True)

        for job_id in (FETCH_JOB_ID, REPORT_JOB_ID, HOUSEKEEPING_JOB_ID):
            existing = self._scheduler.get_job(job_id)
            if existing:
                existing.remove()

        if not enabled:
            log.info("scheduler disabled by configuration")
            return

        self._scheduler.add_job(
            lambda: run_cycle(trigger="schedule"),
            trigger=IntervalTrigger(hours=hours, timezone=tz),
            id=FETCH_JOB_ID,
            name="fetch, score and notify",
            max_instances=1,
            coalesce=True,        # a missed tick runs once, not N times
            misfire_grace_time=1800,
        )

        report_at = parse_hhmm(
            settings.get("schedule.report_at", DEFAULT_REPORT_AT), DEFAULT_REPORT_AT
        )
        self._scheduler.add_job(
            run_daily_report,
            trigger=CronTrigger(hour=report_at.hour, minute=report_at.minute, timezone=tz),
            id=REPORT_JOB_ID,
            name="daily report",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        housekeeping_at = parse_hhmm(HOUSEKEEPING_AT, HOUSEKEEPING_AT)
        self._scheduler.add_job(
            _run_housekeeping_job,
            trigger=CronTrigger(hour=housekeeping_at.hour,
                                minute=housekeeping_at.minute, timezone=tz),
            id=HOUSEKEEPING_JOB_ID,
            name="nightly backup and WAL checkpoint",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600 * 4,   # a backup 4h late still beats none
        )
        log.info("scheduled: cycle every %sh, report at %s, housekeeping at %s (%s)",
                 hours, report_at.strftime("%H:%M"), HOUSEKEEPING_AT, tz)

    def jobs(self):
        """(id, name, next_run_time) for the status panel."""
        if self._scheduler is None:
            return []
        return [
            {"id": job.id, "name": job.name, "next_run": job.next_run_time}
            for job in self._scheduler.get_jobs()
        ]

    def shutdown(self):
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None


#: Process-wide instance, wired up by app.create_app().
scheduler = Scheduler()
