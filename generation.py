"""Turning a job posting into a CV and cover letter plan.

The model returns structured JSON describing *what to say*; documents.py
decides *how it looks*. Nothing here emits formatting.

Writing quality matters more than throughput on this path — one document at a
time, on demand — so it routes to Claude by default while scoring stays on the
local model. The cover letter in particular has to not read as machine-written,
which is exactly where the capability gap shows.
"""

import json
import logging
import threading
from contextlib import contextmanager

from . import doc_styles, documents, profile, prompts
from .doc_styles import DEFAULT_BRAND

log = logging.getLogger(__name__)

# One generation per (job, kind) at a time, across every entry point. A
# double-click during the 60-80s wait — from Slack *or* the web page — must
# not produce two documents and two LLM bills, so the guard lives here rather
# than in either caller.
_in_flight = set()
_in_flight_lock = threading.Lock()


@contextmanager
def in_flight_guard(job_id, kind):
    """Yields True if this (job, kind) was free and is now claimed.

    The caller must treat False as "someone else is already generating this"
    and do nothing. Always releases on exit, including on exceptions.
    """
    key = (int(job_id), kind)
    with _in_flight_lock:
        acquired = key not in _in_flight
        if acquired:
            _in_flight.add(key)
    try:
        yield acquired
    finally:
        if acquired:
            with _in_flight_lock:
                _in_flight.discard(key)

CV_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_color": {"type": "string"},
        # A separate boolean rather than trusting the prose: asked only for a
        # reason, the model justified the fallback navy as "matching Agraya's
        # branding" — a company whose palette it cannot possibly know.
        "brand_color_recognised": {"type": "boolean"},
        "brand_color_reason": {"type": "string"},
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "core_strengths": {
            "type": "array", "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                "required": ["label", "value"],
            },
        },
        "roles": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "dates": {"type": "string"},
                    "location": {"type": "string"},
                    "groups": {
                        "type": "array", "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                "bullets": {
                                    "type": "array", "maxItems": 5,
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["heading", "bullets"],
                        },
                    },
                },
                "required": ["title", "company", "dates", "location", "groups"],
            },
        },
        "education": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
        "awards": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    },
    "required": ["brand_color", "brand_color_recognised", "brand_color_reason",
                 "headline", "summary", "core_strengths", "roles", "education",
                 "awards"],
}

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "brand_color": {"type": "string"},
        # A separate boolean rather than trusting the prose: asked only for a
        # reason, the model justified the fallback navy as "matching Agraya's
        # branding" — a company whose palette it cannot possibly know.
        "brand_color_recognised": {"type": "boolean"},
        "brand_color_reason": {"type": "string"},
        "headline": {"type": "string"},
        "subject": {"type": "string"},
        "salutation": {"type": "string"},
        "paragraphs": {"type": "array", "minItems": 4, "maxItems": 6,
                       "items": {"type": "string"}},
    },
    "required": ["brand_color", "brand_color_recognised", "brand_color_reason",
                 "headline", "subject", "salutation", "paragraphs"],
}


def _posting_block(job):
    return (
        "=== JOB POSTING (data) ===\n"
        "Title: {title}\nCompany: {company}\nLocation: {location}\n\n"
        "{description}\n=== END OF POSTING ==="
    ).format(
        title=job["title"], company=job["company"],
        location=job["location"] or "not stated",
        description=(job["description_text"] or "")[:6000],
    )


def _assessment_block(job):
    """The stored score, so the writing plays to the same strengths and gaps."""
    keys = job.keys() if hasattr(job, "keys") else []
    if "fit_score" not in keys or job["fit_score"] is None:
        return ""
    matched = _load_list(job["matched_json"] if "matched_json" in keys else None)
    gaps = _load_list(job["gaps_json"] if "gaps_json" in keys else None)
    parts = ["\nPRIOR ASSESSMENT OF THIS ROLE (fit {0}/100):".format(job["fit_score"])]
    if matched:
        parts.append("Strengths to lead with: " + "; ".join(matched))
    if gaps:
        parts.append("Known gaps — do not hide these: " + "; ".join(gaps))
    return "\n".join(parts)


def _load_list(raw):
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _length_hint(spec):
    return (
        "\nTarget length: about {0} page(s). At most {1} bullets per role, and "
        "{2}."
    ).format(
        spec.target_pages, spec.max_bullets_per_role,
        "group them under short bold sub-headings" if spec.supports_subheadings
        else "return exactly one group per role with an empty heading",
    )


def plan_cv(conn, job, provider, template_key=None):
    spec = doc_styles.get_template(template_key)
    user = "\n\n".join([
        "CANDIDATE PROFILE:\n" + profile.scoring_profile(conn),
        _posting_block(job) + _assessment_block(job),
        _length_hint(spec),
    ])
    plan = provider.complete_json(prompts.system_prompt(conn, "cv"), user,
                                  CV_SCHEMA, max_tokens=4000, temperature=0.3)
    return _sanitise_plan(plan)


def plan_cover_letter(conn, job, provider, template_key=None):
    user = "\n\n".join([
        "CANDIDATE PROFILE:\n" + profile.scoring_profile(conn),
        _posting_block(job) + _assessment_block(job),
    ])
    plan = provider.complete_json(prompts.system_prompt(conn, "letter"), user,
                                  LETTER_SCHEMA, max_tokens=2500, temperature=0.4)
    plan = _sanitise_plan(plan)
    plan.setdefault("role", job["title"])
    return plan


def _sanitise_plan(plan):
    """Normalise the colour and strip anything the model should not control."""
    plan = dict(plan or {})
    plan["brand_color"] = doc_styles.normalise_hex(
        plan.get("brand_color"), DEFAULT_BRAND)

    # A colour is only kept when the model said it actually recognised the
    # brand. Otherwise it reverts to navy regardless of what it suggested —
    # the same rule as the scoring quotes: a claim without backing is dropped.
    if not plan.get("brand_color_recognised"):
        plan["brand_color"] = DEFAULT_BRAND
        if not plan.get("brand_color_reason"):
            plan["brand_color_reason"] = (
                "Company palette not recognised — using the default navy.")
    # Contact details and language claims are rendered from the profile; drop
    # any the model supplied so they cannot be mistaken for authoritative.
    for key in ("contact", "languages", "german", "phone", "email", "linkedin"):
        plan.pop(key, None)
    return plan


class GenerationBlocked(Exception):
    """A confidentiality term reached the draft. Nothing is written to disk."""

    def __init__(self, terms):
        self.terms = terms
        Exception.__init__(
            self,
            "Blocked: the draft contains {0}. Nothing was saved.".format(
                ", ".join(terms)))


def generate_document(conn, job, provider, kind="cv", template_key=None,
                      brand=None, blocklist=None, output_dir=None):
    """Plan, check, render and save. Returns a dict describing the result."""
    template_key = template_key or doc_styles.DEFAULT_TEMPLATE
    contact = documents.contact_details(conn)

    if kind == "cv":
        plan = plan_cv(conn, job, provider, template_key)
    elif kind == "cover_letter":
        plan = plan_cover_letter(conn, job, provider, template_key)
    else:
        raise ValueError("Unknown document kind '{0}'".format(kind))

    # Confidentiality is checked on the draft, before anything is written.
    found = documents.confidentiality_scan(documents.plan_text(plan), blocklist)
    if found:
        log.warning("generation blocked for job %s: %s", job["id"], found)
        raise GenerationBlocked(found)

    colour = doc_styles.normalise_hex(brand or plan.get("brand_color"), DEFAULT_BRAND)
    if kind == "cv":
        document = documents.build_cv(plan, contact, template_key, colour, colour,
                                      languages=profile.languages_line(conn))
    else:
        document = documents.build_cover_letter(
            plan, contact, template_key, colour, colour)

    version = _next_version(conn, job["id"], kind)
    docx_path = documents.save(document, job, kind, version, output_dir)
    pdf_path = documents.to_pdf(docx_path)

    _record(conn, job["id"], kind, version, docx_path, pdf_path,
            provider.model_label(), colour)
    return {
        "kind": kind,
        "template": template_key,
        "brand_color": colour,
        "brand_color_reason": plan.get("brand_color_reason", ""),
        "docx": docx_path,
        "pdf": pdf_path,
        "version": version,
        "plan": plan,
        "model": provider.model_label(),
    }


def _next_version(conn, job_id, kind):
    row = conn.execute(
        "SELECT MAX(version) FROM documents WHERE job_id = ? AND kind = ?",
        (job_id, kind)).fetchone()
    return (row[0] or 0) + 1


def _record(conn, job_id, kind, version, docx_path, pdf_path, model, colour):
    from .util import now_iso

    # Stored relative to OUTPUT_DIR so the row means the same on the Mac and
    # the Pi — see documents.store_path.
    rows = [(job_id, kind, version, "docx", documents.store_path(docx_path),
             model, colour, now_iso())]
    if pdf_path:
        rows.append((job_id, kind, version, "pdf",
                     documents.store_path(pdf_path), model, colour, now_iso()))
    conn.executemany(
        "INSERT INTO documents (job_id, kind, version, format, path, model_used, "
        "brand_color, created_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def documents_for(conn, job_id):
    return conn.execute(
        "SELECT * FROM documents WHERE job_id = ? ORDER BY created_at DESC, id DESC",
        (job_id,)).fetchall()
