"""
Extract structured fields from Sales Arrangement PDFs with pdfplumber.

For each PDF we try to read (from the full text):
  - property_name : English development / phase name
  - issue_date    : "Date of Issue (...)"   -> normalized to YYYY-MM-DD
  - revision_date : "Date of Revision (...)"-> normalized to YYYY-MM-DD
  - sa_no         : "Sales Arrangements No. X" or "(No.X)" -> e.g. 38, 12H, 1A

Rules (per the agreed spec):
  - revision_date takes precedence over issue_date when present;
    if several revision dates exist, keep the latest one.
  - sa_no is left empty if it cannot be found in the text.
  - All dates are normalized to YYYY-MM-DD; unparseable dates stay empty.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple, List

import pdfplumber

# ---------------------------------------------------------------------------
# Field: property_name
# ---------------------------------------------------------------------------
# Lines look like:
#   "Name of the Development: The Haddon"          (new format)
#   "Name of the Solaria 嘉熙"                      (old format, name in middle)
#   "Name of the Phase: Phase 2 of Grand YOHO ..." (phase docs)
#   "Name of the Phase of the KT Marina 1"
_CJK = re.compile(r"[\u4e00-\u9fff\uff00-\uffef]+")


def extract_property_name(text: str) -> Optional[str]:
    for raw in text.split("\n"):
        line = raw.strip()
        if not line.startswith("Name of"):
            continue
        # Drop CJK characters first.
        line = _CJK.sub("", line).strip()
        if ":" in line:
            # "Name of the Development: <name>"  or  "Name of the Phase: <name>"
            after = line.split(":", 1)[1]
        else:
            # "Name of the <name> Development" -> strip leading "Name of the"
            # and the trailing "Development" label.
            after = re.sub(r"^Name of (?:the |this )?", "", line)
            after = re.sub(r"\s*Development\s*:?\s*$", "", after)
        after = after.strip(" ^:,|\t")
        if after:
            return after
    return None


# ---------------------------------------------------------------------------
# Field: dates
# ---------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], start=1)}

# A single date token in any of the three formats the PDFs use:
#   31/10/2023  or  14-5-19      (numeric, / or -)
#   7-June-2024                  (hyphenated month)
#   15 April 2022                (spaced month, e.g. on combined lines)
_DATE_TOKEN = (
    r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|\d{1,2}-[A-Za-z]+-\d{4}"
    r"|\d{1,2}\s+[A-Za-z]+\s+\d{4}"
)
_RE_DATE_TOKEN = re.compile(_DATE_TOKEN)


def normalize_date(raw: Optional[str]) -> Optional[str]:
    """Convert one raw date string to YYYY-MM-DD, or None if unparseable."""
    if not raw:
        return None
    s = raw.strip()
    # D-Month-YYYY  e.g. 7-June-2024
    m = re.match(r"(\d{1,2})-([A-Za-z]+)-(\d{4})$", s)
    if m:
        day, mon, year = m.groups()
        mo = _month_num(mon)
        if mo:
            return f"{int(year):04d}-{mo:02d}-{int(day):02d}"
    # D Month YYYY  e.g. 15 April 2022
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$", s)
    if m:
        day, mon, year = m.groups()
        mo = _month_num(mon)
        if mo:
            return f"{int(year):04d}-{mo:02d}-{int(day):02d}"
    # D/M/YYYY or D-M-YY  e.g. 31/10/2023, 14/5/2019
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$", s)
    if m:
        day, mo, year = m.groups()
        year_i = int(year)
        year_i = 2000 + year_i if year_i < 100 else year_i
        return f"{year_i:04d}-{int(mo):02d}-{int(day):02d}"
    return None


def _month_num(mon: str) -> int:
    """Resolve a month name (full or abbreviated) to 1-12, else 0."""
    mon = mon.lower()
    return next((v for k, v in _MONTHS.items() if k.startswith(mon[:3])), 0)


def _parse_all_dates(segment: str) -> List[str]:
    """Return all YYYY-MM-DD dates found in `segment` (in order of appearance)."""
    return [d for d in (normalize_date(m.group(0)) for m in _RE_DATE_TOKEN.finditer(segment)) if d]


def extract_dates(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (issue_date, latest_revision_date), both normalized.

    Handles three layouts found in the PDFs:
      1. "Date of Issue (發出日期): 31/10/2023"          (issue only)
      2. "Date of Revision (修改日期): 28/3/2024"        (revision only)
      3. "Date of Issue Revision: 14 Mar 2019 26 Apr..." (combined line:
         first date = issue, remaining dates = revisions)
    """
    issue = None
    revisions: List[str] = []

    for line in text.split("\n"):
        s = line.strip()
        if not re.search(r"Date of (Issue|Revision)", s, re.I):
            continue
        after = s.split(":", 1)[1] if ":" in s else ""
        # Split the label portion to tell issue vs revision apart.
        label = s.split(":", 1)[0].lower() if ":" in s else s.lower()
        dates = _parse_all_dates(after)
        if not dates:
            continue
        if "revision" in label and "issue" not in label:
            # Pure revision line.
            revisions.extend(dates)
        elif "issue" in label and "revision" in label:
            # Combined line: first date is the issue, the rest are revisions.
            issue = issue or dates[0]
            revisions.extend(dates[1:])
        else:
            # Pure issue line.
            issue = issue or dates[0]

    latest_rev = max(revisions) if revisions else None
    return issue, latest_rev


# ---------------------------------------------------------------------------
# Field: sa_no
# ---------------------------------------------------------------------------
# Two known shapes:
#   "Information on the Sales Arrangements Sales Arrangements No. 1"
#   "Sales Arrangements No. 12H"
#   "Information on Sales Arrangements (No.38)"
_RE_SA_INLINE = re.compile(r"Sales Arrangements?\s*No\.?\s*([0-9]+[A-Za-z]?)", re.I)
_RE_SA_PAREN = re.compile(r"Sales Arrangements?\s*\(No\.?\s*([0-9]+[A-Za-z]?)\)", re.I)


def extract_sa_no(text: str) -> Optional[str]:
    for pattern in (_RE_SA_INLINE, _RE_SA_PAREN):
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Top-level extraction
# ---------------------------------------------------------------------------
def extract_from_pdf(path: str) -> dict:
    """Read `path` with pdfplumber and return the extracted fields.

    Returns dict with keys: property_name, issue_date, revision_date, sa_no,
    date (revision_date if present else issue_date), error.
    """
    result = {
        "property_name": None,
        "issue_date": None,
        "revision_date": None,
        "sa_no": None,
        "date": None,
        "error": None,
    }
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if not text.strip():
        result["error"] = "no extractable text"
        return result

    result["property_name"] = extract_property_name(text)
    issue, latest_rev = extract_dates(text)
    result["issue_date"] = issue
    result["revision_date"] = latest_rev
    result["date"] = latest_rev or issue
    result["sa_no"] = extract_sa_no(text)
    return result
