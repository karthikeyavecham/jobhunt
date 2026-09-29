"""Import and export for the application tracker.

Export exists so the tracker is never a one-way trip into a SQLite file on an
SD card — `.xlsx` for reading and sharing, JSON for anything programmatic.

Import exists because twelve applications already lived in
a spreadsheet before this app existed, and a tracker
that starts empty is a tracker with a hole in its history. The importer maps
loosely: spreadsheet headers vary, so columns are matched on normalised names
with several accepted spellings each, and anything unrecognised is reported
rather than silently dropped.
"""

import io
import json
import logging
import re
from datetime import date, datetime

from . import tracker
from .util import now_iso

log = logging.getLogger(__name__)

EXPORT_COLUMNS = [
    ("company", "Company"),
    ("title", "Role"),
    ("pipeline_status", "Status"),
    ("fit_score", "Fit"),
    ("shortlisted_at", "Shortlisted"),
    ("applied_at", "Applied"),
    ("next_action", "Next action"),
    ("next_action_date", "Due"),
    ("contact_name", "Contact"),
    ("contact_email", "Contact email"),
    ("referral", "Referral"),
    ("location", "Location"),
    ("url", "Posting"),
    ("notes", "Notes"),
]


def export_rows(conn):
    """The tracker as a list of plain dicts."""
    rows = []
    for row in tracker.all_applications(conn):
        rows.append({key: row[key] for key, _ in EXPORT_COLUMNS if key in row.keys()})
    return rows


def to_json(conn):
    return json.dumps(
        {"exported_at": now_iso(), "applications": export_rows(conn)},
        indent=2, ensure_ascii=False).encode("utf-8")


def to_xlsx(conn):
    """A real .xlsx, styled to be readable rather than just machine-parseable."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Applications"

    header_fill = PatternFill("solid", fgColor="1B4F8A")
    header_font = Font(color="FFFFFF", bold=True, name="Arial", size=10)
    for index, (_, label) in enumerate(EXPORT_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index, value=label)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center")

    for row_index, record in enumerate(export_rows(conn), start=2):
        for col_index, (key, _) in enumerate(EXPORT_COLUMNS, start=1):
            value = record.get(key)
            cell = sheet.cell(row=row_index, column=col_index, value=value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=(key == "notes"))

    widths = {"company": 26, "title": 40, "pipeline_status": 14, "fit_score": 6,
              "shortlisted_at": 20, "applied_at": 12, "next_action": 28,
              "next_action_date": 12, "contact_name": 18, "contact_email": 26,
              "referral": 18, "location": 22, "url": 44, "notes": 40}
    for index, (key, _) in enumerate(EXPORT_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(key, 18)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

#: Header spellings seen in the wild, normalised to our field names. Matching
#: is on lowercase alphanumerics only, so "Job Title", "job_title" and
#: "JobTitle" all collapse to the same key.
HEADER_ALIASES = {
    "company": "company", "employer": "company", "organisation": "company",
    "organization": "company", "firma": "company",
    "role": "title", "position": "title", "jobtitle": "title", "title": "title",
    "job": "title", "stelle": "title",
    "status": "pipeline_status", "stage": "pipeline_status",
    "pipelinestatus": "pipeline_status", "applicationstatus": "pipeline_status",
    "applied": "applied_at", "applieddate": "applied_at",
    "dateapplied": "applied_at", "applicationdate": "applied_at",
    "appliedon": "applied_at",
    "nextaction": "next_action", "action": "next_action",
    "nextactiondate": "next_action_date", "due": "next_action_date",
    "followup": "next_action_date", "followupdate": "next_action_date",
    "contact": "contact_name", "contactname": "contact_name",
    "recruiter": "contact_name",
    "contactemail": "contact_email", "email": "contact_email",
    "referral": "referral", "referredby": "referral",
    "notes": "notes", "comments": "notes", "note": "notes",
    "link": "url", "posting": "url", "url": "url", "joburl": "url",
    "location": "location", "city": "location", "ort": "location",
}

#: Free-text statuses mapped onto the pipeline. Anything unmatched falls back
#: to "applied", since a row in someone's tracker means they applied.
STATUS_ALIASES = {
    "applied": "applied", "submitted": "applied", "sent": "applied",
    "inprogress": "applied", "pending": "applied", "open": "applied",
    "shortlisted": "shortlisted", "tosend": "shortlisted", "todo": "shortlisted",
    "draft": "shortlisted", "prepared": "shortlisted",
    "screening": "screening", "phonescreen": "screening", "hrscreen": "screening",
    "interview": "interview", "interviewing": "interview",
    "firstinterview": "interview", "secondinterview": "interview",
    "offer": "offer", "offered": "offer",
    "rejected": "rejected", "declined": "rejected", "no": "rejected",
    "unsuccessful": "rejected", "closed": "rejected",
    "withdrawn": "withdrawn", "cancelled": "withdrawn",
}


def _norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def map_headers(headers):
    """(field -> column index, unrecognised header labels)."""
    mapping, unknown = {}, []
    for index, header in enumerate(headers):
        if header is None or not str(header).strip():
            continue
        field = HEADER_ALIASES.get(_norm(header))
        if field is None:
            unknown.append(str(header).strip())
        elif field not in mapping:
            mapping[field] = index
    return mapping, unknown


def normalise_status(value):
    return STATUS_ALIASES.get(_norm(value), "applied")


def normalise_date(value):
    """Accept datetimes, dates, and the usual string shapes. None if unusable."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y",
                    "%d-%m-%Y", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def read_table(data, filename=""):
    """Read an .xlsx or .csv upload into (headers, rows)."""
    name = (filename or "").lower()
    if name.endswith(".csv") or (data[:1] not in (b"P",) and not name.endswith(".xlsx")):
        import csv

        text = data.decode("utf-8-sig", errors="replace")
        # Sniff the delimiter: German exports frequently use semicolons.
        sample = text[:4000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = list(csv.reader(io.StringIO(text), dialect))
        if not reader:
            return [], []
        return reader[0], reader[1:]

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sheet = workbook.active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    workbook.close()
    if not rows:
        return [], []
    return rows[0], rows[1:]


class ImportResult(object):
    def __init__(self):
        self.imported = 0
        self.updated = 0
        self.skipped = []          # (row number, reason)
        self.unknown_headers = []
        self.mapped_fields = []

    def summary(self):
        parts = ["{0} imported".format(self.imported)]
        if self.updated:
            parts.append("{0} updated".format(self.updated))
        if self.skipped:
            parts.append("{0} skipped".format(len(self.skipped)))
        return ", ".join(parts)


def import_applications(conn, data, filename="", source_name="Imported tracker"):
    """Import a tracker spreadsheet into jobs + applications.

    Each row becomes a job (so it has a title, company and detail page) plus an
    application row. Rows are matched to existing entries by company + title,
    so re-importing a corrected spreadsheet updates rather than duplicates.
    """
    from . import dedup, ingest
    from .sources.base import RawJob

    headers, rows = read_table(data, filename)
    if not headers:
        raise ValueError("That file has no header row")

    mapping, unknown = map_headers(headers)
    result = ImportResult()
    result.unknown_headers = unknown
    result.mapped_fields = sorted(mapping)

    if "company" not in mapping and "title" not in mapping:
        raise ValueError(
            "Could not find a Company or Role column. Headers seen: {0}".format(
                ", ".join(str(h) for h in headers if h)[:200]))

    def cell(row, field):
        index = mapping.get(field)
        if index is None or index >= len(row):
            return None
        value = row[index]
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    for number, row in enumerate(rows, start=2):
        if not any(c not in (None, "") for c in row):
            continue
        company = cell(row, "company")
        title = cell(row, "title")
        if not (company or title):
            result.skipped.append((number, "no company or role"))
            continue

        company = company or "Unknown company"
        title = title or "Unspecified role"
        url = cell(row, "url") or ""

        key = dedup.dedup_key(company, title, cell(row, "location"))
        existing = conn.execute(
            "SELECT id FROM jobs WHERE dedup_key = ?", (key,)).fetchone()

        if existing:
            job_id = existing["id"]
            result.updated += 1
        else:
            raw = RawJob(
                external_id="import-" + key[:16],
                url=url, title=title, company=company,
                location=cell(row, "location"),
                description_text=cell(row, "notes") or
                "Imported from an existing tracker; no posting text.",
                raw={"_imported": True, "row": number},
            )
            job_id = ingest.store_manual(conn, raw)
            result.imported += 1

        tracker.update(
            conn, job_id, backfill_applied=False,
            pipeline_status=normalise_status(cell(row, "pipeline_status")),
            applied_at=normalise_date(cell(row, "applied_at")),
            next_action=cell(row, "next_action"),
            next_action_date=normalise_date(cell(row, "next_action_date")),
            contact_name=cell(row, "contact_name"),
            contact_email=cell(row, "contact_email"),
            referral=cell(row, "referral"),
            notes=cell(row, "notes"),
        )

    log.info("tracker import: %s", result.summary())
    return result
