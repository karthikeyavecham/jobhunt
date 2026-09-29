"""Company career boards — one adapter family over five ATS platforms.

These are the long tail that job aggregators miss: a company posts to its own
board and the role never reaches Arbeitnow or the Arbeitsagentur. Each platform
exposes a public JSON or XML feed keyed by a company slug, so a source here is
just "platform + slug".

All five endpoints were probed live before being written down:

    greenhouse       boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
    lever            api.lever.co/v0/postings/{slug}?mode=json
    smartrecruiters  api.smartrecruiters.com/v1/companies/{slug}/postings
    personio         {slug}.jobs.personio.de/xml
    recruitee        {slug}.recruitee.com/api/offers/

**Ashby is deliberately absent.** Its public posting API returns titles and
locations but no description — the per-job endpoint returns 401 — and a posting
with no description cannot be scored meaningfully. Adding it would generate
exactly the noise the region filter and threshold exist to remove.

Slugs cannot be guessed. Each source is configured with one and validated by the
Test button before it is ever scheduled.
"""

import html
import logging
import re
import time
from datetime import datetime, timezone

import requests

from .base import (DEFAULT_TIMEOUT, USER_AGENT, Adapter, RawJob, SourceError,
                   html_to_text)

log = logging.getLogger(__name__)

POLITENESS_DELAY = 0.5


def _session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _get(session, url, expect_json=True, timeout=DEFAULT_TIMEOUT,
         follow_redirects=True):
    """Fetch, treating an unknown slug as an error rather than an empty board.

    `follow_redirects=False` matters for Personio: an unknown tenant answers
    307 to personio.com rather than 404, so following it yields a marketing
    page that parses to zero postings and looks like a working but empty board.
    """
    try:
        response = session.get(url, timeout=timeout, allow_redirects=follow_redirects)
    except requests.RequestException as exc:
        raise SourceError("Cannot reach {0}: {1}".format(url, exc))
    if response.status_code in (301, 302, 303, 307, 308):
        raise SourceError(
            "{0} redirected away — that company slug does not exist on this "
            "platform".format(url))
    if response.status_code == 404:
        raise SourceError(
            "404 from {0} — the company slug is probably wrong".format(url))
    if response.status_code == 429:
        raise SourceError("Rate limited by {0} — try again shortly".format(url))
    if response.status_code != 200:
        raise SourceError("HTTP {0} from {1}".format(response.status_code, url))
    if not expect_json:
        return response.content
    try:
        return response.json()
    except ValueError:
        raise SourceError("{0} did not return JSON".format(url))


def _iso(value):
    """Normalise the several date shapes these platforms use."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(
            timespec="seconds")
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


class _AtsAdapter(Adapter):
    """Shared plumbing. Subclasses supply the URL and the field mapping."""

    #: Set when the list endpoint omits descriptions and a second call is needed.
    needs_detail_fetch = False

    def _slug(self, config):
        slug = (config.get("slug") or "").strip().strip("/")
        if not slug:
            raise SourceError(
                "This source needs a company slug — the identifier in the board's "
                "URL, e.g. 'gitlab' in boards.greenhouse.io/gitlab")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", slug):
            raise SourceError("'{0}' is not a valid company slug".format(slug))
        return slug

    def _company(self, config, fallback=None):
        return (config.get("company") or fallback or self._slug(config)).strip()

    def test(self, config):
        try:
            jobs = self.fetch(config)
        except SourceError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, "{0}: {1}".format(type(exc).__name__, exc)
        if not jobs:
            return True, "Reachable, but the board is currently empty"

        # Description coverage decides whether these postings can be scored at
        # all, so it is reported here rather than discovered weeks later as a
        # run of oddly low scores.
        if self.needs_detail_fetch:
            note = "descriptions fetched per posting"
        else:
            with_body = sum(1 for j in jobs if (j.description_text or "").strip())
            if with_body == 0:
                note = ("WARNING: none of these postings include a description, "
                        "so they cannot be scored meaningfully")
            elif with_body < len(jobs):
                note = "{0}/{1} include a description".format(with_body, len(jobs))
            else:
                note = "all include descriptions"
        return "WARNING" not in note, "{0} posting(s) — {1}. e.g. '{2}'".format(
            len(jobs), note, jobs[0].title[:52])


class GreenhouseAdapter(_AtsAdapter):
    """`content=true` returns full descriptions inline, so no detail fetch."""

    name = "greenhouse"
    label = "Greenhouse board (company slug)"
    URL = "https://boards-api.greenhouse.io/v1/boards/{0}/jobs?content=true"

    def fetch(self, config):
        slug = self._slug(config)
        payload = _get(_session(), self.URL.format(slug))
        jobs = []
        for item in payload.get("jobs", []):
            job = self._parse(item, config)
            if job:
                jobs.append(job)
        return jobs

    def _parse(self, item, config):
        job_id, url, title = item.get("id"), item.get("absolute_url"), item.get("title")
        if not (job_id and url and title):
            return None
        # Greenhouse double-escapes the HTML body.
        body = html.unescape(item.get("content") or "")
        return RawJob(
            external_id=str(job_id), url=url, title=title.strip(),
            company=self._company(config, item.get("company_name")),
            location=(item.get("location") or {}).get("name"),
            posted_at=_iso(item.get("first_published") or item.get("updated_at")),
            description_html=body, description_text=html_to_text(body), raw=item,
        )


class LeverAdapter(_AtsAdapter):
    """Lever ships `descriptionPlain` in the list response."""

    name = "lever"
    label = "Lever board (company slug)"
    URL = "https://api.lever.co/v0/postings/{0}?mode=json"

    def fetch(self, config):
        slug = self._slug(config)
        payload = _get(_session(), self.URL.format(slug))
        if not isinstance(payload, list):
            raise SourceError("Unexpected Lever response shape")
        jobs = []
        for item in payload:
            job = self._parse(item, config)
            if job:
                jobs.append(job)
        return jobs

    def _parse(self, item, config):
        job_id = item.get("id")
        url = item.get("hostedUrl") or item.get("applyUrl")
        title = item.get("text")
        if not (job_id and url and title):
            return None
        categories = item.get("categories") or {}
        # The requirement lists live outside the description body.
        parts = [item.get("descriptionPlain") or ""]
        for block in item.get("lists") or []:
            parts.append(block.get("text", ""))
            parts.append(html_to_text(block.get("content", "")))
        parts.append(item.get("additionalPlain") or "")
        return RawJob(
            external_id=str(job_id), url=url, title=title.strip(),
            company=self._company(config),
            location=categories.get("location"),
            employment_type=categories.get("commitment"),
            remote=(item.get("workplaceType") or "").lower() in ("remote", "hybrid"),
            posted_at=_iso(item.get("createdAt")),
            description_html=item.get("description"),
            description_text="\n\n".join(p for p in parts if p).strip(),
            raw=item,
        )


class SmartRecruitersAdapter(_AtsAdapter):
    """The list endpoint omits descriptions; each posting needs a second call."""

    name = "smartrecruiters"
    label = "SmartRecruiters board (company slug)"
    needs_detail_fetch = True
    LIST_URL = "https://api.smartrecruiters.com/v1/companies/{0}/postings?limit=100"
    DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{0}/postings/{1}"

    def fetch(self, config):
        slug = self._slug(config)
        payload = _get(_session(), self.LIST_URL.format(slug))
        jobs = []
        for item in payload.get("content", []):
            job = self._parse(item, config, slug)
            if job:
                jobs.append(job)
        return jobs

    def _parse(self, item, config, slug):
        job_id, title = item.get("id"), item.get("name")
        if not (job_id and title):
            return None
        location = item.get("location") or {}
        where = ", ".join(x for x in (location.get("city"), location.get("region"))
                          if x and x.upper() != "REMOTE")
        return RawJob(
            external_id=str(job_id),
            url="https://jobs.smartrecruiters.com/{0}/{1}".format(slug, job_id),
            title=title.strip(),
            company=self._company(config, (item.get("company") or {}).get("name")),
            location=where or None,
            remote=bool(location.get("remote")),
            employment_type=(item.get("typeOfEmployment") or {}).get("label"),
            posted_at=_iso(item.get("releasedDate")),
            raw=item,
        )

    def fetch_detail(self, job, config):
        slug = self._slug(config)
        try:
            detail = _get(_session(),
                          self.DETAIL_URL.format(slug, job.external_id))
        except SourceError as exc:
            log.warning("SmartRecruiters detail failed for %s: %s", job.external_id, exc)
            return job
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        parts = []
        for key in ("companyDescription", "jobDescription", "qualifications",
                    "additionalInformation"):
            section = sections.get(key) or {}
            if section.get("text"):
                parts.append(html_to_text(section["text"]))
        job.description_text = "\n\n".join(parts).strip() or job.description_text
        job.raw = dict(job.raw or {})
        job.raw["_detail"] = detail
        time.sleep(POLITENESS_DELAY)
        return job


class PersonioAdapter(_AtsAdapter):
    """Personio publishes an XML feed. Very common in German Mittelstand."""

    name = "personio"
    label = "Personio board (company slug)"
    URL = "https://{0}.jobs.personio.de/xml"
    JOB_URL = "https://{0}.jobs.personio.de/job/{1}"

    def fetch(self, config):
        from bs4 import BeautifulSoup

        slug = self._slug(config)
        # Redirects are not followed: an unknown Personio tenant answers 307 to
        # personio.com, which would otherwise parse to a silently empty board.
        raw = _get(_session(), self.URL.format(slug), expect_json=False,
                   follow_redirects=False)
        soup = BeautifulSoup(raw, "xml")
        jobs = []
        for position in soup.find_all("position"):
            job = self._parse(position, config, slug)
            if job:
                jobs.append(job)
        return jobs

    def _parse(self, position, config, slug):
        def text_of(tag):
            found = position.find(tag)
            return found.get_text().strip() if found else None

        job_id, title = text_of("id"), text_of("name")
        if not (job_id and title):
            return None

        # Descriptions are nested one level deeper, as name/value pairs.
        body_parts = []
        container = position.find("jobDescriptions")
        if container:
            for block in container.find_all("jobDescription"):
                heading = block.find("name")
                value = block.find("value")
                if heading and heading.get_text().strip():
                    body_parts.append(heading.get_text().strip())
                if value:
                    body_parts.append(html_to_text(value.get_text()))

        offices = [x for x in (text_of("office"), text_of("additionalOffices")) if x]
        return RawJob(
            external_id=str(job_id), url=self.JOB_URL.format(slug, job_id),
            title=title, company=self._company(config, text_of("subcompany")),
            location=", ".join(offices) or None,
            employment_type=text_of("employmentType"),
            posted_at=_iso(text_of("createdAt")),
            description_text="\n\n".join(p for p in body_parts if p).strip() or None,
            raw={"id": job_id, "office": text_of("office"),
                 "department": text_of("department"),
                 "seniority": text_of("seniority")},
        )


class RecruiteeAdapter(_AtsAdapter):
    """Documented pattern. No public demo board was reachable to probe against,
    so the Test button is the validation step for a given slug."""

    name = "recruitee"
    label = "Recruitee board (company slug)"
    URL = "https://{0}.recruitee.com/api/offers/"

    def fetch(self, config):
        slug = self._slug(config)
        payload = _get(_session(), self.URL.format(slug))
        jobs = []
        for item in payload.get("offers", []):
            job = self._parse(item, config, slug)
            if job:
                jobs.append(job)
        return jobs

    def _parse(self, item, config, slug):
        job_id, title = item.get("id"), item.get("title")
        if not (job_id and title):
            return None
        body = item.get("description") or ""
        requirements = item.get("requirements") or ""
        return RawJob(
            external_id=str(job_id),
            url=item.get("careers_url") or item.get("careers_apply_url")
                or "https://{0}.recruitee.com/o/{1}".format(slug, item.get("slug", job_id)),
            title=title.strip(), company=self._company(config, item.get("company_name")),
            location=", ".join(x for x in (item.get("city"), item.get("country")) if x) or None,
            remote=bool(item.get("remote")),
            employment_type=item.get("employment_type"),
            posted_at=_iso(item.get("published_at") or item.get("created_at")),
            description_html=body,
            description_text="\n\n".join(
                html_to_text(x) for x in (body, requirements) if x).strip() or None,
            raw=item,
        )


ATS_ADAPTERS = [GreenhouseAdapter, LeverAdapter, SmartRecruitersAdapter,
                PersonioAdapter, RecruiteeAdapter]
