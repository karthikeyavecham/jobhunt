"""SQLite storage for JobHunt.

The whole schema is created up front rather than grown slice by slice, so there
are no migrations to write while the app is still being built. Every statement
is IF NOT EXISTS, so calling init_db() on every start is safe.

Connections are handed out two ways: get_db() for Flask request handlers (bound
to `g`, closed by the teardown hook) and connect() for the scheduler and Slack
threads, which live outside the request cycle. WAL journalling is on because
those threads write concurrently with the web UI.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from flask import g

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "jobhunt.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Kept apart from settings so the whole table can be excluded from exports.
CREATE TABLE IF NOT EXISTS secrets (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS profile (
    section      TEXT PRIMARY KEY,
    content_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    term        TEXT NOT NULL UNIQUE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    match_scope TEXT NOT NULL DEFAULT 'title',   -- title | title_and_body
    created_at  TEXT NOT NULL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    adapter      TEXT NOT NULL,
    config_json  TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_status  TEXT,                            -- ok | error | never
    last_error   TEXT,
    last_found   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id      TEXT NOT NULL,
    url              TEXT NOT NULL,
    canonical_url    TEXT NOT NULL,
    title            TEXT NOT NULL,
    company          TEXT NOT NULL,
    location         TEXT,
    remote           INTEGER,
    employment_type  TEXT,
    posted_at        TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    description_html TEXT,
    description_text TEXT,
    raw_json         TEXT,
    content_hash     TEXT,
    dedup_key        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'new', -- new | shortlisted | dismissed
    dismissed_reason TEXT,
    -- Stage-0 relevance. Postings are marked, never dropped, so exclusions
    -- stay auditable and can be revisited when the region config changes.
    stage0_pass      INTEGER,
    stage0_reason    TEXT,
    slack_channel    TEXT,
    slack_ts         TEXT,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup   ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_seen    ON jobs(first_seen_at);
-- Indexes on migrated-in columns are created by _migrate(), after the ALTER.

CREATE TABLE IF NOT EXISTS job_details (
    job_id                    INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    salary_min                REAL,
    salary_max                REAL,
    salary_currency           TEXT,
    salary_period             TEXT,               -- year | month | day | hour
    salary_stated             INTEGER NOT NULL DEFAULT 0,
    salary_quote              TEXT,
    german_level_required     TEXT,               -- none | A1..C2 | native | not_stated
    english_sufficient        INTEGER,
    language_quote            TEXT,
    remote_policy             TEXT,               -- onsite | hybrid | remote_de | remote_eu | not_stated
    remote_quote              TEXT,
    seniority                 TEXT,
    people_management_required INTEGER,
    team_size                 TEXT,
    reports_to                TEXT,
    visa_sponsorship_mentioned INTEGER,
    tech_stack_json           TEXT,
    contract_type             TEXT,
    start_date                TEXT,
    application_deadline      TEXT,
    contact_person            TEXT,
    extracted_at              TEXT,
    model_used                TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    job_id         INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    fit_score      INTEGER NOT NULL,
    verdict        TEXT NOT NULL,                 -- strong | good | stretch | no
    matched_json   TEXT,
    gaps_json      TEXT,
    rationale      TEXT,
    model_used     TEXT,
    prompt_version TEXT,
    scored_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_fit ON scores(fit_score DESC);

CREATE TABLE IF NOT EXISTS applications (
    job_id           INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    pipeline_status  TEXT NOT NULL,               -- shortlisted | applied | screening | interview | offer | rejected | withdrawn
    shortlisted_at   TEXT,
    applied_at       TEXT,
    contact_name     TEXT,
    contact_email    TEXT,
    referral         TEXT,
    next_action      TEXT,
    next_action_date TEXT,
    notes            TEXT,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                  -- cv | cover_letter
    version       INTEGER NOT NULL DEFAULT 1,
    format        TEXT NOT NULL,                  -- docx | pdf
    path          TEXT NOT NULL,
    model_used    TEXT,
    brand_color   TEXT,
    slack_file_id TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_job ON documents(job_id);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    trigger     TEXT NOT NULL,                    -- schedule | manual | slack
    jobs_found  INTEGER NOT NULL DEFAULT 0,
    jobs_new    INTEGER NOT NULL DEFAULT 0,
    jobs_scored INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT
);
"""

# Seeded on first run. Terms come from the karthikeya-job-search skill.
DEFAULT_SEARCH_TERMS = [
    "Zendesk Platform Owner",
    "Zendesk Administrator",
    "Support Operations Manager",
    "Customer Operations Manager",
    "CX Operations",
    "Head of Customer Support",
    "Technical Customer Support Manager",
    "Customer Success Manager",
    "Support Ops",
    "Product Owner Chatbot",
    "Knowledge Manager",
    "AI Enablement Engineer",
    "IT Operations Manager",
    "Solution Owner",
]

#: The nationwide Bundesagentur source, added 2026-08-13. A blank `wo` drops
#: the geographic filter, so this finds remote roles advertised anywhere in
#: Germany — which the Köln-centred source cannot return however wide its radius.
#: Same data as make-it-in-germany.com, which is a translated front-end over
#: this database and cannot be fetched anyway (robots.txt disallows its filtered
#: URLs and it sits behind bot protection).
#:
#: Deliberately a 7-day window where the regional source uses 30: nationwide
#: matches roughly eleven times as many postings, and a 30-day first run would
#: queue over a thousand scorings at 40 per cycle.
NATIONWIDE_SOURCE = (
    "Bundesagentur für Arbeit (nationwide)", "arbeitsagentur",
    '{"wo": "", "veroeffentlichtseit": 7, "angebotsart": 1, "max_pages": 4}')

DEFAULT_SOURCES = [
    ("Arbeitnow", "arbeitnow", '{"pages": 2}'),
    # Terms come from the search_terms table; wo/umkreis centre the search on
    # the configured home region.
    ("Bundesagentur für Arbeit", "arbeitsagentur",
     '{"wo": "K\\u00f6ln", "umkreis": 50, "veroeffentlichtseit": 30, "max_pages": 2}'),
    NATIONWIDE_SOURCE,
]


def connect():
    """A standalone connection. Callers outside a Flask request own closing it."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the scheduler thread write while the web UI reads.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    """Request-scoped connection, closed by close_db() on teardown."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the schema and seed defaults. Idempotent."""
    fresh = not DB_PATH.exists()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed(conn)
        conn.commit()
    finally:
        conn.close()
    if fresh:
        # Holds API keys and personal data — keep it owner-only.
        os.chmod(DB_PATH, 0o600)


# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add
# them to an existing table, so they are applied here instead.
_ADDED_COLUMNS = [
    ("jobs", "stage0_pass", "INTEGER"),
    ("jobs", "stage0_reason", "TEXT"),
    # When the posting was accounted for in a Slack notification — either as
    # its own card or inside a rollup line. Distinct from slack_ts, which only
    # exists for jobs that got a card and is needed to edit that card.
    ("jobs", "notified_at", "TEXT"),
]


# Indexes over migrated-in columns, created after the ALTER statements above.
_MIGRATED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_stage0 ON jobs(stage0_pass)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_notified ON jobs(notified_at)",
]


def _migrate(conn):
    for table, column, coltype in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(
            "PRAGMA table_info({0})".format(table)
        ).fetchall()}
        if column not in existing:
            conn.execute("ALTER TABLE {0} ADD COLUMN {1} {2}".format(table, column, coltype))
    for statement in _MIGRATED_INDEXES:
        conn.execute(statement)
    _relativise_document_paths(conn)
    _add_nationwide_source(conn)


def _add_nationwide_source(conn):
    """Add the nationwide Bundesagentur source to databases that predate it.

    `_seed` only runs on an empty table, so without this the Pi — the one
    database that matters — would never get it. Keyed on name, and it stays
    deleted if it is ever removed on purpose: re-adding a source the user
    disabled would be its own kind of bug.
    """
    name, adapter, config_json = NATIONWIDE_SOURCE
    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0:
        return          # a fresh database; _seed inserts the full set
    existing = conn.execute(
        "SELECT 1 FROM sources WHERE name = ?", (name,)).fetchone()
    if existing or _source_was_removed(conn, name):
        return
    conn.execute(
        "INSERT INTO sources (name, adapter, config_json, enabled, last_status) "
        "VALUES (?, ?, ?, 1, 'never')", (name, adapter, config_json))
    log.info("added the nationwide Bundesagentur source")


def _source_was_removed(conn, name):
    """Has this source been added and then deliberately deleted before?"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'sources.removed'").fetchone()
    if not row or not row[0]:
        return False
    try:
        return name in json.loads(row[0])
    except ValueError:
        return False


def _relativise_document_paths(conn):
    """Rewrite absolute document paths to be relative to the output directory.

    Rows written before 2026-08-07 hold the generating machine's absolute path,
    so documents produced on the Mac 403'd on the Pi even though the files were
    synced across. Only the basename is trusted here: these rows are known to
    point at flat files in the output directory, and anything else would be a
    path we should not be serving anyway.
    """
    rows = conn.execute(
        "SELECT id, path FROM documents WHERE path LIKE '/%'").fetchall()
    if not rows:
        return
    conn.executemany(
        "UPDATE documents SET path = ? WHERE id = ?",
        [(os.path.basename(row[1]), row[0]) for row in rows])
    log.info("migrated %d document path(s) to be relative", len(rows))


def _seed(conn):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if conn.execute("SELECT COUNT(*) FROM search_terms").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO search_terms (term, enabled, match_scope, created_at) VALUES (?, 1, 'title', ?)",
            [(t, now) for t in DEFAULT_SEARCH_TERMS],
        )

    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO sources (name, adapter, config_json, enabled, last_status) "
            "VALUES (?, ?, ?, 1, 'never')",
            DEFAULT_SOURCES,
        )
