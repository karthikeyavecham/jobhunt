"""Stage-0 relevance: the free, deterministic filter that runs before any LLM call.

Arbeitnow has no server-side location filter and returns postings from all over
Europe — measured on live data, 72 of 270 were UK roles and 112 were in Berlin,
Munich or Hamburg. Sending those to a model would waste most of the scoring
budget on jobs that were never viable.

Postings are **marked, not dropped**. A failing job is stored with a reason, so
the exclusion is auditable, reversible when the region config changes, and
available for backfill when a new job title is added later.
"""

import re

from . import dedup

# Shipped default: the Rhineland commuter belt. This is a starting point,
# not a fact about the user — `filter.commutable_cities` overrides the whole
# list from the config page, and should be set to wherever you actually are.
DEFAULT_COMMUTABLE = [
    "koeln", "leverkusen", "duesseldorf", "bonn", "bergisch gladbach",
    "monchengladbach", "moenchengladbach", "neuss", "hilden", "langenfeld",
    "solingen", "wuppertal", "remscheid", "dormagen", "troisdorf",
    "siegburg", "bruehl", "huerth", "frechen", "pulheim", "erkrath",
    "ratingen", "mettmann", "opladen", "wesseling", "kerpen", "bornheim",
    "sankt augustin", "lohmar", "rosrath", "roesrath", "overath", "burscheid",
    "monheim", "haan", "velbert", "duisburg", "essen", "mulheim", "muelheim",
    "oberhausen", "krefeld", "aachen", "dortmund", "bochum", "gelsenkirchen",
    "hagen", "lindlar", "juelich", "julich", "gummersbach", "euskirchen",
]

# Elsewhere in Germany: reachable only if the role is remote.
DEFAULT_GERMANY_ELSEWHERE = [
    "berlin", "muenchen", "hamburg", "frankfurt", "stuttgart", "nuernberg",
    "hannover", "bremen", "leipzig", "dresden", "mannheim", "karlsruhe",
    "augsburg", "wiesbaden", "mainz", "muenster", "bielefeld", "kiel",
    "rostock", "erfurt", "freiburg", "heidelberg", "regensburg", "ulm",
    "wolfsburg", "ingolstadt", "darmstadt", "kassel", "saarbruecken",
    "potsdam", "jena", "magdeburg", "luebeck", "osnabrueck", "paderborn",
    "wuerzburg", "heilbronn", "gilching", "herzogenaurach", "walldorf",
]

# Markers that place a posting outside Germany. Checked against the raw
# location string, so multi-word country names are matched too.
FOREIGN_MARKERS = [
    "united kingdom", "london", "manchester", "birmingham", "cheltenham",
    "chatham", "edinburgh", "glasgow", "bristol", "leeds", "cambridge",
    "oxford", "england", "scotland", "wales",
    "ireland", "dublin", "cork",
    "netherlands", "amsterdam", "rotterdam", "utrecht", "eindhoven",
    "belgium", "brussels", "antwerp", "france", "paris", "lyon",
    "spain", "madrid", "barcelona", "valencia", "portugal", "lisbon", "porto",
    "italy", "milan", "rome", "poland", "warsaw", "krakow", "wroclaw",
    "czech", "prague", "brno", "romania", "bucharest", "bulgaria", "sofia",
    "hungary", "budapest", "greece", "athens", "sweden", "stockholm",
    "denmark", "copenhagen", "norway", "oslo", "finland", "helsinki",
    "switzerland", "zurich", "zuerich", "geneva", "basel", "bern",
    "austria", "vienna", "wien", "graz", "salzburg",
    "united states", "usa", "new york", "san francisco", "canada", "toronto",
    "india", "bangalore", "singapore", "australia", "sydney", "dubai",
    "estonia", "tallinn", "latvia", "riga", "lithuania", "vilnius",
    "croatia", "zagreb", "slovakia", "bratislava", "slovenia", "ljubljana",
    "serbia", "belgrade", "ukraine", "turkey", "istanbul", "israel", "tel aviv",
]

GERMANY_MARKERS = ["deutschland", "germany"]

# Reasons, used both for storage and for the UI.
PASS_COMMUTABLE = "commutable"
PASS_REMOTE_DE = "remote (Germany)"
PASS_REMOTE_UNKNOWN = "remote (location unstated)"
PASS_RELOCATION = "elsewhere in Germany, relocation allowed"
FAIL_FOREIGN = "outside Germany"
FAIL_TOO_FAR = "elsewhere in Germany, not remote"
FAIL_NO_LOCATION = "no location and not remote"
FAIL_TOO_OLD = "posting too old"

_WS = re.compile(r"\s+")


def _haystack(location):
    """Umlaut-folded, alias-resolved, punctuation-stripped location.

    Aliases are applied **per token**, so "Cologne" matches the commutable list
    entry "koeln" and "Mindfuel Munich" resolves its city half to "muenchen".
    Without this the city lists would only match German spellings, and a
    Cologne posting from an English-language board would be filtered out —
    the exact opposite of what the region config says.
    """
    if not location:
        return ""
    folded = dedup._fold(location)
    cleaned = _WS.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", folded)).strip()
    tokens = [
        dedup._CITY_ALIASES.get(token, token)
        for token in cleaned.split()
        if token not in dedup.NOISE_TOKENS
    ]
    return " ".join(tokens)


def _contains(haystack, needles):
    return any(
        re.search(r"\b" + re.escape(n) + r"\b", haystack) for n in needles
    )


def check_location(location, remote, config=None):
    """Return (passes, reason).

    A posting is relevant when it is commutable from the configured home region,
    or remote and
    plausibly German. Foreign postings are rejected even when remote, because a
    UK-based remote role generally cannot be filled from Germany.

    With `allow_relocation`, on-site roles elsewhere in Germany pass too. That
    only applies to locations *recognised* as German: an unrecognised city
    stays out, because the foreign check is a denylist and "not on it" is not
    evidence of being in Germany — Arbeitnow returns Europe-wide results, and
    relocating to Köln is the point, not relocating to Kraków.
    """
    config = config or {}
    commutable = config.get("commutable_cities") or DEFAULT_COMMUTABLE
    elsewhere = config.get("germany_cities") or DEFAULT_GERMANY_ELSEWHERE
    allow_remote = config.get("allow_remote", True)
    allow_relocation = config.get("allow_relocation", False)

    hay = _haystack(location)

    # Commutable wins outright, even if the string also names a country.
    if hay and _contains(hay, commutable):
        return True, PASS_COMMUTABLE

    if hay and _contains(hay, FOREIGN_MARKERS):
        return False, FAIL_FOREIGN

    if hay and (_contains(hay, elsewhere) or _contains(hay, GERMANY_MARKERS)):
        if remote and allow_remote:
            return True, PASS_REMOTE_DE
        if allow_relocation:
            return True, PASS_RELOCATION
        return False, FAIL_TOO_FAR

    # Unrecognised or empty location: only worth keeping if it is remote.
    if remote and allow_remote:
        return True, PASS_REMOTE_UNKNOWN
    if not hay:
        return False, FAIL_NO_LOCATION
    # An unrecognised city that is not remote — treat as too far, but the
    # reason makes it findable in the excluded view if the list needs widening.
    return False, FAIL_TOO_FAR


def check_age(posted_at, max_age_days=None):
    from .util import age_days

    if not max_age_days:
        return True, None
    days = age_days(posted_at)
    if days is not None and days > max_age_days:
        return False, FAIL_TOO_OLD
    return True, None


def _field(job, name):
    """Read a field from either a RawJob or a sqlite3.Row / dict."""
    if isinstance(job, dict):
        return job.get(name)
    try:
        return job[name]          # sqlite3.Row
    except (TypeError, IndexError, KeyError):
        return getattr(job, name, None)


def evaluate(job, config=None):
    """Full stage-0 verdict for one posting. Returns (passes, reason)."""
    config = config or {}

    ok, reason = check_age(_field(job, "posted_at"), config.get("max_age_days"))
    if not ok:
        return False, reason

    company = _field(job, "company") or ""
    blocked = [c.lower() for c in (config.get("company_blocklist") or [])]
    if blocked and company.lower() in blocked:
        return False, "company blocklisted"

    return check_location(_field(job, "location"), _field(job, "remote"), config)


# Settings keys, so the config page and the filter agree on names.
SETTING_KEYS = {
    "commutable_cities": "filter.commutable_cities",
    "germany_cities": "filter.germany_cities",
    "company_blocklist": "filter.company_blocklist",
    "allow_remote": "filter.allow_remote",
    "allow_relocation": "filter.allow_relocation",
    "max_age_days": "filter.max_age_days",
}


def load_config(conn):
    """Read filter settings, falling back to the defaults in this module."""
    import json

    rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    config = {}
    for name, key in SETTING_KEYS.items():
        raw = rows.get(key)
        if raw in (None, ""):
            continue
        if name in ("allow_remote", "allow_relocation"):
            config[name] = raw not in ("0", "false", "False")
        elif name == "max_age_days":
            try:
                config[name] = int(raw)
            except ValueError:
                pass
        else:
            try:
                config[name] = json.loads(raw)
            except ValueError:
                config[name] = [v.strip() for v in raw.split(",") if v.strip()]
    return config


def reevaluate_all(conn, config=None):
    """Re-run stage-0 over every stored posting.

    Used after the region config changes, and to backfill rows that were stored
    before the filter existed. Returns (passed, failed).
    """
    config = config if config is not None else load_config(conn)
    passed = failed = 0
    for job in conn.execute("SELECT * FROM jobs").fetchall():
        ok, reason = evaluate(job, config)
        conn.execute(
            "UPDATE jobs SET stage0_pass = ?, stage0_reason = ? WHERE id = ?",
            (1 if ok else 0, reason, job["id"]),
        )
        if ok:
            passed += 1
        else:
            failed += 1
    conn.commit()
    return passed, failed
