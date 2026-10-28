"""
Kestrel Motor Group — Data Cleaning Pipeline
=============================================
Takes consolidated_raw.csv (from consolidate.py) and produces
consolidated_clean.csv with all cleaning, validation, and
standardisation applied.

Steps:
  1.  Drop junk rows (all-null data, banners, subtotals, markers)
  2.  Chermside dedup (FINAL vs FINAL v2 → keep v2)
  3.  Drop _unmapped_ columns
  4.  Parse & normalise dates → YYYY-MM-DD
  5.  Standardise departments → Sales | Service | Parts | Unknown
  6.  Normalise ratings → rating_std on 1-5 scale (5 = best)
  7.  Route North QLD rows → QLD07 / QLD08
  8.  Validate Ipswich site_raw
  9.  Frankston subtotal reconciliation → log then drop
  10. Dedup on (site_code, date, department, rating_raw, comment_hash)
  11. PII redaction (hash names/phones/emails/rego; scan comments)
  12. Add derived columns (rating_scale, date_quality)
  13. Assign feedback_id (site_code + sequence, e.g. VIC01-00001)

Outputs:
  - consolidated_clean.csv   — the cleaned dataset
  - data_quality_errors.csv  — one row per error, linked to feedback_id
  - rejects.csv              — rows removed entirely

Usage:
    python clean.py --input consolidated_raw.csv --output consolidated_clean.csv
"""

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Error log collector
# ─────────────────────────────────────────────────────────────────────

_error_log: list[dict] = []
_error_counter: int = 0


def log_error(
    *,
    step: int,
    error_code: str,
    error_description: str,
    severity: str = "warning",
    action_taken: str = "",
    field: str = "",
    original_value: str = "",
    feedback_id: str = "",
    source_file: str = "",
    source_row: str = "",
    site_code: str = "",
):
    """Append one error record to the in-memory error log."""
    global _error_counter
    _error_counter += 1
    _error_log.append({
        "error_id": f"ERR-{_error_counter:05d}",
        "feedback_id": feedback_id,
        "site_code": site_code,
        "source_file": source_file,
        "source_row": source_row,
        "step": step,
        "error_code": error_code,
        "severity": severity,
        "field": field,
        "original_value": str(original_value)[:200],  # truncate long values
        "error_description": error_description,
        "action_taken": action_taken,
    })

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

DATA_COLUMNS = [
    "date", "department", "rating_raw", "comment", "customer_name",
    "phone", "email", "customer_ref", "vehicle", "model", "rego",
    "booking_ref", "action_taken", "staff", "response_id",
    "source_detail", "dealer_raw", "site_raw",
]

PROVENANCE_COLUMNS = [
    "site_code", "site_name", "site_status", "source_file",
    "source_sheet", "source_row", "from_google_southport", "segment_id",
]

# Patterns for junk rows (matched against the first non-null cell)
JUNK_PATTERNS = [
    re.compile(r"^Grand Total", re.IGNORECASE),
    re.compile(r"^\*\*\*"),
    re.compile(r"^NOTE:", re.IGNORECASE),
]

# Frankston subtotal pattern — captured separately for reconciliation
SUBTOTAL_PATTERN = re.compile(r"^Total\s+[A-Za-z]{3}\s+\d{4}", re.IGNORECASE)

# ─────────────────────────────────────────────────────────────────────
# Department mapping
# ─────────────────────────────────────────────────────────────────────

DEPARTMENT_MAP = {
    # Sales variants
    "sales": "Sales",
    "s": "Sales",
    "new vehicle sales": "Sales",
    "new vehicles": "Sales",
    "new car sales": "Sales",
    "new cars": "Sales",
    "sales - new": "Sales",
    "sales - used": "Sales",
    "used vehicle sales": "Sales",
    "used vehicles": "Sales",
    "used car sales": "Sales",
    "used cars": "Sales",
    "fleet": "Sales",
    "fleet sales": "Sales",
    "vehicle sales": "Sales",
    "showroom": "Sales",
    # Service variants
    "service": "Service",
    "sv": "Service",
    "svc": "Service",
    "service dept": "Service",
    "service department": "Service",
    "servicing": "Service",
    "workshop": "Service",
    "mechanical": "Service",
    "repairs": "Service",
    "repair": "Service",
    "body shop": "Service",
    "bodyshop": "Service",
    "panel": "Service",
    "panel shop": "Service",
    "warranty": "Service",
    # Parts variants
    "parts": "Parts",
    "p": "Parts",
    "p&a": "Parts",
    "parts counter": "Parts",
    "parts dept": "Parts",
    "parts department": "Parts",
    "accessories": "Parts",
    "parts & accessories": "Parts",
    "parts and accessories": "Parts",
    "spare parts": "Parts",
    "spares": "Parts",
}

# ─────────────────────────────────────────────────────────────────────
# Text rating → numeric lookup
# ─────────────────────────────────────────────────────────────────────

TEXT_RATING_MAP = {
    # 5-point text scale (5 = best)
    "excellent": 5,
    "very good": 4,
    "good": 4,
    "great": 5,
    "fantastic": 5,
    "outstanding": 5,
    "satisfied": 4,
    "very satisfied": 5,
    "ok": 3,
    "okay": 3,
    "average": 3,
    "fair": 3,
    "neutral": 3,
    "satisfactory": 3,
    "poor": 2,
    "bad": 1,
    "very poor": 1,
    "terrible": 1,
    "awful": 1,
    "very bad": 1,
    "dissatisfied": 2,
    "very dissatisfied": 1,
    "unsatisfied": 2,
    "unhappy": 2,
    "happy": 4,
    "very happy": 5,
}

# ─────────────────────────────────────────────────────────────────────
# PII patterns for scanning comments
# ─────────────────────────────────────────────────────────────────────

PII_PATTERNS = {
    "phone": re.compile(
        r"\b(?:(?:\+?61|0)[\s.-]?)?(?:\(0\d\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b"
    ),
    "email": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
    "rego": re.compile(
        r"\b[A-Z]{2,3}[\s-]?\d{2,3}[\s-]?[A-Z]{0,3}\b"
    ),
}

# ─────────────────────────────────────────────────────────────────────
# North QLD site routing patterns
# ─────────────────────────────────────────────────────────────────────

QLD07_PATTERNS = re.compile(
    r"^(ROCK|ROCKY|ROK|ROCKHAMPTON)$", re.IGNORECASE
)
QLD08_PATTERNS = re.compile(
    r"^(TSV|TOWNSVILLE)$", re.IGNORECASE
)

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def sha256_short(value: str, length: int = 12) -> str:
    """Return a truncated SHA-256 hex digest for PII hashing."""
    if pd.isna(value) or str(value).strip() == "":
        return value
    return hashlib.sha256(str(value).strip().lower().encode()).hexdigest()[:length]


def first_notnull_cell(row: pd.Series, cols: list[str]) -> str | None:
    """Return the string value of the first non-null cell in the given columns."""
    for col in cols:
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            return str(val).strip()
    return None


# ─────────────────────────────────────────────────────────────────────
# Step 1: Drop junk rows
# ─────────────────────────────────────────────────────────────────────

def drop_junk_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Drop rows where:
      - All data columns are null/empty
      - First non-null cell matches a junk pattern (Grand Total, ***, NOTE:)
    Returns (cleaned_df, rejected_df).
    """
    rejects = []

    # 1a. All-null data rows
    data_cols = [c for c in DATA_COLUMNS if c in df.columns]
    all_null_mask = df[data_cols].replace("", np.nan).isna().all(axis=1)
    null_count = all_null_mask.sum()
    if null_count > 0:
        log.info(f"  Step 1a: Dropping {null_count} all-null data rows")
        null_rows = df[all_null_mask].copy()
        null_rows["reject_reason"] = "all_data_columns_null"
        rejects.append(null_rows)
        df = df[~all_null_mask].copy()

    # 1b. Banner / marker rows
    junk_mask = pd.Series(False, index=df.index)
    for idx, row in df.iterrows():
        first_cell = first_notnull_cell(row, data_cols)
        if first_cell:
            for pat in JUNK_PATTERNS:
                if pat.search(first_cell):
                    junk_mask.at[idx] = True
                    break

    junk_count = junk_mask.sum()
    if junk_count > 0:
        log.info(f"  Step 1b: Dropping {junk_count} banner/marker rows")
        junk_rows = df[junk_mask].copy()
        junk_rows["reject_reason"] = "banner_or_marker"
        rejects.append(junk_rows)
        df = df[~junk_mask].copy()

    rejected = pd.concat(rejects, ignore_index=True) if rejects else pd.DataFrame()
    return df, rejected


# ─────────────────────────────────────────────────────────────────────
# Step 2: Chermside dedup (FINAL vs FINAL v2)
# ─────────────────────────────────────────────────────────────────────

def chermside_dedup(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    For Chermside Q1 2026: if both FINAL and FINAL v2 files are present,
    keep v2 rows, drop FINAL rows (identified by source_file).
    Returns (df, dropped_count).
    """
    chermside_mask = df["site_code"] == "QLD01"
    chermside = df[chermside_mask]

    final_file = None
    v2_file = None
    for f in chermside["source_file"].unique():
        if "FINAL v2" in str(f):
            v2_file = f
        elif "FINAL" in str(f) and "v2" not in str(f):
            final_file = f

    if final_file and v2_file:
        # Both exist — drop the FINAL (non-v2) rows
        drop_mask = (df["site_code"] == "QLD01") & (df["source_file"] == final_file)
        dropped = drop_mask.sum()
        log.info(f"  Step 2: Chermside dedup — dropping {dropped} rows from '{final_file}' (keeping '{v2_file}')")
        df = df[~drop_mask].copy()
        return df, dropped
    else:
        log.info("  Step 2: Chermside dedup — no duplicate FINAL/v2 files found; skipping")
        return df, 0


# ─────────────────────────────────────────────────────────────────────
# Step 3: Drop _unmapped_ columns
# ─────────────────────────────────────────────────────────────────────

def drop_unmapped_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Remove columns whose name starts with '_unmapped_'."""
    unmapped = [c for c in df.columns if str(c).startswith("_unmapped_")]
    if unmapped:
        log.info(f"  Step 3: Dropping {len(unmapped)} unmapped columns: {unmapped}")
        df = df.drop(columns=unmapped)
    return df


# ─────────────────────────────────────────────────────────────────────
# Step 4: Date parsing & normalisation
# ─────────────────────────────────────────────────────────────────────

def parse_relative_date(text: str, reference_date: datetime | None = None) -> tuple[str | None, str]:
    """
    Parse relative date strings like '3 weeks ago', '2 months ago'.
    Returns (YYYY-MM-DD or None, quality).
    reference_date defaults to 2026-09-30 (end of reporting period).
    """
    if reference_date is None:
        reference_date = datetime(2026, 9, 30)

    text_lower = text.lower().strip()
    match = re.match(r"(\d+)\s+(day|week|month|year)s?\s+ago", text_lower)
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        if unit == "day":
            dt = reference_date - timedelta(days=n)
        elif unit == "week":
            dt = reference_date - timedelta(weeks=n)
        elif unit == "month":
            dt = reference_date - timedelta(days=n * 30)
        elif unit == "year":
            dt = reference_date - timedelta(days=n * 365)
        else:
            return None, "unparseable"
        return dt.strftime("%Y-%m-%d"), "approximate"

    # "a week ago", "a month ago"
    match2 = re.match(r"(?:a|an)\s+(day|week|month|year)\s+ago", text_lower)
    if match2:
        unit = match2.group(1)
        return parse_relative_date(f"1 {unit} ago", reference_date)

    return None, "unparseable"


def parse_excel_serial(value) -> str | None:
    """Convert Excel date serial number to YYYY-MM-DD."""
    try:
        num = float(value)
        if 40000 < num < 50000:  # reasonable Excel date range
            # Excel epoch: 1899-12-30
            dt = datetime(1899, 12, 30) + timedelta(days=num)
            return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def parse_date_value(value, site_code: str = "") -> tuple[str | None, str]:
    """
    Attempt to parse a date value into YYYY-MM-DD.
    Returns (parsed_date, quality) where quality is
    'exact', 'approximate', or 'unparseable'.

    Handles:
      - ISO format (YYYY-MM-DD)
      - AU format (DD/MM/YYYY, DD-MM-YYYY)
      - US format avoided: assumes DD/MM for ambiguous dates
      - Excel serial numbers
      - Relative text ('3 weeks ago')
      - Timestamps with time component
    """
    if pd.isna(value) or str(value).strip() == "":
        return None, "unparseable"

    raw = str(value).strip()

    # Try Excel serial first (pure numeric)
    if re.match(r"^\d{5}(\.\d+)?$", raw):
        result = parse_excel_serial(raw)
        if result:
            return result, "exact"

    # Try relative text
    if "ago" in raw.lower():
        return parse_relative_date(raw)

    # Strip time component if present
    raw_date = re.split(r"\s+\d{1,2}:\d{2}", raw)[0].strip()
    # Also handle ISO datetime with T
    raw_date = raw_date.split("T")[0].strip()

    # Try several date formats
    formats_exact = [
        ("%Y-%m-%d", "exact"),        # ISO
        ("%d/%m/%Y", "exact"),        # AU
        ("%d-%m-%Y", "exact"),        # AU with dashes
        ("%d/%m/%y", "exact"),        # AU short year
        ("%d-%m-%y", "exact"),        # AU short year dashes
        ("%Y/%m/%d", "exact"),        # ISO with slashes
        ("%d %b %Y", "exact"),        # 15 Jan 2026
        ("%d %B %Y", "exact"),        # 15 January 2026
        ("%b %d, %Y", "exact"),       # Jan 15, 2026
        ("%B %d, %Y", "exact"),       # January 15, 2026
        ("%d.%m.%Y", "exact"),        # European dot format
    ]

    for fmt, quality in formats_exact:
        try:
            dt = datetime.strptime(raw_date, fmt)
            # Sanity check: should be within Oct 2025 – Sep 2026 (± buffer)
            if datetime(2024, 1, 1) <= dt <= datetime(2027, 12, 31):
                return dt.strftime("%Y-%m-%d"), quality
        except ValueError:
            continue

    # Last resort: pandas parser (dayfirst=True for AU convention)
    try:
        dt = pd.to_datetime(raw_date, dayfirst=True)
        if pd.notna(dt) and datetime(2024, 1, 1) <= dt.to_pydatetime() <= datetime(2027, 12, 31):
            return dt.strftime("%Y-%m-%d"), "exact"
    except (ValueError, TypeError, pd.errors.OutOfBoundsDatetime):
        pass

    return None, "unparseable"


def normalise_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Parse all date values; add date_parsed and date_quality columns."""
    parsed_dates = []
    qualities = []

    for idx, row in df.iterrows():
        site = row.get("site_code", "")
        raw_date = row.get("date", "")
        parsed, quality = parse_date_value(raw_date, site_code=site)
        parsed_dates.append(parsed)
        qualities.append(quality)

        if quality == "unparseable" and pd.notna(raw_date) and str(raw_date).strip():
            log_error(
                step=4, error_code="DATE_UNPARSEABLE",
                error_description=f"Could not parse date value '{raw_date}'",
                severity="warning", action_taken="Set date_parsed to NaT",
                field="date", original_value=raw_date,
                site_code=str(site),
                source_file=str(row.get("source_file", "")),
                source_row=str(row.get("source_row", "")),
            )
        elif quality == "approximate":
            log_error(
                step=4, error_code="DATE_APPROXIMATE",
                error_description=f"Relative date '{raw_date}' converted to approximate date {parsed}",
                severity="info", action_taken=f"Estimated as {parsed}",
                field="date", original_value=raw_date,
                site_code=str(site),
                source_file=str(row.get("source_file", "")),
                source_row=str(row.get("source_row", "")),
            )

    df = df.copy()
    df["date_parsed"] = parsed_dates
    df["date_quality"] = qualities

    exact_count = sum(1 for q in qualities if q == "exact")
    approximate = sum(1 for q in qualities if q == "approximate")
    unparseable = sum(1 for q in qualities if q == "unparseable")
    log.info(f"  Step 4: Dates — {exact_count} exact, "
             f"{approximate} approximate, {unparseable} unparseable")

    return df


# ─────────────────────────────────────────────────────────────────────
# Step 5: Standardise departments
# ─────────────────────────────────────────────────────────────────────

def standardise_departments(df: pd.DataFrame) -> pd.DataFrame:
    """Map raw department values to Sales | Service | Parts | Unknown."""
    df = df.copy()
    df["department_raw"] = df["department"]

    def map_dept(val):
        if pd.isna(val) or str(val).strip() == "":
            return "Unknown"
        cleaned = str(val).strip().lower()
        return DEPARTMENT_MAP.get(cleaned, "Unknown")

    df["department"] = df["department_raw"].apply(map_dept)

    # Log unmapped values — both to console and error log
    unknown_mask = (df["department"] == "Unknown") & df["department_raw"].notna() & (df["department_raw"].str.strip() != "")
    if unknown_mask.any():
        unmapped_vals = df.loc[unknown_mask, "department_raw"].str.strip().str.lower().unique()
        log.warning(f"  Step 5: {unknown_mask.sum()} rows mapped to Unknown. "
                    f"Unmapped raw values: {sorted(set(unmapped_vals))}")
        for idx in df[unknown_mask].index:
            row = df.loc[idx]
            log_error(
                step=5, error_code="DEPT_UNMAPPED",
                error_description=f"Department '{row['department_raw']}' not in lookup table",
                severity="warning", action_taken="Set department to 'Unknown'",
                field="department", original_value=row["department_raw"],
                site_code=str(row.get("site_code", "")),
                source_file=str(row.get("source_file", "")),
                source_row=str(row.get("source_row", "")),
            )

    # Log missing department
    missing_mask = df["department_raw"].isna() | (df["department_raw"].str.strip() == "")
    missing_mask = missing_mask & (df["department"] == "Unknown")
    if missing_mask.any():
        for idx in df[missing_mask].index:
            row = df.loc[idx]
            log_error(
                step=5, error_code="DEPT_MISSING",
                error_description="Department value is blank or missing",
                severity="warning", action_taken="Set department to 'Unknown'",
                field="department", original_value="",
                site_code=str(row.get("site_code", "")),
                source_file=str(row.get("source_file", "")),
                source_row=str(row.get("source_row", "")),
            )

    counts = df["department"].value_counts()
    log.info(f"  Step 5: Department distribution — {counts.to_dict()}")

    return df


# ─────────────────────────────────────────────────────────────────────
# Step 6: Normalise ratings → rating_std (1-5, 5 = best)
# ─────────────────────────────────────────────────────────────────────

def normalise_ratings(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert all ratings to a standardised 1-5 scale (5 = best).
    Handles:
      - Numeric on 1-5 scale (most sites): keep as-is
      - Geelong (VIC06): inverted scale (1=best) → 6 - raw
      - Ringwood new template (VIC02 segment new_template): 1-10 → raw / 2
      - Text ratings: lookup table → numeric
      - Stars emoji/text: extract number
    """
    df = df.copy()
    rating_std = []
    rating_scales = []

    for _, row in df.iterrows():
        raw = row.get("rating_raw", "")
        site = row.get("site_code", "")
        segment = row.get("segment_id", "")

        if pd.isna(raw) or str(raw).strip() == "":
            rating_std.append(np.nan)
            rating_scales.append("")
            log_error(
                step=6, error_code="RATING_MISSING",
                error_description="Rating value is blank or missing",
                severity="info", action_taken="Set rating_std to NaN",
                field="rating_raw", original_value="",
                site_code=str(site),
                source_file=str(row.get("source_file", "")),
                source_row=str(row.get("source_row", "")),
            )
            continue

        raw_str = str(raw).strip()

        # Try to extract a number
        numeric_val = None

        # Check for star emoji (★ or ⭐)
        star_count = raw_str.count("★") + raw_str.count("⭐")
        if star_count > 0:
            numeric_val = float(star_count)
        else:
            # Try parsing as number (handle decimal commas like '4,5')
            cleaned_num = raw_str.replace(",", ".")
            try:
                numeric_val = float(cleaned_num)
            except ValueError:
                pass

        # If still no number, try text lookup
        if numeric_val is None:
            text_lower = raw_str.lower().strip()
            if text_lower in TEXT_RATING_MAP:
                numeric_val = float(TEXT_RATING_MAP[text_lower])
                rating_std.append(numeric_val)
                rating_scales.append("text_lookup→1-5")
                continue
            else:
                # Unparseable rating
                rating_std.append(np.nan)
                rating_scales.append("unparseable")
                log_error(
                    step=6, error_code="RATING_UNPARSEABLE",
                    error_description=f"Could not parse rating value '{raw_str}'",
                    severity="warning", action_taken="Set rating_std to NaN",
                    field="rating_raw", original_value=raw_str,
                    site_code=str(site),
                    source_file=str(row.get("source_file", "")),
                    source_row=str(row.get("source_row", "")),
                )
                continue

        # Apply site-specific transformations
        if site == "VIC06":
            # Geelong: inverted scale (1 = Excellent, 5 = Poor)
            # rating_std = 6 - raw
            std = 6.0 - numeric_val
            std = max(1.0, min(5.0, std))  # clamp
            rating_std.append(std)
            rating_scales.append("1-5_inverted")
        elif site == "VIC02" and segment == "new_template":
            # Ringwood new template: 1-10 scale → divide by 2
            std = numeric_val / 2.0
            std = max(1.0, min(5.0, std))  # clamp
            rating_std.append(round(std, 1))
            rating_scales.append("1-10→1-5")
        else:
            # Default: assume 1-5 scale, 5 = best
            std = max(1.0, min(5.0, numeric_val))  # clamp to 1-5
            rating_std.append(std)
            rating_scales.append("1-5")

    df["rating_std"] = rating_std
    df["rating_scale"] = rating_scales

    valid = sum(1 for r in rating_std if not (isinstance(r, float) and np.isnan(r)))
    log.info(f"  Step 6: Ratings — {valid} parsed, "
             f"{len(rating_std) - valid} missing/unparseable")

    return df


# ─────────────────────────────────────────────────────────────────────
# Step 7: Route North QLD → QLD07 / QLD08
# ─────────────────────────────────────────────────────────────────────

def route_north_qld(df: pd.DataFrame) -> pd.DataFrame:
    """
    Split rows from the shared North QLD file into QLD07 (Rockhampton)
    and QLD08 (Townsville) based on site_raw column.
    """
    df = df.copy()
    nqld_mask = (df["site_code"] == "QLD07") & df["site_raw"].notna()
    nqld_count = nqld_mask.sum()

    if nqld_count == 0:
        log.info("  Step 7: No North QLD rows to route")
        return df

    routed = 0
    unrouted = 0

    for idx in df[nqld_mask].index:
        site_val = str(df.at[idx, "site_raw"]).strip()
        if QLD07_PATTERNS.match(site_val):
            df.at[idx, "site_code"] = "QLD07"
            df.at[idx, "site_name"] = "Rockhampton"
            routed += 1
        elif QLD08_PATTERNS.match(site_val):
            df.at[idx, "site_code"] = "QLD08"
            df.at[idx, "site_name"] = "Townsville"
            routed += 1
        else:
            unrouted += 1
            log.warning(f"  Step 7: Unrouted North QLD row — site_raw='{site_val}' (row {idx})")
            log_error(
                step=7, error_code="NQLD_UNROUTED",
                error_description=f"North QLD row could not be routed — site_raw='{site_val}' does not match Rockhampton or Townsville patterns",
                severity="warning", action_taken="Left as QLD07 (default)",
                field="site_raw", original_value=site_val,
                site_code="QLD07",
                source_file=str(df.at[idx, "source_file"]) if "source_file" in df.columns else "",
                source_row=str(df.at[idx, "source_row"]) if "source_row" in df.columns else "",
            )

    log.info(f"  Step 7: North QLD routing — {routed} routed, {unrouted} unrouted out of {nqld_count}")
    return df


# ─────────────────────────────────────────────────────────────────────
# Step 8: Validate Ipswich site_raw
# ─────────────────────────────────────────────────────────────────────

def validate_ipswich(df: pd.DataFrame) -> pd.DataFrame:
    """Assert that all Ipswich (QLD03) rows have site_raw matching 'Ipswich'."""
    ips_mask = df["site_code"] == "QLD03"
    if not ips_mask.any():
        return df

    # Known Ipswich codes/labels (case-insensitive)
    ipswich_aliases = {"ipswich", "kmg-ips", "ips", ""}

    ips_rows = df[ips_mask]
    if "site_raw" in ips_rows.columns:
        non_ipswich = ips_rows[
            ips_rows["site_raw"].notna() &
            (~ips_rows["site_raw"].str.strip().str.lower().isin(ipswich_aliases))
        ]
        if len(non_ipswich) > 0:
            bad_vals = non_ipswich["site_raw"].unique()
            log.warning(f"  Step 8: {len(non_ipswich)} Ipswich rows have unexpected "
                        f"site_raw values: {list(bad_vals)}")
            for idx in non_ipswich.index:
                row = df.loc[idx]
                log_error(
                    step=8, error_code="IPSWICH_SITE_MISMATCH",
                    error_description=f"Ipswich (QLD03) row has unexpected site_raw='{row['site_raw']}'",
                    severity="warning", action_taken="Kept row, flagged for review",
                    field="site_raw", original_value=str(row["site_raw"]),
                    site_code="QLD03",
                    source_file=str(row.get("source_file", "")),
                    source_row=str(row.get("source_row", "")),
                )
        else:
            log.info(f"  Step 8: Ipswich validation passed ({len(ips_rows)} rows)")
    return df


# ─────────────────────────────────────────────────────────────────────
# Step 9: Frankston subtotal reconciliation
# ─────────────────────────────────────────────────────────────────────

def reconcile_frankston_subtotals(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Find Frankston subtotal rows (matching 'Total MMM YYYY' pattern),
    compare them against computed values for that month, log the
    comparison, and drop the subtotal rows.
    """
    frankston_mask = df["site_code"] == "VIC04"
    if not frankston_mask.any():
        return df, pd.DataFrame()

    data_cols = [c for c in DATA_COLUMNS if c in df.columns]

    # Find subtotal rows
    subtotal_mask = pd.Series(False, index=df.index)
    for idx in df[frankston_mask].index:
        first_cell = first_notnull_cell(df.loc[idx], data_cols)
        if first_cell and SUBTOTAL_PATTERN.match(first_cell):
            subtotal_mask.at[idx] = True

    subtotal_count = subtotal_mask.sum()
    if subtotal_count == 0:
        log.info("  Step 9: No Frankston subtotals found")
        return df, pd.DataFrame()

    subtotal_rows = df[subtotal_mask].copy()
    subtotal_rows["reject_reason"] = "frankston_subtotal"
    log.info(f"  Step 9: Found {subtotal_count} Frankston subtotal rows — "
             f"logged for reconciliation and dropping")

    # TODO: Extract month and expected avg/count from subtotal rows,
    # compare against computed values for that month. For now, log and drop.

    df = df[~subtotal_mask].copy()
    return df, subtotal_rows


# ─────────────────────────────────────────────────────────────────────
# Step 10: Dedup
# ─────────────────────────────────────────────────────────────────────

def compute_comment_hash(comment) -> str:
    """Hash a comment for dedup purposes."""
    if pd.isna(comment) or str(comment).strip() == "":
        return ""
    normalised = re.sub(r"\s+", " ", str(comment).strip().lower())
    return hashlib.md5(normalised.encode()).hexdigest()[:16]


def dedup_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    Remove duplicate rows based on:
      site_code + date_parsed + department + rating_raw + comment_hash
    If response_id is present and non-null, use it as the primary key instead.
    """
    df = df.copy()
    df["_comment_hash"] = df["comment"].apply(compute_comment_hash)

    # Build dedup key
    dedup_cols = ["site_code", "date_parsed", "department", "rating_raw", "_comment_hash"]
    # For rows with response_id, use that as a tighter key
    has_resp_id = df["response_id"].notna() & (df["response_id"].str.strip() != "")

    before = len(df)

    # Dedup rows WITH response_id on (site_code, response_id)
    if has_resp_id.any():
        df_with_id = df[has_resp_id].drop_duplicates(
            subset=["site_code", "response_id"], keep="last"
        )
        df_without_id = df[~has_resp_id].drop_duplicates(
            subset=dedup_cols, keep="last"
        )
        df = pd.concat([df_with_id, df_without_id], ignore_index=True)
    else:
        df = df.drop_duplicates(subset=dedup_cols, keep="last")

    df = df.drop(columns=["_comment_hash"])
    dropped = before - len(df)
    log.info(f"  Step 10: Dedup — dropped {dropped} duplicate rows "
             f"({before} → {len(df)})")
    return df, dropped


# ─────────────────────────────────────────────────────────────────────
# Step 11: PII redaction
# ─────────────────────────────────────────────────────────────────────

def redact_pii(df: pd.DataFrame) -> pd.DataFrame:
    """
    Hash/redact PII columns and scan comments for embedded PII.
    """
    df = df.copy()

    # Hash PII columns
    pii_cols = ["customer_name", "phone", "email", "rego"]
    for col in pii_cols:
        if col in df.columns:
            non_null = df[col].notna().sum()
            df[col] = df[col].apply(sha256_short)
            if non_null > 0:
                log.info(f"  Step 11: Hashed {non_null} non-null values in '{col}'")

    # Scan comments for embedded PII and redact
    if "comment" in df.columns:
        pii_found = 0
        redacted_comments = []
        for idx, comment in enumerate(df["comment"]):
            if pd.isna(comment) or str(comment).strip() == "":
                redacted_comments.append(comment)
                continue

            text = str(comment)
            found_any = False
            found_types = []
            for pii_type, pattern in PII_PATTERNS.items():
                matches = pattern.findall(text)
                if matches:
                    found_any = True
                    found_types.append(pii_type)
                    for m in matches:
                        text = text.replace(m, f"[REDACTED_{pii_type.upper()}]")
            if found_any:
                pii_found += 1
                row = df.iloc[idx]
                log_error(
                    step=11, error_code="PII_IN_COMMENT",
                    error_description=f"Comment contains embedded PII ({', '.join(found_types)})",
                    severity="info", action_taken=f"Redacted {', '.join(found_types)} patterns",
                    field="comment", original_value="[redacted]",
                    site_code=str(row.get("site_code", "")),
                    source_file=str(row.get("source_file", "")),
                    source_row=str(row.get("source_row", "")),
                )
            redacted_comments.append(text)

        df["comment"] = redacted_comments
        if pii_found > 0:
            log.info(f"  Step 11: Redacted PII from {pii_found} comments")

    return df


# ─────────────────────────────────────────────────────────────────────
# Step 12: Derived columns
# ─────────────────────────────────────────────────────────────────────

def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add rating_scale and date_quality columns (already added in steps 4 & 6)."""
    # These are already added by normalise_dates and normalise_ratings.
    # This step ensures they're present and adds any final columns.
    df = df.copy()

    # Add year-month for time-series analysis
    if "date_parsed" in df.columns:
        df["year_month"] = df["date_parsed"].apply(
            lambda d: d[:7] if pd.notna(d) and isinstance(d, str) and len(d) >= 7 else None
        )

    # Ensure from_google_southport is integer
    if "from_google_southport" in df.columns:
        df["from_google_southport"] = pd.to_numeric(
            df["from_google_southport"], errors="coerce"
        ).fillna(0).astype(int)

    return df


# ─────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────

def clean(input_path: Path, output_path: Path, rejects_path: Path | None = None,
          errors_path: Path | None = None):
    """Run the full cleaning pipeline."""
    global _error_log, _error_counter
    _error_log = []
    _error_counter = 0

    log.info(f"Loading {input_path}")
    df = pd.read_csv(input_path, dtype=str, low_memory=False, encoding="utf-8")
    log.info(f"Loaded {len(df):,} rows, {len(df.columns)} columns")

    all_rejects: list[pd.DataFrame] = []

    # Step 1: Drop junk rows
    log.info("Step 1: Dropping junk rows...")
    df, rejects1 = drop_junk_rows(df)
    if len(rejects1) > 0:
        all_rejects.append(rejects1)

    # Step 2: Chermside dedup
    log.info("Step 2: Chermside dedup...")
    df, chermside_dropped = chermside_dedup(df)

    # Step 3: Drop unmapped columns
    log.info("Step 3: Dropping unmapped columns...")
    df = drop_unmapped_columns(df)

    # Step 4: Parse dates
    log.info("Step 4: Parsing dates...")
    df = normalise_dates(df)

    # Step 5: Standardise departments
    log.info("Step 5: Standardising departments...")
    df = standardise_departments(df)

    # Step 6: Normalise ratings
    log.info("Step 6: Normalising ratings...")
    df = normalise_ratings(df)

    # Step 7: Route North QLD
    log.info("Step 7: Routing North QLD...")
    df = route_north_qld(df)

    # Step 8: Validate Ipswich
    log.info("Step 8: Validating Ipswich...")
    df = validate_ipswich(df)

    # Step 9: Frankston subtotals
    log.info("Step 9: Frankston subtotal reconciliation...")
    df, rejects9 = reconcile_frankston_subtotals(df)
    if len(rejects9) > 0:
        all_rejects.append(rejects9)

    # Step 10: Dedup
    log.info("Step 10: Dedup...")
    df, dedup_dropped = dedup_rows(df)

    # Step 11: PII redaction
    log.info("Step 11: PII redaction...")
    df = redact_pii(df)

    # Step 12: Derived columns
    log.info("Step 12: Adding derived columns...")
    df = add_derived_columns(df)

    # Step 13: Assign stable feedback_id (after all drops/dedup/routing)
    log.info("Step 13: Assigning feedback_id...")
    df = df.sort_values(["site_code", "date_parsed", "source_file", "source_row"]).reset_index(drop=True)
    df["feedback_id"] = df.apply(
        lambda r: f"{r['site_code']}-{r.name + 1:05d}", axis=1
    )
    log.info(f"  Step 13: Assigned {len(df):,} feedback IDs "
             f"({df['feedback_id'].iloc[0]} … {df['feedback_id'].iloc[-1]})")

    # ── Back-fill feedback_id into error log ──
    # Build a lookup from (source_file, source_row) → feedback_id
    fid_lookup = {}
    for _, row in df[["feedback_id", "source_file", "source_row"]].iterrows():
        key = (str(row["source_file"]), str(row["source_row"]))
        fid_lookup[key] = row["feedback_id"]

    for err in _error_log:
        if not err["feedback_id"]:
            key = (err["source_file"], err["source_row"])
            err["feedback_id"] = fid_lookup.get(key, "")

    # ── Save error log ──
    if errors_path and _error_log:
        errors_df = pd.DataFrame(_error_log)
        errors_df.to_csv(errors_path, index=False)
        log.info(f"Saved error log: {errors_path} ({len(errors_df):,} errors)")
    elif errors_path:
        log.info("No data quality errors recorded")

    # ── Final column ordering ──
    priority_cols = [
        "feedback_id",
        "site_code", "site_name", "site_status",
        "source_file", "source_sheet", "source_row",
        "from_google_southport", "segment_id",
        "date", "date_parsed", "date_quality", "year_month",
        "department", "department_raw",
        "rating_raw", "rating_std", "rating_scale",
        "comment",
        "customer_name", "phone", "email", "customer_ref",
        "vehicle", "model", "rego",
        "booking_ref", "action_taken", "staff",
        "response_id", "source_detail",
        "dealer_raw", "site_raw",
    ]
    ordered = [c for c in priority_cols if c in df.columns]
    extras = sorted(c for c in df.columns if c not in priority_cols)
    df = df[ordered + extras]

    # ── Save outputs ──
    df.to_csv(output_path, index=False)
    log.info(f"Saved clean data: {output_path} ({len(df):,} rows)")

    if rejects_path and all_rejects:
        rejected = pd.concat(all_rejects, ignore_index=True)
        rejected.to_csv(rejects_path, index=False)
        log.info(f"Saved rejects: {rejects_path} ({len(rejected):,} rows)")

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"CLEANING COMPLETE")
    print(f"{'='*60}")
    print(f"Input rows:           {pd.read_csv(input_path, nrows=0).__len__() if False else 'see log'}")
    print(f"Output rows:          {len(df):,}")
    print(f"Chermside dedup:      {chermside_dropped} dropped")
    print(f"General dedup:        {dedup_dropped} dropped")
    print(f"Rejects:              {sum(len(r) for r in all_rejects):,}")
    print(f"\nRows per site:")
    if "site_code" in df.columns:
        print(df.groupby(["site_code", "site_name"]).size()
              .reset_index(name="rows").to_string(index=False))
    print(f"\nDepartment distribution:")
    print(df["department"].value_counts().to_string())
    print(f"\nRating stats (rating_std):")
    if "rating_std" in df.columns:
        numeric_ratings = pd.to_numeric(df["rating_std"], errors="coerce")
        print(f"  Count:  {numeric_ratings.notna().sum():,}")
        print(f"  Mean:   {numeric_ratings.mean():.2f}")
        print(f"  Median: {numeric_ratings.median():.1f}")
        print(f"  Min:    {numeric_ratings.min():.1f}")
        print(f"  Max:    {numeric_ratings.max():.1f}")
    print(f"\nDate quality:")
    if "date_quality" in df.columns:
        print(df["date_quality"].value_counts().to_string())

    if _error_log:
        print(f"\nData quality errors: {len(_error_log):,}")
        err_df = pd.DataFrame(_error_log)
        print("  By error code:")
        print(err_df["error_code"].value_counts().to_string())
        print("  By severity:")
        print(err_df["severity"].value_counts().to_string())

    return df


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Clean consolidated Kestrel Motor Group feedback data"
    )
    parser.add_argument(
        "--input", type=Path, default=Path("consolidated_raw.csv"),
        help="Path to consolidated_raw.csv from consolidate.py",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("consolidated_clean.csv"),
        help="Path for the cleaned output CSV",
    )
    parser.add_argument(
        "--rejects", type=Path, default=Path("rejects.csv"),
        help="Path for rejected rows CSV",
    )
    parser.add_argument(
        "--errors", type=Path, default=Path("data_quality_errors.csv"),
        help="Path for data quality error log CSV",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        log.error(f"Input file not found: {args.input}")
        sys.exit(1)

    clean(args.input, args.output, args.rejects, args.errors)


if __name__ == "__main__":
    main()