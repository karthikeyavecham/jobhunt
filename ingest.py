"""Fetch sources and write postings to the database.

Adapters produce RawJob records; this module decides what is new, what is a
re-listing, and what is the same role arriving from a second source. It is the
only place that writes to `jobs`, which keeps the dedup rules in one testable
spot.

Three outcomes per incoming posting:
  inserted   — genuinely new, eligible for scoring and a Slack card
  refreshed  — already known from this source; bump last_seen_at
  merged     — same role from a different source; recorded as an alternate URL
"""

import json
import logging

from . import dedup, filters
from .db import connect
from .sources import get_adapter
from .sources.base import SourceError
from .util import now_iso

log = logging.getLogger(__name__)


class RunResult(object):
    def __init__(self):
        self.found = 0
        self.inserted = 0
        self.refreshed = 0
        self.merged = 0
        self.errors = []          # list of {"source": name, "error": message}
        self.new_job_ids = []     # inserted rows, in insertion order

    def as_dict(self):
        return {
            "found": self.found,
            "inserted": self.inserted,
            "refreshed": self.refreshed,
            "merged": self.merged,
            "errors": self.errors,
            "new_job_ids": self.new_job_ids,
        }


def run_fetch(trigger="manual", source_ids=None, conn=None):
    """Fetch every enabled source (or just `source_ids`) and store results.

    A failing source is recorded and skipped — one broken adapter must never
    prevent the others from delivering jobs.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = connect()

    result = RunResult()
    started = now_iso()
    run_id = conn.execute(
        "INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (started, trigger)
    ).lastrowid
    conn.commit()

    try:
        sql = "SELECT * FROM sources WHERE enabled = 1"
        params = []
        if source_ids:
            placeholders = ",".join("?" * len(source_ids))
            sql += " AND id IN ({0})".format(placeholders)
            params = list(source_ids)
        sources = conn.execute(sql + " ORDER BY id", params).fetchall()

        search_terms = _enabled_search_terms(conn)
        filter_config = filters.load_config(conn)
        for source in sources:
            _fetch_one_source(conn, source, result, search_terms, filter_config)

        conn.execute(
            "UPDATE runs SET finished_at = ?, jobs_found = ?, jobs_new = ?, errors_json = ? "
            "WHERE id = ?",
            (
                now_iso(),
                result.found,
                result.inserted,
                json.dumps(result.errors) if result.errors else None,
                run_id,
            ),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()

    return result


def _enabled_search_terms(conn):
    """Job titles the user cares about, injected into adapters that can search.

    Adapters have no database access by design, so the terms are passed in
    through the config dict as `_search_terms`. Adapters with no server-side
    search (Arbeitnow) simply ignore it.
    """
    rows = conn.execute(
        "SELECT term FROM search_terms WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [row["term"] for row in rows]


def _fetch_one_source(conn, source, result, search_terms=None, filter_config=None):
    name = source["name"]
    try:
        adapter = get_adapter(source["adapter"])
        config = json.loads(source["config_json"] or "{}")
        config["_search_terms"] = search_terms or []
        raw_jobs = adapter.fetch(config)
    except (SourceError, ValueError) as exc:
        message = str(exc)
        log.warning("source %s failed: %s", name, message)
        result.errors.append({"source": name, "error": message})
        conn.execute(
            "UPDATE sources SET last_run_at = ?, last_status = 'error', last_error = ? WHERE id = ?",
            (now_iso(), message, source["id"]),
        )
        conn.commit()
        return
    except Exception as exc:  # unexpected adapter bug — record, don't crash the run
        message = "{0}: {1}".format(type(exc).__name__, exc)
        log.exception("source %s raised unexpectedly", name)
        result.errors.append({"source": name, "error": message})
        conn.execute(
            "UPDATE sources SET last_run_at = ?, last_status = 'error', last_error = ? WHERE id = ?",
            (now_iso(), message, source["id"]),
        )
        conn.commit()
        return

    result.found += len(raw_jobs)
    for raw in raw_jobs:
        try:
            _store(conn, source["id"], raw, result, adapter, config, filter_config)
        except Exception:
            # One malformed posting must not lose the rest of the page.
            log.exception("failed to store posting %s from %s", raw.external_id, name)
    conn.execute(
        "UPDATE sources SET last_run_at = ?, last_status = 'ok', last_error = NULL, last_found = ? "
        "WHERE id = ?",
        (now_iso(), len(raw_jobs), source["id"]),
    )
    conn.commit()


def _store(conn, source_id, raw, result, adapter=None, config=None, filter_config=None):
    now = now_iso()
    key = dedup.dedup_key(raw.company, raw.title, raw.location)
    canonical = dedup.canonical_url(raw.url)

    existing = conn.execute(
        "SELECT * FROM jobs WHERE source_id = ? AND external_id = ?",
        (source_id, raw.external_id),
    ).fetchone()

    if existing is not None:
        _refresh(conn, existing, raw, dedup.content_hash(raw.description_text), now)
        result.refreshed += 1
        return

    twin = conn.execute(
        "SELECT * FROM jobs WHERE dedup_key = ? ORDER BY id LIMIT 1", (key,)
    ).fetchone()
    if twin is not None:
        _merge(conn, twin, source_id, raw, now)
        result.merged += 1
        return

    # Only now — once the posting is known to be genuinely new — is it worth a
    # second HTTP request for the description.
    if adapter is not None and getattr(adapter, "needs_detail_fetch", False):
        try:
            raw = adapter.fetch_detail(raw, config or {})
        except Exception:
            log.exception("detail fetch failed for %s; storing summary only", raw.external_id)

    body_hash = dedup.content_hash(raw.description_text)
    passes, reason = filters.evaluate(raw, filter_config)
    job_id = conn.execute(
        "INSERT INTO jobs (source_id, external_id, url, canonical_url, title, company, "
        "location, remote, employment_type, posted_at, first_seen_at, last_seen_at, "
        "description_html, description_text, raw_json, content_hash, dedup_key, status, "
        "stage0_pass, stage0_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
        (
            source_id, raw.external_id, raw.url, canonical, raw.title, raw.company,
            raw.location, _as_int(raw.remote), raw.employment_type, raw.posted_at,
            now, now, raw.description_html, raw.description_text,
            json.dumps(raw.raw, ensure_ascii=False), body_hash, key,
            1 if passes else 0, reason,
        ),
    ).lastrowid
    conn.commit()
    result.inserted += 1
    result.new_job_ids.append(job_id)


def _refresh(conn, existing, raw, body_hash, now):
    """Same posting, same source. Only touch the body if it actually changed."""
    if body_hash and body_hash != (existing["content_hash"] or ""):
        conn.execute(
            "UPDATE jobs SET last_seen_at = ?, description_html = ?, description_text = ?, "
            "content_hash = ? WHERE id = ?",
            (now, raw.description_html, raw.description_text, body_hash, existing["id"]),
        )
    else:
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (now, existing["id"]))
    conn.commit()


def _merge(conn, twin, source_id, raw, now):
    """Same role from a different source.

    The richest description wins, the alternate URL is remembered, and no new
    row appears — so the job is scored once and posted to Slack once.
    """
    incoming = raw.description_text or ""
    current = twin["description_text"] or ""

    try:
        payload = json.loads(twin["raw_json"] or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"_original": payload}

    alternates = payload.setdefault("_alt_sources", [])
    already = any(a.get("url") == raw.url for a in alternates if isinstance(a, dict))
    if not already:
        alternates.append({
            "source_id": source_id,
            "external_id": raw.external_id,
            "url": raw.url,
            "seen_at": now,
        })

    if len(incoming) > len(current):
        conn.execute(
            "UPDATE jobs SET last_seen_at = ?, description_html = ?, description_text = ?, "
            "content_hash = ?, raw_json = ? WHERE id = ?",
            (
                now, raw.description_html, raw.description_text,
                dedup.content_hash(raw.description_text),
                json.dumps(payload, ensure_ascii=False), twin["id"],
            ),
        )
    else:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ?, raw_json = ? WHERE id = ?",
            (now, json.dumps(payload, ensure_ascii=False), twin["id"]),
        )
    conn.commit()


def _as_int(value):
    if value is None:
        return None
    return 1 if value else 0


MANUAL_SOURCE_NAME = "Manual import"


def store_manual(conn, raw, filter_config=None):
    """Store a hand-imported posting and return its job id.

    Manual imports bypass the region filter: pasting a posting is a deliberate
    act, and silently filing a Berlin role as out-of-region because the config
    says so would be obstructive rather than helpful.
    """
    source = conn.execute(
        "SELECT * FROM sources WHERE adapter = 'manual'").fetchone()
    if source is None:
        conn.execute(
            "INSERT INTO sources (name, adapter, config_json, enabled, last_status) "
            "VALUES (?, 'manual', '{}', 1, 'ok')", (MANUAL_SOURCE_NAME,))
        conn.commit()
        source = conn.execute(
            "SELECT * FROM sources WHERE adapter = 'manual'").fetchone()

    now = now_iso()
    key = dedup.dedup_key(raw.company, raw.title, raw.location)
    existing = conn.execute(
        "SELECT * FROM jobs WHERE source_id = ? AND external_id = ?",
        (source["id"], raw.external_id)).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE jobs SET last_seen_at = ?, description_text = ?, "
            "content_hash = ?, status = CASE WHEN status = 'dismissed' THEN 'new' "
            "ELSE status END WHERE id = ?",
            (now, raw.description_text, dedup.content_hash(raw.description_text),
             existing["id"]))
        conn.commit()
        return existing["id"]

    job_id = conn.execute(
        "INSERT INTO jobs (source_id, external_id, url, canonical_url, title, company, "
        "location, remote, employment_type, posted_at, first_seen_at, last_seen_at, "
        "description_html, description_text, raw_json, content_hash, dedup_key, "
        "status, stage0_pass, stage0_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',1,'imported by hand')",
        (source["id"], raw.external_id, raw.url, dedup.canonical_url(raw.url),
         raw.title, raw.company, raw.location, _as_int(raw.remote),
         raw.employment_type, raw.posted_at, now, now, raw.description_html,
         raw.description_text, json.dumps(raw.raw, ensure_ascii=False),
         dedup.content_hash(raw.description_text), key),
    ).lastrowid
    conn.commit()
    return job_id
