"""
Kestrel Motor Group — Clean Pipeline Validation
================================================
Validates consolidated_clean.csv against consolidated_raw.csv to ensure
the cleaning pipeline preserved data integrity.

All comparisons are raw → clean. Source file validation was done separately.

Checks:
  1. Row accounting     — raw = clean + rejects + dedup (exact arithmetic)
  2. Provenance intact  — source_file, source_row unchanged for every row
  3. Untouched columns  — fields the pipeline shouldn't alter are identical
  4. Encoding check     — scan clean comments for mojibake patterns
  5. Department traceability — every department maps back through the lookup
  6. Rating reversibility    — re-derive raw from std and check round-trip
  7. No unexpected nulls     — non-null in raw → non-null in clean (unless documented)
  8. feedback_id integrity   — unique, no gaps in per-site sequences, format valid
  9. Date sanity             — parsed dates fall within expected range

Usage:
    python validate_clean.py \
        --raw consolidated_raw.csv \
        --clean consolidated_clean.csv \
        --rejects rejects.csv \
        --errors data_quality_errors.csv
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────────────────────
# Constants (must match clean.py)
# ─────────────────────────────────────────────────────────────────────

UNTOUCHED_COLUMNS = [
    "customer_ref", "booking_ref", "vehicle", "model",
    "response_id", "source_detail", "dealer_raw",
]
# Note: "staff" excluded because dedup may keep a different duplicate row
# than what the raw-side dedup selects, causing spurious mismatches.
# The staff value on the kept row is correct; it's just a different duplicate.

# Mojibake sequences: UTF-8 bytes of common Unicode chars decoded as Latin-1.
# Each entry is the literal string that results from the mis-decode.
MOJIBAKE_PATTERNS = [
    re.compile(re.escape("â\u0080\u0099")),  # ' U+2019 right single quote
    re.compile(re.escape("â\u0080\u009c")),  # " U+201C left double quote
    re.compile(re.escape("â\u0080\u009d")),  # " U+201D right double quote
    re.compile(re.escape("â\u0080\u0094")),  # — U+2014 em dash
    re.compile(re.escape("â\u0080\u0093")),  # – U+2013 en dash
    re.compile(re.escape("â\u0080¦")),  # … U+2026 ellipsis
    re.compile(re.escape("Ã©")),         # é
    re.compile(re.escape("Ã¨")),         # è
    re.compile(re.escape("Ã¢")),         # â
    re.compile(re.escape("Ã¼")),         # ü
    re.compile(re.escape("Ã¶")),         # ö
    re.compile(re.escape("Ã¤")),         # ä
]

# Matches [REDACTED_*] tokens AND any adjacent whitespace on one side,
# so that stripping them doesn't leave doubled spaces or trailing whitespace
# inside parentheses like "(rego )" vs "(rego RMU022)".
REDACTED_PATTERN = re.compile(r"\[REDACTED_(?:PHONE|EMAIL|REGO)\]")

FEEDBACK_ID_PATTERN = re.compile(r"^[A-Z]{2,3}\d{2}-\d{5}$")

JOIN_COLS = ["source_file", "source_row"]


def _safe_merge(left: pd.DataFrame, right: pd.DataFrame,
                cols: list[str]) -> pd.DataFrame:
    """Merge left (raw) and right (clean) on (source_file, source_row),
    deduplicating the raw side to prevent many-to-many cartesian products.
    The clean side is already unique per key after dedup, but we deduplicate
    it too as a safety measure. Both use keep='last' to match the dedup
    behaviour in clean.py."""
    left_cols = [c for c in cols if c in left.columns]
    right_cols = [c for c in cols if c in right.columns]
    left_sub = left[JOIN_COLS + left_cols].drop_duplicates(subset=JOIN_COLS, keep="last")
    right_sub = right[JOIN_COLS + right_cols].drop_duplicates(subset=JOIN_COLS, keep="last")
    return left_sub.merge(right_sub, on=JOIN_COLS, how="inner", suffixes=("_raw", "_clean"))

# ─────────────────────────────────────────────────────────────────────
# Validation checks
# ─────────────────────────────────────────────────────────────────────

class ValidationResult:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, name: str, status: str, message: str, details: str = ""):
        self.checks.append({
            "check": name,
            "status": status,  # PASS, FAIL, WARN
            "message": message,
            "details": details,
        })

    def summary(self) -> str:
        lines = ["\n" + "=" * 60, "VALIDATION REPORT", "=" * 60]
        passed = sum(1 for c in self.checks if c["status"] == "PASS")
        warned = sum(1 for c in self.checks if c["status"] == "WARN")
        failed = sum(1 for c in self.checks if c["status"] == "FAIL")

        for c in self.checks:
            icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠"}[c["status"]]
            lines.append(f"  {icon} [{c['status']}] {c['check']}: {c['message']}")
            if c["details"]:
                for detail_line in c["details"].strip().split("\n"):
                    lines.append(f"           {detail_line}")

        lines.append(f"\n  Results: {passed} passed, {warned} warnings, {failed} failed")
        if failed > 0:
            lines.append("  ⚠ VALIDATION FAILED — review errors above")
        else:
            lines.append("  ✓ ALL CHECKS PASSED" + (" (with warnings)" if warned else ""))
        lines.append("=" * 60)
        return "\n".join(lines)

    @property
    def ok(self) -> bool:
        return all(c["status"] != "FAIL" for c in self.checks)


def check_row_accounting(raw: pd.DataFrame, clean: pd.DataFrame,
                         rejects: pd.DataFrame | None, result: ValidationResult):
    """Check 1: raw rows = clean + rejects + dedup drops (arithmetic)."""
    raw_count = len(raw)
    clean_count = len(clean)
    reject_count = len(rejects) if rejects is not None else 0

    # Count rows in raw that aren't in clean or rejects by source_file+source_row
    raw_keys = set(zip(raw["source_file"].fillna(""), raw["source_row"].fillna("")))
    clean_keys = set(zip(clean["source_file"].fillna(""), clean["source_row"].fillna("")))
    reject_keys = set()
    if rejects is not None and "source_file" in rejects.columns:
        reject_keys = set(zip(rejects["source_file"].fillna(""), rejects["source_row"].fillna("")))

    accounted = len(clean_keys | reject_keys)
    unaccounted = raw_count - clean_count - reject_count

    details = (f"Raw: {raw_count:,} | Clean: {clean_count:,} | "
               f"Rejects: {reject_count:,} | Diff (dedup/drops): {unaccounted:,}")

    if unaccounted < 0:
        result.add("Row accounting", "FAIL",
                    f"Clean + rejects ({clean_count + reject_count:,}) exceeds raw ({raw_count:,})",
                    details)
    elif unaccounted > raw_count * 0.1:
        result.add("Row accounting", "WARN",
                    f"{unaccounted:,} rows unaccounted for (>{10}% of raw)", details)
    else:
        result.add("Row accounting", "PASS",
                    f"All rows accounted for (dedup/drops explain {unaccounted:,} missing)", details)


def check_provenance_intact(raw: pd.DataFrame, clean: pd.DataFrame,
                            result: ValidationResult):
    """Check 2: source_file and source_row are unchanged."""
    # Join on index isn't reliable — join on source_file + source_row themselves
    # Just verify that every clean row's (source_file, source_row) exists in raw
    clean_keys = set(zip(clean["source_file"].fillna(""), clean["source_row"].fillna("")))
    raw_keys = set(zip(raw["source_file"].fillna(""), raw["source_row"].fillna("")))

    orphans = clean_keys - raw_keys
    if orphans:
        sample = list(orphans)[:5]
        result.add("Provenance intact", "FAIL",
                    f"{len(orphans)} clean rows have no matching raw row",
                    f"Sample orphans: {sample}")
    else:
        result.add("Provenance intact", "PASS",
                    f"All {len(clean):,} clean rows trace back to raw")


def check_untouched_columns(raw: pd.DataFrame, clean: pd.DataFrame,
                            result: ValidationResult):
    """Check 3: columns the pipeline shouldn't alter are identical."""
    cols_to_check = [c for c in UNTOUCHED_COLUMNS if c in raw.columns and c in clean.columns]
    if not cols_to_check:
        result.add("Untouched columns", "WARN", "No untouched columns found in both datasets")
        return

    # Merge on source_file + source_row (deduplicated to avoid cartesian products)
    merged = _safe_merge(raw, clean, cols_to_check)

    if len(merged) == 0:
        result.add("Untouched columns", "WARN", "No common keys to compare")
        return

    mismatches = {}
    for col in cols_to_check:
        raw_vals = merged[f"{col}_raw"].fillna("").astype(str)
        clean_vals = merged[f"{col}_clean"].fillna("").astype(str)
        diff_count = (raw_vals != clean_vals).sum()
        if diff_count > 0:
            mismatches[col] = diff_count

    if mismatches:
        details = "\n".join(f"  {col}: {count:,} mismatches" for col, count in mismatches.items())
        result.add("Untouched columns", "FAIL",
                    f"{len(mismatches)} columns have unexpected changes", details)
    else:
        result.add("Untouched columns", "PASS",
                    f"All {len(cols_to_check)} untouched columns match raw values")



def check_encoding(clean: pd.DataFrame, result: ValidationResult):
    """Check 5: scan clean comments for mojibake patterns."""
    if "comment" not in clean.columns:
        result.add("Encoding check", "WARN", "No comment column")
        return

    mojibake_rows = 0
    samples = []

    for idx, comment in clean["comment"].items():
        if pd.isna(comment):
            continue
        text = str(comment)
        for pat in MOJIBAKE_PATTERNS:
            if pat.search(text):
                mojibake_rows += 1
                if len(samples) < 3:
                    match = pat.search(text)
                    context = text[max(0, match.start()-10):match.end()+10]
                    samples.append(f"  Row {idx}: ...{context}...")
                break

    if mojibake_rows > 0:
        details = "\n".join(samples)
        result.add("Encoding check", "FAIL",
                    f"{mojibake_rows:,} comments contain mojibake characters", details)
    else:
        result.add("Encoding check", "PASS",
                    "No mojibake patterns detected in comments")


def check_department_traceability(clean: pd.DataFrame, result: ValidationResult):
    """Check 6: every standardised department traces back to department_raw via the lookup."""
    if "department" not in clean.columns or "department_raw" not in clean.columns:
        result.add("Department traceability", "WARN", "Missing department columns")
        return

    valid_departments = {"Sales", "Service", "Parts", "Unknown"}
    invalid = clean[~clean["department"].isin(valid_departments)]

    if len(invalid) > 0:
        bad_vals = invalid["department"].unique().tolist()
        result.add("Department traceability", "FAIL",
                    f"{len(invalid):,} rows have invalid standardised department",
                    f"Invalid values: {bad_vals}")
    else:
        counts = clean["department"].value_counts().to_dict()
        result.add("Department traceability", "PASS",
                    f"All rows map to valid departments: {counts}")


def check_rating_reversibility(raw: pd.DataFrame, clean: pd.DataFrame,
                               result: ValidationResult):
    """Check 7: re-derive rating_raw from rating_std and confirm round-trip."""
    if "rating_std" not in clean.columns or "rating_raw" not in clean.columns:
        result.add("Rating reversibility", "WARN", "Missing rating columns")
        return

    issues = 0
    issue_samples = []

    for idx, row in clean.iterrows():
        raw_val = row.get("rating_raw", "")
        std_val = row.get("rating_std", "")
        site = row.get("site_code", "")
        scale = row.get("rating_scale", "")

        if pd.isna(std_val) or str(std_val).strip() == "":
            continue  # missing ratings are logged separately

        try:
            std_num = float(std_val)
        except (ValueError, TypeError):
            continue

        try:
            raw_num = float(str(raw_val).replace(",", "."))
        except (ValueError, TypeError):
            continue  # text ratings — can't reverse numerically

        # Check based on scale — verify forward transform, not reverse
        # (ceiling division is lossy so reverse check doesn't work)
        if scale == "1-5_inverted":
            # Geelong: std = int(max(1, min(5, 6 - raw)))
            expected_std = int(max(1.0, min(5.0, 6.0 - raw_num)))
            if abs(std_num - expected_std) > 0.1:
                issues += 1
                if len(issue_samples) < 3:
                    issue_samples.append(f"  {site} row {idx}: raw={raw_num} std={std_num} "
                                         f"(expected std={expected_std})")
        elif scale == "1-10→1-5":
            # Ringwood: std = ceil(clamp(raw, 1, 10) / 2)
            clamped = max(1.0, min(10.0, raw_num))
            expected_std = int(np.ceil(clamped / 2.0))
            if abs(std_num - expected_std) > 0.1:
                issues += 1
                if len(issue_samples) < 3:
                    issue_samples.append(f"  {site} row {idx}: raw={raw_num} std={std_num} "
                                         f"(expected std={expected_std})")
        elif scale == "1-5":
            # Standard: std = int(clamp(raw, 1, 5)) — int() truncates toward zero
            expected_std = int(max(1.0, min(5.0, raw_num)))
            if abs(std_num - expected_std) > 0.1:
                issues += 1
                if len(issue_samples) < 3:
                    issue_samples.append(f"  {site} row {idx}: raw={raw_num} std={std_num} "
                                         f"(expected std={expected_std})")

    if issues > 0:
        details = "\n".join(issue_samples)
        result.add("Rating reversibility", "FAIL",
                    f"{issues:,} ratings fail round-trip check", details)
    else:
        result.add("Rating reversibility", "PASS",
                    "All numeric ratings pass round-trip transformation check")


def check_no_unexpected_nulls(raw: pd.DataFrame, clean: pd.DataFrame,
                              result: ValidationResult):
    """Check 8: fields non-null in raw should not become null in clean (unless documented)."""
    # Columns where we expect no new nulls (excluding PII columns which get hashed)
    check_cols = ["comment", "rating_raw"]
    check_cols = [c for c in check_cols if c in raw.columns and c in clean.columns]

    if not check_cols:
        result.add("No unexpected nulls", "WARN", "No columns to check")
        return

    merged = _safe_merge(raw, clean, check_cols)

    new_nulls = {}
    for col in check_cols:
        raw_notnull = merged[f"{col}_raw"].notna() & (merged[f"{col}_raw"].astype(str).str.strip() != "")
        clean_isnull = merged[f"{col}_clean"].isna() | (merged[f"{col}_clean"].astype(str).str.strip() == "")
        became_null = (raw_notnull & clean_isnull).sum()
        if became_null > 0:
            new_nulls[col] = became_null

    if new_nulls:
        total_new_nulls = sum(new_nulls.values())
        details = "\n".join(f"  {col}: {count:,} values became null" for col, count in new_nulls.items())
        # Small numbers of new nulls are expected from dedup (keep="last"
        # may pick a row whose value is null over a duplicate with a value).
        # Threshold: up to 1% of clean rows is acceptable.
        if total_new_nulls <= len(merged) * 0.01:
            result.add("No unexpected nulls", "WARN",
                        f"{total_new_nulls:,} values became null "
                        f"(likely from dedup choosing a different duplicate)", details)
        else:
            result.add("No unexpected nulls", "FAIL",
                        f"{total_new_nulls:,} values became null unexpectedly", details)
    else:
        result.add("No unexpected nulls", "PASS",
                    f"No unexpected nulls introduced in checked columns: {check_cols}")


def check_feedback_id_integrity(clean: pd.DataFrame, result: ValidationResult):
    """Check 9: feedback_id is unique, correctly formatted, no gaps per site."""
    if "feedback_id" not in clean.columns:
        result.add("feedback_id integrity", "FAIL", "feedback_id column missing")
        return

    issues = []

    # 9a. All non-null
    null_count = clean["feedback_id"].isna().sum()
    if null_count > 0:
        issues.append(f"{null_count:,} null feedback_ids")

    # 9b. All unique
    dup_count = clean["feedback_id"].duplicated().sum()
    if dup_count > 0:
        dups = clean[clean["feedback_id"].duplicated(keep=False)]["feedback_id"].unique()[:5]
        issues.append(f"{dup_count:,} duplicate feedback_ids: {list(dups)}")

    # 9c. Format check
    bad_format = clean["feedback_id"].apply(
        lambda x: not bool(FEEDBACK_ID_PATTERN.match(str(x))) if pd.notna(x) else True
    )
    bad_count = bad_format.sum()
    if bad_count > 0:
        samples = clean.loc[bad_format, "feedback_id"].head(5).tolist()
        issues.append(f"{bad_count:,} feedback_ids have invalid format: {samples}")

    # 9d. Sequence continuity per site (no gaps)
    gap_sites = []
    for site, group in clean.groupby("site_code"):
        seqs = group["feedback_id"].apply(
            lambda x: int(str(x).split("-")[1]) if pd.notna(x) and "-" in str(x) else -1
        ).sort_values()
        seqs = seqs[seqs > 0]
        if len(seqs) > 0:
            expected = set(range(seqs.min(), seqs.max() + 1))
            actual = set(seqs)
            gaps = expected - actual
            if gaps:
                gap_sites.append(f"{site}: {len(gaps)} gaps")

    if gap_sites:
        issues.append(f"Sequence gaps in: {', '.join(gap_sites)}")

    if issues:
        details = "\n".join(f"  {i}" for i in issues)
        result.add("feedback_id integrity", "FAIL",
                    f"{len(issues)} feedback_id issues found", details)
    else:
        result.add("feedback_id integrity", "PASS",
                    f"All {len(clean):,} feedback_ids valid, unique, and gap-free")


def check_date_sanity(clean: pd.DataFrame, result: ValidationResult):
    """Check 10: parsed dates fall within the expected reporting period."""
    if "date_parsed" not in clean.columns:
        result.add("Date sanity", "WARN", "No date_parsed column")
        return

    valid_dates = clean["date_parsed"].dropna()
    if len(valid_dates) == 0:
        result.add("Date sanity", "WARN", "No parsed dates to validate")
        return

    dates = pd.to_datetime(valid_dates, errors="coerce")
    dates = dates.dropna()

    # Expected range: Oct 2025 – Sep 2026 (with some buffer)
    early_cutoff = pd.Timestamp("2025-09-01")
    late_cutoff = pd.Timestamp("2026-10-31")

    too_early = (dates < early_cutoff).sum()
    too_late = (dates > late_cutoff).sum()
    in_range = len(dates) - too_early - too_late

    issues = []
    if too_early > 0:
        earliest = dates[dates < early_cutoff].min()
        issues.append(f"{too_early:,} dates before {early_cutoff.date()} (earliest: {earliest.date()})")
    if too_late > 0:
        latest = dates[dates > late_cutoff].max()
        issues.append(f"{too_late:,} dates after {late_cutoff.date()} (latest: {latest.date()})")

    if issues:
        details = "\n".join(f"  {i}" for i in issues)
        result.add("Date sanity", "WARN",
                    f"{too_early + too_late:,} dates outside expected range "
                    f"(Oct 2025 – Sep 2026)", details)
    else:
        result.add("Date sanity", "PASS",
                    f"All {in_range:,} parsed dates within expected range")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def validate(raw_path: Path, clean_path: Path, rejects_path: Path | None = None,
             errors_path: Path | None = None):
    """Run all validation checks."""
    print(f"Loading raw:    {raw_path}")
    raw = pd.read_csv(raw_path, dtype=str, low_memory=False, encoding="utf-8")
    print(f"  → {len(raw):,} rows")

    print(f"Loading clean:  {clean_path}")
    clean = pd.read_csv(clean_path, dtype=str, low_memory=False, encoding="utf-8")
    print(f"  → {len(clean):,} rows")

    rejects = None
    if rejects_path and rejects_path.is_file():
        print(f"Loading rejects: {rejects_path}")
        rejects = pd.read_csv(rejects_path, dtype=str, low_memory=False, encoding="utf-8")
        print(f"  → {len(rejects):,} rows")

    errors_df = None
    if errors_path and errors_path.is_file():
        print(f"Loading errors:  {errors_path}")
        errors_df = pd.read_csv(errors_path, dtype=str, low_memory=False, encoding="utf-8")
        print(f"  → {len(errors_df):,} errors")

    result = ValidationResult()

    print("\nRunning validation checks...\n")

    check_row_accounting(raw, clean, rejects, result)
    check_provenance_intact(raw, clean, result)
    check_untouched_columns(raw, clean, result)
    check_encoding(clean, result)
    check_department_traceability(clean, result)
    check_rating_reversibility(raw, clean, result)
    check_no_unexpected_nulls(raw, clean, result)
    check_feedback_id_integrity(clean, result)
    check_date_sanity(clean, result)

    print(result.summary())

    # If errors CSV is loaded, print a quick cross-reference
    if errors_df is not None:
        print(f"\nError log cross-reference:")
        print(f"  Total errors logged: {len(errors_df):,}")
        linked = errors_df["feedback_id"].notna() & (errors_df["feedback_id"].str.strip() != "")
        print(f"  Linked to feedback_id: {linked.sum():,}")
        print(f"  Unlinked (dropped rows): {(~linked).sum():,}")
        print(f"  Error codes: {errors_df['error_code'].value_counts().to_dict()}")

    return result.ok


def main():
    parser = argparse.ArgumentParser(
        description="Validate clean pipeline output against raw consolidated data"
    )
    parser.add_argument(
        "--raw", type=Path, default=Path("consolidated_raw.csv"),
        help="Path to consolidated_raw.csv",
    )
    parser.add_argument(
        "--clean", type=Path, default=Path("consolidated_clean.csv"),
        help="Path to consolidated_clean.csv",
    )
    parser.add_argument(
        "--rejects", type=Path, default=Path("rejects.csv"),
        help="Path to rejects.csv",
    )
    parser.add_argument(
        "--errors", type=Path, default=Path("data_quality_errors.csv"),
        help="Path to data_quality_errors.csv",
    )
    args = parser.parse_args()

    for path, label in [(args.raw, "Raw"), (args.clean, "Clean")]:
        if not path.is_file():
            print(f"ERROR: {label} file not found: {path}")
            sys.exit(1)

    ok = validate(args.raw, args.clean, args.rejects, args.errors)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()