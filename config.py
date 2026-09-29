"""Settings and secrets.

Two stores, deliberately separate. `settings` holds ordinary configuration and
is safe to export. `secrets` holds API keys and tokens: it is never exported,
never rendered back into a form, and an environment variable always wins over
the stored value — so the Pi's systemd unit can supply the key without it
entering the database at all.
"""

import json

from . import llm

#: Environment variable that overrides each secret, checked before the DB.
SECRET_ENV = {
    "anthropic.api_key": "ANTHROPIC_API_KEY",
    "slack.bot_token": "SLACK_BOT_TOKEN",
    "slack.app_token": "SLACK_APP_TOKEN",
}

DEFAULTS = {
    # --- LLM: local primary -------------------------------------------------
    "llm.local.base_url": llm.DEFAULT_LOCAL_BASE_URL,
    "llm.local.model": llm.DEFAULT_LOCAL_MODEL,
    "llm.local.attempts": 5,
    "llm.local.backoff": 2.0,
    "llm.timeout": 180,
    # --- LLM: Claude fallback ----------------------------------------------
    "llm.fallback.enabled": True,
    "llm.anthropic.model": llm.DEFAULT_ANTHROPIC_MODEL,
    # --- Scoring ------------------------------------------------------------
    "scoring.enabled": True,
    "scoring.max_description_chars": 3500,
    "scoring.batch_limit": 40,
    # Scoring requests run in flight together. LM Studio batches them, so
    # this is a real throughput gain up to about four; past that the gain
    # flattens while each request gets slower. 1 keeps the Mac responsive.
    "scoring.concurrency": 1,
    # Score at or above this gets its own Slack card; the rest are rolled up
    # into a single line. Calibrated against the real backlog: qwen produces
    # discrete scores (…45, 55, 65, 72, 75) with an empty band between 56 and
    # 64, so 60 sits in a gap and is robust to run-to-run wobble. On the
    # 154-posting backlog it yields 11 cards.
    "slack.score_threshold": 60,
    "slack.max_cards_per_run": 10,
    "slack.enabled": True,
    # Channel is stored by ID, not name: a renamed channel keeps its ID, so
    # posting never silently breaks.
    "slack.channel_id": "",
    # Only these users may trigger actions. Without it, any workspace member
    # could generate a CV or alter the tracker.
    "slack.allowed_user_ids": [],
    # --- Documents ----------------------------------------------------------
    "documents.template": "classic",
    # Terms that must never reach an application document. A hit blocks
    # generation outright rather than warning after the file is written.
    "documents.blocklist": ["BotConnect"],
    # --- Schedule -----------------------------------------------------------
    "schedule.enabled": True,
    "schedule.interval_hours": 3,
    "schedule.timezone": "Europe/Berlin",
    "schedule.report_at": "18:00",
    # Cards are only posted inside this window. Fetching and scoring continue
    # around the clock, so nothing is missed overnight — the cards simply wait.
    "slack.notify_from": "08:00",
    "slack.notify_until": "22:00",
    # --- Region filter ------------------------------------------------------
    "filter.allow_remote": True,
    # On-site roles elsewhere in Germany. Off by default: relocating is a
    # bigger decision than commuting, and leaving it on would bury the
    # commutable roles under everything from Munich to Rostock.
    "filter.allow_relocation": False,
    "filter.max_age_days": 0,          # 0 = no age cutoff
    "filter.company_blocklist": [],
}

#: Values stored as JSON rather than bare strings.
_JSON_KEYS = {"filter.company_blocklist", "filter.commutable_cities",
              "filter.germany_cities", "slack.allowed_user_ids",
              "documents.blocklist", "sources.removed"}
_BOOL_KEYS = {"llm.fallback.enabled", "scoring.enabled", "filter.allow_remote",
              "filter.allow_relocation", "slack.enabled", "schedule.enabled"}
_INT_KEYS = {"llm.local.attempts", "llm.timeout", "scoring.max_description_chars",
             "scoring.batch_limit", "scoring.concurrency", "filter.max_age_days",
             "slack.score_threshold", "slack.max_cards_per_run",
             "schedule.interval_hours"}
_FLOAT_KEYS = {"llm.local.backoff"}


def _decode(key, raw):
    if raw is None:
        return DEFAULTS.get(key)
    if key in _BOOL_KEYS:
        return raw not in ("0", "false", "False", "")
    if key in _INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULTS.get(key)
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return DEFAULTS.get(key)
    if key in _JSON_KEYS:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return [v.strip() for v in str(raw).split(",") if v.strip()]
    return raw


def _encode(key, value):
    if key in _BOOL_KEYS:
        return "1" if value else "0"
    if key in _JSON_KEYS:
        return json.dumps(value)
    return str(value)


def load_settings(conn):
    """Every setting, defaults filled in for anything unset."""
    stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    config = {}
    for key in set(list(DEFAULTS.keys()) + list(stored.keys())):
        config[key] = _decode(key, stored.get(key))
    return config


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key, default)
    return _decode(key, row[0])


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, _encode(key, value)),
    )
    conn.commit()


def local_timezone(conn):
    """The configured timezone — what "today" means in reports and filters."""
    from .util import DEFAULT_TIMEZONE, get_timezone

    return get_timezone(get_setting(conn, "schedule.timezone", DEFAULT_TIMEZONE))


def get_secret(conn, key):
    """Environment first, then the database. Returns None when unset."""
    import os

    env_name = SECRET_ENV.get(key)
    if env_name:
        from_env = os.environ.get(env_name)
        if from_env:
            return from_env
    row = conn.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] else None


def set_secret(conn, key, value):
    """Store or clear a secret. An empty value deletes the row."""
    if value:
        conn.execute(
            "INSERT INTO secrets (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    else:
        conn.execute("DELETE FROM secrets WHERE key = ?", (key,))
    conn.commit()


def secret_status(conn, key):
    """(is_set, source, masked) for display. The real value never leaves here."""
    import os

    env_name = SECRET_ENV.get(key)
    if env_name and os.environ.get(env_name):
        return True, "environment ({0})".format(env_name), mask(os.environ[env_name])
    row = conn.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
    if row and row[0]:
        return True, "stored in database", mask(row[0])
    return False, "not set", ""


def mask(value):
    """'sk-ant-api03-abcd…wxyz' — enough to recognise, not enough to use."""
    if not value:
        return ""
    if len(value) <= 12:
        return "…" * 3
    return "{0}…{1}".format(value[:8], value[-4:])


def load_secrets(conn):
    """All secrets, resolved env-first. For building providers only."""
    return {key: get_secret(conn, key) for key in SECRET_ENV}


def build_provider(conn):
    """The scoring chain: local first, Claude as fallback."""
    return llm.build_provider(load_settings(conn), load_secrets(conn))


def build_writing_provider(conn):
    """The chain for CVs and cover letters — the reverse preference.

    Scoring is high-volume and mechanical, so it runs locally. Writing is
    low-volume and judged on quality: a cover letter that reads as
    machine-written is worse than useless, and that is exactly where the local
    model is weakest. So Claude leads here when a key exists, with the local
    model as the fallback rather than the default.
    """
    settings = load_settings(conn)
    secrets = load_secrets(conn)
    local = llm.build_provider(settings, {})     # local only, no fallback
    api_key = secrets.get("anthropic.api_key")
    if not api_key:
        return local
    claude = llm.AnthropicProvider(
        api_key=api_key,
        model=settings.get("llm.anthropic.model", llm.DEFAULT_ANTHROPIC_MODEL),
        timeout=settings.get("llm.timeout", llm.DEFAULT_TIMEOUT),
    )
    return llm.FallbackProvider(claude, local)
