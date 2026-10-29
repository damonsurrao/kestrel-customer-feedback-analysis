"""
Kestrel Motor Group — Consolidation Validation
================================================
Checks that consolidate.py didn't lose or mangle data.

Runs against:
  - consolidated_raw.csv (output of consolidate.py)
  - feedback/ directory (the 52 source files)
  - reference/dealerships.csv (official site list)

Usage:
    python validate_consolidation.py \
        --consolidated ./consolidated_raw.csv \
        --feedback-dir ./feedback \
        --dealerships ./reference/dealerships.csv
"""

import argparse
import io
import re
import sys
from pathlib import Path
from collections import Counter

import pandas as pd
import openpyxl


# ─────────────────────────────────────────────────────────────────────
# Tee: print to both console and an in-memory buffer for file output
# ─────────────────────────────────────────────────────────────────────

class Tee:
    """Wraps stdout so every print() goes to console AND a buffer."""

    def __init__(self, original_stdout):
        self.original = original_stdout
        self.buffer = io.StringIO()

    def write(self, text):
        self.original.write(text)
        # Strip emoji to keep the text file clean and portable
        clean = (
            text
            .replace("✅", "[PASS]")
            .replace("❌", "[FAIL]")
            .replace("⚠️", "[WARN]")
        )
        self.buffer.write(clean)

    def flush(self):
        self.original.flush()
        self.buffer.flush()

    def get_text(self) -> str:
        return self.buffer.getvalue()

# ─────────────────────────────────────────────────────────────────────
# Expected files & sites (from source_registry.yaml)
# ─────────────────────────────────────────────────────────────────────

# All 52 file patterns from the registry, with expected counts
EXPECTED_FILE_PATTERNS = {
    # VIC
    r"^Dandenong Customer Feedback \d{4}-\d{2}\.xlsx$": ("VIC01", "Dandenong"),
    r"^Ringwood feedback Q[1-4] \d{4}\.xlsx$": ("VIC02", "Ringwood"),
    r"^ESSENDON - feedback register\.xlsx$": ("VIC03", "Essendon"),
    r"^Frankston_CX_tracker\.xlsx$": ("VIC04", "Frankston"),
    r"^boxhill_survey_export_\d{4}Q[1-4]\.csv$": ("VIC05", "Box Hill"),
    r"^Geelong Feedback\.xlsx$": ("VIC06", "Geelong"),
    r"^Ballarat - Customer Satisfaction\.xlsx$": ("VIC07", "Ballarat"),
    r"^bendigo feedback\.csv$": ("VIC08", "Bendigo"),
    r"^Shepparton feedback FINAL - site closed\.xlsx$": ("VIC09", "Shepparton"),
    # NSW
    r"^Parramatta_Feedback_\d{4}-\d{2}\.xlsx$": ("NSW01", "Parramatta"),
    r"^Penrith Feedback\.xlsx$": ("NSW02", "Penrith"),
    r"^chatswood_\d{4}_q[1-4]\.csv$": ("NSW03", "Chatswood"),
    r"^liverpool_q[1-4]_\d{4}\.csv$": ("NSW04", "Liverpool"),
    r"^Hunter Motors feedback 25-26\.xlsx$": ("NSW05", "Newcastle"),
    r"^Wollongong_[A-Za-z]{3}\d{2}-[A-Za-z]{3}\d{2}\.csv$": ("NSW06", "Wollongong"),
    r"^Tamworth\.xlsx$": ("NSW07", "Tamworth"),
    # QLD
    r"^Chermside Q[1-4] \d{4}( FINAL( v2)?)?\.xlsx$": ("QLD01", "Chermside"),
    r"^Springwood feedback\.xlsx$": ("QLD02", "Springwood"),
    r"^IPS_feedback_log\.xlsx$": ("QLD03", "Ipswich"),
    r"^Southport Feedback Tracker\.xlsx$": ("QLD04", "Southport"),
    r"^Noosa feedback cards\.xlsx$": ("QLD05", "Noosa"),
    r"^Toowoomba feedback 2026\.xlsx$": ("QLD06", "Toowoomba detail"),
    r"^Toowoomba Q4 2025 summary\.xlsx$": ("QLD06", "Toowoomba summary (EXCLUDED)"),
    r"^North QLD feedback \(Rocky \+ TSV\)\.xlsx$": ("QLD07/08", "Rockhampton+Townsville"),
}

EXCLUDED_FILES = {
    "Toowoomba Q4 2025 summary.xlsx",  # D2: summary file excluded
}

# Sites that should appear in consolidated output
EXPECTED_SITES = [
    "VIC01", "VIC02", "VIC03", "VIC04", "VIC05", "VIC06", "VIC07", "VIC08", "VIC09",
    "NSW01", "NSW02", "NSW03", "NSW04", "NSW05", "NSW06", "NSW07",
    "QLD01", "QLD02", "QLD03", "QLD04", "QLD05", "QLD06", "QLD07",
    # QLD08 (Townsville) shares a file with QLD07 — it may or may not
    # have its own site_code yet depending on whether routing was done
]

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

class ValidationResult:
    def __init__(self):
        self.checks = []  # list of (name, passed: bool, detail: str)

    def check(self, name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        self.checks.append((name, passed, detail))
        icon = "✅" if passed else "❌"
        print(f"  {icon} {name}: {detail}" if detail else f"  {icon} {name}")

    def warn(self, name: str, detail: str = ""):
        self.checks.append((name, None, detail))
        print(f"  ⚠️  {name}: {detail}" if detail else f"  ⚠️  {name}")

    def summary(self):
        passed = sum(1 for _, p, _ in self.checks if p is True)
        failed = sum(1 for _, p, _ in self.checks if p is False)
        warned = sum(1 for _, p, _ in self.checks if p is None)
        print(f"\n{'='*60}")
        print(f"VALIDATION SUMMARY: {passed} passed, {failed} failed, {warned} warnings")
        print(f"{'='*60}")
        if failed > 0:
            print("\nFailed checks:")
            for name, p, detail in self.checks:
                if p is False:
                    print(f"  ❌ {name}: {detail}")
        if warned > 0:
            print("\nWarnings:")
            for name, p, detail in self.checks:
                if p is None:
                    print(f"  ⚠️  {name}: {detail}")
        return failed == 0


def count_data_rows_xlsx(filepath, sheet_name=0, header_row=0):
    """Count data rows in an xlsx sheet (rows below header, excluding fully blank)."""
    try:
        df = pd.read_excel(filepath, sheet_name=sheet_name, header=header_row, dtype=str)
        return len(df.dropna(how="all"))
    except Exception:
        return None


def count_data_rows_csv(filepath, delimiter=",", header_row=0):
    """Count data rows in a CSV."""
    encodings = ["utf-8", "cp1252", "latin-1"]
    for enc in encodings:
        try:
            df = pd.read_csv(filepath, sep=delimiter, header=header_row, dtype=str, encoding=enc)
            return len(df.dropna(how="all"))
        except (UnicodeDecodeError, Exception):
            continue
    return None


# ─────────────────────────────────────────────────────────────────────
# Check 1: File coverage
# ─────────────────────────────────────────────────────────────────────

def check_file_coverage(feedback_dir: Path, df: pd.DataFrame, vr: ValidationResult):
    """Every file in feedback/ should be accounted for."""
    print("\n--- CHECK 1: File Coverage ---")

    # All files in the feedback directory
    all_files = sorted(p.name for p in feedback_dir.iterdir() if p.is_file())
    print(f"  Files in feedback/: {len(all_files)}")

    # Files loaded (from consolidated output)
    loaded_files = sorted(df["source_file"].unique()) if "source_file" in df.columns else []
    print(f"  Distinct source_file values in consolidated: {len(loaded_files)}")

    # Check total count
    vr.check(
        "Total file count",
        len(all_files) == 52,
        f"Expected 52, found {len(all_files)} in feedback/"
    )

    # Match each file against registry patterns
    matched_files = set()
    unmatched_files = []
    for fname in all_files:
        found = False
        for pattern in EXPECTED_FILE_PATTERNS:
            if re.match(pattern, fname):
                matched_files.add(fname)
                found = True
                break
        if not found:
            unmatched_files.append(fname)

    vr.check(
        "All files match a registry pattern",
        len(unmatched_files) == 0,
        f"Unmatched: {unmatched_files}" if unmatched_files else "All matched"
    )

    # Check excluded files are NOT in consolidated output
    for excl in EXCLUDED_FILES:
        if excl in all_files:
            in_output = excl in loaded_files
            vr.check(
                f"Excluded file '{excl}' not in output",
                not in_output,
                "Correctly excluded" if not in_output else "SHOULD NOT BE IN OUTPUT"
            )

    # Check all non-excluded files ARE in consolidated output
    expected_loaded = set(all_files) - EXCLUDED_FILES
    actually_loaded = set(loaded_files)
    missing_from_output = expected_loaded - actually_loaded
    extra_in_output = actually_loaded - expected_loaded

    vr.check(
        "All non-excluded files appear in output",
        len(missing_from_output) == 0,
        f"Missing: {sorted(missing_from_output)}" if missing_from_output else "All present"
    )

    if extra_in_output:
        vr.warn("Extra files in output not in feedback/", f"{sorted(extra_in_output)}")


# ─────────────────────────────────────────────────────────────────────
# Check 2: Site coverage
# ─────────────────────────────────────────────────────────────────────

def check_site_coverage(df: pd.DataFrame, dealerships_path: Path | None, vr: ValidationResult):
    """Every expected site should have rows."""
    print("\n--- CHECK 2: Site Coverage ---")

    sites_in_data = sorted(df["site_code"].unique()) if "site_code" in df.columns else []
    print(f"  Distinct site_codes in output: {len(sites_in_data)}")
    print(f"  Sites: {sites_in_data}")

    for site in EXPECTED_SITES:
        has_rows = site in sites_in_data
        count = len(df[df["site_code"] == site]) if has_rows else 0
        vr.check(f"Site {site} present", has_rows, f"{count} rows" if has_rows else "MISSING")

    # Check against dealerships.csv if provided
    if dealerships_path and dealerships_path.exists():
        ref = pd.read_csv(dealerships_path, dtype=str)
        # Try common column names for site code
        code_col = None
        for candidate in ["site_code", "Site Code", "Code", "code", "SiteCode"]:
            if candidate in ref.columns:
                code_col = candidate
                break
        if code_col:
            ref_sites = set(ref[code_col].dropna().unique())
            data_sites = set(sites_in_data)
            in_ref_not_data = ref_sites - data_sites
            in_data_not_ref = data_sites - ref_sites
            if in_ref_not_data:
                vr.warn("Sites in dealerships.csv but not in data", f"{sorted(in_ref_not_data)}")
            if in_data_not_ref:
                vr.warn("Sites in data but not in dealerships.csv", f"{sorted(in_data_not_ref)}")
        else:
            vr.warn("Could not identify site code column in dealerships.csv", f"Columns: {list(ref.columns)}")


# ─────────────────────────────────────────────────────────────────────
# Check 3: Row counts per source file
# ─────────────────────────────────────────────────────────────────────

def _count_actual_rows(filepath: Path) -> int | None:
    """
    Count total data rows in a source file across ALL sheets (xlsx)
    or the single table (csv). Returns None if the file can't be read.
    A data row is any row below the header that isn't entirely blank.
    """
    suffix = filepath.suffix.lower()

    if suffix == ".csv":
        encodings = ["utf-8", "cp1252", "latin-1"]
        delimiters = [",", ";", "\t"]
        best_count = None
        best_cols = 0
        for enc in encodings:
            for delim in delimiters:
                try:
                    df = pd.read_csv(
                        filepath, dtype=str, encoding=enc,
                        sep=delim, on_bad_lines="warn",
                    )
                    n_cols = len(df.columns)
                    n_rows = len(df.dropna(how="all"))
                    # The right delimiter produces the most columns;
                    # a wrong one lumps everything into one column.
                    if n_cols > best_cols:
                        best_cols = n_cols
                        best_count = n_rows
                except (UnicodeDecodeError, Exception):
                    continue
            if best_count is not None:
                return best_count
        return None

    if suffix == ".xlsx":
        try:
            xl = pd.ExcelFile(filepath)
            total = 0
            for sheet in xl.sheet_names:
                # Try header=0 as a reasonable default; the consolidation
                # loader may use a different header row, but for a rough
                # row count this is close enough — we're looking for big
                # discrepancies (e.g. 32 loaded vs 400 actual), not
                # off-by-one from header position.
                try:
                    sdf = pd.read_excel(xl, sheet_name=sheet, header=0, dtype=str)
                    total += len(sdf.dropna(how="all"))
                except Exception:
                    pass
            return total
        except Exception:
            return None

    return None


def check_row_counts(df: pd.DataFrame, feedback_dir: Path, vr: ValidationResult):
    """
    Report row counts per source file and compare against actual
    row counts in the source files.
    """
    print("\n--- CHECK 3: Row Counts per Source File ---")

    if "source_file" not in df.columns:
        vr.check("source_file column exists", False, "Column missing!")
        return

    counts = df.groupby("source_file").size().sort_index()

    # Count actual rows in each source file
    actual_counts: dict[str, int | None] = {}
    for fname in counts.index:
        filepath = feedback_dir / fname
        if filepath.exists():
            actual_counts[fname] = _count_actual_rows(filepath)
        else:
            actual_counts[fname] = None

    print(f"\n  {'Source File':<50} {'Loaded':>7} {'Actual':>7} {'Match':>7}")
    print(f"  {'-'*50} {'-'*7} {'-'*7} {'-'*7}")

    mismatches = []
    for fname, loaded in counts.items():
        actual = actual_counts.get(fname)
        if actual is not None:
            diff = loaded - actual
            if abs(diff) <= 2:
                # Close enough — header detection quirks can cause ±1-2
                status = "ok"
            elif loaded < actual:
                status = f"-{actual - loaded}"
                mismatches.append((fname, loaded, actual))
            else:
                status = f"+{loaded - actual}"
                mismatches.append((fname, loaded, actual))
            print(f"  {fname:<50} {loaded:>7} {actual:>7} {status:>7}")
        else:
            print(f"  {fname:<50} {loaded:>7} {'???':>7} {'???':>7}")

    loaded_total = counts.sum()
    actual_total = sum(v for v in actual_counts.values() if v is not None)
    print(f"  {'TOTAL':<50} {loaded_total:>7} {actual_total:>7}")

    vr.check("Total row count > 0", len(df) > 0, f"{len(df):,} rows")

    # Flag files with suspiciously few rows
    for fname, count in counts.items():
        if count < 5:
            vr.warn(f"Very few rows from {fname}", f"Only {count} rows — verify this is correct")

    # Flag row count mismatches
    # Use a 10% tolerance for minor header/junk row differences
    for fname, loaded, actual in mismatches:
        gap = actual - loaded
        pct = (gap / actual * 100) if actual > 0 else 0
        if pct > 10:
            vr.check(
                f"Row count: {fname}",
                False,
                f"Loaded {loaded} but source has {actual} data rows "
                f"(missing {gap}, {pct:.0f}%)"
            )
        elif gap > 2:
            vr.warn(
                f"Row count: {fname}",
                f"Loaded {loaded} vs source {actual} (diff {gap}, {pct:.0f}%)"
            )


# ─────────────────────────────────────────────────────────────────────
# Check 4: Sheet coverage (multi-sheet files)
# ─────────────────────────────────────────────────────────────────────

def check_sheet_coverage(df: pd.DataFrame, feedback_dir: Path, vr: ValidationResult):
    """Verify multi-sheet files loaded all expected sheets."""
    print("\n--- CHECK 4: Sheet Coverage ---")

    if "source_sheet" not in df.columns:
        vr.check("source_sheet column exists", False, "Column missing!")
        return

    # ── 4a. Known multi-sheet/segment checks (unchanged) ──

    # Dandenong: should have ~11-12 month sheets
    dandenong = df[df["site_code"] == "VIC01"]
    if len(dandenong) > 0:
        sheets = dandenong["source_sheet"].unique()
        sheets_nonempty = [s for s in sheets if s and str(s).strip()]
        vr.check(
            "VIC01 Dandenong sheet count",
            len(sheets_nonempty) >= 11,
            f"{len(sheets_nonempty)} sheets loaded: {sorted(sheets_nonempty)}"
        )

    # Southport: should have two segments (Feedback + Google reviews)
    southport = df[df["site_code"] == "QLD04"]
    if len(southport) > 0:
        segments = southport["segment_id"].unique() if "segment_id" in df.columns else []
        sheets = southport["source_sheet"].unique()
        vr.check(
            "QLD04 Southport: both sheets/segments loaded",
            len([s for s in sheets if s and str(s).strip()]) >= 2 or
            len([s for s in segments if s and str(s).strip()]) >= 2,
            f"Sheets: {list(sheets)}, Segments: {list(segments)}"
        )

    # Ringwood: should have segments (old + new template)
    ringwood = df[df["site_code"] == "VIC02"]
    if len(ringwood) > 0 and "segment_id" in df.columns:
        segments = [s for s in ringwood["segment_id"].unique() if s and str(s).strip()]
        vr.check(
            "VIC02 Ringwood: both segments found",
            len(segments) >= 2,
            f"Segments: {segments}"
        )

    # Frankston: should have segments
    frankston = df[df["site_code"] == "VIC04"]
    if len(frankston) > 0 and "segment_id" in df.columns:
        segments = [s for s in frankston["segment_id"].unique() if s and str(s).strip()]
        vr.check(
            "VIC04 Frankston: both segments found",
            len(segments) >= 2,
            f"Segments: {segments}"
        )

    # ── 4b. Generic xlsx sheet coverage check ──
    # For every xlsx file in the consolidated output, open the actual
    # file, count its non-empty sheets, and compare against how many
    # distinct source_sheet values were loaded. Catches cases where
    # the loader silently skipped sheets (e.g. Essendon).

    print("\n  --- 4b: Generic xlsx sheet coverage ---")

    xlsx_source_files = sorted(
        f for f in df["source_file"].unique()
        if str(f).lower().endswith(".xlsx")
    )

    for source_fname in xlsx_source_files:
        filepath = feedback_dir / source_fname
        if not filepath.exists():
            continue

        # Count non-empty sheets in the actual file
        try:
            wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
            actual_sheets = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                # A sheet is "non-empty" if it has at least 2 rows (header + 1 data row)
                row_count = 0
                for row in ws.iter_rows(max_row=5, values_only=True):
                    if any(cell is not None for cell in row):
                        row_count += 1
                if row_count >= 2:
                    actual_sheets.append(sheet_name)
            wb.close()
        except Exception as e:
            vr.warn(f"Could not inspect {source_fname}", str(e))
            continue

        if len(actual_sheets) <= 1:
            # Single-sheet file — nothing to check
            continue

        # Count what was loaded into consolidated output for this file
        file_rows = df[df["source_file"] == source_fname]
        loaded_sheets = file_rows["source_sheet"].unique()
        loaded_nonempty = set(
            s for s in loaded_sheets if s and str(s).strip()
        )

        # For segmented files (source_sheet is blank but segment_id
        # is populated), the loader intentionally reads a single sheet
        # and splits by header detection. That's fine — skip these.
        if "segment_id" in df.columns:
            segments = set(
                s for s in file_rows["segment_id"].unique()
                if s and str(s).strip()
            )
            if segments and not loaded_nonempty:
                # Segmented loader — segments within one sheet, not
                # separate sheets. Skip the sheet-count check.
                continue

        # Compare
        actual_count = len(actual_sheets)
        loaded_count = len(loaded_nonempty) if loaded_nonempty else 1  # blank = sheet 0 only

        if loaded_count < actual_count:
            missing_sheets = set(actual_sheets) - loaded_nonempty
            vr.check(
                f"Sheet coverage: {source_fname}",
                False,
                f"File has {actual_count} non-empty sheets {actual_sheets} "
                f"but only {loaded_count} loaded. "
                f"Missing sheets: {sorted(missing_sheets)}"
            )
        else:
            vr.check(
                f"Sheet coverage: {source_fname}",
                True,
                f"All {actual_count} non-empty sheets loaded"
            )


# ─────────────────────────────────────────────────────────────────────
# Check 5: Column mapping — unmapped columns
# ─────────────────────────────────────────────────────────────────────

def check_unmapped_columns(df: pd.DataFrame, vr: ValidationResult):
    """Flag any _unmapped_ columns — they indicate column map mismatches."""
    print("\n--- CHECK 5: Unmapped Columns ---")

    unmapped = [c for c in df.columns if c.startswith("_unmapped_")]
    if unmapped:
        vr.warn(
            f"Found {len(unmapped)} unmapped column(s)",
            f"{unmapped}"
        )
        # Show which sites contributed to each unmapped column
        for col in unmapped:
            non_null = df[df[col].notna()]
            if len(non_null) > 0:
                sites = non_null["site_code"].unique()
                print(f"    {col}: {len(non_null)} non-null values from sites {list(sites)}")
    else:
        vr.check("No unmapped columns", True, "All source columns mapped to canonical names")


# ─────────────────────────────────────────────────────────────────────
# Check 6: Provenance completeness
# ─────────────────────────────────────────────────────────────────────

def check_provenance(df: pd.DataFrame, vr: ValidationResult):
    """Every row should have provenance columns filled."""
    print("\n--- CHECK 6: Provenance Completeness ---")

    for col in ["site_code", "source_file", "source_row"]:
        if col in df.columns:
            nulls = df[col].isna().sum()
            empties = (df[col].astype(str).str.strip() == "").sum()
            total_missing = nulls + empties
            vr.check(
                f"Provenance: {col} fully populated",
                total_missing == 0,
                f"{total_missing} missing/empty" if total_missing > 0 else "Complete"
            )
        else:
            vr.check(f"Provenance: {col} exists", False, "Column missing!")


# ─────────────────────────────────────────────────────────────────────
# Check 7: Chermside duplicate files
# ─────────────────────────────────────────────────────────────────────

def check_chermside_duplicates(df: pd.DataFrame, vr: ValidationResult):
    """Flag if both Chermside Q1 FINAL and FINAL v2 are loaded."""
    print("\n--- CHECK 7: Chermside Q1 Duplicate Check ---")

    chermside_files = df[df["site_code"] == "QLD01"]["source_file"].unique() if "source_file" in df.columns else []
    q1_files = [f for f in chermside_files if "Q1 2026" in str(f)]

    if len(q1_files) > 1:
        vr.warn(
            "Chermside Q1 2026: multiple versions loaded",
            f"Files: {q1_files}. You need to diff these and keep only one."
        )
        # Show row counts per file
        for f in q1_files:
            count = len(df[df["source_file"] == f])
            print(f"    {f}: {count} rows")
    elif len(q1_files) == 1:
        vr.check("Chermside Q1 2026: single version loaded", True, f"{q1_files[0]}")
    else:
        vr.warn("Chermside Q1 2026: no Q1 file found", "Expected at least one Q1 2026 file")

    # Total Chermside files
    vr.check(
        "Chermside file count",
        True,  # informational
        f"{len(chermside_files)} files: {sorted(chermside_files)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Check 8: from_google_southport flag
# ─────────────────────────────────────────────────────────────────────

def check_google_flag(df: pd.DataFrame, vr: ValidationResult):
    """from_google_southport should be 1 only for Southport Google reviews."""
    print("\n--- CHECK 8: from_google_southport Flag ---")

    if "from_google_southport" not in df.columns:
        vr.check("from_google_southport column exists", False, "Column missing!")
        return

    flagged = df[df["from_google_southport"].astype(str).isin(["1", "1.0", "True"])]
    flagged_sites = flagged["site_code"].unique() if len(flagged) > 0 else []

    vr.check(
        "Google flag only on QLD04",
        len(flagged) == 0 or (len(flagged_sites) == 1 and "QLD04" in flagged_sites),
        f"{len(flagged)} rows flagged, from sites: {list(flagged_sites)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Check 9: Quick data sanity
# ─────────────────────────────────────────────────────────────────────

def check_data_sanity(df: pd.DataFrame, vr: ValidationResult):
    """Basic sanity: not all comments null, not all ratings null, etc."""
    print("\n--- CHECK 9: Basic Data Sanity ---")

    for col in ["comment", "rating_raw", "date"]:
        if col in df.columns:
            non_null = df[col].notna().sum()
            pct = non_null / len(df) * 100
            vr.check(
                f"{col}: has data",
                non_null > 0,
                f"{non_null:,} non-null ({pct:.1f}%)"
            )
        else:
            vr.warn(f"{col} column not found in output")

    # Rows where ALL data columns are null (possible junk rows)
    data_cols = ["date", "department", "rating_raw", "comment"]
    existing = [c for c in data_cols if c in df.columns]
    if existing:
        all_null = df[existing].isna().all(axis=1).sum()
        vr.check(
            "Rows with all key fields null",
            True,  # informational
            f"{all_null} rows have date+dept+rating+comment ALL null (these are likely junk to clean)"
        )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validate consolidation output")
    parser.add_argument("--consolidated", type=Path, default=Path("consolidated_raw.csv"))
    parser.add_argument("--feedback-dir", type=Path, default=Path("feedback"))
    parser.add_argument("--dealerships", type=Path, default=None,
                        help="Path to reference/dealerships.csv (optional)")
    parser.add_argument("--report", type=Path, default=Path("validation_report.txt"),
                        help="Path to save the validation report (default: validation_report.txt)")
    args = parser.parse_args()

    if not args.consolidated.exists():
        print(f"ERROR: Consolidated file not found: {args.consolidated}")
        sys.exit(1)
    if not args.feedback_dir.is_dir():
        print(f"ERROR: Feedback directory not found: {args.feedback_dir}")
        sys.exit(1)

    # Tee stdout so everything printed also goes to a buffer
    tee = Tee(sys.stdout)
    sys.stdout = tee

    print(f"{'='*60}")
    print("CONSOLIDATION VALIDATION")
    print(f"{'='*60}")
    print(f"Consolidated file: {args.consolidated}")
    print(f"Feedback dir:      {args.feedback_dir}")

    df = pd.read_csv(args.consolidated, dtype=str)
    print(f"Rows loaded:       {len(df):,}")
    print(f"Columns:           {len(df.columns)}")

    vr = ValidationResult()

    check_file_coverage(args.feedback_dir, df, vr)
    check_site_coverage(df, args.dealerships, vr)
    check_row_counts(df, args.feedback_dir, vr)
    check_sheet_coverage(df, args.feedback_dir, vr)
    check_unmapped_columns(df, vr)
    check_provenance(df, vr)
    check_chermside_duplicates(df, vr)
    check_google_flag(df, vr)
    check_data_sanity(df, vr)

    all_passed = vr.summary()

    # Restore stdout and save report
    sys.stdout = tee.original
    report_text = tee.get_text()
    args.report.write_text(report_text, encoding="utf-8")
    print(f"\nReport saved to: {args.report}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()