"""The end-of-day Slack report.

Its job is to catch what the per-job cards cannot: applications going stale,
follow-ups falling due, and sources that quietly stopped working. A card tells
you about one posting; this tells you about the state of the search.
"""

import logging
from datetime import datetime

from . import config, slackbot, tracker
from .util import day_bounds_utc

log = logging.getLogger(__name__)


def day_start_utc(conn, on_date):
    """The UTC instant local midnight falls on, for `on_date`."""
    return day_bounds_utc(on_date, config.local_timezone(conn))[0]


def gather(conn, on_date=None):
    """Everything the report needs, as plain data so it can be tested."""
    # The configured timezone decides which day "today" is, not the host clock:
    # the Pi and the Mac should produce the same report for the same moment.
    today = on_date or datetime.now(config.local_timezone(conn)).date().isoformat()
    day_start = day_start_utc(conn, today)

    new_scored = conn.execute(
        "SELECT COUNT(*) FROM jobs j JOIN scores sc ON sc.job_id = j.id "
        "WHERE sc.scored_at >= ?", (day_start,)
    ).fetchone()[0]

    threshold = config.get_setting(conn, "slack.score_threshold", 60)
    cards_today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE notified_at >= ? AND slack_ts IS NOT NULL",
        (day_start,)
    ).fetchone()[0]

    top = conn.execute(
        "SELECT j.id, j.title, j.company, j.url, sc.fit_score "
        "FROM jobs j JOIN scores sc ON sc.job_id = j.id "
        "WHERE j.status = 'new' AND COALESCE(j.stage0_pass, 1) = 1 "
        "AND sc.fit_score >= ? ORDER BY sc.fit_score DESC LIMIT 5",
        (threshold,)
    ).fetchall()

    shortlisted_today = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE shortlisted_at >= ?", (day_start,)
    ).fetchone()[0]
    applied_today = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE applied_at = ?", (today,)
    ).fetchone()[0]

    failing_sources = conn.execute(
        "SELECT name, last_error FROM sources WHERE enabled = 1 AND last_status = 'error'"
    ).fetchall()

    return {
        "date": today,
        "new_scored": new_scored,
        "cards_today": cards_today,
        "threshold": threshold,
        "top": top,
        "shortlisted_today": shortlisted_today,
        "applied_today": applied_today,
        "pipeline": tracker.counts(conn),
        "due": tracker.due_actions(conn, today),
        "stale": tracker.stale_shortlists(conn, on_date=today),
        "failing_sources": failing_sources,
    }


def render(data):
    """Block Kit blocks for the report."""
    esc = slackbot._escape
    lines = [
        "*JobHunt — {0}*".format(data["date"]),
        "{0} postings scored today · {1} card{2} posted".format(
            data["new_scored"], data["cards_today"],
            "" if data["cards_today"] == 1 else "s"),
    ]
    if data["shortlisted_today"] or data["applied_today"]:
        lines.append("You shortlisted {0} and applied to {1} today.".format(
            data["shortlisted_today"], data["applied_today"]))

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]

    if data["top"]:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Best matches still untouched*\n" + "\n".join(
                "• *{0}* <{1}|{2}> — {3}".format(
                    row["fit_score"], row["url"], esc(row["title"]), esc(row["company"]))
                for row in data["top"])},
        })

    if data["due"]:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Follow-ups due*\n" + "\n".join(
                "• {0} — {1} _(due {2})_".format(
                    esc(row["company"]), esc(row["next_action"] or "follow up"),
                    row["next_action_date"])
                for row in data["due"][:8])},
        })

    if data["stale"]:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Shortlisted but not applied to*\n" + "\n".join(
                "• {0} — {1}".format(esc(row["company"]), esc(row["title"][:60]))
                for row in data["stale"][:8])},
        })

    pipeline = data["pipeline"]
    if pipeline:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Pipeline — " + " · ".join(
                "{0}: {1}".format(k, v) for k, v in sorted(pipeline.items()))}],
        })

    if data["failing_sources"]:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*⚠️ Sources failing*\n" + "\n".join(
                "• {0}: {1}".format(esc(row["name"]), esc((row["last_error"] or "")[:120]))
                for row in data["failing_sources"])},
        })

    if not (data["top"] or data["due"] or data["stale"] or data["new_scored"]):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_Nothing new today._"},
        })
    return blocks


def fallback_text(data):
    return "JobHunt {0}: {1} scored, {2} cards, {3} follow-up(s) due".format(
        data["date"], data["new_scored"], data["cards_today"], len(data["due"])
    )


def post_daily_report(conn, client=None, on_date=None):
    """Compose and post. Returns the gathered data, or None if Slack is unset."""
    try:
        client = client or slackbot.get_client(conn)
        _, channel = slackbot._require(conn)
    except slackbot.SlackNotConfigured as exc:
        log.info("daily report skipped: %s", exc)
        return None

    data = gather(conn, on_date)
    client.chat_postMessage(
        channel=channel, blocks=render(data), text=fallback_text(data),
        unfurl_links=False,
    )
    log.info("daily report posted for %s", data["date"])
    return data
