"""
Kestrel Motor Group — Raw Consolidation (NO cleaning)
=====================================================
Reads all 52 feedback files according to source_registry.yaml,
renames columns to canonical names, tags every row with provenance
(site_code, source_file, source_sheet, source_row), and stacks
them into one DataFrame / CSV.

NO cleaning is performed:
  - No date parsing or normalisation
  - No rating scale conversion
  - No department standardisation
  - No deduplication
  - No PII handling
  - No row filtering (subtotals, banners, markers are kept as-is)
  - Values are kept exactly as read from the source files

The only transformations are:
  1. Column renaming (source column name → canonical name)
  2. Adding provenance columns
  3. Adding from_google_southport flag (0 or 1)
  4. For the shared North QLD file: adding a site_raw column copy

Usage:
    python consolidate.py --feedback-dir ./feedback --output ./consolidated_raw.csv
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Canonical output columns (order matters for the final CSV)
# ─────────────────────────────────────────────────────────────────────
CANONICAL_COLUMNS = [
    "site_code",
    "site_name",
    "site_status",
    "source_file",
    "source_sheet",
    "source_row",
    "from_google_southport",
    "segment_id",
    # --- data columns (raw, untouched) ---
    "date",
    "department",
    "rating_raw",
    "comment",
    "customer_name",
    "phone",
    "email",
    "customer_ref",
    "vehicle",
    "model",
    "rego",
    "booking_ref",
    "action_taken",
    "staff",
    "response_id",
    "source_detail",
    # --- extra raw columns kept for traceability ---
    "dealer_raw",   # Newcastle / Hunter Motors
    "site_raw",     # Ipswich, North QLD shared file
]


# ─────────────────────────────────────────────────────────────────────
# Site configurations — one entry per logical ingest unit
# ─────────────────────────────────────────────────────────────────────
# Each dict has:
#   site_code, name, status
#   file_pattern (regex), file_format (xlsx|csv), delimiter (csv only)
#   sheets: None (default/single sheet) or dict:
#       include_pattern: regex to match sheet names (optional)
#       load_all_non_empty: True → iterate all non-empty sheets
#       skip_if_empty: True → skip sheets with no data rows
#       sheet_name_to_column: canonical column to populate from sheet name
#           e.g. "department" for Essendon where sheets = Sales/Service/Parts
#   header_row_hint: 0-based int or None (auto-detect)
#   header_detect_must_contain: list of strings to scan for
#   column_map: {source_col: canonical_col}
#   segments: list of segment dicts (overrides column_map etc.)
#   from_google_southport: 0 or 1
#   exclude: bool
#   shared_with: site_code or None  (to avoid loading twice)

def build_site_configs():
    """Return a list of ingest-unit dicts derived from the registry."""
    configs = []

    # ── VIC01 Dandenong ──
    configs.append({
        "site_code": "VIC01",
        "name": "Dandenong",
        "status": "active",
        "file_pattern": r"^Dandenong Customer Feedback \d{4}-\d{2}\.xlsx$",
        "file_format": "xlsx",
        "sheets": {"include_pattern": r"^[A-Z][a-z]{2} \d{2}$", "skip_if_empty": True},
        "header_detect_must_contain": ["Date", "Dept"],
        "header_row_hint": None,  # use detection
        "column_map": {
            "Date": "date", "Dept": "department", "Customer": "customer_name",
            "Vehicle": "vehicle", "Rego": "rego", "Rating (1-5)": "rating_raw",
            "Comments": "comment", "Action taken": "action_taken",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC02 Ringwood (segmented) ──
    configs.append({
        "site_code": "VIC02",
        "name": "Ringwood",
        "status": "active",
        "file_pattern": r"^Ringwood feedback Q[1-4] \d{4}\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": None,
        "column_map": None,  # segments override
        "segments": [
            {
                "id": "old_template",
                "header_detect_must_contain": ["Date Received", "Department"],
                "column_map": {
                    "Date Received": "date", "Department": "department",
                    "Score (1-5)": "rating_raw", "Customer comments": "comment",
                    "Followed up?": "action_taken",
                },
            },
            {
                "id": "new_template",
                "header_detect_must_contain": ["Date", "Model", "Score (out of 10)"],
                "column_map": {
                    "Date": "date", "Department": "department", "Model": "model",
                    "Score (out of 10)": "rating_raw", "Customer comments": "comment",
                    "Follow up (Y/N)": "action_taken", "Staff": "staff",
                },
            },
        ],
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC03 Essendon ──
    # 3 sheets: Sales, Service, Parts — sheet name IS the department
    configs.append({
        "site_code": "VIC03",
        "name": "Essendon",
        "status": "active",
        "file_pattern": r"^ESSENDON - feedback register\.xlsx$",
        "file_format": "xlsx",
        "sheets": {
            "load_all_non_empty": True,
            "skip_if_empty": True,
            "sheet_name_to_column": "department",
        },
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Taken by": "staff", "Customer said": "comment",
            "Score": "rating_raw", "Action": "action_taken",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC04 Frankston (segmented — two layouts in one sheet) ──
    configs.append({
        "site_code": "VIC04",
        "name": "Frankston",
        "status": "active",
        "file_pattern": r"^Frankston_CX_tracker\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": None,
        "column_map": None,
        "segments": [
            {
                "id": "old_layout",
                "header_detect_must_contain": ["Date", "Area", "Rating", "Comments"],
                "column_map": {
                    "Date": "date", "Area": "department", "Rating": "rating_raw",
                    "Comments": "comment",
                },
            },
            {
                "id": "new_layout",
                "header_detect_must_contain": ["Date", "Area", "CSAT", "Customer comments", "Source"],
                "column_map": {
                    "Date": "date", "Area": "department", "CSAT": "rating_raw",
                    "Customer comments": "comment", "Source": "source_detail",
                    "Customer": "customer_name", "Rego": "rego",
                },
            },
        ],
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC05 Box Hill ──
    configs.append({
        "site_code": "VIC05",
        "name": "Box Hill",
        "status": "active",
        "file_pattern": r"^boxhill_survey_export_\d{4}Q[1-4]\.csv$",
        "file_format": "csv",
        "delimiter": ",",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "response_id": "response_id", "submitted_at": "date",
            "department": "department", "csat": "rating_raw",
            "verbatim": "comment", "vehicle": "vehicle",
            "dms_customer_no": "customer_ref",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC06 Geelong ──
    # 2 sheets: 2025, 2026 — both have the same layout with header detection
    configs.append({
        "site_code": "VIC06",
        "name": "Geelong",
        "status": "active",
        "file_pattern": r"^Geelong Feedback\.xlsx$",
        "file_format": "xlsx",
        "sheets": {"load_all_non_empty": True, "skip_if_empty": True},
        "header_detect_must_contain": ["Date", "Department", "Rating"],
        "header_row_hint": None,
        "column_map": {
            "Date": "date", "Department": "department", "Rating": "rating_raw",
            "Feedback": "comment", "Customer": "customer_name",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC07 Ballarat ──
    configs.append({
        "site_code": "VIC07",
        "name": "Ballarat",
        "status": "active",
        "file_pattern": r"^Ballarat - Customer Satisfaction\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": ["Date", "Dept", "Name", "Phone"],
        "header_row_hint": None,
        "column_map": {
            "Date": "date", "Dept": "department", "Name": "customer_name",
            "Phone": "phone", "How satisfied?": "rating_raw", "Comments": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC08 Bendigo ──
    configs.append({
        "site_code": "VIC08",
        "name": "Bendigo",
        "status": "active",
        "file_pattern": r"^bendigo feedback\.csv$",
        "file_format": "csv",
        "delimiter": ",",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Dept": "department", "Stars": "rating_raw",
            "What did they say?": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── VIC09 Shepparton (closed) ──
    configs.append({
        "site_code": "VIC09",
        "name": "Shepparton",
        "status": "closed",
        "file_pattern": r"^Shepparton feedback FINAL - site closed\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Dept": "department", "Rating": "rating_raw",
            "Comment": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW01 Parramatta ──
    configs.append({
        "site_code": "NSW01",
        "name": "Parramatta",
        "status": "active",
        "file_pattern": r"^Parramatta_Feedback_\d{4}-\d{2}\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Cust No": "customer_ref", "Mobile": "phone",
            "Department": "department", "Rating": "rating_raw",
            "Comments": "comment", "Staff": "staff", "Booking ref": "booking_ref",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW02 Penrith ──
    # 12 monthly sheets (Oct 25 … Sep 26), same layout as Dandenong
    configs.append({
        "site_code": "NSW02",
        "name": "Penrith",
        "status": "active",
        "file_pattern": r"^Penrith Feedback\.xlsx$",
        "file_format": "xlsx",
        "sheets": {"load_all_non_empty": True, "skip_if_empty": True},
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Department": "department", "Score": "rating_raw",
            "Comments": "comment", "Customer": "customer_name",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW03 Chatswood ──
    configs.append({
        "site_code": "NSW03",
        "name": "Chatswood",
        "status": "active",
        "file_pattern": r"^chatswood_\d{4}_q[1-4]\.csv$",
        "file_format": "csv",
        "delimiter": ";",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Dept": "department", "Score": "rating_raw",
            "Comment": "comment", "Vehicle": "vehicle",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW04 Liverpool ──
    configs.append({
        "site_code": "NSW04",
        "name": "Liverpool",
        "status": "active",
        "file_pattern": r"^liverpool_q[1-4]_\d{4}\.csv$",
        "file_format": "csv",
        "delimiter": ",",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Department": "department", "Customer": "customer_name",
            "Phone": "phone", "Rego": "rego", "Rating": "rating_raw",
            "Feedback": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW05 Newcastle (loaded from Hunter Motors file) ──
    configs.append({
        "site_code": "NSW05",
        "name": "Newcastle",
        "status": "active",
        "file_pattern": r"^Hunter Motors feedback 25-26\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": ["Dealer", "Date", "Department", "Rating"],
        "header_row_hint": None,
        "column_map": {
            "Dealer": "dealer_raw", "Date": "date", "Department": "department",
            "Rating": "rating_raw", "Comments": "comment", "Vehicle": "vehicle",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW06 Wollongong ──
    configs.append({
        "site_code": "NSW06",
        "name": "Wollongong",
        "status": "active",
        "file_pattern": r"^Wollongong_[A-Za-z]{3}\d{2}-[A-Za-z]{3}\d{2}\.csv$",
        "file_format": "csv",
        "delimiter": ",",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Dept": "department", "Rating": "rating_raw",
            "Customer feedback": "comment", "Name": "customer_name", "Mobile": "phone",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── NSW07 Tamworth ──
    configs.append({
        "site_code": "NSW07",
        "name": "Tamworth",
        "status": "active",
        "file_pattern": r"^Tamworth\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "DATE": "date", "DEPT": "department", "HOW WAS IT": "rating_raw",
            "COMMENTS": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── QLD01 Chermside ──
    configs.append({
        "site_code": "QLD01",
        "name": "Chermside",
        "status": "active",
        "file_pattern": r"^Chermside Q[1-4] \d{4}( FINAL( v2)?)?\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Ref": "response_id", "Date": "date",
            "department": "department", "Rating": "rating_raw",
            "Comments": "comment", "Customer No": "customer_ref",
            "Model": "model",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── QLD02 Springwood ──
    configs.append({
        "site_code": "QLD02",
        "name": "Springwood",
        "status": "active",
        "file_pattern": r"^Springwood feedback\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Department": "department", "Name": "customer_name",
            "Mobile": "phone", "Email": "email", "Rating": "rating_raw",
            "Comments": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── QLD03 Ipswich ──
    configs.append({
        "site_code": "QLD03",
        "name": "Ipswich",
        "status": "active",
        "file_pattern": r"^IPS_feedback_log\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Site": "site_raw", "Date": "date", "Dept": "department",
            "Rating": "rating_raw", "Notes": "comment", "Logged by": "staff",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── QLD04 Southport (segmented: two sheets) ──
    configs.append({
        "site_code": "QLD04",
        "name": "Southport",
        "status": "active",
        "file_pattern": r"^Southport Feedback Tracker\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,  # handled by segments
        "header_detect_must_contain": None,
        "header_row_hint": None,
        "column_map": None,
        "segments": [
            {
                "id": "dealership_feedback",
                "sheet": "Feedback",
                "header_row": 0,  # 0-based (registry says row 1 = 1-based)
                "from_google_southport": 0,
                "column_map": {
                    "Date": "date", "Department": "department",
                    "Rating": "rating_raw", "Comments": "comment",
                    "Vehicle": "vehicle", "Rego": "rego",
                    "Customer name": "customer_name", "Email": "email",
                },
            },
            {
                "id": "google_reviews",
                "sheet": "Google reviews (copied)",
                "header_row": 1,  # 0-based (registry says row 2 = 1-based)
                "from_google_southport": 1,
                "column_map": {
                    "Reviewer": "customer_name", "Stars": "rating_raw",
                    "Review": "comment", "When": "date",
                },
            },
        ],
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── QLD05 Noosa ──
    configs.append({
        "site_code": "QLD05",
        "name": "Noosa",
        "status": "active",
        "file_pattern": r"^Noosa feedback cards\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Department": "department", "Rating": "rating_raw",
            "Comments": "comment", "Card given by": "staff",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })

    # ── QLD06 Toowoomba (detail file only; summary excluded) ──
    configs.append({
        "site_code": "QLD06",
        "name": "Toowoomba",
        "status": "active",
        "file_pattern": r"^Toowoomba feedback 2026\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Date": "date", "Department": "department", "Rating": "rating_raw",
            "Comments": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": None,
    })
    # Excluded summary file — logged but not loaded
    configs.append({
        "site_code": "QLD06",
        "name": "Toowoomba",
        "status": "active",
        "file_pattern": r"^Toowoomba Q4 2025 summary\.xlsx$",
        "file_format": "xlsx",
        "sheets": None,
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {},
        "segments": None,
        "from_google_southport": 0,
        "exclude": True,   # <-- decision D2
        "shared_with": None,
    })

    # ── QLD07 Rockhampton + QLD08 Townsville (shared file) ──
    # Loaded once under QLD07; rows carry site_raw for later routing.
    # 4 quarterly sheets: Q4 2025, Q1 2026, Q2 2026, Q3 2026
    configs.append({
        "site_code": "QLD07",  # primary loader; site_raw column tells you which site
        "name": "Rockhampton+Townsville",
        "status": "active",
        "file_pattern": r"^North QLD feedback \(Rocky \+ TSV\)\.xlsx$",
        "file_format": "xlsx",
        "sheets": {"load_all_non_empty": True, "skip_if_empty": True},
        "header_detect_must_contain": None,
        "header_row_hint": 0,
        "column_map": {
            "Site": "site_raw", "Date": "date", "Department": "department",
            "Rating": "rating_raw", "Comments": "comment",
        },
        "segments": None,
        "from_google_southport": 0,
        "exclude": False,
        "shared_with": "QLD08",  # marks this as the canonical loader
    })

    return configs


# ─────────────────────────────────────────────────────────────────────
# File matching
# ─────────────────────────────────────────────────────────────────────

def find_matching_files(feedback_dir: Path, pattern: str) -> list[Path]:
    """Return all files in feedback_dir whose name matches the regex pattern."""
    regex = re.compile(pattern)
    matches = sorted(
        p for p in feedback_dir.iterdir()
        if p.is_file() and regex.match(p.name)
    )
    return matches


# ─────────────────────────────────────────────────────────────────────
# Header detection
# ─────────────────────────────────────────────────────────────────────

def detect_header_row(df_raw: pd.DataFrame, must_contain: list[str], max_scan: int = 15) -> int | None:
    """
    Scan the first `max_scan` rows of a raw (headerless) DataFrame
    to find the row whose cells contain all `must_contain` strings.
    Returns 0-based row index, or None if not found.
    """
    for i in range(min(max_scan, len(df_raw))):
        row_values = [str(v).strip() for v in df_raw.iloc[i].values]
        if all(any(mc in rv for rv in row_values) for mc in must_contain):
            return i
    return None


# ─────────────────────────────────────────────────────────────────────
# Sheet reading helpers
# ─────────────────────────────────────────────────────────────────────

def read_xlsx_sheet_raw(filepath: Path, sheet_name=0) -> pd.DataFrame:
    """Read an xlsx sheet with no header assumption (header=None)."""
    return pd.read_excel(filepath, sheet_name=sheet_name, header=None, dtype=str)


def read_xlsx_sheet(filepath: Path, sheet_name=0, header_row: int = 0) -> pd.DataFrame:
    """Read an xlsx sheet using a specific header row (0-based)."""
    return pd.read_excel(filepath, sheet_name=sheet_name, header=header_row, dtype=str)


def read_csv_file(filepath: Path, delimiter: str = ",", header_row: int = 0) -> pd.DataFrame:
    """Read a CSV with a specific delimiter and header row."""
    encodings = ["utf-8", "cp1252", "latin-1"]
    for enc in encodings:
        try:
            return pd.read_csv(
                filepath, sep=delimiter, header=header_row,
                dtype=str, encoding=enc, on_bad_lines="warn",
            )
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {filepath} with any of {encodings}")


# ─────────────────────────────────────────────────────────────────────
# Core: read one sheet/segment into a DataFrame with canonical columns
# ─────────────────────────────────────────────────────────────────────

def dedup_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    If a DataFrame has duplicate column names (common in messy spreadsheets),
    append _2, _3, ... to the duplicates so every column is unique.
    This prevents pandas concat from failing on non-unique indexes.
    """
    seen: dict[str, int] = {}
    new_cols = []
    for col in df.columns:
        col_str = str(col)
        if col_str in seen:
            seen[col_str] += 1
            new_cols.append(f"{col_str}_{seen[col_str]}")
        else:
            seen[col_str] = 1
            new_cols.append(col_str)
    df.columns = new_cols
    return df


def apply_column_map(df: pd.DataFrame, column_map: dict[str, str]) -> pd.DataFrame:
    """
    Rename columns that appear in column_map.
    Columns not in the map are kept with their original names prefixed with '_unmapped_'.
    """
    # First ensure unique column names so rename/concat don't break
    df = dedup_columns(df)

    rename = {}
    for src_col, canon_col in column_map.items():
        # Case-insensitive match against actual columns
        for actual_col in df.columns:
            if str(actual_col).strip().lower() == src_col.strip().lower():
                rename[actual_col] = canon_col
                break
    df = df.rename(columns=rename)

    # Prefix unmapped columns so they're preserved but distinguishable
    mapped_targets = set(rename.values())
    for col in df.columns:
        if col not in mapped_targets and col not in CANONICAL_COLUMNS:
            df = df.rename(columns={col: f"_unmapped_{col}"})

    return df


def add_provenance(df: pd.DataFrame, site_code: str, site_name: str,
                   site_status: str, source_file: str, source_sheet: str,
                   from_google_southport: int, segment_id: str | None,
                   row_offset: int = 0) -> pd.DataFrame:
    """Add provenance columns to every row."""
    df = df.copy()
    df["site_code"] = site_code
    df["site_name"] = site_name
    df["site_status"] = site_status
    df["source_file"] = source_file
    df["source_sheet"] = source_sheet if source_sheet else ""
    # source_row: 1-based position in the source sheet
    df["source_row"] = range(row_offset + 1, row_offset + 1 + len(df))
    df["from_google_southport"] = from_google_southport
    df["segment_id"] = segment_id if segment_id else ""
    return df


# ─────────────────────────────────────────────────────────────────────
# Per-site loaders
# ─────────────────────────────────────────────────────────────────────

def load_simple_xlsx(filepath: Path, cfg: dict, sheet_name=0) -> pd.DataFrame:
    """
    Load a simple xlsx file (one layout per sheet).
    If header_detect_must_contain is set, auto-detect header row.
    Otherwise use header_row_hint (default 0).
    """
    must_contain = cfg.get("header_detect_must_contain")

    if must_contain:
        raw = read_xlsx_sheet_raw(filepath, sheet_name=sheet_name)
        hdr_row = detect_header_row(raw, must_contain)
        if hdr_row is None:
            log.warning(f"  Header not detected in {filepath.name} sheet={sheet_name}; "
                        f"falling back to row 0. Expected: {must_contain}")
            hdr_row = 0
        else:
            log.info(f"  Header detected at row {hdr_row} in {filepath.name} sheet={sheet_name}")
        # Re-read with detected header
        df = read_xlsx_sheet(filepath, sheet_name=sheet_name, header_row=hdr_row)
        row_offset = hdr_row + 1  # data starts after header
    else:
        hdr_row = cfg.get("header_row_hint", 0) or 0
        df = read_xlsx_sheet(filepath, sheet_name=sheet_name, header_row=hdr_row)
        row_offset = hdr_row + 1

    return df, row_offset


def load_multi_sheet_xlsx(filepath: Path, cfg: dict) -> list[pd.DataFrame]:
    """
    Generic multi-sheet loader.  Iterates all (or pattern-filtered)
    non-empty sheets in the workbook, applies the site's column_map
    and header detection to each, and returns one DataFrame per sheet.

    Config options (in cfg["sheets"]):
      load_all_non_empty : True → iterate every sheet
      include_pattern    : regex → only sheets whose name matches
      skip_if_empty      : True → skip sheets with 0 data rows
      sheet_name_to_column : canonical column name (e.g. "department")
          → populate that column with the sheet name for every row
          (used for Essendon where sheets = Sales / Service / Parts)
    """
    sheets_cfg = cfg["sheets"]
    include_re = re.compile(sheets_cfg["include_pattern"]) if "include_pattern" in sheets_cfg else None

    frames = []
    xl = pd.ExcelFile(filepath)

    for sheet in xl.sheet_names:
        # Filter by pattern if one is set
        if include_re and not include_re.match(sheet):
            log.info(f"  Skipping sheet '{sheet}' (doesn't match pattern)")
            continue

        # Quick emptiness check (read headerless)
        raw = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=str)
        if sheets_cfg.get("skip_if_empty") and raw.dropna(how="all").empty:
            log.info(f"  Skipping sheet '{sheet}' (empty)")
            continue

        # Header detection
        must_contain = cfg.get("header_detect_must_contain")
        if must_contain:
            hdr_row = detect_header_row(raw, must_contain)
            if hdr_row is None:
                log.warning(f"  Header not found in sheet '{sheet}'; skipping")
                continue
        else:
            hdr_row = cfg.get("header_row_hint", 0) or 0

        df = pd.read_excel(xl, sheet_name=sheet, header=hdr_row, dtype=str)
        row_offset = hdr_row + 1

        df = apply_column_map(df, cfg["column_map"])

        # Populate a column from the sheet name if configured
        col_from_sheet = sheets_cfg.get("sheet_name_to_column")
        if col_from_sheet:
            df[col_from_sheet] = sheet

        df = add_provenance(
            df, cfg["site_code"], cfg["name"], cfg["status"],
            filepath.name, sheet, cfg["from_google_southport"], None,
            row_offset=row_offset,
        )
        frames.append(df)
        log.info(f"  Sheet '{sheet}': {len(df)} rows (header at row {hdr_row})")

    return frames


def load_segmented_single_sheet(filepath: Path, cfg: dict) -> list[pd.DataFrame]:
    """
    Ringwood / Frankston: one sheet with multiple layouts separated by
    a repeated header row or marker row. We detect each segment's header
    and split accordingly.
    """
    frames = []
    raw = read_xlsx_sheet_raw(filepath)

    segment_boundaries = []
    for seg in cfg["segments"]:
        must_contain = seg["header_detect_must_contain"]
        # Find ALL rows matching this segment's header
        for i in range(len(raw)):
            row_values = [str(v).strip() for v in raw.iloc[i].values]
            if all(any(mc in rv for rv in row_values) for mc in must_contain):
                segment_boundaries.append((i, seg))
                break  # take first match per segment

    # Sort by row position
    segment_boundaries.sort(key=lambda x: x[0])

    for idx, (start_row, seg) in enumerate(segment_boundaries):
        # End = start of next segment, or end of file
        if idx + 1 < len(segment_boundaries):
            end_row = segment_boundaries[idx + 1][0]
        else:
            end_row = len(raw)

        # The header is at start_row; data is start_row+1 to end_row
        header_values = [str(v).strip() for v in raw.iloc[start_row].values]
        data_slice = raw.iloc[start_row + 1: end_row].copy()
        data_slice.columns = header_values
        data_slice = data_slice.reset_index(drop=True)

        data_slice = apply_column_map(data_slice, seg["column_map"])
        fg = seg.get("from_google_southport", cfg["from_google_southport"])
        data_slice = add_provenance(
            data_slice, cfg["site_code"], cfg["name"], cfg["status"],
            filepath.name, "", fg, seg["id"],
            row_offset=start_row + 1,
        )
        frames.append(data_slice)
        log.info(f"  Segment '{seg['id']}': rows {start_row+1}-{end_row}, {len(data_slice)} data rows")

    return frames


def load_southport(filepath: Path, cfg: dict) -> list[pd.DataFrame]:
    """QLD04 Southport: two named sheets, each a separate segment."""
    frames = []
    xl = pd.ExcelFile(filepath)

    for seg in cfg["segments"]:
        sheet = seg["sheet"]
        if sheet not in xl.sheet_names:
            log.warning(f"  Sheet '{sheet}' not found in {filepath.name}; skipping")
            continue

        hdr_row = seg.get("header_row", 0)
        df = pd.read_excel(xl, sheet_name=sheet, header=hdr_row, dtype=str)
        df = apply_column_map(df, seg["column_map"])
        fg = seg.get("from_google_southport", 0)
        df = add_provenance(
            df, cfg["site_code"], cfg["name"], cfg["status"],
            filepath.name, sheet, fg, seg["id"],
            row_offset=hdr_row + 1,
        )
        frames.append(df)
        log.info(f"  Sheet '{sheet}' (segment '{seg['id']}'): {len(df)} rows")

    return frames


# ─────────────────────────────────────────────────────────────────────
# Main consolidation loop
# ─────────────────────────────────────────────────────────────────────

def consolidate(feedback_dir: Path) -> pd.DataFrame:
    """Read all files per registry config and return one stacked DataFrame."""
    configs = build_site_configs()
    all_frames: list[pd.DataFrame] = []
    files_loaded = 0
    files_skipped = 0
    files_not_found = []
    already_loaded: set[str] = set()  # to avoid double-loading shared files

    for cfg in configs:
        pattern = cfg["file_pattern"]
        site_code = cfg["site_code"]
        site_name = cfg["name"]

        # Skip excluded files (e.g. Toowoomba summary)
        if cfg.get("exclude"):
            log.info(f"[{site_code}] EXCLUDED by decision: pattern={pattern}")
            files_skipped += 1
            continue

        matched_files = find_matching_files(feedback_dir, pattern)

        if not matched_files:
            # Chermside format uncertainty: try .csv if .xlsx not found
            if site_code == "QLD01":
                csv_pattern = pattern.replace(r"\.xlsx$", r"\.(xlsx|csv)$")
                matched_files = find_matching_files(feedback_dir, csv_pattern)

            if not matched_files:
                log.warning(f"[{site_code} {site_name}] No files matched pattern: {pattern}")
                files_not_found.append((site_code, site_name, pattern))
                continue

        for filepath in matched_files:
            # Avoid double-loading shared files
            if filepath.name in already_loaded:
                log.info(f"[{site_code}] Skipping {filepath.name} (already loaded by shared partner)")
                continue
            already_loaded.add(filepath.name)

            log.info(f"[{site_code} {site_name}] Loading {filepath.name}")

            try:
                frames = _load_one_file(filepath, cfg)
                for f in frames:
                    all_frames.append(f)
                files_loaded += 1
            except Exception as e:
                log.error(f"[{site_code}] FAILED to load {filepath.name}: {e}")
                # Continue with other files

    log.info(f"\n{'='*60}")
    log.info(f"Files loaded: {files_loaded}")
    log.info(f"Files skipped (excluded): {files_skipped}")
    log.info(f"Files not found: {len(files_not_found)}")
    for sc, sn, pat in files_not_found:
        log.info(f"  {sc} {sn}: {pat}")

    if not all_frames:
        log.warning("No data loaded!")
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    combined = pd.concat(all_frames, ignore_index=True, sort=False)

    # Reorder: canonical columns first, then any unmapped extras
    ordered_cols = [c for c in CANONICAL_COLUMNS if c in combined.columns]
    extra_cols = sorted(c for c in combined.columns if c not in CANONICAL_COLUMNS)
    combined = combined[ordered_cols + extra_cols]

    log.info(f"Total rows consolidated: {len(combined)}")
    log.info(f"Columns: {list(combined.columns)}")

    return combined


def _load_one_file(filepath: Path, cfg: dict) -> list[pd.DataFrame]:
    """Dispatch to the right loader based on config."""
    site_code = cfg["site_code"]

    # ── Multi-sheet xlsx (Dandenong, Essendon, Geelong, Penrith, North QLD) ──
    if cfg.get("sheets") and cfg["file_format"] == "xlsx":
        return load_multi_sheet_xlsx(filepath, cfg)

    # ── Segmented xlsx (Ringwood, Frankston, Southport) ──
    if cfg.get("segments") and cfg["file_format"] == "xlsx":
        # Southport has named sheets per segment
        if site_code == "QLD04":
            return load_southport(filepath, cfg)
        # Ringwood / Frankston: segments within a single sheet
        return load_segmented_single_sheet(filepath, cfg)

    # ── CSV files ──
    if cfg["file_format"] == "csv":
        delimiter = cfg.get("delimiter", ",")
        hdr_row = cfg.get("header_row_hint", 0) or 0
        df = read_csv_file(filepath, delimiter=delimiter, header_row=hdr_row)
        df = apply_column_map(df, cfg["column_map"])
        df = add_provenance(
            df, cfg["site_code"], cfg["name"], cfg["status"],
            filepath.name, "", cfg["from_google_southport"], None,
            row_offset=hdr_row + 1,
        )
        return [df]

    # ── Simple xlsx (single sheet, possibly with header detection) ──
    df, row_offset = load_simple_xlsx(filepath, cfg)
    df = apply_column_map(df, cfg["column_map"])
    df = add_provenance(
        df, cfg["site_code"], cfg["name"], cfg["status"],
        filepath.name, "", cfg["from_google_southport"], None,
        row_offset=row_offset,
    )
    return [df]


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Consolidate Kestrel Motor Group feedback files (raw, no cleaning)"
    )
    parser.add_argument(
        "--feedback-dir", type=Path, default=Path("feedback"),
        help="Path to the folder containing the 52 feedback files",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("consolidated_raw.csv"),
        help="Path for the output CSV",
    )
    parser.add_argument(
        "--parquet", action="store_true",
        help="Also save as .parquet alongside the CSV",
    )
    args = parser.parse_args()

    if not args.feedback_dir.is_dir():
        log.error(f"Feedback directory not found: {args.feedback_dir}")
        sys.exit(1)

    combined = consolidate(args.feedback_dir)

    # Save CSV
    combined.to_csv(args.output, index=False, encoding='utf-8-sig')
    log.info(f"Saved CSV: {args.output} ({os.path.getsize(args.output) / 1024:.0f} KB)")

    # Optionally save parquet
    if args.parquet:
        pq_path = args.output.with_suffix(".parquet")
        combined.to_parquet(pq_path, index=False)
        log.info(f"Saved Parquet: {pq_path}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"CONSOLIDATION COMPLETE")
    print(f"{'='*60}")
    print(f"Total rows:    {len(combined):,}")
    print(f"Total columns: {len(combined.columns)}")
    print(f"Output file:   {args.output}")
    print(f"\nRows per site:")
    if "site_code" in combined.columns:
        print(combined.groupby(["site_code", "site_name"]).size()
              .reset_index(name="rows").to_string(index=False))
    print(f"\nSource files loaded:")
    if "source_file" in combined.columns:
        for f in sorted(combined["source_file"].unique()):
            n = len(combined[combined["source_file"] == f])
            print(f"  {f}: {n:,} rows")


if __name__ == "__main__":
    main()