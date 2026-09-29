"""Arbeitnow — free public job board API, English-friendly roles in Germany.

https://www.arbeitnow.com/api/job-board-api — no key required. Their terms ask
callers not to abuse the endpoint and to link back to the site, so the default
page count is modest and each request is spaced out. The dashboard credits the
source on every job row.

Roughly 175 postings per page, ordered newest first, refreshed hourly.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from .base import DEFAULT_TIMEOUT, USER_AGENT, Adapter, RawJob, SourceError, html_to_text

API_URL = "https://www.arbeitnow.com/api/job-board-api"

# Their data updates hourly and is newest-first; two pages (~350 postings)
# comfortably covers a 3-hour window.
DEFAULT_PAGES = 2
POLITENESS_DELAY = 1.5


class ArbeitnowAdapter(Adapter):
    name = "arbeitnow"
    label = "Arbeitnow (public API, no key)"

    def fetch(self, config: Dict[str, Any]) -> List[RawJob]:
        pages = int(config.get("pages", DEFAULT_PAGES))
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

        jobs = []
        url = API_URL
        for page_no in range(pages):
            if page_no:
                time.sleep(POLITENESS_DELAY)
            payload = self._get(session, url)
            for item in payload.get("data", []):
                job = self._parse(item)
                if job is not None:
                    jobs.append(job)
            url = (payload.get("links") or {}).get("next")
            if not url:
                break
        return jobs

    @staticmethod
    def _get(session, url):
        try:
            resp = session.get(url, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError("Arbeitnow unreachable: {0}".format(exc))
        if resp.status_code != 200:
            raise SourceError("Arbeitnow returned HTTP {0}".format(resp.status_code))
        try:
            return resp.json()
        except ValueError:
            raise SourceError("Arbeitnow returned a non-JSON body")

    @staticmethod
    def _parse(item):
        slug = item.get("slug")
        url = item.get("url")
        title = (item.get("title") or "").strip()
        company = (item.get("company_name") or "").strip()
        # A posting missing any of these cannot be deduplicated or shown, so
        # skip it rather than storing a half-record.
        if not (slug and url and title and company):
            return None

        html = item.get("description") or ""
        return RawJob(
            external_id=str(slug),
            url=url,
            title=title,
            company=company,
            location=(item.get("location") or "").strip() or None,
            remote=bool(item.get("remote")),
            employment_type=", ".join(item.get("job_types") or []) or None,
            posted_at=_epoch_to_iso(item.get("created_at")),
            description_html=html,
            description_text=html_to_text(html),
            raw=item,
        )


def _epoch_to_iso(value):
    """created_at arrives as a Unix timestamp (int or numeric string)."""
    if value in (None, ""):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
