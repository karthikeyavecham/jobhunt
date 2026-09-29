"""The candidate profile: who the app is job-hunting for.

Everything personal lives here, and nothing personal is committed. The module
ships a clearly-fake placeholder profile; the real one comes from whichever of
these exists first:

  1. the `profile` table in the database — written by the Profile page, and
     always the winner once set;
  2. `profile_local.json` next to this file — gitignored, handy for seeding a
     fresh install without retyping, and for keeping your own profile out of
     the repository;
  3. the placeholder below, so a fresh clone starts and shows you what to fill
     in rather than crashing or, worse, quietly writing someone else's CV.

Contact details are structured fields rather than prose because documents.py
injects them verbatim: the model is never given the opportunity to invent a
phone number or email address.
"""

import json
import logging
from pathlib import Path

from .util import now_iso

log = logging.getLogger(__name__)

#: Gitignored. Copy `profile.example.json` to this name and fill it in.
LOCAL_PROFILE_PATH = Path(__file__).parent / "profile_local.json"

#: Sections a profile is made of. `languages` and `constraints` are rendered
#: and prompted verbatim, so they are stored as written rather than derived.
SECTIONS = ("contact", "summary", "skills", "preferences", "languages",
            "constraints")

PLACEHOLDER = {
    "contact": {
        "name": "Your Name",
        "email": "you@example.com",
        "phone": "+00 000 0000000",
        "location": "Your City, Country",
        "linkedin": "https://www.linkedin.com/in/your-handle",
    },
    "summary": """\
Replace this with your career summary: what you do, how long you have done it,
and the roles you have held. Write it the way you would say it out loud — this
text is fed to the model that scores postings and drafts your CV, so detail
here is what makes both of them accurate.

Current — Employer, City (Month Year to present):
- What you own, and what you actually did with it.
- An achievement with a number attached, if you have one.

Prior — Employer (Years):
- Same again, more briefly.

Education: Degree, Institution, Country (Years).
""",
    "skills": """\
Replace this with your skills, as a comma-separated run of phrases rather than
a bullet list: tools and platforms you administer, languages you write, methods
you use, anything a posting might name.
""",
    "preferences": """\
Target roles: the job titles you actually want.

Region: the cities you can commute to, and whether you will consider remote,
hybrid or relocation.

Languages: which you speak and at what level, and the working language you
prefer.

Work authorisation: your right to work where you are applying.
""",
    "languages": "English: Fluent",
    "constraints": """\
Things the model must never overstate about you. These are injected into every
CV and cover letter prompt as hard rules, so write them as plain statements:

- If your level in a language is below what postings ask for, say so here and
  it will be stated honestly rather than hidden.
- If you have mentored but never managed, say so here and you will not be
  described as having run a team.
""",
}


def load_seed():
    """The starting profile: `profile_local.json` if present, else placeholder.

    A malformed local file is reported and ignored rather than crashing the
    app — losing the seed is recoverable, not starting is not.
    """
    if not LOCAL_PROFILE_PATH.exists():
        return dict(PLACEHOLDER)
    try:
        with open(str(LOCAL_PROFILE_PATH), encoding="utf-8") as handle:
            local = json.load(handle)
    except (ValueError, OSError) as exc:
        log.warning("ignoring %s: %s", LOCAL_PROFILE_PATH.name, exc)
        return dict(PLACEHOLDER)
    if not isinstance(local, dict):
        log.warning("ignoring %s: expected an object", LOCAL_PROFILE_PATH.name)
        return dict(PLACEHOLDER)

    seed = dict(PLACEHOLDER)
    for section in SECTIONS:
        if section in local and local[section]:
            seed[section] = local[section]
    return seed


SEED = load_seed()

#: Kept as module-level names because documents.py and the scoring prompt read
#: them directly as fallbacks.
CONTACT = SEED["contact"]
SUMMARY = SEED["summary"]
SKILLS = SEED["skills"]
PREFERENCES = SEED["preferences"]
LANGUAGES = SEED["languages"]
CONSTRAINTS = SEED["constraints"]


def ensure_seeded(conn):
    """Write the seed for any section that has no stored copy yet."""
    existing = {row[0] for row in conn.execute("SELECT section FROM profile").fetchall()}
    now = now_iso()
    for section in SECTIONS:
        if section not in existing:
            conn.execute(
                "INSERT INTO profile (section, content_json, updated_at) VALUES (?, ?, ?)",
                (section, json.dumps(SEED[section], ensure_ascii=False), now),
            )
    conn.commit()


def get_section(conn, section):
    row = conn.execute(
        "SELECT content_json FROM profile WHERE section = ?", (section,)
    ).fetchone()
    if row is None:
        return SEED.get(section)
    try:
        return json.loads(row[0])
    except ValueError:
        return SEED.get(section)


def set_section(conn, section, content):
    conn.execute(
        "INSERT INTO profile (section, content_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(section) DO UPDATE SET content_json = excluded.content_json, "
        "updated_at = excluded.updated_at",
        (section, json.dumps(content, ensure_ascii=False), now_iso()),
    )
    conn.commit()


def all_sections(conn):
    ensure_seeded(conn)
    return {section: get_section(conn, section) for section in SECTIONS}


def is_placeholder(conn):
    """Is this still the shipped example rather than a real person?

    Drives the setup prompt. Checked on the name alone: someone who has filled
    in their name has understood the page, and nagging past that point is worse
    than trusting them.
    """
    contact = get_section(conn, "contact") or {}
    return (contact.get("name") or "").strip() in ("", PLACEHOLDER["contact"]["name"])


def scoring_profile(conn):
    """Compact profile text for the scoring prompt.

    Kept short on purpose — it is prepended to every scoring call, and the
    local model's context is the scarce resource, not the profile's detail.
    """
    ensure_seeded(conn)
    contact = get_section(conn, "contact") or CONTACT
    return (
        "{name}, based in {location}.\n\n"
        "{summary}\n"
        "SKILLS: {skills}\n"
        "PREFERENCES AND CONSTRAINTS: {preferences}"
    ).format(
        name=contact.get("name", ""),
        location=contact.get("location", ""),
        summary=(get_section(conn, "summary") or SUMMARY).strip(),
        skills=(get_section(conn, "skills") or SKILLS).strip(),
        preferences=(get_section(conn, "preferences") or PREFERENCES).strip(),
    )


def _stored(conn, section):
    """The stored value, or None when the section has never been written.

    Distinct from `get_section`, which falls back to the seed. That fallback is
    wrong for a section the user has deliberately emptied: treating "" as
    unset hands them back the seed, and on a machine whose seed came from
    someone else's `profile_local.json` that is how one person's constraints
    end up on another person's CV.
    """
    row = conn.execute(
        "SELECT content_json FROM profile WHERE section = ?", (section,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def writing_constraints(conn):
    """The must-not-overstate rules, for the CV and cover letter prompts."""
    ensure_seeded(conn)
    value = _stored(conn, "constraints")
    return (CONSTRAINTS if value is None else value or "").strip()


def languages_line(conn):
    """The languages line as it appears on the CV, verbatim from the profile."""
    ensure_seeded(conn)
    value = _stored(conn, "languages")
    return (LANGUAGES if value is None else value or "").strip()
