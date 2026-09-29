"""Source adapter contract.

An adapter turns one configured source into a list of RawJob records. It does
no database work and no deduplication — ingest.py owns that — so adapters stay
trivially testable against saved fixtures.
"""

import re
from dataclasses import dataclass, field
import os
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

#: Sent on every outbound fetch. Several boards ask callers to identify
#: themselves and offer a contact address; set JOBHUNT_CONTACT to yours so a
#: source owner with a problem can reach you rather than silently blocking.
USER_AGENT = "JobHunt/0.1 (self-hosted job-search aggregator{contact})".format(
    contact=("; contact " + os.environ["JOBHUNT_CONTACT"])
    if os.environ.get("JOBHUNT_CONTACT") else "")

DEFAULT_TIMEOUT = 20


@dataclass
class RawJob:
    """One posting as the source described it, before any normalisation."""

    external_id: str
    url: str
    title: str
    company: str
    location: Optional[str] = None
    remote: Optional[bool] = None
    employment_type: Optional[str] = None
    posted_at: Optional[str] = None          # ISO-8601 date or datetime
    description_html: Optional[str] = None
    description_text: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class SourceError(Exception):
    """Adapter failed in a way worth showing on the source health row."""


class Adapter:
    """Base class. Subclasses set `name` and implement fetch()."""

    name = "base"
    label = "Base"

    #: True when the source's search endpoint returns summaries only and a
    #: second request is needed for the description. ingest.py then calls
    #: fetch_detail() for genuinely new postings only, which keeps the request
    #: count near zero once the backlog has been seen.
    needs_detail_fetch = False

    def fetch(self, config: Dict[str, Any]) -> List[RawJob]:
        raise NotImplementedError

    def fetch_detail(self, job: RawJob, config: Dict[str, Any]) -> RawJob:
        """Enrich one posting with its full description. Default: no-op."""
        return job

    def test(self, config: Dict[str, Any]):
        """Return (ok, message) for the config page's Test button."""
        try:
            jobs = self.fetch(config)
        except SourceError as exc:
            return False, str(exc)
        except Exception as exc:  # surfaced verbatim in the UI, not swallowed
            return False, "{0}: {1}".format(type(exc).__name__, exc)
        return True, "OK — {0} postings returned".format(len(jobs))


_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")

# Markdown emphasis and list markers, stripped for the plain-text version.
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_HEADING = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def markdown_to_text(markdown):
    """Flatten Markdown to plain text.

    The Arbeitsagentur returns descriptions as Markdown with HTML entities
    mixed in ('Produktion &amp; Logistik'), so entities are unescaped first.
    """
    if not markdown:
        return ""
    import html as _html

    text = _html.unescape(markdown)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("• ", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _WS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def html_to_text(html):
    """Flatten a posting's HTML body to readable text for the LLM and search.

    Block elements become newlines so that bullet lists survive as separate
    lines; without this the model sees requirements run together.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(["br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        tag.append("\n")
    text = soup.get_text()
    text = _WS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(line for line in lines if line)).strip()
