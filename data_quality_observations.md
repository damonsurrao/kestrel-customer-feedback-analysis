# Observations about Data Quality

## The shape of the problem

52 files, zero shared template. Every dealership evolved its own spreadsheet over time, so the incoming data varied in column names, column order, rating scales, date formats, sheet structure, and what counted as a "row." The source registry documents the per-file reality; the cleaning pipeline logged 619 data quality errors and rejected 32 rows.

## Structural inconsistencies across files

No two dealerships used exactly the same schema. Specific issues:

- **Dandenong (VIC03)** — header row shifts down by one in the May sheet due to an extra annotation row.
- **Essendon (VIC06)** — no department column at all; every row assigned `Unknown`.
- **Frankston (VIC04)** — subtotal rows embedded in data ("Total Mar 2026", "Grand Total") that parse as valid records. 12 of these were caught and rejected.
- **Ringwood (VIC02)** — template changes mid-file at row 36 in Q1 2026. Column names change, and the rating scale jumps from 1–5 to 1–10.
- **North QLD (QLD07/QLD08)** — two dealerships share one file, distinguished by a "Site" column with inconsistent abbreviations (ROK, ROCK, ROCKY, TSV, T'VILLE, etc.).
- **Chermside (QLD01)** — two versions of the Q1 2026 file exist (FINAL and FINAL v2). Kept v2, dropped FINAL.
- **Parramatta (NSW03)** — schema drifts across quarters with columns appearing and disappearing.
- **Shepparton (VIC09)** — site appears to have closed mid-year; sparse data, 1 unparseable date, 1 missing rating, 1 missing department.

## Date quality

31 date-related errors across four sites:

- **Frankston (VIC04):** 12 unparseable dates, all from the subtotal/summary rows that don't represent real feedback events.
- **Ipswich (QLD03):** 10 approximate dates from hand-typed entries like "wk5 Mar", "early Apr", "mid Nov". These were converted to mid-point estimates (e.g. "early Apr" → Apr 10) and flagged as `date_quality = approximate`.
- **Southport (QLD04):** 8 approximate dates from Google review relative timestamps ("3 weeks ago", "2 months ago"), resolved from the end of the reporting period (30 Sep 2026).
- **Shepparton (VIC09):** 1 unparseable date.

All approximate dates are flagged so downstream analysis can exclude or weight them if needed.

## Rating scale chaos

Every site nominally used a 1–5 scale, but:

- **Geelong (VIC07)** inverted it: 1 = Excellent, 5 = Poor. A banner row in the spreadsheet says so. Normalised via `6 - raw`.
- **Ringwood (VIC02)** switched to 1–10 from February 2026 onwards. Normalised via `ceil(raw / 2)`.
- **Tamworth (NSW07)** used text ratings ("Excellent", "Good", "Average", "Poor", "Very Poor") instead of numbers. 10 rows had no rating at all — the highest count of missing ratings for any site.
- **Southport (QLD04)** Google reviews used star emoji (★★★½☆). Parsed to numeric.
- **Shepparton (VIC09):** 1 missing rating.

11 total `RATING_MISSING` errors (10 Tamworth, 1 Shepparton).

## Department mapping

~40 raw department variants across all sites, mapped to four canonical values: Sales, Service, Parts, Unknown.

21 `DEPT_MISSING` errors:
- **Frankston (VIC04):** 12 — all from the subtotal rows (no department on a "Grand Total" line).
- **Southport (QLD04):** 8 — Google reviews have no department context.
- **Shepparton (VIC09):** 1.

Essendon (VIC06) had no department column at all, so all its rows default to `Unknown` — but this isn't logged as an error per row because it's a structural issue, not a data entry issue.

## PII in free-text comments

The largest category: **556 instances** of personally identifiable information found inside comment text. This includes names, phone numbers, email addresses, and registration plates that customers or staff typed into the free-text field.

Every site had at least some PII leakage. The worst offenders:

| Site | PII instances |
|---|---|
| Box Hill (VIC01) | 54 |
| Parramatta (NSW01) | 46 |
| Penrith (NSW02) | 44 |
| Dandenong (VIC02) | 43 |
| Geelong (VIC05) | 35 |
| Ballarat (VIC07) | 35 |
| Chatswood (NSW03) | 35 |
| Liverpool (NSW05) | 33 |
| Bendigo (VIC03) | 32 |

All PII was redacted before any data left the pipeline — name/phone/email/rego columns were SHA-256 hashed, and comments were regex-scanned and scrubbed before being sent to the AI classification API.

This is logged as severity `info` (not a data error, but a privacy hygiene finding). All 556 instances were actioned.

## Rejected rows

32 rows were removed from the clean dataset, all with documented reason codes:

| Reason | Count | Source |
|---|---|---|
| `all_data_columns_null` | 14 | VIC02 (1), VIC04 (12), VIC09 (1) — empty rows, mostly from Frankston's padded spreadsheet |
| `frankston_subtotal` | 12 | VIC04 — embedded summary rows like "Total Mar 2026" |
| `test_submission` | 4 | QLD04 — entries like "testing new form", "asdf" |
| `banner_or_marker` | 1 | VIC02 — a formatting row ("***") |
| `rating_std_comment_date_parsed_all_blank` | 1 | VIC09 — a row with no usable data after cleaning |

All rejected rows are preserved in `rejects.csv` with full provenance. Nothing was silently dropped.

## Coverage gaps

- **Toowoomba (QLD06):** Q4 2025 data exists only as a pivot-table summary — no individual feedback records. This creates a known gap for Oct–Dec 2025.
- **Southport (QLD04):** Google reviews are a different population (self-selected public reviews vs dealership-collected feedback). Kept but flagged with `from_google_southport = True` so they can be filtered in analysis.

## Summary

Of the 619 logged errors, 585 (94%) are severity `info` (mostly PII redactions — things handled automatically). Only 34 (6%) are `warning` severity, covering unparseable or approximate dates, missing ratings, and missing departments. The data is messy but tractable — the problems are well-understood and concentrated in a handful of sites.
