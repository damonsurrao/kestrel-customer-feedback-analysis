"""
Diagnose unmapped columns in consolidated_raw.csv.
Shows which source files produced each _unmapped_ column and sample values.
"""
import pandas as pd

df = pd.read_csv("consolidated_raw.csv", dtype=str)

unmapped_cols = [c for c in df.columns if c.startswith("_unmapped_")]

if not unmapped_cols:
    print("No unmapped columns found — registry is complete!")
else:
    print(f"Found {len(unmapped_cols)} unmapped columns:\n")

    for col in unmapped_cols:
        # Rows where this column has a non-null value
        mask = df[col].notna() & (df[col].str.strip() != "")
        hits = df.loc[mask]

        print(f"{'='*60}")
        print(f"Column: {col}")
        print(f"  Non-empty rows: {len(hits)} / {len(df)}")

        if len(hits) == 0:
            print("  (all empty — likely a blank spreadsheet column)\n")
            continue

        # Which source files / sheets / segments
        group_cols = ["site_code", "site_name", "source_file", "source_sheet", "segment_id"]
        sources = hits.fillna({c: "" for c in group_cols}).groupby(group_cols).size()
        print(f"  Source files:")
        for idx, count in sources.items():
            site_code, site_name, src_file, src_sheet, seg_id = idx
            parts = [f"{site_code} {site_name}", src_file]
            if src_sheet:
                parts.append(f"sheet='{src_sheet}'")
            if seg_id:
                parts.append(f"segment='{seg_id}'")
            print(f"    {' | '.join(parts)}: {count} rows")

        # Sample values
        samples = hits[col].dropna().head(5).tolist()
        print(f"  Sample values: {samples}")
        print()

    # Summary: what's likely going on
    print(f"\n{'='*60}")
    print("DIAGNOSIS")
    print(f"{'='*60}")
    print("""
Each unmapped column means one of:
  1. REGISTRY GAP: the file has a column the registry column_map doesn't cover.
     → Add it to the site's column_map in source_registry.yaml
  2. NAME MISMATCH: the registry expects e.g. 'Rating' but the file has 'Rating '
     (trailing space) or different casing not caught by case-insensitive match.
     → Check exact column names in the source file
  3. EMPTY COLUMN: _unmapped_nan = a column with no header in the spreadsheet
     (blank cells in the header row). Safe to ignore.
""")