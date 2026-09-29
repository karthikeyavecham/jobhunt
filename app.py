"""JobHunt — Flask application.

Run with:  venv/bin/python -m jobhunt.app
Then open  http://127.0.0.1:5002

Binds to localhost by default. Set HOST=0.0.0.0 to expose it on the LAN, which
also requires JOBHUNT_PASSWORD to be set (see README) because the database
holds API keys and personal data.
"""

import hashlib
import json
import logging
import os
import sqlite3
from collections import OrderedDict, namedtuple
from datetime import date, datetime, timedelta
from hmac import compare_digest
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, send_file, url_for)

from . import (config, doc_styles, documents, filters, generation, ingest,
               llm, profile, prompts, scheduler, scoring, slackbot, tasks,
               tracker, tracker_io)
from .db import close_db, connect, get_db, init_db
from .sources import ADAPTERS, adapter_choices, get_adapter, manual
from .sources.base import SourceError
from .util import day_bounds_utc, humanize_age, now_iso

logging.basicConfig(
    level=os.environ.get("JOBHUNT_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("jobhunt")

DEFAULT_PORT = 5002

#: Sort orders offered on the job list. Kept as a whitelist mapping to fixed
#: SQL rather than interpolating a user-supplied column: the value reaches an
#: ORDER BY, where a bound parameter is not allowed.
Sort = namedtuple("Sort", "label order_by")

_UNSCORED_LAST = "CASE WHEN sc.fit_score IS NULL THEN 1 ELSE 0 END"

JOB_SORTS = OrderedDict([
    # Unscored postings sort after scored ones rather than being treated as
    # zero, so a scoring backlog never buries good jobs.
    ("fit", Sort("Best fit", "{0}, sc.fit_score DESC, "
                 "COALESCE(j.posted_at, j.first_seen_at) DESC".format(_UNSCORED_LAST))),
    ("newest", Sort("Newest first", "j.first_seen_at DESC, j.id DESC")),
    ("oldest", Sort("Oldest first", "j.first_seen_at ASC, j.id ASC")),
    ("posted", Sort("Recently posted",
                    "COALESCE(j.posted_at, j.first_seen_at) DESC")),
    ("company", Sort("Company A–Z", "j.company COLLATE NOCASE ASC, j.title ASC")),
])

APPLICATION_SORTS = OrderedDict([
    ("stage", Sort("Pipeline stage", None)),        # tracker's own ordering
    ("applied", Sort("Recently applied",
                     "a.applied_at IS NULL, a.applied_at DESC")),
    ("due", Sort("Next action due",
                 "a.next_action_date IS NULL, a.next_action_date ASC")),
    ("fit", Sort("Best fit", "sc.fit_score IS NULL, sc.fit_score DESC")),
    ("company", Sort("Company A–Z", "j.company COLLATE NOCASE ASC")),
])

#: How many days of history the "last N days" shortcuts offer.
DAY_SHORTCUTS = (1, 3, 7, 14)

Window = namedtuple("Window", "clause params day days label")

#: No date filter applied.
NO_WINDOW = Window(None, (), None, None, None)


def create_app():
    app = Flask(__name__)
    # Only used to sign flash-message cookies; regenerated per start is fine
    # while there is no login session to preserve.
    app.secret_key = os.environ.get("JOBHUNT_SECRET_KEY") or os.urandom(32)
    app.teardown_appcontext(close_db)

    # Cross-site request forgery defence. Browsers attach HTTP Basic
    # credentials automatically once entered, so without this any website
    # could fire authenticated POSTs at the app's LAN address —
    # generating documents (spends LLM money), rewriting the config,
    # dismissing jobs. A same-origin app doesn't need per-form tokens: it is
    # enough to reject state-changing requests whose Origin (or, failing
    # that, Referer) names a different host. Requests carrying neither
    # header are allowed — that is curl and scripts, which cannot be
    # CSRF'd because CSRF requires a browser, and browsers always send
    # Origin on cross-site POSTs.
    @app.before_request
    def _reject_cross_site_writes():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source:
            return None
        if urlsplit(source).netloc == request.host:
            return None
        log.warning("rejected cross-site %s to %s from %s",
                    request.method, request.path, source)
        return Response("Cross-site requests are not accepted.", 403)

    # HTTP Basic auth, enabled whenever JOBHUNT_PASSWORD is set. The database
    # holds Slack tokens, an API key and personal data, and the app serves
    # generated CVs — on a LAN-exposed host that needs an actual check, not
    # just a refusal to start without a password configured.
    password = os.environ.get("JOBHUNT_PASSWORD")
    if password:
        @app.before_request
        def _require_auth():
            auth = request.authorization
            if auth and auth.password and compare_digest(auth.password, password):
                return None
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="JobHunt"'})

    init_db()

    # Exactly one instance may own the schedule and the Slack connection. Two
    # would mean two schedulers double-fetching and two Socket Mode
    # connections splitting button clicks between two databases. Owning them
    # is therefore opt-in: only `JOBHUNT_ROLE=production` (set in jobhunt.env
    # on the Pi) starts them, so running the app on the Mac out of habit gives
    # a harmless read-only web UI instead of a second live instance.
    production = os.environ.get("JOBHUNT_ROLE", "").strip().lower() == "production"
    app.config["JOBHUNT_PRODUCTION"] = production

    # Socket Mode runs in its own thread inside this process: connect() is
    # non-blocking and reconnects on its own, so one systemd unit covers the
    # web UI and the Slack listener together.
    if production and os.environ.get("JOBHUNT_NO_SLACK") != "1":
        try:
            app.slack_handler = slackbot.start_socket_mode()
        except Exception:
            log.exception("Slack Socket Mode failed to start; continuing without it")
            app.slack_handler = None

    if production and os.environ.get("JOBHUNT_NO_SCHEDULER") != "1":
        try:
            scheduler.scheduler.start()
        except Exception:
            log.exception("scheduler failed to start; continuing without it")

    if production:
        log.info("role=production — this instance owns the scheduler and Slack")
    elif (os.environ.get("JOBHUNT_NO_SLACK") == "1"
          or os.environ.get("JOBHUNT_NO_SCHEDULER") == "1"):
        # Tests and the local preview turn these off deliberately; saying so
        # loudly every time would train the eye to ignore the real warning.
        log.debug("role=web, scheduler and Slack explicitly disabled")
    else:
        # Said plainly, because the symptom of getting this wrong on the Pi is
        # "nothing has been fetched for hours" with nothing else in the log.
        log.warning(
            "role=web — no scheduler, no Slack. Nothing will be fetched or "
            "posted by this instance. Set JOBHUNT_ROLE=production if this is "
            "the machine that should own the schedule.")

    @app.context_processor
    def _inject_activity():
        """Running and recent background work, shown on every page."""
        return {"activity_running": tasks.registry.running(),
                "activity_recent": tasks.registry.recent()}

    @app.route("/")
    def dashboard():
        db = get_db()
        status = request.args.get("status", "new")
        sort = request.args.get("sort", "fit")
        if sort not in JOB_SORTS:
            sort = "fit"

        sql = (
            "SELECT j.*, src.name AS source_name, sc.fit_score, sc.verdict, "
            "sc.rationale, sc.gaps_json, d.german_level_required, "
            "d.english_sufficient, d.salary_stated, d.salary_quote "
            "FROM jobs j "
            "JOIN sources src ON src.id = j.source_id "
            "LEFT JOIN scores sc ON sc.job_id = j.id "
            "LEFT JOIN job_details d ON d.job_id = j.id "
        )
        where, params = [], []
        if status == "excluded":
            # The audit view: postings stage-0 filtered out, with the reason.
            where.append("COALESCE(j.stage0_pass, 1) = 0")
        else:
            # Every other view hides out-of-region postings. Arbeitnow returns
            # Europe-wide results, so without this the list is mostly noise.
            where.append("COALESCE(j.stage0_pass, 1) = 1")
            if status != "all":
                where.append("j.status = ?")
                params.append(status)

        window = _first_seen_window(db)
        if window.clause:
            where.append(window.clause)
            params.extend(window.params)

        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY " + JOB_SORTS[sort].order_by + " LIMIT 150"

        rows = []
        for job in db.execute(sql, params).fetchall():
            row = dict(job)
            row["posted_human"] = humanize_age(job["posted_at"] or job["first_seen_at"])
            row["seen_human"] = humanize_age(job["first_seen_at"])
            row["gaps"] = _json_list(job["gaps_json"])
            rows.append(row)
        return render_template(
            "dashboard.html", active="dashboard", jobs=rows,
            stats=_stats(db), status=status, sort=sort, sorts=JOB_SORTS,
            window=window, day_counts=_recent_day_counts(db),
            day_shortcuts=DAY_SHORTCUTS,
        )

    @app.route("/job/<int:job_id>")
    def job_detail(job_id):
        db = get_db()
        job = db.execute(
            "SELECT j.*, src.name AS source_name, sc.fit_score, sc.verdict, "
            "sc.matched_json, sc.gaps_json, sc.rationale, sc.model_used, sc.scored_at, "
            "d.german_level_required, d.english_sufficient, d.language_quote, "
            "d.salary_stated, d.salary_quote, d.remote_policy, d.seniority, "
            "d.people_management_required "
            "FROM jobs j "
            "JOIN sources src ON src.id = j.source_id "
            "LEFT JOIN scores sc ON sc.job_id = j.id "
            "LEFT JOIN job_details d ON d.job_id = j.id "
            "WHERE j.id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            abort(404)

        row = dict(job)
        row["posted_human"] = humanize_age(job["posted_at"] or job["first_seen_at"])
        row["first_seen_human"] = humanize_age(job["first_seen_at"])
        row["alt_sources"] = _alt_sources(job["raw_json"])
        row["matched"] = _json_list(job["matched_json"])
        row["gaps"] = _json_list(job["gaps_json"])
        return render_template(
            "job_detail.html", active="dashboard", job=row,
            application=tracker.get(db, job_id),
            pipeline=tracker.PIPELINE,
            docs=generation.documents_for(db, job_id),
            templates=doc_styles.template_choices(),
            default_template=config.get_setting(db, "documents.template", "classic"),
            default_brand=doc_styles.DEFAULT_BRAND,
            pdf_available=documents.pdf_available(),
        )

    @app.route("/job/<int:job_id>/generate", methods=["POST"])
    def generate_document_route(job_id):
        db = get_db()
        job = db.execute(slackbot.CARD_SQL + "WHERE j.id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)

        kind = request.form.get("kind", "cv")
        if kind not in ("cv", "cover_letter"):
            flash("Unknown document type.", "error")
            return redirect(url_for("job_detail", job_id=job_id))

        template = request.form.get("template") or config.get_setting(
            db, "documents.template", "classic")
        brand = (request.form.get("brand") or "").strip() or None

        # Generation takes 60-80 seconds, well past what a browser will wait
        # for, so it runs on a thread and the activity panel reports the
        # outcome. Two guards, deliberately: the registry key stops a duplicate
        # *task*, and in_flight_guard stops a duplicate *generation* across
        # both entry points, so a Slack click and a web click cannot collide.
        label = "{0} for {1}".format(
            "CV" if kind == "cv" else "Cover letter", job["company"])

        def work():
            conn = connect()
            try:
                with generation.in_flight_guard(job_id, kind) as acquired:
                    if not acquired:
                        return "already generating from another button"
                    row = conn.execute(
                        slackbot.CARD_SQL + "WHERE j.id = ?", (job_id,)).fetchone()
                    provider = config.build_writing_provider(conn)
                    result = generation.generate_document(
                        conn, row, provider, kind=kind, template_key=template,
                        brand=brand,
                        blocklist=config.get_setting(conn, "documents.blocklist",
                                                     None))
                    return "{0} v{1} ready — {2}, {3}{4}".format(
                        "CV" if kind == "cv" else "Cover letter",
                        result["version"], result["template"],
                        result["brand_color"],
                        "" if result["pdf"] else " (no PDF on this host)")
            except generation.GenerationBlocked as exc:
                return "BLOCKED — the draft mentioned {0}; nothing was saved".format(
                    ", ".join(exc.terms))
            finally:
                conn.close()

        if tasks.registry.start("generate:{0}:{1}".format(job_id, kind), label, work):
            flash("Generating in the background — about a minute. The result "
                  "appears in Activity at the top of the page.", "ok")
        else:
            flash("That document is already being generated.", "error")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/document/<int:document_id>")
    def download_document(document_id):
        db = get_db()
        row = db.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            abort(404)
        path = documents.resolve_path(row["path"])
        # Confine downloads to the documents directory: the stored path is
        # generated, not user input, but a traversal here would serve any file.
        try:
            path.resolve().relative_to(documents.OUTPUT_DIR.resolve())
        except ValueError:
            abort(403)
        if not path.exists():
            flash("That file is no longer on disk.", "error")
            return redirect(url_for("job_detail", job_id=row["job_id"]))
        return send_file(str(path), as_attachment=True, download_name=path.name)

    @app.route("/job/<int:job_id>/score", methods=["POST"])
    def score_one(job_id):
        db = get_db()
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            abort(404)
        try:
            provider = config.build_provider(db)
            result = scoring.score_job(db, job, provider)
        except llm.LLMError as exc:
            flash("Scoring failed: {0}".format(exc), "error")
        else:
            flash(
                "Scored {0}/100 ({1}) via {2}.".format(
                    result["fit_score"], result["verdict"], provider.model_label()
                ),
                "ok",
            )
        return redirect(url_for("job_detail", job_id=job_id))

    @app.route("/score", methods=["POST"])
    def score_pending_route():
        db = get_db()
        try:
            provider = config.build_provider(db)
        except llm.LLMError as exc:
            flash("No usable LLM provider: {0}".format(exc), "error")
            return redirect(url_for("dashboard"))

        limit = config.get_setting(db, "scoring.batch_limit", 40)
        workers = config.get_setting(db, "scoring.concurrency", 1)

        def work():
            conn = connect()
            try:
                run = scoring.score_pending(conn, provider, limit=limit,
                                            concurrency=workers)
                summary = "scored {0}, {1} failed".format(run.scored, run.failed)
                if run.aborted_reason:
                    summary += " — stopped: " + run.aborted_reason
                return summary
            finally:
                conn.close()

        if tasks.registry.start("score", "Score outstanding postings", work):
            flash("Scoring in the background — progress shows in Activity above.",
                  "ok")
        else:
            flash("Scoring is already running.", "error")
        return redirect(url_for("dashboard"))

    @app.route("/scoring")
    def scoring_page():
        db = get_db()
        tz = config.local_timezone(db)
        days = scoring.daily_progress(db, tz)
        totals = {
            "total": sum(d["total"] for d in days),
            "scored": sum(d["scored"] for d in days),
            "pending": sum(d["pending"] for d in days),
            "skipped": sum(d["skipped"] for d in days),
        }
        batch = int(config.get_setting(db, "scoring.batch_limit", 40) or 40)
        interval = int(config.get_setting(db, "schedule.interval_hours", 3) or 3)
        return render_template(
            "scoring.html", active="scoring", days=days, totals=totals,
            batch=batch, running=tasks.registry.is_running("score"),
            concurrency=int(config.get_setting(db, "scoring.concurrency", 1) or 1),
            backlog_hint=_backlog_hint(totals["pending"], batch, interval),
            today=datetime.now(tz).date().isoformat(),
        )

    @app.route("/scoring/run", methods=["POST"])
    def score_day():
        """Score one day's outstanding postings, or all of them."""
        db = get_db()
        try:
            provider = config.build_provider(db)
        except llm.LLMError as exc:
            flash("No usable LLM provider: {0}".format(exc), "error")
            return redirect(url_for("scoring_page"))

        raw_day = (request.form.get("day") or "").strip()
        since = until = None
        label = "Score every outstanding posting"
        if raw_day:
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                flash("Not a date: {0!r}.".format(raw_day[:20]), "error")
                return redirect(url_for("scoring_page"))
            since, until = day_bounds_utc(day, config.local_timezone(db))
            label = "Score postings first seen on {0}".format(day.isoformat())

        outstanding = scoring.pending_count(db, since=since, until=until)
        if not outstanding:
            flash("Nothing outstanding there.", "ok")
            return redirect(url_for("scoring_page"))

        workers = config.get_setting(db, "scoring.concurrency", 1)

        def work():
            conn = connect()
            try:
                # No batch limit: picking a day is a deliberate "work through
                # this one", and capping it at 40 would leave the day looking
                # half-done with no indication why.
                run = scoring.score_pending(conn, provider, since=since,
                                            until=until, concurrency=workers)
                summary = "scored {0}, {1} failed".format(run.scored, run.failed)
                if run.aborted_reason:
                    summary += " — stopped: " + run.aborted_reason
                return summary
            finally:
                conn.close()

        # The same key the dashboard's "Score unscored" button uses, so the two
        # cannot both be hammering the local model at once.
        if tasks.registry.start("score", label, work):
            flash("Scoring {0} posting(s) in the background — progress shows in "
                  "Activity above.".format(outstanding), "ok")
        else:
            flash("Scoring is already running.", "error")
        return redirect(url_for("scoring_page"))

    @app.route("/job/<int:job_id>/status", methods=["POST"])
    def set_job_status(job_id):
        status = request.form.get("status", "")
        if status not in ("new", "shortlisted", "dismissed"):
            flash("Unknown status '{0}'.".format(status), "error")
            return redirect(url_for("job_detail", job_id=job_id))

        db = get_db()
        cursor = db.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
        )
        db.commit()
        if cursor.rowcount == 0:
            abort(404)

        flash("Marked as {0}.".format(status), "ok")
        return redirect(request.form.get("next") or url_for("job_detail", job_id=job_id))

    @app.route("/import", methods=["GET", "POST"])
    def manual_import():
        # No get_db() here: the work runs on a thread with its own connection,
        # because a request-scoped one is closed at teardown.
        if request.method == "GET":
            return render_template("import.html", active="import",
                                   blocked=manual.NO_FETCH_HOSTS)

        url = (request.form.get("url") or "").strip()
        pasted = (request.form.get("text") or "").strip()
        if not (url or pasted):
            flash("Paste a URL or the posting text.", "error")
            return redirect(url_for("manual_import"))

        # Fetch, extract and score together take about a minute, so the work
        # runs on a thread. It is scored immediately rather than waiting for
        # the next cycle: importing by hand is a deliberate act, and a verdict
        # three hours later would defeat the point.
        def work():
            conn = connect()
            try:
                text = pasted or manual.fetch_url(url)
                fields = manual.extract(text, config.build_provider(conn),
                                        url or None)
                raw = manual.make_raw_job(fields, url or None, text)
                job_id = ingest.store_manual(conn, raw)
                try:
                    result = scoring.score_job(conn, conn.execute(
                        "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(),
                        config.build_provider(conn))
                    verdict = " — {0}/100".format(result["fit_score"])
                except llm.LLMError as exc:
                    verdict = " — imported, but scoring failed: {0}".format(exc)
                return "{0} at {1}{2} (job {3})".format(
                    raw.title, raw.company, verdict, job_id)
            except SourceError as exc:
                return "could not import: {0}".format(exc)
            finally:
                conn.close()

        key = "import:" + (url or hashlib.sha1(
            pasted.encode("utf-8")).hexdigest()[:12])
        if tasks.registry.start(key, "Import posting", work):
            flash("Importing in the background — the result appears in Activity "
                  "above, and the job lands on the dashboard.", "ok")
        else:
            flash("That posting is already being imported.", "error")
        return redirect(url_for("manual_import"))

    # ---- Application pipeline -------------------------------------------
    @app.route("/applications")
    def applications_page():
        db = get_db()
        sort = request.args.get("sort", "stage")
        if sort not in APPLICATION_SORTS:
            sort = "stage"
        return render_template(
            "applications.html", active="applications",
            rows=tracker.all_applications(db, APPLICATION_SORTS[sort].order_by),
            counts=tracker.counts(db),
            due=tracker.due_actions(db),
            stale=tracker.stale_shortlists(db),
            pipeline=tracker.PIPELINE,
            sort=sort, sorts=APPLICATION_SORTS,
        )

    @app.route("/job/<int:job_id>/pipeline", methods=["POST"])
    def update_pipeline(job_id):
        db = get_db()
        if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            abort(404)

        # A blank input means "leave unchanged", so submitting the form with an
        # empty notes box never wipes existing notes.
        fields = {}
        for key in ("pipeline_status", "applied_at", "next_action",
                    "next_action_date", "notes", "contact_name",
                    "contact_email", "referral"):
            value = (request.form.get(key) or "").strip()
            if value:
                fields[key] = value
        try:
            row = tracker.update(db, job_id, **fields)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Pipeline updated — now {0}.".format(row["pipeline_status"]), "ok")
        return redirect(request.form.get("next") or url_for("job_detail", job_id=job_id))

    @app.route("/applications/export.<fmt>")
    def export_applications(fmt):
        db = get_db()
        stamp = date.today().isoformat()
        if fmt == "json":
            return Response(
                tracker_io.to_json(db), mimetype="application/json",
                headers={"Content-Disposition":
                         'attachment; filename="jobhunt-applications-{0}.json"'
                         .format(stamp)})
        if fmt == "xlsx":
            return Response(
                tracker_io.to_xlsx(db),
                mimetype="application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet",
                headers={"Content-Disposition":
                         'attachment; filename="jobhunt-applications-{0}.xlsx"'
                         .format(stamp)})
        abort(404)

    @app.route("/applications/import", methods=["POST"])
    def import_tracker_route():
        db = get_db()
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a .xlsx or .csv file to import.", "error")
            return redirect(url_for("applications_page"))
        try:
            result = tracker_io.import_applications(db, upload.read(), upload.filename)
        except Exception as exc:
            flash("Import failed: {0}".format(exc), "error")
            return redirect(url_for("applications_page"))

        message = "Imported from {0}: {1}.".format(upload.filename, result.summary())
        if result.unknown_headers:
            message += " Ignored columns: {0}.".format(
                ", ".join(result.unknown_headers[:6]))
        flash(message, "ok")
        for number, reason in result.skipped[:5]:
            flash("Row {0} skipped — {1}.".format(number, reason), "error")
        return redirect(url_for("applications_page"))

    # ---- Sources ----------------------------------------------------------
    # Only the per-company boards can be added from the UI. The aggregators
    # (arbeitnow, arbeitsagentur) and `manual` are singletons configured at
    # install time: a second copy of one would fetch the same feed twice every
    # cycle — wasted HTTP, doubled dedup work, and racing inserts in _store().
    SLUG_ADAPTERS = ("greenhouse", "lever", "smartrecruiters", "personio",
                     "recruitee")

    @app.route("/sources/add", methods=["POST"])
    def add_source():
        db = get_db()
        adapter = request.form.get("adapter", "")
        if adapter not in ADAPTERS:
            flash("Unknown source type.", "error")
            return redirect(url_for("sources_page"))
        if adapter not in SLUG_ADAPTERS:
            flash("'{0}' is configured once at install time and cannot be added "
                  "again — a second copy would fetch the same feed twice every "
                  "cycle. Only company career boards can be added here."
                  .format(adapter), "error")
            return redirect(url_for("sources_page"))

        slug = (request.form.get("slug") or "").strip().strip("/")
        company = (request.form.get("company") or "").strip()
        if not slug:
            flash("{0} needs a company slug — the identifier in the board's URL, "
                  "e.g. 'gitlab' in boards.greenhouse.io/gitlab.".format(adapter),
                  "error")
            return redirect(url_for("sources_page"))

        name = (request.form.get("name") or "").strip() or "{0} ({1})".format(
            company or slug, adapter)
        settings = {"slug": slug}
        if company:
            settings["company"] = company

        try:
            db.execute(
                "INSERT INTO sources (name, adapter, config_json, enabled, "
                "last_status) VALUES (?, ?, ?, 1, 'never')",
                (name, adapter, json.dumps(settings)))
            db.commit()
        except sqlite3.IntegrityError:
            flash("A source named '{0}' already exists.".format(name), "error")
            return redirect(url_for("sources_page"))

        flash("Added '{0}'. Press Test to check the slug before the next run."
              .format(name), "ok")
        return redirect(url_for("sources_page"))

    @app.route("/sources/<int:source_id>/toggle", methods=["POST"])
    def toggle_source(source_id):
        db = get_db()
        db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id = ?",
                   (source_id,))
        db.commit()
        return redirect(url_for("sources_page"))

    @app.route("/sources/<int:source_id>/delete", methods=["POST"])
    def delete_source(source_id):
        db = get_db()
        source = db.execute("SELECT * FROM sources WHERE id = ?",
                            (source_id,)).fetchone()
        if source is None:
            abort(404)
        # jobs cascade-delete with their source, which would take scores and
        # tracked applications with them. Disabling is nearly always what is
        # meant, so deletion is refused once a source has stored postings.
        count = db.execute("SELECT COUNT(*) FROM jobs WHERE source_id = ?",
                           (source_id,)).fetchone()[0]
        if count:
            flash("'{0}' has {1} stored posting(s) — disable it instead, so its "
                  "jobs and any tracked applications survive.".format(
                      source["name"], count), "error")
            return redirect(url_for("sources_page"))
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        # Remember the removal, so a seeded source stays gone instead of
        # reappearing on the next start.
        removed = set(config.get_setting(db, "sources.removed", []) or [])
        removed.add(source["name"])
        config.set_setting(db, "sources.removed", sorted(removed))
        db.commit()
        flash("Removed '{0}'.".format(source["name"]), "ok")
        return redirect(url_for("sources_page"))

    @app.route("/config")
    def config_page():
        db = get_db()
        secrets = {
            key: dict(zip(("is_set", "source", "masked"),
                          config.secret_status(db, key)))
            for key in config.SECRET_ENV
        }
        terms = db.execute("SELECT * FROM search_terms ORDER BY term").fetchall()
        return render_template(
            "config.html", active="config",
            s=config.load_settings(db), secrets=secrets, terms=terms,
            anthropic_models=llm.ANTHROPIC_MODELS,
            jobs=scheduler.scheduler.jobs(),
        )

    #: The free-text profile sections, in the order the page shows them.
    PROFILE_TEXT_SECTIONS = (
        ("summary", "Career summary",
         "Roles, employers, dates and what you actually did. This is fed to "
         "the model that scores postings and drafts your CV, so detail here is "
         "what makes both accurate."),
        ("skills", "Skills",
         "Tools, platforms, languages and methods, as a run of phrases rather "
         "than a list. Anything a posting might name."),
        ("preferences", "Preferences and constraints",
         "Target roles, the region you can work in, languages, and your right "
         "to work. Used for scoring, not printed on the CV."),
        ("languages", "Languages line",
         "Printed verbatim on the CV. The model never writes this — stating a "
         "level you do not have is the one mistake a CV cannot recover from."),
        ("constraints", "Never overstate",
         "Injected into every CV and cover letter prompt as hard rules. Write "
         "plain statements: a language level below what postings ask for, "
         "mentoring rather than managing, anything that must not be dressed up."),
    )

    @app.route("/profile")
    def profile_page():
        db = get_db()
        profile.ensure_seeded(db)
        return render_template(
            "profile.html", active="profile",
            contact=profile.get_section(db, "contact") or {},
            contact_fields=profile.PLACEHOLDER["contact"].keys(),
            sections=[(key, label, hint, profile.get_section(db, key) or "")
                      for key, label, hint in PROFILE_TEXT_SECTIONS],
            is_placeholder=profile.is_placeholder(db),
        )

    @app.route("/profile/save", methods=["POST"])
    def save_profile():
        db = get_db()
        section = request.form.get("section", "")

        if section == "contact":
            contact = dict(profile.get_section(db, "contact") or {})
            for field in profile.PLACEHOLDER["contact"]:
                if field in request.form:
                    contact[field] = request.form[field].strip()
            profile.set_section(db, "contact", contact)
            flash("Contact details saved.", "ok")
            return redirect(url_for("profile_page"))

        known = {key for key, _, _ in PROFILE_TEXT_SECTIONS}
        if section not in known:
            flash("Unknown profile section.", "error")
            return redirect(url_for("profile_page"))

        # Stored even when empty: clearing a section has to mean "none", not
        # "fall back to whatever the seed said".
        profile.set_section(db, section, request.form.get(section, "").strip())
        flash("Saved. The next scoring run and the next document use it.", "ok")
        return redirect(url_for("profile_page"))

    @app.route("/config/prompts")
    def prompts_page():
        db = get_db()
        blocks = []
        for name, (_, _, description) in prompts.EDITABLE.items():
            blocks.append({
                "name": name,
                "description": description,
                "text": prompts.get(db, name),
                "customised": prompts.is_customised(db, name),
                "default_length": len(prompts.default(name)),
            })
        return render_template(
            "prompts.html", active="config", blocks=blocks,
            version=prompts.version(db),
            effective_cv=prompts.system_prompt(db, "cv"),
            effective_letter=prompts.system_prompt(db, "letter"),
        )

    @app.route("/config/prompts/save", methods=["POST"])
    def save_prompts():
        db = get_db()
        changed = []
        for name in prompts.EDITABLE:
            if name not in request.form:
                continue            # the form posts one block at a time
            before = prompts.get(db, name)
            prompts.set_prompt(db, name, request.form[name])
            if prompts.get(db, name) != before:
                changed.append(name)
        if not changed:
            flash("No changes.", "ok")
        else:
            flash("Saved. The next document generated uses the new wording — "
                  "already-generated documents are unaffected.", "ok")
        return redirect(url_for("prompts_page"))

    @app.route("/config/prompts/reset", methods=["POST"])
    def reset_prompt():
        db = get_db()
        name = request.form.get("name", "")
        if name not in prompts.EDITABLE:
            flash("Unknown prompt.", "error")
            return redirect(url_for("prompts_page"))
        prompts.reset(db, name)
        flash("Restored the shipped wording for {0}.".format(
            name.replace("_", " ")), "ok")
        return redirect(url_for("prompts_page"))

    #: Which keys each config form is allowed to write. Anything not listed is
    #: ignored, so a crafted POST cannot reach an unrelated setting.
    CONFIG_SECTIONS = {
        "schedule": ["schedule.enabled", "schedule.interval_hours",
                     "schedule.timezone", "schedule.report_at",
                     "slack.notify_from", "slack.notify_until"],
        "llm": ["llm.local.base_url", "llm.local.model", "llm.local.attempts",
                "llm.fallback.enabled", "llm.anthropic.model"],
        "scoring": ["scoring.enabled", "scoring.batch_limit",
                    "scoring.concurrency"],
        "slack": ["slack.enabled", "slack.channel_id", "slack.score_threshold",
                  "slack.max_cards_per_run", "slack.allowed_user_ids"],
        "filter": ["filter.allow_remote", "filter.allow_relocation",
                   "filter.max_age_days", "filter.company_blocklist"],
    }
    LIST_KEYS = {"slack.allowed_user_ids", "filter.company_blocklist"}

    @app.route("/config/save", methods=["POST"])
    def save_config():
        db = get_db()
        section = request.form.get("section", "")
        allowed = CONFIG_SECTIONS.get(section)
        if allowed is None:
            flash("Unknown settings section.", "error")
            return redirect(url_for("config_page"))

        for key in allowed:
            if key not in request.form:
                continue
            raw = request.form[key].strip()
            if key in LIST_KEYS:
                value = [v.strip() for v in raw.split(",") if v.strip()]
            elif key in config._BOOL_KEYS:
                value = raw == "1"
            elif key in config._INT_KEYS:
                try:
                    value = int(raw)
                except ValueError:
                    flash("{0} must be a number — left unchanged.".format(key), "error")
                    continue
            else:
                value = raw
            config.set_setting(db, key, value)

        if section in ("schedule",):
            scheduler.scheduler.reschedule()
            flash("Schedule saved and applied without a restart.", "ok")
        else:
            flash("Saved.", "ok")
        return redirect(url_for("config_page"))

    @app.route("/config/secret", methods=["POST"])
    def save_secret():
        db = get_db()
        key = request.form.get("key", "")
        if key not in config.SECRET_ENV:
            flash("Unknown secret.", "error")
            return redirect(url_for("config_page"))
        value = request.form.get("value", "").strip()
        if not value:
            flash("No value entered — the existing key is unchanged.", "ok")
            return redirect(url_for("config_page"))
        config.set_secret(db, key, value)
        _, _, masked = config.secret_status(db, key)
        flash("Stored {0} = {1}.".format(key, masked), "ok")
        return redirect(url_for("config_page"))

    @app.route("/config/test-provider", methods=["POST"])
    def test_provider():
        db = get_db()
        try:
            provider = config.build_provider(db)
            ok, message = provider.test()
        except Exception as exc:
            ok, message = False, "{0}: {1}".format(type(exc).__name__, exc)
        flash(message, "ok" if ok else "error")
        return redirect(url_for("config_page"))

    @app.route("/config/terms/add", methods=["POST"])
    def add_term():
        db = get_db()
        term = request.form.get("term", "").strip()
        scope = request.form.get("match_scope", "title")
        if not term:
            flash("Enter a job title.", "error")
            return redirect(url_for("config_page"))
        try:
            db.execute(
                "INSERT INTO search_terms (term, enabled, match_scope, created_at) "
                "VALUES (?, 1, ?, ?)", (term, scope, now_iso()))
            db.commit()
        except sqlite3.IntegrityError:
            flash("'{0}' is already in the list.".format(term), "error")
        else:
            flash("Added '{0}'. It applies from the next run — use Backfill to "
                  "re-check stored postings too.".format(term), "ok")
        return redirect(url_for("config_page"))

    @app.route("/config/terms/<int:term_id>/toggle", methods=["POST"])
    def toggle_term(term_id):
        db = get_db()
        db.execute(
            "UPDATE search_terms SET enabled = 1 - enabled WHERE id = ?", (term_id,))
        db.commit()
        return redirect(url_for("config_page"))

    @app.route("/config/terms/<int:term_id>/delete", methods=["POST"])
    def delete_term(term_id):
        db = get_db()
        db.execute("DELETE FROM search_terms WHERE id = ?", (term_id,))
        db.commit()
        flash("Removed.", "ok")
        return redirect(url_for("config_page"))

    @app.route("/config/backfill", methods=["POST"])
    def backfill_filters():
        db = get_db()
        passed, failed = filters.reevaluate_all(db)
        flash("Re-checked every stored posting: {0} in region, {1} excluded."
              .format(passed, failed), "ok")
        return redirect(url_for("config_page"))

    @app.route("/run", methods=["POST"])
    def run_cycle_now():
        def work():
            return scheduler.run_cycle(trigger="manual", force_notify=True).summary()

        if tasks.registry.start("cycle", "Fetch, score and notify", work):
            flash("Full cycle started in the background.", "ok")
        else:
            flash("A cycle is already running.", "error")
        return redirect(url_for("dashboard"))

    @app.route("/sources")
    def sources_page():
        db = get_db()
        rows = []
        for source in db.execute("SELECT * FROM sources ORDER BY id").fetchall():
            row = dict(source)
            row["last_run_human"] = (
                humanize_age(source["last_run_at"]) if source["last_run_at"] else "never"
            )
            rows.append(row)
        return render_template(
            "sources.html", active="sources", sources=rows,
            adapters=[(n, l) for n, l in adapter_choices()
                      if n in SLUG_ADAPTERS])

    @app.route("/fetch", methods=["POST"])
    def fetch_now():
        # Routed through run_cycle rather than run_fetch: that takes the cycle
        # lock, so a manual fetch can no longer overlap a scheduled one and
        # race it inside _store().
        def work():
            return scheduler.run_cycle(trigger="manual", force_notify=True).summary()

        if tasks.registry.start("cycle", "Fetch, score and notify", work):
            flash("Fetching in the background — progress shows in Activity above.",
                  "ok")
        else:
            flash("A cycle is already running.", "error")
        return redirect(url_for("dashboard"))

    @app.route("/sources/<int:source_id>/test", methods=["POST"])
    def test_source(source_id):
        db = get_db()
        source = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            flash("No such source.", "error")
            return redirect(url_for("sources_page"))
        try:
            adapter = get_adapter(source["adapter"])
            ok, message = adapter.test(json.loads(source["config_json"] or "{}"))
        except SourceError as exc:
            ok, message = False, str(exc)

        # Record the outcome, or the health column keeps saying "never run"
        # immediately after a successful test — which reads like a failure.
        db.execute(
            "UPDATE sources SET last_run_at = ?, last_status = ?, last_error = ? "
            "WHERE id = ?",
            (now_iso(), "ok" if ok else "error", None if ok else message, source_id))
        db.commit()

        flash("{0} — {1}".format(source["name"], message), "ok" if ok else "error")
        return redirect(url_for("sources_page"))

    return app


def _first_seen_window(db):
    """Read `day` / `days` from the query string into a SQL fragment.

    Filters on `first_seen_at` — when JobHunt found the posting — rather than
    `posted_at`, because the question this answers is "what came in since I
    last looked", and `posted_at` is missing or approximate on several feeds.

    Days are local days (Europe/Berlin by default), so "13 August" means the
    Berlin day, not 02:00–02:00. `day` wins over `days` if both are given.
    """
    tz = config.local_timezone(db)
    today = datetime.now(tz).date()

    day = (request.args.get("day") or "").strip()
    if day:
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            flash("Ignored an unreadable date filter: {0!r}.".format(day[:20]),
                  "error")
            return NO_WINDOW
        start, end = day_bounds_utc(parsed, tz)
        label = "first seen {0}".format(
            "today" if parsed == today
            else "yesterday" if parsed == today - timedelta(days=1)
            else parsed.isoformat())
        return Window("j.first_seen_at >= ? AND j.first_seen_at < ?",
                      (start, end), parsed.isoformat(), None, label)

    raw_days = (request.args.get("days") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            days = 0
        if days > 0:
            # Inclusive of today: days=1 is today, days=7 is today plus the
            # six before it.
            start = day_bounds_utc(today - timedelta(days=days - 1), tz)[0]
            label = ("first seen today" if days == 1
                     else "first seen in the last {0} days".format(days))
            return Window("j.first_seen_at >= ?", (start,), None, days, label)

    return NO_WINDOW


def _recent_day_counts(db, limit=10):
    """(iso date, count) for the days postings arrived on, newest first.

    Drives the dashboard's day picker. Shares `scoring.daily_progress` with the
    scoring page so the two can never disagree about how many postings a day
    holds, and so the Berlin-day bucketing lives in one place.
    """
    return [(day["day"], day["total"])
            for day in scoring.daily_progress(db, config.local_timezone(db),
                                              days=limit)]


def _json_list(raw):
    """Decode a stored JSON array, tolerating nulls and malformed values."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _alt_sources(raw_json):
    """Alternate URLs recorded when the same role arrived from another source."""
    if not raw_json:
        return []
    try:
        payload = json.loads(raw_json)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    return [a for a in payload.get("_alt_sources", []) if isinstance(a, dict)]


def _stats(db):
    # Counts are over relevant postings only — the excluded pile is reported
    # separately so the headline numbers reflect what is actually actionable.
    counts = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT status, COUNT(*) FROM jobs WHERE COALESCE(stage0_pass, 1) = 1 "
            "GROUP BY status"
        ).fetchall()
    }
    total = sum(counts.values())
    excluded = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(stage0_pass, 1) = 0"
    ).fetchone()[0]
    sources_total = db.execute("SELECT COUNT(*) FROM sources WHERE enabled = 1").fetchone()[0]
    sources_ok = db.execute(
        "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND last_status = 'ok'"
    ).fetchone()[0]

    last = db.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_run = humanize_age(last["finished_at"]) if last else None
    last_counts = (
        "{0} found, {1} new".format(last["jobs_found"], last["jobs_new"]) if last else None
    )

    pending = scoring.pending_count(db)
    batch = config.get_setting(db, "scoring.batch_limit", 40) or 40
    interval = config.get_setting(db, "schedule.interval_hours", 3) or 3
    # Cycles needed to clear the backlog, so a big number reads as "about a
    # day" rather than as an alarming count with no scale.
    cycles = -(-pending // int(batch)) if pending else 0

    return {
        "total": total,
        "excluded": excluded,
        "new": counts.get("new", 0),
        "shortlisted": counts.get("shortlisted", 0),
        "dismissed": counts.get("dismissed", 0),
        "pending_scoring": pending,
        "pending_hint": _backlog_hint(pending, int(batch), int(interval)),
        "sources_total": sources_total,
        "sources_ok": sources_ok,
        "last_run": last_run,
        "last_run_counts": last_counts,
        "pending_cycles": cycles,
    }


def _backlog_hint(pending, batch, interval_hours):
    """Plain-English scale for the scoring backlog."""
    if not pending:
        return "all caught up"
    cycles = -(-pending // batch)
    if cycles <= 1:
        return "clears next cycle"
    hours = cycles * interval_hours
    if hours < 24:
        return "about {0}h at {1}/cycle".format(hours, batch)
    return "about {0} day{1} at {2}/cycle".format(
        round(hours / 24), "" if round(hours / 24) == 1 else "s", batch)


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", DEFAULT_PORT))

    if host != "127.0.0.1" and not os.environ.get("JOBHUNT_PASSWORD"):
        raise SystemExit(
            "Refusing to bind {0} without JOBHUNT_PASSWORD set — the database holds "
            "API keys and personal data. Set the variable or use HOST=127.0.0.1.".format(host)
        )

    app = create_app()
    from waitress import serve

    log.info("JobHunt listening on http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
