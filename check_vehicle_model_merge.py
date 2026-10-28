#!/usr/bin/env python3
"""Check whether the vehicle and model columns can be merged safely.

Usage:
    python check_vehicle_model_merge.py consolidated_clean.csv

Rows where both columns contain different nonblank values are written to
vehicle_model_conflicts.csv for manual review. Comparison ignores case and
extra/leading/trailing whitespace, but does not guess whether different names
refer to the same vehicle.
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd


def normalise(value: object) -> str:
    """Normalise only whitespace and case for comparison."""
    text = str(value).strip()
    return re.sub(r"\s+", " ", text).casefold()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check for conflicts between vehicle and model in a cleaned CSV."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=Path("consolidated_clean.csv"),
        help="Cleaned CSV path (default: consolidated_clean.csv)",
    )
    parser.add_argument(
        "--conflicts-output",
        type=Path,
        default=Path("vehicle_model_conflicts.csv"),
        help="Where to save rows with conflicting vehicle/model values",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        return 2

    try:
        df = pd.read_csv(args.input, dtype=str, keep_default_na=False)
    except Exception as exc:
        print(f"ERROR: Could not read {args.input}: {exc}", file=sys.stderr)
        return 2

    missing = [col for col in ("vehicle", "model") if col not in df.columns]
    if missing:
        print(f"ERROR: Missing required column(s): {', '.join(missing)}", file=sys.stderr)
        print(f"Columns found: {', '.join(df.columns)}", file=sys.stderr)
        return 2

    vehicle = df["vehicle"].fillna("").astype(str).str.strip()
    model = df["model"].fillna("").astype(str).str.strip()
    vehicle_present = vehicle.ne("")
    model_present = model.ne("")

    both_present = vehicle_present & model_present
    vehicle_only = vehicle_present & ~model_present
    model_only = model_present & ~vehicle_present
    neither_present = ~vehicle_present & ~model_present

    vehicle_norm = vehicle.map(normalise)
    model_norm = model.map(normalise)
    equivalent_both = both_present & vehicle_norm.eq(model_norm)
    conflicts = both_present & ~vehicle_norm.eq(model_norm)

    print(f"Input file:                    {args.input}")
    print(f"Total rows:                    {len(df):,}")
    print(f"Vehicle only:                  {int(vehicle_only.sum()):,}")
    print(f"Model only:                    {int(model_only.sum()):,}")
    print(f"Both populated:                {int(both_present.sum()):,}")
    print(f"Both populated, equivalent:    {int(equivalent_both.sum()):,}")
    print(f"Both populated, conflicting:   {int(conflicts.sum()):,}")
    print(f"Neither populated:             {int(neither_present.sum()):,}")
    print("\nComparison ignores case and repeated/outer whitespace only.")

    # Keep conflict reports focused on values and source traceability.
    trace_columns = [
        col for col in (
            "feedback_id", "site_code", "site_name", "source_file",
            "source_sheet", "source_row", "vehicle", "model",
        )
        if col in df.columns
    ]
    conflict_df = df.loc[conflicts, trace_columns].copy()
    # Add the physical CSV data-row number (header is row 1 in the file).
    conflict_df.insert(0, "csv_data_row", conflict_df.index + 2)
    args.conflicts_output.parent.mkdir(parents=True, exist_ok=True)
    conflict_df.to_csv(args.conflicts_output, index=False)
    print(f"Conflict details saved to:      {args.conflicts_output} ({len(conflict_df):,} rows)")

    print("\nRecommendation:")
    if conflicts.any():
        print(
            "DO NOT merge automatically yet. At least one row has different "
            "nonblank values in vehicle and model. Review the conflict report; "
            "you may still merge with a rule that preserves both values on conflicts."
        )
        return 1

    print(
        "Merging looks safe based on direct value conflicts: no row has two "
        "different nonblank values. A combined column can take vehicle when "
        "present, otherwise model. This check cannot prove the fields have the "
        "same meaning in every source file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
