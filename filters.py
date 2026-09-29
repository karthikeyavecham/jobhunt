"""Stage-0 relevance: the free, deterministic filter that runs before any LLM call.

Arbeitnow has no server-side location filter and returns postings from all over
the world — measured on live data, 72 of 270 were UK roles and 112 were in
Berlin, Munich or Hamburg. Sending those to a model would waste most of the
scoring budget on jobs that were never viable.

Postings are **marked, not dropped**. A failing job is stored with a reason, so
the exclusion is auditable and reversible when the region config changes.
"""

import re

from . import dedup

# Country selector values.
COUNTRY_DE = "de"
COUNTRY_IN = "in"
DEFAULT_COUNTRY = COUNTRY_IN  # change this to switch the default region

# --- Germany defaults -------------------------------------------------------
DE_COMMUTABLE = [
    "berlin", "münchen", "munich", "hamburg", "koeln", "cologne",
    "frankfurt", "stuttgart", "düsseldorf", "dortmund", "essen",
    "leipzig", "bremen", "dresden", "hanover", "heinrichsdorf",
]

DE_FOREIGN_MARKERS = [
    # India / South Asia (foreign when searching Germany)
    "india", "bharat", "bangalore", "mumbai", "delhi", "hyderabad",
    "chennai", "pune", "kolkata", "ahmedabad", "kochi", "coimbatore",
    # Everywhere else
    "united states", "usa", "new york", "san francisco", "toronto",
    "vancouver", "canada", "seattle", "boston", "australia", "sydney",
    "singapore", "dubai", "uae", "israel", "tel aviv", "turkey",
    "istanbul", "mexico", "brazil", "argentina", "japan", "tokyo",
    "south korea", "seoul", "china", "shanghai", "beijing",
]

# --- India defaults ---------------------------------------------------------
IN_COMMUTABLE = [
    "bangalore", "bengaluru", "hyderabad", "chennai", "mumbai", "pune",
    "delhi", "new delhi", "noida", "gurgaon", "gurugram", "faridabad",
]

IN_ELSEWHERE = [
    "ahmedabad", "kochi", "kozhikode", "calicut", "thiruvananthapuram",
    "trivandrum", "coimbatore", "madurai", "visakhapatnam", "vijayawada",
    "chandigarh", "lucknow", "kanpur", "jaipur", "indore", "bhopal",
    "patna", "kolkata", "surat", "vadodara", "nagpur", "thane", "mysuru",
    "mangalore", "tiruchirappalli", "warangal", "pimpri-chinchwad",
]

IN_FOREIGN_MARKERS = [
    # Europe
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
    # North America
    "united states", "usa", "new york", "san francisco", "toronto", "vancouver",
    "canada", "seattle", "boston", "austin", "chicago", "miami",
    # Middle East / Asia-Pacific
    "singapore", "australia", "sydney", "dubai", "abu dhabi", "uae",
    "estonia", "tallinn", "latvia", "riga", "lithuania", "vilnius",
    "croatia", "zagreb", "slovakia", "bratislava", "slovenia", "ljubljana",
    "serbia", "belgrade", "ukraine", "turkey", "istanbul", "israel", "tel aviv",
]

INDIA_MARKERS = ["india", "bharat"]

# --- resolved defaults (set at import time) ---------------------------------
DEFAULT_COMMUTABLE = locals()[f"{DEFAULT_COUNTRY.upper()}_COMMUTABLE"]
FOREIGN_MARKERS = locals()[f"{DEFAULT_COUNTRY.upper()}_FOREIGN_MARKERS"]
if DEFAULT_COUNTRY == COUNTRY_IN:
    DEFAULT_INDIA_ELSEWHERE = IN_ELSEWHERE
else:
    DEFAULT_INDIA_ELSEWHERE = []  # not used for Germany

# Reasons, used both for storage and for the UI.
PASS_COMMUTABLE = "commutable"
PASS_REMOTE_IN = "remote (local)"
PASS_REMOTE_UNKNOWN = "remote (location unstated)"
PASS_RELOCATION = "elsewhere in country, relocation allowed"
FAIL_FOREIGN = "outside country"
FAIL_TOO_FAR = "elsewhere in country, not remote"
FAIL_NO_LOCATION = "no location and not remote"
FAIL_TOO_OLD = "posting too old"

_WS = re.compile(r"\s+")


def _haystack(location):
    """Lowercase, punctuation-stripped, alias-resolved location.

    Aliases are applied per token so "Madras" matches the commutable list entry
    "chennai". Without this, city lists would only match exact spellings.
    """
    if not location:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", location.lower()).strip()
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
    or remote and plausibly local. Foreign postings are rejected even when
    remote.

    With `allow_relocation`, on-site roles elsewhere in the country pass too.
    """
    config = config or {}
    country = config.get("country") or DEFAULT_COUNTRY
    commutable = config.get("commutable_cities") or DEFAULT_COMMUTABLE
    allow_remote = config.get("allow_remote", True)
    allow_relocation = config.get("allow_relocation", False)

    hay = _haystack(location)

    # Commutable wins outright.
    if hay and _contains(hay, commutable):
        return True, PASS_COMMUTABLE

    if hay and _contains(hay, FOREIGN_MARKERS):
        return False, FAIL_FOREIGN

    # Country-specific markers (e.g. "india" when searching India).
    country_markers = INDIA_MARKERS if country == COUNTRY_IN else []
    elsewhere = config.get("india_cities") or (
        IN_ELSEWHERE if country == COUNTRY_IN else []
    )

    if hay and (_contains(hay, elsewhere) or _contains(hay, country_markers)):
        if remote and allow_remote:
            return True, PASS_REMOTE_IN
        if allow_relocation:
            return True, PASS_RELOCATION
        return False, FAIL_TOO_FAR

    # Unrecognised or empty location: only worth keeping if it is remote.
    if remote and allow_remote:
        return True, PASS_REMOTE_UNKNOWN
    if not hay:
        return False, FAIL_NO_LOCATION
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
    "country": "filter.country",
    "commutable_cities": "filter.commutable_cities",
    "india_cities": "filter.india_cities",
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
