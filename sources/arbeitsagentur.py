"""Bundesagentur für Arbeit Jobsuche — the largest German job dataset.

Authentication is the well-known static client id `jobboerse-jobsuche` sent as
`X-API-Key`. This endpoint is community-documented (github.com/bundesAPI/jobsuche-api)
rather than officially supported, so it can change without notice — every
failure is recorded on the source health row instead of being swallowed.

Two-step protocol:
  1. /pc/v6/jobs                          search, summaries only
  2. /pc/v4/jobdetails/{base64(refnr)}    the description

Search results carry no description, so step 2 runs only for postings that are
genuinely new (ingest.py decides). After the initial backlog that is close to
zero requests per cycle.

The public web page for a posting is
https://www.arbeitsagentur.de/jobsuche/jobdetail/{referenznummer}
"""

import base64
import logging
import time
from typing import Any, Dict, List

import requests

from .base import (DEFAULT_TIMEOUT, USER_AGENT, Adapter, RawJob, SourceError,
                   markdown_to_text)

log = logging.getLogger(__name__)

API_KEY = "jobboerse-jobsuche"
SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAIL_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{0}"
PUBLIC_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{0}"

DEFAULT_LOCATION = "Köln"
DEFAULT_RADIUS_KM = 50
DEFAULT_MAX_AGE_DAYS = 30
PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 2
POLITENESS_DELAY = 0.6


class ArbeitsagenturAdapter(Adapter):
    name = "arbeitsagentur"
    label = "Bundesagentur für Arbeit (Jobsuche)"
    needs_detail_fetch = True

    def fetch(self, config: Dict[str, Any]) -> List[RawJob]:
        terms = config.get("terms") or config.get("_search_terms") or []
        terms = [t for t in terms if t and t.strip()]
        if not terms:
            raise SourceError(
                "No search terms. Add job titles on the config page, or set "
                "\"terms\" in this source's config."
            )

        # A blank `wo` means nationwide: the API drops the geographic filter
        # entirely when neither `wo` nor `umkreis` is sent. That is how the
        # second source finds remote and relocation roles advertised in Berlin
        # or Munich, which a Köln-centred radius can never return.
        where = config.get("wo", DEFAULT_LOCATION)
        where = where.strip() if isinstance(where, str) else where
        radius = int(config.get("umkreis", DEFAULT_RADIUS_KM))
        max_age = int(config.get("veroeffentlichtseit", DEFAULT_MAX_AGE_DAYS))
        max_pages = int(config.get("max_pages", DEFAULT_MAX_PAGES))
        # 1 = Arbeitsplatz (salaried employment). Left unset by default so the
        # original source keeps returning what it always has; the nationwide
        # source sets it to keep apprenticeships out.
        offer_type = config.get("angebotsart")

        session = self._shared_session()

        # The same posting matches several of his search terms, so collect by
        # reference number and let the last write win.
        by_ref = {}
        for term in terms:
            for item in self._search(session, term, where, radius, max_age,
                                     max_pages, offer_type):
                job = self._parse(item)
                if job is not None:
                    by_ref[job.external_id] = job
        return list(by_ref.values())

    def fetch_detail(self, job: RawJob, config: Dict[str, Any]) -> RawJob:
        """Add the description. Failure here downgrades, it does not abort.

        Called once per genuinely new posting, so on the first run this fires a
        few hundred times. The connection is reused and each call is spaced out
        — this endpoint is unofficial and worth treating gently.
        """
        session = self._shared_session()
        self._throttle()
        encoded = base64.b64encode(job.external_id.encode("utf-8")).decode("ascii")
        try:
            resp = session.get(DETAIL_URL.format(encoded), timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("detail fetch failed for %s: %s", job.external_id, exc)
            return job
        if resp.status_code != 200:
            log.warning("detail fetch for %s returned HTTP %s", job.external_id, resp.status_code)
            return job
        try:
            detail = resp.json()
        except ValueError:
            log.warning("detail fetch for %s returned non-JSON", job.external_id)
            return job

        description = detail.get("stellenangebotsBeschreibung")
        if description:
            job.description_text = markdown_to_text(description)
        job.raw = dict(job.raw or {})
        job.raw["_detail"] = detail
        return job

    def __init__(self):
        self._session_cache = None
        self._last_request_at = 0.0

    def _shared_session(self):
        if self._session_cache is None:
            self._session_cache = self._session()
        return self._session_cache

    def _throttle(self):
        """Keep at least POLITENESS_DELAY between consecutive requests."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < POLITENESS_DELAY:
            time.sleep(POLITENESS_DELAY - elapsed)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _session():
        session = requests.Session()
        session.headers.update({
            "X-API-Key": API_KEY,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        return session

    def _search(self, session, term, where, radius, max_age, max_pages,
                offer_type=None):
        results = []
        for page in range(1, max_pages + 1):
            self._throttle()
            params = {
                "was": term,
                "veroeffentlichtseit": max_age,
                "size": PAGE_SIZE,
                "page": page,
            }
            if where:
                params["wo"] = where
                params["umkreis"] = radius
            if offer_type:
                params["angebotsart"] = offer_type
            try:
                resp = session.get(SEARCH_URL, params=params, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise SourceError("Arbeitsagentur unreachable: {0}".format(exc))
            if resp.status_code != 200:
                raise SourceError(
                    "Arbeitsagentur search returned HTTP {0} for '{1}'".format(
                        resp.status_code, term
                    )
                )
            try:
                payload = resp.json()
            except ValueError:
                raise SourceError("Arbeitsagentur returned a non-JSON body")

            batch = payload.get("ergebnisliste") or []
            results.extend(batch)
            # Stop as soon as the result set is exhausted.
            if len(batch) < PAGE_SIZE or len(results) >= (payload.get("maxErgebnisse") or 0):
                break
        return results

    @staticmethod
    def _parse(item):
        ref = item.get("referenznummer")
        title = (item.get("stellenangebotsTitel") or "").strip()
        if not (ref and title):
            return None

        return RawJob(
            external_id=str(ref),
            url=PUBLIC_URL.format(ref),
            title=title,
            company=_company_of(item),
            location=_location_of(item),
            remote=bool(item.get("homeofficemoeglich")),
            employment_type=_employment_of(item),
            posted_at=_date_of(item),
            description_html=None,   # the API returns Markdown, filled in by fetch_detail
            description_text=None,
            raw=item,
        )


def _company_of(item):
    """Anonymous 'Chiffre' listings have no employer name.

    The chiffre number is folded in so two different anonymous postings do not
    collapse into one another during deduplication.
    """
    name = (item.get("firma") or "").strip()
    if name:
        return name
    partner = (item.get("allianzpartnerName") or "").strip()
    if partner:
        return partner
    chiffre = item.get("chiffrenummer")
    if chiffre:
        return "Arbeitgeber nicht genannt (Chiffre {0})".format(chiffre)
    return "Arbeitgeber nicht genannt"


def _location_of(item):
    places = item.get("stellenlokationen") or []
    if not places:
        return None
    address = (places[0] or {}).get("adresse") or {}
    city = (address.get("ort") or "").strip()
    if not city:
        return None
    postcode = (address.get("plz") or "").strip()
    return "{0} {1}".format(postcode, city).strip()


def _employment_of(item):
    parts = []
    if item.get("arbeitszeitVollzeit"):
        parts.append("Vollzeit")
    if any(item.get(k) for k in (
        "arbeitszeitTeilzeitFlexibel", "arbeitszeitTeilzeitVormittag",
        "arbeitszeitTeilzeitNachmittag", "arbeitszeitTeilzeitAbend",
    )):
        parts.append("Teilzeit")
    duration = item.get("vertragsdauer")
    if duration == "UNBEFRISTET":
        parts.append("unbefristet")
    elif duration == "BEFRISTET":
        parts.append("befristet")
    return ", ".join(parts) or None


def _date_of(item):
    """First publication date, as a plain ISO date."""
    date = item.get("datumErsteVeroeffentlichung")
    if not date:
        window = item.get("veroeffentlichungszeitraum") or {}
        date = window.get("von")
    if not date:
        return None
    return "{0}T00:00:00+00:00".format(str(date)[:10])
