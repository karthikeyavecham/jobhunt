"""Application pipeline — the replacement for JobApplicationTracker.xlsx.

A row appears here the moment a posting is shortlisted, not when it is applied
to. That distinction is the point: the gap between "worth applying to" and
"applied" is exactly where applications get forgotten, and the daily report
chases anything that sits in `shortlisted` too long.
"""

import logging
from datetime import date, timedelta

from .util import now_iso

log = logging.getLogger(__name__)

#: Ordered so the UI and the report can render a pipeline.
PIPELINE = [
    "shortlisted", "applied", "screening", "interview", "offer",
    "rejected", "withdrawn",
]
OPEN_STATUSES = ("shortlisted", "applied", "screening", "interview", "offer")

#: How long a shortlisted posting may sit before the daily report nudges.
STALE_SHORTLIST_DAYS = 7
#: Default follow-up window after applying.
FOLLOW_UP_DAYS = 10


def get(conn, job_id):
    return conn.execute(
        "SELECT * FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()


def shortlist(conn, job_id, note=None):
    """Mark a posting as worth applying to, with a follow-up date."""
    now = now_iso()
    follow_up = (date.today() + timedelta(days=3)).isoformat()
    existing = get(conn, job_id)
    if existing is None:
        conn.execute(
            "INSERT INTO applications (job_id, pipeline_status, shortlisted_at, "
            "next_action, next_action_date, notes, updated_at) "
            "VALUES (?, 'shortlisted', ?, 'Prepare CV and cover letter', ?, ?, ?)",
            (job_id, now, follow_up, note, now),
        )
    else:
        # Never demote a real application back to shortlisted.
        if existing["pipeline_status"] in ("shortlisted",):
            conn.execute(
                "UPDATE applications SET shortlisted_at = COALESCE(shortlisted_at, ?), "
                "updated_at = ? WHERE job_id = ?",
                (now, now, job_id),
            )
    conn.execute("UPDATE jobs SET status = 'shortlisted' WHERE id = ?", (job_id,))
    conn.commit()
    return get(conn, job_id)


def mark_applied(conn, job_id, applied_on=None, note=None):
    """Record that the application actually went out."""
    now = now_iso()
    applied_on = applied_on or date.today().isoformat()
    follow_up = (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
    existing = get(conn, job_id)
    if existing is None:
        conn.execute(
            "INSERT INTO applications (job_id, pipeline_status, shortlisted_at, "
            "applied_at, next_action, next_action_date, notes, updated_at) "
            "VALUES (?, 'applied', ?, ?, 'Follow up if no reply', ?, ?, ?)",
            (job_id, now, applied_on, follow_up, note, now),
        )
    else:
        conn.execute(
            "UPDATE applications SET pipeline_status = 'applied', applied_at = ?, "
            "next_action = 'Follow up if no reply', next_action_date = ?, "
            "updated_at = ? WHERE job_id = ?",
            (applied_on, follow_up, now, job_id),
        )
    conn.execute("UPDATE jobs SET status = 'shortlisted' WHERE id = ?", (job_id,))
    conn.commit()
    return get(conn, job_id)


def set_status(conn, job_id, status, note=None):
    if status not in PIPELINE:
        raise ValueError("Unknown pipeline status '{0}'".format(status))
    now = now_iso()
    if get(conn, job_id) is None:
        conn.execute(
            "INSERT INTO applications (job_id, pipeline_status, notes, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (job_id, status, note, now),
        )
    else:
        conn.execute(
            "UPDATE applications SET pipeline_status = ?, updated_at = ? WHERE job_id = ?",
            (status, now, job_id),
        )
    conn.commit()
    return get(conn, job_id)


#: Fields the pipeline form may write. `None` always means "leave unchanged",
#: so a form that omits a field never silently blanks it.
EDITABLE = ("pipeline_status", "applied_at", "next_action", "next_action_date",
            "notes", "contact_name", "contact_email", "referral")

#: Statuses that imply the application actually went out. Reaching any of them
#: without an applied_at backfills today's date — otherwise a role that goes
#: straight to "interview" would sit in the pipeline with no application date.
IMPLIES_APPLIED = ("applied", "screening", "interview", "offer")


def update(conn, job_id, backfill_applied=True, **fields):
    """Partial update of one application row, creating it if needed.

    Only keys in EDITABLE are honoured; anything else is ignored rather than
    raising, so a stray form field cannot reach the database.

    `backfill_applied=False` turns off the IMPLIES_APPLIED date backfill. The
    importer passes it: when a spreadsheet leaves the applied date blank, the
    honest record is "unknown", not today — these rows are history, and dating
    a June application to today would also reset the follow-up nudges.
    """
    status = fields.get("pipeline_status")
    if status is not None and status not in PIPELINE:
        raise ValueError("Unknown pipeline status '{0}'".format(status))

    now = now_iso()
    existing = get(conn, job_id)
    if existing is None:
        conn.execute(
            "INSERT INTO applications (job_id, pipeline_status, shortlisted_at, "
            "updated_at) VALUES (?, ?, ?, ?)",
            (job_id, status or "shortlisted", now, now))
        existing = get(conn, job_id)

    changes = {k: v for k, v in fields.items() if k in EDITABLE and v is not None}

    if (backfill_applied
            and changes.get("pipeline_status") in IMPLIES_APPLIED
            and not existing["applied_at"] and not changes.get("applied_at")):
        changes["applied_at"] = date.today().isoformat()

    if changes:
        assignments = ", ".join("{0} = ?".format(k) for k in changes)
        conn.execute(
            "UPDATE applications SET {0}, updated_at = ? WHERE job_id = ?".format(
                assignments),
            list(changes.values()) + [now, job_id])

    # The triage state follows the pipeline: anything closed out stops
    # cluttering the "shortlisted" view, and anything live belongs there.
    new_status = changes.get("pipeline_status", existing["pipeline_status"])
    if new_status in ("rejected", "withdrawn"):
        conn.execute("UPDATE jobs SET status = 'dismissed' WHERE id = ?", (job_id,))
    elif new_status in OPEN_STATUSES:
        conn.execute("UPDATE jobs SET status = 'shortlisted' WHERE id = ?", (job_id,))

    conn.commit()
    return get(conn, job_id)


#: Pipeline order as SQL, for ORDER BY.
STAGE_ORDER = "CASE a.pipeline_status {0} ELSE 99 END".format(
    " ".join("WHEN '{0}' THEN {1}".format(stage, index)
             for index, stage in enumerate(PIPELINE)))


def all_applications(conn, order_by=None):
    """Every tracked application, for the pipeline view.

    `order_by` is a SQL fragment chosen by the caller from a fixed whitelist —
    never anything derived from a request. Stage order is always the tiebreak,
    so rows with an equal sort key stay in a stable, meaningful sequence.
    """
    ordering = "{0}, COALESCE(a.next_action_date, '9999'), a.updated_at DESC".format(
        STAGE_ORDER)
    if order_by:
        ordering = "{0}, {1}".format(order_by, ordering)
    return conn.execute(
        "SELECT a.*, j.title, j.company, j.url, j.location, "
        "       sc.fit_score, sc.verdict "
        "FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "LEFT JOIN scores sc ON sc.job_id = a.job_id "
        "ORDER BY " + ordering
    ).fetchall()


def dismiss(conn, job_id, reason=None):
    """Take a posting out of triage. Leaves any application row intact."""
    conn.execute(
        "UPDATE jobs SET status = 'dismissed', dismissed_reason = ? WHERE id = ?",
        (reason, job_id),
    )
    conn.commit()


def open_applications(conn):
    return conn.execute(
        "SELECT a.*, j.title, j.company, j.url FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "WHERE a.pipeline_status IN ({0}) "
        "ORDER BY COALESCE(a.next_action_date, '9999') ASC".format(
            ",".join("?" * len(OPEN_STATUSES))
        ),
        OPEN_STATUSES,
    ).fetchall()


def due_actions(conn, on_date=None):
    """Next actions due today or overdue."""
    on_date = on_date or date.today().isoformat()
    return conn.execute(
        "SELECT a.*, j.title, j.company FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "WHERE a.next_action_date IS NOT NULL AND a.next_action_date <= ? "
        "AND a.pipeline_status IN ({0}) "
        "ORDER BY a.next_action_date ASC".format(",".join("?" * len(OPEN_STATUSES))),
        [on_date] + list(OPEN_STATUSES),
    ).fetchall()


def stale_shortlists(conn, days=STALE_SHORTLIST_DAYS, on_date=None):
    """Shortlisted a while ago and still not applied to."""
    cutoff = (
        (date.fromisoformat(on_date) if on_date else date.today())
        - timedelta(days=days)
    ).isoformat()
    return conn.execute(
        "SELECT a.*, j.title, j.company FROM applications a "
        "JOIN jobs j ON j.id = a.job_id "
        "WHERE a.pipeline_status = 'shortlisted' AND a.shortlisted_at < ? "
        "ORDER BY a.shortlisted_at ASC",
        (cutoff,),
    ).fetchall()


def counts(conn):
    rows = conn.execute(
        "SELECT pipeline_status, COUNT(*) FROM applications GROUP BY pipeline_status"
    ).fetchall()
    return {row[0]: row[1] for row in rows}
