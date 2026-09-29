"""Template specifications, measured from real CVs rather than guessed.

Every number here was extracted from the PDFs with pdfplumber rather than
copied from prose, so the generated documents match what he already sends.

Two templates, because his three samples use two distinct layouts:

  CLASSIC  — Schneider Electric / Scheidt & Bachmann. Two to three pages,
             PROFILE then PROFESSIONAL EXPERIENCE with bold sub-headings
             grouping bullets inside each role. This is the layout the written
             style guide describes.
  COMPACT  — SumUp. One page, adds a CORE STRENGTHS block, tighter bullets in
             the accent colour, slightly larger body text.

Measured facts that settle ambiguities in the written guide:
  * "20pt Arial" bullets are 20 half-points — the PDFs render body text at 10pt.
  * "480/240 indent" is exact: the bullet glyph sits at x=62 and its text at
    x=74, i.e. 24pt left indent with a 12pt hanging indent, on a 50pt margin.
  * The "tinted contact bar" is a real filled rectangle: #EDF2F8 in CLASSIC,
    #EAF3F3 in COMPACT, spanning the full text width.
"""

from dataclasses import dataclass
from typing import Dict

#: A4, matching every sample.
PAGE_WIDTH_PT = 595.3
PAGE_HEIGHT_PT = 841.9

#: Margins. 50pt left/right is exactly the 1000 twips in the style guide;
#: 48pt top is its 900 twips (900tw = 45pt) rounded to where text actually sits.
MARGIN_TOP_PT = 45
MARGIN_BOTTOM_PT = 45
MARGIN_SIDE_PT = 50

DEFAULT_BRAND = "#1B4F8A"          # the style guide's default navy
BODY_COLOUR = "#222222"
MUTED_COLOUR = "#555555"           # italic dates and locations
LINK_COLOUR = "#1155CC"

FONT = "Arial"



@dataclass
class TemplateSpec:
    """Every measurement the renderer needs, in points."""

    key: str
    label: str
    description: str

    name_size: float
    name_uppercase: bool
    name_bold: bool

    subtitle_size: float
    subtitle_bold: bool
    subtitle_uses_accent: bool

    contact_bar_fill: str          # tint applied when no accent is derived
    contact_size: float

    section_size: float
    section_uppercase: bool
    section_rule: bool

    body_size: float

    job_title_size: float
    job_title_uses_brand: bool
    job_meta_size: float

    #: Bold sub-headings that group bullets inside a role. CLASSIC only.
    supports_subheadings: bool
    subheading_size: float

    #: "Label: value" block. COMPACT only.
    supports_core_strengths: bool
    strengths_size: float

    bullet_indent_pt: float        # left indent from the margin
    bullet_hanging_pt: float       # how far the glyph hangs back
    bullet_size: float
    bullet_uses_accent: bool

    #: Rough guidance handed to the model so it writes to length.
    target_pages: int
    max_bullets_per_role: int


CLASSIC = TemplateSpec(
    key="classic",
    label="Classic (2 pages)",
    description=(
        "PROFILE then PROFESSIONAL EXPERIENCE, with bold sub-headings grouping "
        "bullets inside each role. Matches the written style guide and the "
        "Schneider Electric and Scheidt & Bachmann CVs."
    ),
    name_size=17, name_uppercase=True, name_bold=True,
    subtitle_size=11, subtitle_bold=False, subtitle_uses_accent=False,
    contact_bar_fill="#EDF2F8", contact_size=9,
    section_size=10.5, section_uppercase=True, section_rule=True,
    body_size=10,
    job_title_size=11, job_title_uses_brand=False, job_meta_size=9,
    supports_subheadings=True, subheading_size=10,
    supports_core_strengths=False, strengths_size=10,
    bullet_indent_pt=24, bullet_hanging_pt=12, bullet_size=10,
    bullet_uses_accent=False,
    target_pages=2, max_bullets_per_role=6,
)

COMPACT = TemplateSpec(
    key="compact",
    label="Compact (1 page)",
    description=(
        "PROFILE, CORE STRENGTHS then EXPERIENCE. Tighter bullets in the accent "
        "colour and slightly larger body text. Matches the SumUp CV."
    ),
    name_size=19, name_uppercase=False, name_bold=True,
    subtitle_size=11, subtitle_bold=True, subtitle_uses_accent=True,
    contact_bar_fill="#EAF3F3", contact_size=9,
    section_size=11, section_uppercase=True, section_rule=True,
    body_size=11,
    job_title_size=10.5, job_title_uses_brand=True, job_meta_size=9.5,
    supports_subheadings=False, subheading_size=10,
    supports_core_strengths=True, strengths_size=10.5,
    bullet_indent_pt=15, bullet_hanging_pt=8, bullet_size=10,
    bullet_uses_accent=True,
    target_pages=1, max_bullets_per_role=9,
)

TEMPLATES: Dict[str, TemplateSpec] = {CLASSIC.key: CLASSIC, COMPACT.key: COMPACT}
DEFAULT_TEMPLATE = CLASSIC.key


def get_template(key):
    return TEMPLATES.get(key or DEFAULT_TEMPLATE, CLASSIC)


def template_choices():
    return [(spec.key, spec.label, spec.description) for spec in TEMPLATES.values()]


def normalise_hex(value, fallback=DEFAULT_BRAND):
    """Accept '#1B4F8A', '1b4f8a' or junk; always return a usable '#RRGGBB'."""
    if not value:
        return fallback
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return fallback
    try:
        int(text, 16)
    except ValueError:
        return fallback
    return "#" + text.upper()


def tint(hex_colour, amount=0.92):
    """A very light wash of the brand colour, for the contact bar."""
    hex_colour = normalise_hex(hex_colour)
    r = int(hex_colour[1:3], 16)
    g = int(hex_colour[3:5], 16)
    b = int(hex_colour[5:7], 16)
    mix = lambda c: int(round(c + (255 - c) * amount))
    return "#%02X%02X%02X" % (mix(r), mix(g), mix(b))
