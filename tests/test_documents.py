"""Document fidelity, measured against real CVs rather than guessed.

Every target in TestClassicFidelity was extracted with pdfplumber from
a real CV PDF. These are regression tests: if a
future change drifts the layout away from the CVs he actually sends, they fail.
"""

import os
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from jobhunt import app as app_module
from jobhunt import db as db_module
from jobhunt import doc_styles, documents
from jobhunt.profile import CONTACT
from jobhunt.util import now_iso

# EMU per twip.
TWIPS = 635


def tw(length):
    return round(length.emu / TWIPS)


SAMPLE_PLAN = {
    "headline": "Support Operations & Platform Owner",
    "summary": "Operations professional with 12+ years owning support platforms.",
    "core_strengths": [
        {"label": "AI-assisted support", "value": "Zendesk AI Agents, intent design"},
        {"label": "Platform", "value": "Zendesk full admin, REST APIs"},
    ],
    "roles": [
        {"title": "Support Operations Manager", "company": "Acme SE",
         "dates": "Apr 2021 – Present", "location": "Cologne, Germany",
         "groups": [
             {"heading": "Service delivery",
              "bullets": ["Sole administrator of the Zendesk platform.",
                          "Identified a ~€300,000 fraud attempt."]},
         ]},
        {"title": "Technology Analyst", "company": "Infosys",
         "dates": "2014 – 2021", "location": "India & Germany",
         "bullets": ["L2/L3 application support."]},
    ],
    "education": ["B.Tech, Computer Science Engineering"],
    "awards": ["Hackathon winner"],
}

JOB = {"company": "Vodafone GmbH", "title": "System Specialist Operations"}


LANGUAGES = "English: Fluent  ·  Dutch: Elementary (A2)  ·  Tamil: Native"


def render(template_key="classic", brand=None, plan=None, languages=LANGUAGES):
    doc = documents.build_cv(plan or SAMPLE_PLAN, CONTACT,
                             template_key=template_key, brand=brand,
                             languages=languages)
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    tmp.close()
    doc.save(tmp.name)
    return Document(tmp.name), Path(tmp.name)


def paragraph_attrs(paragraph):
    pPr = paragraph._p.pPr
    shd = pPr.find(qn("w:shd")) if pPr is not None else None
    bdr = pPr.find(qn("w:pBdr")) if pPr is not None else None
    rule = None
    if bdr is not None:
        bottom = bdr.find(qn("w:bottom"))
        if bottom is not None:
            rule = "#" + bottom.get(qn("w:color")).upper()
    return {
        "shade": ("#" + shd.get(qn("w:fill")).upper()) if shd is not None else None,
        "rule": rule,
    }


def find_paragraph(doc, text):
    for paragraph in doc.paragraphs:
        if text.lower() in paragraph.text.lower():
            return paragraph
    return None


class TestPageGeometry(unittest.TestCase):
    """Margins are the style guide's DXA numbers, confirmed in the real PDFs."""

    def setUp(self):
        self.doc, self.path = render()

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_a4_page(self):
        section = self.doc.sections[0]
        self.assertAlmostEqual(section.page_width.pt, 595.3, delta=1)
        self.assertAlmostEqual(section.page_height.pt, 841.9, delta=1)

    def test_side_margins_are_1000_twips(self):
        section = self.doc.sections[0]
        self.assertEqual(tw(section.left_margin), 1000)
        self.assertEqual(tw(section.right_margin), 1000)

    def test_vertical_margins_are_900_twips(self):
        section = self.doc.sections[0]
        self.assertEqual(tw(section.top_margin), 900)
        self.assertEqual(tw(section.bottom_margin), 900)


class TestClassicFidelity(unittest.TestCase):
    """Targets measured from the Schneider Electric CV."""

    def setUp(self):
        self.doc, self.path = render("classic")

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_name_is_17pt_bold_navy_uppercase(self):
        para = self.doc.paragraphs[0]
        run = para.runs[0]
        self.assertEqual(run.font.size.pt, 17)
        self.assertTrue(run.font.bold)
        self.assertEqual(str(run.font.color.rgb), "1B4F8A")
        self.assertEqual(para.text, CONTACT["name"].upper())

    def test_body_is_10pt(self):
        para = find_paragraph(self.doc, "Operations professional")
        self.assertEqual(para.runs[0].font.size.pt, 10)

    def test_section_header_is_105pt_bold_navy_with_a_rule(self):
        para = find_paragraph(self.doc, "PROFESSIONAL EXPERIENCE")
        self.assertIsNotNone(para)
        self.assertEqual(para.runs[0].font.size.pt, 10.5)
        self.assertTrue(para.runs[0].font.bold)
        self.assertEqual(str(para.runs[0].font.color.rgb), "1B4F8A")
        self.assertEqual(paragraph_attrs(para)["rule"], "#1B4F8A")

    def test_bullets_use_the_480_240_indent(self):
        """The style guide's '480/240' — bullet glyph at x=62, text at x=74."""
        bullets = [p for p in self.doc.paragraphs if p.text.startswith("•")]
        self.assertTrue(bullets)
        for para in bullets:
            fmt = para.paragraph_format
            self.assertEqual(tw(fmt.left_indent), 480)
            self.assertEqual(tw(fmt.first_line_indent), -240)

    def test_contact_bar_uses_the_measured_tint(self):
        para = find_paragraph(self.doc, CONTACT["email"])
        self.assertEqual(paragraph_attrs(para)["shade"], "#EDF2F8")

    def test_subheadings_are_rendered(self):
        self.assertIsNotNone(find_paragraph(self.doc, "Service delivery"))

    def test_core_strengths_are_omitted(self):
        self.assertIsNone(find_paragraph(self.doc, "CORE STRENGTHS"))

    def test_role_header_is_a_borderless_two_column_table(self):
        self.assertTrue(self.doc.tables)
        table = self.doc.tables[0]
        self.assertEqual(len(table.columns), 2)
        self.assertIn("Support Operations Manager", table.cell(0, 0).text)
        self.assertIn("Apr 2021", table.cell(0, 1).text)


class TestCompactFidelity(unittest.TestCase):
    """Targets measured from the SumUp CV."""

    def setUp(self):
        self.doc, self.path = render("compact", brand="#159A9C")

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_name_is_19pt_and_not_uppercased(self):
        para = self.doc.paragraphs[0]
        self.assertEqual(para.runs[0].font.size.pt, 19)
        self.assertEqual(para.text, CONTACT["name"])

    def test_body_is_11pt(self):
        para = find_paragraph(self.doc, "Operations professional")
        self.assertEqual(para.runs[0].font.size.pt, 11)

    def test_core_strengths_are_rendered(self):
        self.assertIsNotNone(find_paragraph(self.doc, "CORE STRENGTHS"))
        self.assertIsNotNone(find_paragraph(self.doc, "AI-assisted support"))

    def test_subheadings_are_flattened_into_plain_bullets(self):
        self.assertIsNone(find_paragraph(self.doc, "Service delivery"))
        self.assertIsNotNone(find_paragraph(self.doc, "Sole administrator"))

    def test_bullets_use_the_tighter_indent(self):
        bullets = [p for p in self.doc.paragraphs if p.text.startswith("•")]
        self.assertTrue(bullets)
        self.assertNotEqual(tw(bullets[0].paragraph_format.left_indent), 480)

    def test_brand_colour_is_applied(self):
        para = find_paragraph(self.doc, "EXPERIENCE")
        self.assertEqual(str(para.runs[0].font.color.rgb), "159A9C")


class TestGuaranteesEnforcedInCode(unittest.TestCase):
    """Things the model is never trusted to get right."""

    def test_contact_details_come_from_the_profile_not_the_plan(self):
        hostile = dict(SAMPLE_PLAN)
        hostile["contact"] = {"email": "attacker@example.com", "phone": "+00 000"}
        doc, path = render(plan=hostile)
        try:
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn(CONTACT["email"], text)
            self.assertIn(CONTACT["phone"], text)
            self.assertNotIn("attacker@example.com", text)
        finally:
            path.unlink(missing_ok=True)

    def test_the_languages_line_is_rendered_verbatim(self):
        """Stating a level you do not have is the one unrecoverable CV error,
        so the line is copied exactly as the candidate wrote it."""
        doc, path = render()
        try:
            para = find_paragraph(doc, "Tamil")
            self.assertIsNotNone(para, "languages line missing")
            self.assertEqual(para.text, LANGUAGES)
        finally:
            path.unlink(missing_ok=True)

    def test_the_language_line_is_never_taken_from_the_plan(self):
        """Even if the model claims fluent Dutch, the profile wording wins."""
        hostile = dict(SAMPLE_PLAN)
        hostile["languages"] = "Dutch: Fluent (C2)"
        doc, path = render(plan=hostile)
        try:
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn(LANGUAGES, text)
            self.assertNotIn("C2", text)
        finally:
            path.unlink(missing_ok=True)

    def test_no_languages_line_when_the_profile_has_none(self):
        """Better to omit the section than to invent a default."""
        doc, path = render(languages="")
        try:
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertNotIn("Languages", text)
        finally:
            path.unlink(missing_ok=True)

    def test_linkedin_url_is_shown_in_full(self):
        doc, path = render()
        try:
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn(CONTACT["linkedin"], text)
        finally:
            path.unlink(missing_ok=True)


class TestConfidentialityScan(unittest.TestCase):
    def test_blocked_term_is_detected(self):
        self.assertEqual(
            documents.confidentiality_scan("Led BotConnect end to end"), ["BotConnect"])

    def test_detection_is_case_insensitive(self):
        self.assertEqual(documents.confidentiality_scan("led botconnect"), ["BotConnect"])

    def test_substring_of_a_longer_word_does_not_match(self):
        self.assertEqual(documents.confidentiality_scan("BotConnectivity layer"), [])

    def test_clean_text_passes(self):
        self.assertEqual(
            documents.confidentiality_scan("Built a REST API integration"), [])

    def test_custom_blocklist_is_honoured(self):
        self.assertEqual(
            documents.confidentiality_scan("Project Falcon shipped", ["Project Falcon"]),
            ["Project Falcon"])

    def test_plan_text_covers_nested_bullets(self):
        text = documents.plan_text(SAMPLE_PLAN)
        self.assertIn("Sole administrator", text)
        self.assertIn("Service delivery", text)
        self.assertIn("Operations professional", text)

    def test_a_blocked_term_hidden_in_a_bullet_is_caught(self):
        plan = {"roles": [{"title": "X", "groups": [
            {"heading": "H", "bullets": ["Led BotConnect delivery"]}]}]}
        self.assertEqual(
            documents.confidentiality_scan(documents.plan_text(plan)), ["BotConnect"])


class TestColourHandling(unittest.TestCase):
    def test_hex_is_normalised(self):
        self.assertEqual(doc_styles.normalise_hex("1b4f8a"), "#1B4F8A")
        self.assertEqual(doc_styles.normalise_hex("#abc"), "#AABBCC")

    def test_junk_falls_back_to_navy(self):
        self.assertEqual(doc_styles.normalise_hex("not-a-colour"), "#1B4F8A")
        self.assertEqual(doc_styles.normalise_hex(None), "#1B4F8A")

    def test_tint_lightens(self):
        self.assertEqual(len(doc_styles.tint("#1B4F8A")), 7)


class TestFilenames(unittest.TestCase):
    def test_slug_is_filesystem_safe(self):
        self.assertEqual(documents.slugify("Scheidt & Bachmann GmbH"),
                         "Scheidt_Bachmann_GmbH")

    def test_empty_input_still_yields_a_name(self):
        self.assertEqual(documents.slugify(""), "document")


class TestPortablePaths(unittest.TestCase):
    """A5: a document row has to mean the same thing on the Mac and the Pi.

    Rows written on the Mac held `/Users/karthik/...`, which the Pi's download
    route rejected as outside its output directory — even though the files had
    been synced across.
    """

    def test_a_path_inside_the_output_dir_is_stored_relative(self):
        stored = documents.store_path(documents.OUTPUT_DIR / "cv_Acme_v1.docx")
        self.assertEqual(stored, "cv_Acme_v1.docx")

    def test_a_relative_row_resolves_under_the_output_dir(self):
        resolved = documents.resolve_path("cv_Acme_v1.docx")
        self.assertEqual(resolved, documents.OUTPUT_DIR / "cv_Acme_v1.docx")

    def test_a_legacy_absolute_row_falls_back_to_the_basename(self):
        """The Mac paths already in the database must still download."""
        resolved = documents.resolve_path(
            "/Users/karthik/Documents/LocalCode/FirstApp/jobhunt/documents/"
            "cv_Gitlab_v4.docx")
        self.assertEqual(resolved, documents.OUTPUT_DIR / "cv_Gitlab_v4.docx")

    def test_a_legacy_absolute_row_that_still_exists_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "on_disk.docx"
            real.write_bytes(b"x")
            self.assertEqual(documents.resolve_path(str(real)), real)

    def test_a_path_outside_the_output_dir_is_stored_unchanged(self):
        """Tests write to a tmpdir; storing a mangled path would hide that."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cv.docx"
            self.assertEqual(documents.store_path(path), str(path))


class TestDocumentDownload(unittest.TestCase):
    """The route around A5: legacy rows must serve, traversal must not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        os.environ.pop("JOBHUNT_PASSWORD", None)
        db_module.init_db()
        self.conn = db_module.connect()
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, "
            "title, company, first_seen_at, last_seen_at, description_text, "
            "dedup_key, status, stage0_pass) VALUES "
            "(1, 1, 'e1', 'https://x/1', 'https://x/1', 'T', 'C', ?, ?, "
            "'desc', 'k1', 'new', 1)", (now_iso(), now_iso()))
        self.conn.commit()
        application = app_module.create_app()
        application.config["TESTING"] = True
        self.client = application.test_client()

    def tearDown(self):
        self.conn.close()
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def add_document(self, path):
        cur = self.conn.execute(
            "INSERT INTO documents (job_id, kind, version, format, path, "
            "created_at) VALUES (1, 'cv', 1, 'docx', ?, ?)", (path, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def test_a_legacy_mac_path_downloads_from_the_output_dir(self):
        real = documents.OUTPUT_DIR / "test_a5_legacy.docx"
        real.write_bytes(b"docx bytes")
        try:
            doc_id = self.add_document(
                "/Users/karthik/Documents/LocalCode/FirstApp/jobhunt/"
                "documents/test_a5_legacy.docx")
            response = self.client.get("/document/{0}".format(doc_id))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"docx bytes")
            response.close()   # send_file holds the handle open
        finally:
            real.unlink()

    def test_a_traversal_row_is_refused(self):
        doc_id = self.add_document("../../../../etc/passwd")
        self.assertEqual(
            self.client.get("/document/{0}".format(doc_id)).status_code, 403)

    def test_an_absolute_path_elsewhere_is_refused(self):
        doc_id = self.add_document("/etc/passwd")
        self.assertEqual(
            self.client.get("/document/{0}".format(doc_id)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
