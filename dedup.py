"""Identity rules for job postings.

At a 3-hour fetch cycle a posting that stays live for two weeks is re-seen
around 70 times, and the same role legitimately appears on Arbeitnow, the
Arbeitsagentur and the company's own board. If these functions leak, Slack
fills with duplicates and the app becomes noise. Everything here is pure and
unit-tested.
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that identify a campaign, not a posting.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid_src", "source", "ref", "referrer", "trk", "trackingid",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "recruiter",
}

# (m/w/d), (w/m/d), (f/m/x), (gn), (all genders) and friends.
_GENDER_BRACKETED = re.compile(
    r"[\(\[]\s*(?:"
    r"[mwfdxgn](?:\s*[/|·,]\s*[mwfdxgn])+"
    r"|all\s+genders?"
    r"|gn"
    r"|divers"
    r"|[mwf]\s*[/|]\s*[mwf]\s*[/|]\s*divers"
    r")\s*[\)\]]",
    re.IGNORECASE,
)
# The same marker without brackets, usually trailing: "Support Manager m/w/d".
_GENDER_BARE = re.compile(r"\b[mwf]\s*/\s*[mwf]\s*/\s*[dx]\b", re.IGNORECASE)

# Legal forms only. Deliberately excludes "Group"/"Holding", which distinguish
# genuinely different entities rather than decorating one.
_LEGAL_FORM = re.compile(
    r"\b(?:gmbh\s*&\s*co\.?\s*kg|gmbh|mbh|ohg|kgaa|kg|ag|se|ug|e\.?\s*v\.?"
    r"|inc|ltd|llc|plc|b\.?\s*v\.?|n\.?\s*v\.?|s\.?\s*a\.?\s*r\.?\s*l\.?)\b\.?",
    re.IGNORECASE,
)

# Words that appear inside a location string without naming a place —
# "Remote job" must resolve to no city, not to the city "job".
NOISE_TOKENS = frozenset([
    "job", "jobs", "position", "role", "opportunity", "vacancy",
    "anywhere", "worldwide", "global", "office", "hq", "headquarters",
])

_UMLAUTS = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))

_POSTCODE = re.compile(r"\b\d{4,5}\b")
_PUNCT = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")

# Work-mode words that decorate a location rather than name one, so that
# "Remote / Düsseldorf" and "Düsseldorf" resolve to the same city.
_WORK_MODE = re.compile(
    r"\b(?:remote|hybrid|home\s*office|homeoffice|onsite|on\s*site|vor\s*ort|flexible)\b",
    re.IGNORECASE,
)

# Live Arbeitnow data carries both "Cologne" and "Köln", and both "Düsseldorf"
# and the umlaut-dropped "Dusseldorf". Without these the same posting on two
# boards would never merge — and Köln/Düsseldorf are the target region.
#
# Keys are already umlaut-folded. Deliberately conservative: only unambiguous
# pairs. "Frankfurt am Main" is left alone because collapsing it to "Frankfurt"
# would merge it with Frankfurt (Oder), a different city. A missed merge is a
# far safer failure than a wrong one.
_CITY_ALIASES = {
    # English exonyms
    "cologne": "koeln",
    "munich": "muenchen",
    "hanover": "hannover",
    "nuremberg": "nuernberg",
    "brunswick": "braunschweig",
    "vienna": "wien",
    "zurich": "zuerich",
    # ASCII spellings that drop the umlaut instead of expanding it
    "koln": "koeln",
    "dusseldorf": "duesseldorf",
    "munchen": "muenchen",
    "nurnberg": "nuernberg",
    "munster": "muenster",
    "osnabruck": "osnabrueck",
    "saarbrucken": "saarbruecken",
    "lubeck": "luebeck",
}


def _fold(value):
    value = value.lower()
    for src, dst in _UMLAUTS:
        value = value.replace(src, dst)
    return value


def normalize_company(company):
    """'Acme SE' and 'acme se.' collapse to 'acme'."""
    if not company:
        return ""
    text = _fold(company)
    text = _LEGAL_FORM.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_title(title):
    """'Support Manager (m/w/d)' and 'support manager' collapse together.

    Seniority words are deliberately preserved: 'Senior Support Manager' is a
    different job from 'Support Manager'.
    """
    if not title:
        return ""
    text = _fold(title)
    text = _GENDER_BRACKETED.sub(" ", text)
    text = _GENDER_BARE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_city(location):
    """'50667 Köln, Germany', 'Cologne' and 'Remote / Köln' all collapse to 'koeln'.

    Only the first meaningful segment is kept — sources disagree on how much
    region and country detail to append, and 'Köln, Nordrhein-Westfalen' is
    the same place as 'Köln'.
    """
    if not location:
        return ""
    text = _fold(location)
    text = _POSTCODE.sub(" ", text)
    text = _WORK_MODE.sub(" ", text)

    # Take the first segment that still has a city name in it; stripping the
    # work-mode word can leave an empty leading segment ("Remote / Köln").
    segments = [s.strip() for s in re.split(r"[,/|]| - ", text)]
    text = next((s for s in segments if s.strip(" -")), "")

    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    text = " ".join(t for t in text.split() if t not in NOISE_TOKENS)
    return _CITY_ALIASES.get(text, text)


def dedup_key(company, title, location):
    """Stable identity for a posting across sources."""
    basis = "{0}|{1}|{2}".format(
        normalize_company(company), normalize_title(title), normalize_city(location)
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def canonical_url(url):
    """Strip tracking params and normalise host casing.

    Ordering of the surviving params is preserved rather than sorted, because
    some ATS URLs are order-sensitive when replayed.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        urlencode(kept),
        "",  # fragments never identify a distinct posting
    ))


def content_hash(text):
    """Hash of the description, used to skip re-scoring unchanged re-listings."""
    if not text:
        return ""
    collapsed = _WS.sub(" ", text).strip().lower()
    return hashlib.sha1(collapsed.encode("utf-8")).hexdigest()
