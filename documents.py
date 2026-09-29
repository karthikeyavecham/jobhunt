"""CV and cover letter generation.

**The model plans; this module renders.** The LLM returns structured JSON —
which bullets, in what order, under which heading — and never emits a .docx or
chooses a font. That split is deliberate: formatting is then always correct and
reproducible, and a bad generation can only ever produce bad *wording*, never a
broken document.

Three guarantees are enforced in code rather than asked for in the prompt,
because a prompt is a request and these are requirements:

  * Contact details are injected from the stored profile. The model is never
    given the chance to invent a phone number or email address.
  * The German level renders as the exact style-guide string, whatever the
    model wrote.
  * Generated text is scanned for employer-internal terms before delivery. A hit
    blocks the document rather than warning after the fact.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from . import doc_styles, profile
from .doc_styles import (BODY_COLOUR, FONT, LINK_COLOUR,
                         MARGIN_BOTTOM_PT, MARGIN_SIDE_PT, MARGIN_TOP_PT,
                         MUTED_COLOUR, get_template, normalise_hex, tint)

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "documents"

#: Terms that must never reach a document. Seeded from the compliance note in
#: the job-search profile; extendable from the config page.
DEFAULT_BLOCKLIST = ["BotConnect"]


def store_path(path):
    """The form a document path takes in the database: relative to OUTPUT_DIR.

    Absolute paths do not survive the trip between hosts — rows written on the
    Mac pointed at `/Users/karthik/...`, which the Pi could not serve even
    though the files themselves had been synced. Storing the location relative
    to the output directory makes a row mean the same thing on both machines.
    """
    path = Path(path)
    try:
        return str(path.resolve().relative_to(OUTPUT_DIR.resolve()))
    except ValueError:
        # Written outside the output directory (tests use a tmpdir). Keep it
        # as-is; resolve_path sends it back unchanged and confinement still
        # gets the final say.
        return str(path)


def resolve_path(stored):
    """Absolute path for a database row, tolerating pre-2026-08 absolute rows.

    Old rows hold a full Mac path. Their basename is still correct — the files
    were synced — so fall back to looking them up in OUTPUT_DIR rather than
    404ing on history.
    """
    path = Path(stored)
    if not path.is_absolute():
        return OUTPUT_DIR / path
    if path.exists():
        return path
    return OUTPUT_DIR / path.name


# --------------------------------------------------------------------------
# Low-level docx helpers. python-docx has no API for shading or paragraph
# borders, so these drop to the underlying XML.
# --------------------------------------------------------------------------

def _rgb(hex_colour):
    hex_colour = normalise_hex(hex_colour)
    return RGBColor(int(hex_colour[1:3], 16), int(hex_colour[3:5], 16),
                    int(hex_colour[5:7], 16))


def shade(element, hex_colour):
    """Fill a paragraph or table cell."""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), normalise_hex(hex_colour).lstrip("#"))
    element.get_or_add_pPr().append(shd) if hasattr(element, "get_or_add_pPr") \
        else element.append(shd)


def shade_paragraph(paragraph, hex_colour):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), normalise_hex(hex_colour).lstrip("#"))
    paragraph._p.get_or_add_pPr().append(shd)


def bottom_rule(paragraph, hex_colour, size_eighths=8):
    """The coloured rule under a section header."""
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size_eighths))
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), normalise_hex(hex_colour).lstrip("#"))
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)


def add_run(paragraph, text, size, colour=BODY_COLOUR, bold=False, italic=False,
            font=FONT):
    run = paragraph.add_run(text)
    run.font.name = font
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = _rgb(colour)
    # East-Asian font must be set separately or Word substitutes it.
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)
    return run


def spacing(paragraph, before=0, after=4, line=None):
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    if line is not None:
        fmt.line_spacing = line
    return paragraph


# --------------------------------------------------------------------------
# Confidentiality
# --------------------------------------------------------------------------

def confidentiality_scan(text, blocklist=None):
    """Return the blocked terms present in `text`.

    Whole-word, case-insensitive. His compliance constraint is that internal
    project and team names must never appear in an application document, and a
    warning after the file is written is too late — callers treat a non-empty
    result as a hard stop.
    """
    blocklist = [t for t in (blocklist or DEFAULT_BLOCKLIST) if t and t.strip()]
    if not text or not blocklist:
        return []
    found = []
    for term in blocklist:
        if re.search(r"\b{0}\b".format(re.escape(term.strip())), text, re.IGNORECASE):
            found.append(term.strip())
    return found


def plan_text(plan):
    """Every piece of model-written prose in a plan, for scanning."""
    parts = [plan.get("headline", ""), plan.get("summary", "")]
    for item in plan.get("core_strengths", []) or []:
        parts.extend([item.get("label", ""), item.get("value", "")])
    for role in plan.get("roles", []) or []:
        parts.append(role.get("title", ""))
        for group in role.get("groups", []) or []:
            parts.append(group.get("heading", ""))
            parts.extend(group.get("bullets", []) or [])
        parts.extend(role.get("bullets", []) or [])
    parts.extend(plan.get("paragraphs", []) or [])
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# Document assembly
# --------------------------------------------------------------------------

def _new_document(spec):
    document = Document()
    section = document.sections[0]
    section.page_width = Pt(doc_styles.PAGE_WIDTH_PT)
    section.page_height = Pt(doc_styles.PAGE_HEIGHT_PT)
    section.top_margin = Pt(MARGIN_TOP_PT)
    section.bottom_margin = Pt(MARGIN_BOTTOM_PT)
    section.left_margin = Pt(MARGIN_SIDE_PT)
    section.right_margin = Pt(MARGIN_SIDE_PT)

    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(spec.body_size)
    normal.font.color.rgb = _rgb(BODY_COLOUR)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.08
    return document


def _add_header_block(document, spec, contact, headline, brand, accent):
    """Name, target-role headline, and the tinted contact bar."""
    name_text = contact["name"].upper() if spec.name_uppercase else contact["name"]
    para = document.add_paragraph()
    spacing(para, after=2)
    add_run(para, name_text, spec.name_size, brand, bold=spec.name_bold)

    if headline:
        para = document.add_paragraph()
        spacing(para, after=8)
        add_run(para, headline, spec.subtitle_size,
                accent if spec.subtitle_uses_accent else BODY_COLOUR,
                bold=spec.subtitle_bold)

    # Contact bar: a single shaded, centred paragraph. Details come from the
    # stored profile verbatim — the model never supplies them.
    bar = document.add_paragraph()
    bar.alignment = WD_ALIGN_PARAGRAPH.CENTER
    spacing(bar, before=4, after=10)
    shade_paragraph(bar, contact_bar_fill(spec, accent))
    add_run(bar, "{0}  |  {1}  |  {2}".format(
        contact["location"], contact["email"], contact["phone"]),
        spec.contact_size, BODY_COLOUR)
    bar.add_run().add_break()
    add_run(bar, contact["linkedin"], spec.contact_size, LINK_COLOUR)


def contact_bar_fill(spec, accent):
    """The tinted bar behind the contact line.

    The template's own value is measured from the real CVs, so it is used
    verbatim for the default palette. A computed tint is only a fallback for
    brand colours we have no sample for.
    """
    if normalise_hex(accent) == normalise_hex(doc_styles.DEFAULT_BRAND):
        return spec.contact_bar_fill
    return tint(accent)


def _add_section_header(document, spec, text, brand):
    para = document.add_paragraph()
    spacing(para, before=10, after=5)
    add_run(para, text.upper() if spec.section_uppercase else text,
            spec.section_size, brand, bold=True)
    if spec.section_rule:
        bottom_rule(para, brand)
    return para


def _add_bullet(document, spec, text, accent):
    para = document.add_paragraph()
    fmt = para.paragraph_format
    fmt.left_indent = Pt(spec.bullet_indent_pt)
    fmt.first_line_indent = Pt(-spec.bullet_hanging_pt)
    spacing(para, after=3)
    add_run(para, "• ", spec.bullet_size,
            accent if spec.bullet_uses_accent else BODY_COLOUR)
    add_run(para, text, spec.bullet_size, BODY_COLOUR)
    return para


def _add_role_header(document, spec, role, brand, accent):
    """Two columns: title and company left, dates and location right."""
    table = document.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    total = doc_styles.PAGE_WIDTH_PT - 2 * MARGIN_SIDE_PT
    table.columns[0].width = Pt(total * 0.62)
    table.columns[1].width = Pt(total * 0.38)
    _strip_table_borders(table)

    left = table.cell(0, 0).paragraphs[0]
    spacing(left, after=0)
    add_run(left, role.get("title", ""), spec.job_title_size,
            brand if spec.job_title_uses_brand else BODY_COLOUR, bold=True)
    if role.get("company"):
        add_run(left, "  |  " + role["company"], spec.job_title_size,
                accent if spec.job_title_uses_brand else BODY_COLOUR, bold=True)

    right = table.cell(0, 1).paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    spacing(right, after=0)
    meta = "  |  ".join(x for x in (role.get("dates"), role.get("location")) if x)
    add_run(right, meta, spec.job_meta_size, MUTED_COLOUR, italic=True)
    return table


def _strip_table_borders(table):
    """The two-column job header is a layout device, not a visible table."""
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement("w:{0}".format(edge))
        element.set(qn("w:val"), "none")
        element.set(qn("w:sz"), "0")
        borders.append(element)
    tbl_pr.append(borders)


def build_cv(plan, contact, template_key=None, brand=None, accent=None,
             languages=None):
    """Render a CV plan to a python-docx Document.

    `languages` is the verbatim line from the profile; omitted when the
    candidate has not set one, rather than inventing a default.
    """
    spec = get_template(template_key)
    brand = normalise_hex(brand or doc_styles.DEFAULT_BRAND)
    accent = normalise_hex(accent or brand)

    document = _new_document(spec)
    _add_header_block(document, spec, contact, plan.get("headline"), brand, accent)

    if plan.get("summary"):
        _add_section_header(document, spec, "Profile", brand)
        para = document.add_paragraph()
        spacing(para, after=6)
        add_run(para, plan["summary"], spec.body_size, BODY_COLOUR)

    strengths = plan.get("core_strengths") or []
    if spec.supports_core_strengths and strengths:
        _add_section_header(document, spec, "Core Strengths", brand)
        for item in strengths:
            para = document.add_paragraph()
            spacing(para, after=3)
            add_run(para, "{0}:  ".format(item.get("label", "")),
                    spec.strengths_size, brand, bold=True)
            add_run(para, item.get("value", ""), spec.strengths_size, BODY_COLOUR)

    roles = plan.get("roles") or []
    if roles:
        _add_section_header(
            document, spec,
            "Professional Experience" if spec.supports_subheadings else "Experience",
            brand)
        for role in roles:
            _add_role_header(document, spec, role, brand, accent)
            groups = role.get("groups") or []
            if spec.supports_subheadings and groups:
                for group in groups:
                    if group.get("heading"):
                        head = document.add_paragraph()
                        spacing(head, before=5, after=2)
                        add_run(head, group["heading"], spec.subheading_size,
                                brand, bold=True)
                    for bullet in group.get("bullets") or []:
                        _add_bullet(document, spec, bullet, accent)
            else:
                # Flatten groups when the template has no sub-headings.
                bullets = list(role.get("bullets") or [])
                for group in groups:
                    bullets.extend(group.get("bullets") or [])
                for bullet in bullets[:spec.max_bullets_per_role]:
                    _add_bullet(document, spec, bullet, accent)

    education = plan.get("education") or []
    if education:
        _add_section_header(document, spec, "Education", brand)
        for line in education:
            para = document.add_paragraph()
            spacing(para, after=2)
            add_run(para, line, spec.body_size, BODY_COLOUR)

    # Languages are rendered from the profile, not the plan: stating a level
    # you do not have is the one mistake a CV cannot recover from, so the line
    # is copied verbatim from what the candidate wrote rather than paraphrased
    # by the model.
    if languages:
        _add_section_header(document, spec, "Languages", brand)
        para = document.add_paragraph()
        spacing(para, after=2)
        add_run(para, languages, spec.body_size, BODY_COLOUR)

    if plan.get("awards"):
        _add_section_header(document, spec, "Awards", brand)
        for line in plan["awards"]:
            _add_bullet(document, spec, line, accent)

    return document


def build_cover_letter(plan, contact, template_key=None, brand=None,
                       accent=None):
    """Render a cover letter. Five to six paragraphs, no bullets."""
    spec = get_template(template_key)
    brand = normalise_hex(brand or doc_styles.DEFAULT_BRAND)
    accent = normalise_hex(accent or brand)

    document = _new_document(spec)
    _add_header_block(document, spec, contact, plan.get("headline"), brand, accent)

    meta = document.add_paragraph()
    spacing(meta, before=6, after=10)
    add_run(meta, date.today().strftime("%d %B %Y"), spec.body_size, MUTED_COLOUR)

    if plan.get("recipient"):
        para = document.add_paragraph()
        spacing(para, after=8)
        add_run(para, plan["recipient"], spec.body_size, BODY_COLOUR)

    subject = document.add_paragraph()
    spacing(subject, after=10)
    add_run(subject, plan.get("subject") or "Application: {0}".format(
        plan.get("role") or ""), spec.body_size, brand, bold=True)

    salutation = document.add_paragraph()
    spacing(salutation, after=8)
    add_run(salutation, plan.get("salutation") or "Dear Hiring Team,",
            spec.body_size, BODY_COLOUR)

    for text in plan.get("paragraphs") or []:
        para = document.add_paragraph()
        spacing(para, after=8)
        add_run(para, text, spec.body_size, BODY_COLOUR)

    closing = document.add_paragraph()
    spacing(closing, before=6, after=2)
    add_run(closing, "Warm regards,", spec.body_size, BODY_COLOUR)
    name = document.add_paragraph()
    spacing(name, after=0)
    add_run(name, contact["name"], spec.body_size, BODY_COLOUR, bold=True)
    return document


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def slugify(text, limit=48):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return (slug[:limit] or "document")


def save(document, job, kind, version=1, output_dir=None):
    output_dir = Path(output_dir or OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = "{0}_{1}_{2}_v{3}.docx".format(
        slugify(kind), slugify(job["company"], 28), slugify(job["title"], 34), version)
    path = output_dir / filename
    document.save(str(path))
    return path


def soffice_path():
    """LibreOffice, used for docx -> pdf. Present on the Pi, often not on the Mac."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return mac if os.path.exists(mac) else None


def pdf_available():
    return soffice_path() is not None


def to_pdf(docx_path, timeout=120):
    """Convert to PDF. Returns the path, or None when LibreOffice is absent.

    Conversion runs in a scratch profile directory: a headless soffice sharing
    the default profile with a running desktop LibreOffice exits silently
    without producing a file.
    """
    binary = soffice_path()
    if binary is None:
        log.info("LibreOffice not found; skipping PDF conversion")
        return None

    docx_path = Path(docx_path)
    with tempfile.TemporaryDirectory() as profile_dir:
        command = [
            binary, "--headless", "--norestore",
            "-env:UserInstallation=file://{0}".format(profile_dir),
            "--convert-to", "pdf", "--outdir", str(docx_path.parent), str(docx_path),
        ]
        try:
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("PDF conversion timed out for %s", docx_path.name)
            return None

    pdf_path = docx_path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        log.warning("PDF conversion failed (%s): %s", result.returncode,
                    (result.stdout or b"")[:300])
        return None
    return pdf_path


def contact_details(conn):
    """Contact block from the stored profile, with the seed as a fallback."""
    profile.ensure_seeded(conn)
    contact = profile.get_section(conn, "contact") or profile.CONTACT
    merged = dict(profile.CONTACT)
    merged.update({k: v for k, v in contact.items() if v})
    return merged
