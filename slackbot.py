"""Slack integration over Socket Mode.

Socket Mode is what makes this work on the Pi: the app opens an outbound
WebSocket to Slack, so button clicks arrive without any public URL, tunnel or
port forwarding.

Two constraints shape the design:

  * **Slack demands an ack within 3 seconds**, but generating a CV is a 30-60s
    LLM call. Every handler acks immediately and does the real work on a
    background thread, then edits the card to show the outcome.
  * **A card must be posted once per job, ever.** `jobs.slack_ts` is the guard.
    At a 3-hour cycle a long-lived posting is re-seen ~70 times, and a leak
    here turns the channel into noise.

Authorisation: Socket Mode payloads arrive over an authenticated WebSocket, so
there is no signing secret to verify — but any workspace member can click a
button, so every action checks the payload's user against an allowlist.
"""

import logging
import threading

from . import config, tracker
from .db import connect
from .util import humanize_age, now_iso

log = logging.getLogger(__name__)

VERDICT_EMOJI = {"strong": "🟢", "good": "🟢", "stretch": "🟡", "no": "⚪"}

# action_id -> label, used for both the buttons and the audit trail on the card.
ACTION_SHORTLIST = "job_shortlist"
ACTION_APPLIED = "job_applied"
ACTION_DISMISS = "job_dismiss"
ACTION_CV = "job_generate_cv"
ACTION_LETTER = "job_generate_letter"


class SlackNotConfigured(Exception):
    """Tokens or channel are missing — not an error, just not set up yet."""


def _require(conn):
    bot_token = config.get_secret(conn, "slack.bot_token")
    channel = config.get_setting(conn, "slack.channel_id")
    if not bot_token:
        raise SlackNotConfigured("No Slack bot token configured")
    if not channel:
        raise SlackNotConfigured("No Slack channel configured")
    return bot_token, channel


def get_client(conn):
    from slack_sdk import WebClient

    bot_token, _ = _require(conn)
    return WebClient(token=bot_token)


# --------------------------------------------------------------------------
# Card rendering
# --------------------------------------------------------------------------

def _language_line(job):
    """Only ever reports what the posting actually said."""
    level = job["german_level_required"]
    if job["english_sufficient"]:
        return "🗣 English sufficient"
    if level and level not in ("not_stated", "none"):
        return "🗣 German {0} required".format(level)
    return "🗣 Language requirement not stated"


def _salary_line(job):
    if job["salary_stated"] and job["salary_quote"]:
        return "💶 {0}".format(job["salary_quote"][:120])
    return "💶 Salary not stated"


def _location_line(job):
    parts = [job["location"] or "location not stated"]
    policy = job["remote_policy"]
    if policy and policy not in ("not_stated", "onsite"):
        parts.append(policy.replace("_", " "))
    elif job["remote"]:
        parts.append("home office possible")
    return "📍 " + " · ".join(parts)


def build_card(job, applied_state=None):
    """Block Kit blocks for one posting.

    `applied_state` is a short string appended as a context line once an
    action has been taken, so the card reflects its own history.
    """
    score = job["fit_score"]
    verdict = job["verdict"] or "no"
    emoji = VERDICT_EMOJI.get(verdict, "⚪")

    header = "*<{url}|{title}>*\n{company}".format(
        url=job["url"], title=_escape(job["title"]), company=_escape(job["company"])
    )
    facts = "\n".join([
        "{0} *{1}/100* · {2}".format(emoji, score, verdict),
        _location_line(job),
        _language_line(job),
        _salary_line(job),
        "🕒 posted {0} · via {1}".format(
            humanize_age(job["posted_at"] or job["first_seen_at"]),
            job["source_name"],
        ),
    ])

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": facts}},
    ]

    if job["language_quote"]:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "> _{0}_".format(
                _escape(job["language_quote"][:280]))}],
        })

    if job["rationale"]:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_{0}_".format(
                _escape(job["rationale"][:600]))},
        })

    gaps = job.get("gaps") if isinstance(job, dict) else None
    if gaps:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Gaps*\n" + "\n".join(
                "• {0}".format(_escape(g[:200])) for g in gaps[:3])},
        })

    value = str(job["id"])
    # Two rows: Slack allows at most five elements per actions block, and
    # splitting them keeps documents visually separate from triage.
    blocks.append({
        "type": "actions",
        "block_id": "job_docs_{0}".format(job["id"]),
        "elements": [
            _button("Generate CV", ACTION_CV, value, style="primary"),
            _button("Generate Cover Letter", ACTION_LETTER, value),
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open posting"},
                "url": job["url"],
                "action_id": "job_open",
            },
        ],
    })
    blocks.append({
        "type": "actions",
        "block_id": "job_actions_{0}".format(job["id"]),
        "elements": [
            _button("Shortlist", ACTION_SHORTLIST, value),
            _button("Mark Applied", ACTION_APPLIED, value),
            _button("Dismiss", ACTION_DISMISS, value, style="danger"),
        ],
    })

    if applied_state:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": applied_state}],
        })
    return blocks


def _button(label, action_id, value, style=None):
    button = {
        "type": "button",
        "text": {"type": "plain_text", "text": label},
        "action_id": action_id,
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def _escape(text):
    """Slack mrkdwn needs &, < and > escaped inside text."""
    if not text:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def card_fallback_text(job):
    """Notification text, shown where blocks cannot render."""
    return "{0}/100 — {1} at {2}".format(
        job["fit_score"], job["title"], job["company"]
    )


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------

CARD_SQL = (
    # matched_json must stay in this list: generation reads it to tell the
    # writer which strengths to lead with. It was missing once, and every CV
    # generated through Slack or the web silently lost that steering.
    "SELECT j.*, src.name AS source_name, sc.fit_score, sc.verdict, sc.rationale, "
    "sc.matched_json, sc.gaps_json, "
    "d.german_level_required, d.english_sufficient, d.language_quote, "
    "d.salary_stated, d.salary_quote, d.remote_policy "
    "FROM jobs j JOIN sources src ON src.id = j.source_id "
    "LEFT JOIN scores sc ON sc.job_id = j.id "
    "LEFT JOIN job_details d ON d.job_id = j.id "
)


def _load_card_job(conn, job_id):
    import json

    row = conn.execute(CARD_SQL + "WHERE j.id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    try:
        job["gaps"] = json.loads(row["gaps_json"] or "[]")
    except ValueError:
        job["gaps"] = []
    return job


#: Shared by both selectors: scored, in-region, still in triage, not yet
#: accounted for in any notification.
_UNNOTIFIED = (
    "j.notified_at IS NULL AND COALESCE(j.stage0_pass, 1) = 1 "
    "AND j.status = 'new' AND sc.fit_score IS NOT NULL "
)


def notifiable_jobs(conn, threshold=None, limit=None):
    """Postings that have earned their own card.

    `notified_at IS NULL` is the once-ever guard — a posting accounted for in
    any previous notification can never be selected again, however many times
    the fetch re-sees it.
    """
    threshold = (
        threshold if threshold is not None
        else config.get_setting(conn, "slack.score_threshold", 60)
    )
    limit = limit or config.get_setting(conn, "slack.max_cards_per_run", 10)
    return conn.execute(
        CARD_SQL + "WHERE " + _UNNOTIFIED + "AND sc.fit_score >= ? "
        "ORDER BY sc.fit_score DESC LIMIT ?",
        (threshold, limit),
    ).fetchall()


def below_threshold_ids(conn, threshold=None):
    """Postings to summarise in the rollup line rather than card individually."""
    threshold = (
        threshold if threshold is not None
        else config.get_setting(conn, "slack.score_threshold", 60)
    )
    rows = conn.execute(
        "SELECT j.id FROM jobs j JOIN scores sc ON sc.job_id = j.id "
        "WHERE " + _UNNOTIFIED + "AND sc.fit_score < ?",
        (threshold,),
    ).fetchall()
    return [row[0] for row in rows]


def post_new_cards(conn, client=None, threshold=None, limit=None):
    """Post a card per above-threshold posting, plus one rollup line.

    Returns (posted, rolled_up).
    """
    import json

    client = client or get_client(conn)
    _, channel = _require(conn)

    jobs = notifiable_jobs(conn, threshold, limit)
    posted = 0
    for row in jobs:
        job = dict(row)
        try:
            job["gaps"] = json.loads(row["gaps_json"] or "[]")
        except ValueError:
            job["gaps"] = []
        try:
            response = client.chat_postMessage(
                channel=channel,
                blocks=build_card(job),
                text=card_fallback_text(job),
                unfurl_links=False,
            )
        except Exception:
            log.exception("failed to post card for job %s", job["id"])
            continue
        # Recording this is what makes the post once-ever. Written immediately
        # so a crash mid-run cannot cause a repost of an already-sent card.
        conn.execute(
            "UPDATE jobs SET slack_channel = ?, slack_ts = ?, notified_at = ? "
            "WHERE id = ?",
            (response["channel"], response["ts"], now_iso(), job["id"]),
        )
        conn.commit()
        posted += 1

    rolled_ids = below_threshold_ids(conn, threshold)
    if rolled_ids:
        count = len(rolled_ids)
        try:
            client.chat_postMessage(
                channel=channel,
                text="+{0} lower-scoring match{1} — see the dashboard".format(
                    count, "" if count == 1 else "es"),
            )
        except Exception:
            log.exception("failed to post the rollup line")
        else:
            # Mark them notified only once the line actually went out, so a
            # failed rollup is retried rather than silently swallowing jobs.
            conn.executemany(
                "UPDATE jobs SET notified_at = ? WHERE id = ?",
                [(now_iso(), job_id) for job_id in rolled_ids],
            )
            conn.commit()
    return posted, len(rolled_ids)


# --------------------------------------------------------------------------
# Socket Mode app
# --------------------------------------------------------------------------

def _is_allowed(conn, user_id):
    allowed = config.get_setting(conn, "slack.allowed_user_ids", []) or []
    return user_id in allowed


def _refresh_card(client, conn, job_id, channel, ts, state_line):
    job = _load_card_job(conn, job_id)
    if job is None:
        return
    try:
        client.chat_update(
            channel=channel, ts=ts,
            blocks=build_card(job, applied_state=state_line),
            text=card_fallback_text(job),
        )
    except Exception:
        log.exception("failed to update card for job %s", job_id)


def _thread_reply(client, channel, thread_ts, text):
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception:
        log.exception("failed to post a thread reply")


def _generate_and_upload(client, conn, job_id, channel, thread_ts, kind, label):
    """Generate a document and deliver it into the card's thread."""
    from . import config as config_module
    from . import generation

    with generation.in_flight_guard(job_id, kind) as acquired:
        if not acquired:
            log.info("%s already generating for job %s; ignoring repeat", kind, job_id)
            return

        job = conn.execute(CARD_SQL + "WHERE j.id = ?", (job_id,)).fetchone()
        if job is None:
            return
        _thread_reply(client, channel, thread_ts,
                      ":hourglass_flowing_sand: Writing the {0} — about a minute."
                      .format(label.lower()))

        provider = config_module.build_writing_provider(conn)
        blocklist = config_module.get_setting(conn, "documents.blocklist", None)
        template = config_module.get_setting(conn, "documents.template", None)
        try:
            result = generation.generate_document(
                conn, job, provider, kind=kind, template_key=template,
                blocklist=blocklist)
        except generation.GenerationBlocked as exc:
            # A confidentiality hit is a hard stop, and worth saying loudly:
            # the whole point is that it never reaches an employer.
            _thread_reply(
                client, channel, thread_ts,
                ":no_entry: *{0} blocked.* The draft mentioned {1}. Nothing was "
                "saved — regenerate, or edit the blocklist in the config page."
                .format(label, ", ".join("`{0}`".format(t) for t in exc.terms)))
            return

        colour_note = (
            "brand colour {0}".format(result["brand_color"])
            if result["plan"].get("brand_color_recognised")
            else "default navy ({0})".format(result["brand_color_reason"][:80])
        )
        comment = "*{0}* — {1} template, {2}. Generated by {3}.".format(
            label, result["template"], colour_note, result["model"])

        for path in (result["docx"], result["pdf"]):
            if not path:
                continue
            try:
                client.files_upload_v2(
                    channel=channel, thread_ts=thread_ts, file=str(path),
                    filename=path.name, title=path.name,
                    initial_comment=comment if path == result["docx"] else None,
                )
            except Exception:
                log.exception("upload failed for %s", path)
                _thread_reply(client, channel, thread_ts,
                              ":warning: generated `{0}` but the upload failed."
                              .format(path.name))
        if not result["pdf"]:
            _thread_reply(client, channel, thread_ts,
                          "_PDF not attached — LibreOffice is not installed on "
                          "this host. The .docx is above._")


def build_app(conn_factory=connect):
    """Assemble the Bolt app. Returns (app, app_token)."""
    from slack_bolt import App

    conn = conn_factory()
    try:
        bot_token = config.get_secret(conn, "slack.bot_token")
        app_token = config.get_secret(conn, "slack.app_token")
    finally:
        conn.close()
    if not (bot_token and app_token):
        raise SlackNotConfigured("Both slack.bot_token and slack.app_token are required")

    app = App(token=bot_token, raise_error_for_unhandled_request=False)

    def _handle(action_id, worker, state_line):
        """Register one button: ack fast, then do the work off-thread."""

        @app.action(action_id)
        def _handler(ack, body, client, logger):
            ack()                     # must be within 3 seconds
            user_id = (body.get("user") or {}).get("id")
            job_id = int(body["actions"][0]["value"])
            channel = (body.get("channel") or {}).get("id")
            ts = (body.get("message") or {}).get("ts")

            def run():
                work_conn = conn_factory()
                try:
                    if not _is_allowed(work_conn, user_id):
                        log.warning("ignoring %s from unauthorised user %s",
                                    action_id, user_id)
                        client.chat_postEphemeral(
                            channel=channel, user=user_id,
                            text="You are not on this app's allowlist, so that "
                                 "action was ignored.",
                        )
                        return
                    worker(work_conn, job_id)
                    _refresh_card(client, work_conn, job_id, channel, ts, state_line)
                except Exception:
                    log.exception("action %s failed for job %s", action_id, job_id)
                finally:
                    work_conn.close()

            threading.Thread(target=run, daemon=True).start()

    def _register_document(action_id, kind, label):
        """Generation takes 30-60s, well past Slack's 3-second ack deadline.

        Ack, post a placeholder into the thread so the click visibly did
        something, then generate and replace it with the file.
        """

        @app.action(action_id)
        def _handler(ack, body, client, logger):
            ack()
            user_id = (body.get("user") or {}).get("id")
            job_id = int(body["actions"][0]["value"])
            channel = (body.get("channel") or {}).get("id")
            thread_ts = (body.get("message") or {}).get("ts")

            def run():
                work_conn = conn_factory()
                try:
                    if not _is_allowed(work_conn, user_id):
                        client.chat_postEphemeral(
                            channel=channel, user=user_id,
                            text="You are not on this app's allowlist.")
                        return
                    _generate_and_upload(client, work_conn, job_id, channel,
                                         thread_ts, kind, label)
                except Exception:
                    log.exception("%s failed for job %s", action_id, job_id)
                    _thread_reply(client, channel, thread_ts,
                                  ":warning: {0} failed — see the server log.".format(label))
                finally:
                    work_conn.close()

            threading.Thread(target=run, daemon=True).start()

    _register_document(ACTION_CV, "cv", "CV")
    _register_document(ACTION_LETTER, "cover_letter", "Cover letter")

    _handle(ACTION_SHORTLIST, lambda c, j: tracker.shortlist(c, j),
            "✅ Shortlisted")
    _handle(ACTION_APPLIED, lambda c, j: tracker.mark_applied(c, j),
            "📮 Marked applied")
    _handle(ACTION_DISMISS, lambda c, j: tracker.dismiss(c, j, "dismissed from Slack"),
            "🚫 Dismissed")

    @app.action("job_open")
    def _open(ack):
        ack()   # link buttons still deliver an interaction

    @app.command("/jobhunt")
    def _command(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip().lower()
        work_conn = conn_factory()
        try:
            respond(_command_response(work_conn, text,
                                      command.get("user_id")))
        finally:
            work_conn.close()

    return app, app_token


def _run_cycle_in_background():
    """Start a full cycle on the shared registry. Returns the reply text.

    Imported late: `scheduler` imports this module at load time, so taking it
    at the top would close the loop. The `cycle` key is the same one the web
    buttons use, which is what stops a phone-triggered run from overlapping a
    scheduled tick.
    """
    from . import tasks
    from .scheduler import run_cycle

    started = tasks.registry.start(
        "cycle", "Fetch, score and notify",
        lambda: run_cycle(trigger="slack", force_notify=True).summary())
    if not started:
        return "A cycle is already running — cards will appear when it finishes."
    return ("Started a full cycle: fetch, score, notify. Takes a few minutes; "
            "cards arrive here as they are found.")


def _command_response(conn, text, user_id=None):
    if text in ("", "help"):
        return ("`/jobhunt status` — pipeline summary\n"
                "`/jobhunt top` — best unactioned matches\n"
                "`/jobhunt titles` — the job titles being searched\n"
                "`/jobhunt run` — fetch, score and notify now")

    # The channel is public and shared, so the same allowlist that guards the
    # buttons guards these — `top` exposes the search just as much as a card.
    if not _is_allowed(conn, user_id):
        return "You are not on this app's allowlist."

    if text == "run":
        return _run_cycle_in_background()
    if text == "status":
        counts = tracker.counts(conn)
        if not counts:
            return "Nothing in the pipeline yet."
        return "*Pipeline*\n" + "\n".join(
            "• {0}: {1}".format(k, v) for k, v in sorted(counts.items()))
    if text == "top":
        rows = conn.execute(
            CARD_SQL + "WHERE j.status = 'new' AND COALESCE(j.stage0_pass,1) = 1 "
            "AND sc.fit_score IS NOT NULL ORDER BY sc.fit_score DESC LIMIT 5"
        ).fetchall()
        if not rows:
            return "No scored matches yet."
        return "*Top matches*\n" + "\n".join(
            "• *{0}/100* <{1}|{2}> — {3}".format(
                r["fit_score"], r["url"], _escape(r["title"]), _escape(r["company"]))
            for r in rows)
    if text == "titles":
        rows = conn.execute(
            "SELECT term FROM search_terms WHERE enabled = 1 ORDER BY term"
        ).fetchall()
        return "*Searching for*\n" + "\n".join("• {0}".format(r[0]) for r in rows)
    return "Unknown command. Try `/jobhunt help`."


def start_socket_mode(conn_factory=connect):
    """Connect in a background thread. Returns the handler, or None if unset."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    try:
        app, app_token = build_app(conn_factory)
    except SlackNotConfigured as exc:
        log.info("Slack not started: %s", exc)
        return None

    handler = SocketModeHandler(app, app_token)
    # connect() is non-blocking and reconnects on its own; start() would block.
    handler.connect()
    log.info("Slack Socket Mode connected")
    return handler
