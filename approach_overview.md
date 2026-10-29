# Approach, Assumptions & Solution

## Approach

The pipeline has four stages: **consolidate → clean → AI-classify → analyse**.

### 1. Source registry & consolidation

Before writing any code, I manually inspected all 52 files — opening each one, noting column names, header positions, sheet structures, and quirks. This produced `source_registry.yaml`: a per-file manifest documenting formats, column maps, anomalies, and decisions.

Some things I caught doing this:
- Ringwood changes template mid-file at row 36 in Q1 2026 (columns change, rating scale goes from 1–5 to 1–10)
- Dandenong's May sheet has an extra annotation row that pushes the header down by one
- Geelong's rating scale is inverted (1 = Excellent, 5 = Poor) — noted in a banner row
- Frankston has subtotal rows embedded in the data ("Total Mar 2026") that look like real records
- Chermside Q1 2026 has two files: FINAL and FINAL v2
- North QLD is a shared file covering two dealerships (Rockhampton + Townsville), identified by a "Site" column with variant abbreviations (ROK, ROCK, ROCKY, TSV, etc.)
- Southport has a second sheet of copy-pasted Google reviews — different population, different schema

I defined a set of canonical columns (date, department, rating_raw, comment, customer_name, etc.) and mapped every source column across all 52 files to them. Unmapped columns were preserved with an `_unmapped_` prefix rather than silently dropped. Every row carries full provenance (site_code, source_file, source_sheet, source_row) so it traces back to its exact origin.

### 2. Cleaning & validation

A 16-step pipeline in `clean.py`, where each step logs what it changed and why:

- **Junk removal:** all-null rows, banner rows ("Grand Total", "***"), test submissions ("testing new form", "asdf")
- **Date parsing:** handles AU formats (DD/MM/YYYY), Excel serial numbers, relative text ("3 weeks ago"), vague hand-typed dates ("wk5 Mar", "early Apr", "mid Nov"), and blank dates interpolated from neighbours
- **Department standardisation:** ~40 raw variants → Sales | Service | Parts | Unknown
- **Rating normalisation:** everything onto a 1–5 scale (5 = best), handling Geelong's inversion, Ringwood's 1–10 scale, text ratings ("excellent", "poor"), and star emoji (★★★½☆)
- **Site routing:** North QLD rows split to QLD07/QLD08 based on the Site column
- **Dedup:** on (site_code, date, department, rating_raw, comment_hash), with response_id as a tighter key where available
- **PII redaction:** SHA-256 hash on name/phone/email/rego columns; regex scan of comments for embedded PII

Every transformation logs to `data_quality_errors.csv` (step, error code, original value, action taken). Rejected rows go to `rejects.csv` with a reason code. Nothing is silently discarded.

Two separate validation scripts verify the work:
- `validate_consolidation.py` — file coverage, row counts vs source files, sheet coverage, provenance completeness
- `validate_clean.py` — row accounting (raw = clean + rejects + dedup), untouched columns preserved, no mojibake, rating round-trip, no unexpected nulls, feedback_id uniqueness, date range sanity

### 3. AI classification

Two-pass architecture using the Anthropic API:

- **Pass 1 (Haiku):** classifies every comment with sentiment + primary theme. Cheap and fast — covers all ~5,300 comments.
- **Pass 2 (Sonnet):** detailed issue-level analysis on negative/mixed comments only (~35% of rows). Extracts multiple issues per comment, severity, evidence quotes, and responsible department.
- **Pass 3 (Sonnet):** one call per theme with all the evidence aggregated — produces 3 prioritised actions per theme instead of repetitive per-issue recommendations.

The theme taxonomy (13 themes, ~50 sub-themes) was built from an initial sample of the data, not imposed top-down. It covers the operational categories that actually appear: Communication, Pricing, Timeliness, Staff Attitude, Repair Quality, Safety, Booking, Parts Availability, Vehicle Knowledge, Sales Process, Facilities, plus catch-alls for general positives and unclear comments.

Cost controls: prompt caching on system prompts, batches of 30, trimmed prompts (~65% shorter), and the two-pass design (only ~35% of rows hit the expensive model). Estimated ~85–90% cost reduction vs sending everything to Sonnet.

A validation sample of 250 stratified comments is exported for human review.

### 4. Analysis & reporting

`generate_charts_for_q4.py` joins the AI-classified feedback with `monthly_revenue.csv` to produce revenue-contextualised metrics: negative issues per $1M revenue, per 1,000 service jobs, estimated revenue at risk per dealership, H1-vs-H2 comparisons. Outputs 13 charts and a `report_stats.md` with every computed number in copy-pasteable tables.

---

## Key Assumptions

| Assumption | Rationale |
|---|---|
| **Hunter Motors = Newcastle (NSW05)** | No Newcastle file existed; Hunter Motors wasn't in dealerships.csv. Treated as Newcastle per implied data owner instruction. Flagged for confirmation. |
| **Vague dates → mid-point estimates** | Ipswich's hand-typed dates ("early Apr" → Apr 10, "wk5 Mar" → Mar 28, "mid Nov" → Nov 15). Marked as `date_quality = approximate`. |
| **Relative dates from end of reporting period** | "3 weeks ago" calculated from 30 Sep 2026. Also marked approximate. |
| **Geelong inverted scale: `6 - raw`** | Banner says "1 = Excellent, 5 = Poor". Flipped to match the group convention (5 = best). |
| **Ringwood 1–10 → 1–5 via `ceil(raw / 2)`** | New template from Feb 2026 uses 1–10. Ceiling division preserves the sentiment direction (1–2 → 1, 9–10 → 5). |
| **Toowoomba Q4 2025 summary excluded** | Pivot table with no individual records. Creates a known coverage gap for Q4 2025. |
| **Chermside: keep FINAL v2, drop FINAL** | v2 is assumed to be the corrected version. |
| **Southport Google reviews: kept but flagged** | Different population (self-selected public reviews). `from_google_southport` flag allows filtering. |
| **Revenue at risk: complaining customers × 15% churn × 3 visits × avg ticket** | Conservative order-of-magnitude estimate, not a forecast. Industry churn benchmarks for dissatisfied customers range 10–30%. |
| **Safety liability ($2.25M per serious-injury claim, TAC average), DMS fix cost ($15–30K), fleet downtime ($500–1,500/day)** | Assumptions; these cannot be calculated from the data provided. |
| **PII treated as real** | Per the brief. All PII columns hashed; comments scanned and redacted before any API calls. |

---

## Solution

### Design philosophy

Three principles throughout:

1. **Trace everything.** Every row carries provenance back to its source file, sheet, and row number. Every cleaning decision is logged with the original value and what was done. Rejected rows are kept in a separate file with reasons — not deleted.
2. **Validate every stage.** Separate validation scripts check consolidation and cleaning independently. The AI classification has a human-review sample. No stage is trusted without a check.
3. **Separate raw from clean.** The consolidation step does zero transformation — just column renaming and stacking. Cleaning is a separate, auditable pipeline. This means you can re-run cleaning with different rules without re-reading 52 source files.

### Tech choices

- **Python + pandas** — the data fits comfortably in memory (~5K rows), so there's no need for Spark/Dask. Pandas is the standard for this kind of tabular wrangling.
- **Anthropic API (Haiku + Sonnet)** — two-pass design matches cost to value. Sentiment/theme classification is a commodity task (Haiku is fine); issue extraction from ambiguous negative comments benefits from a more capable model.
- **No database** — at this scale, CSVs are simpler, more portable, and easier to inspect. A database would add infrastructure overhead without solving a real problem.
- **matplotlib for charts** — nothing interactive needed; static PNGs for a report. No point pulling in Plotly/D3 for bar charts.
- **YAML for the source registry** — human-readable, easy to diff, and captures the messy reality of 52 files with different formats better than code comments would.

### Repo structure

| File | Purpose |
|---|---|
| `source_registry.yaml` | Per-file documentation of formats, quirks, column maps, and decisions |
| `consolidate.py` | Raw consolidation with full provenance — no cleaning |
| `validate_consolidation.py` | Checks consolidation: file coverage, row counts, sheet coverage |
| `clean.py` | 16-step cleaning pipeline with error & reject logging |
| `validate_clean.py` | Checks cleaning: row accounting, data integrity, encoding |
| `ai_analysis_optimised.py` | Two-pass AI classification + theme-level strategy generation |
| `generate_charts_for_q4.py` | 13 charts + `report_stats.md` with all computed numbers |
| `consolidated_raw.csv` | Output of consolidation (untouched values) |
| `consolidated_clean.csv` | Output of cleaning (standardised, deduplicated, PII-redacted) |
| `data_quality_errors.csv` | Every data quality issue logged by the cleaning pipeline |
| `rejects.csv` | Rows removed, with reason codes |
| `ai_output/` | AI classification results, taxonomy, strategies, word clouds, validation sample |
| `charts/` | All generated chart PNGs |
| `reference/` | Dealership list and monthly revenue data |
| `feedback/` | The 52 original source files (unchanged) |

---

## Learning & Reflection

- **Inspecting the files by hand first paid off.** The source registry caught most quirks before any code was written. The ones it missed, such as sheet counts and Chermside's column names, were things I hadn't checked.
- **AI is fast, but only as good as its grounding.** It worked well from my notes and column headers, and went wrong when it had to guess about data it couldn't see.
- **A privacy miss I caught.** The comment redaction covered phone numbers, emails and registrations, but not names at the start of staff-transcribed comments, so 55 customer names reached the API before I found them. Lesson: test redaction on real comment formats before any API call.
- **Validate the write-up, not just the data.** A final review found report figures that didn't match the data (typed-in value-at-risk numbers, wrong site codes). These are now calculated or corrected.
- **Volume isn't priority.** Communication has the most complaints, but Safety carries the biggest risk. Ranking by impact changed the recommendation.
- **Next time:** agree a single feedback template with the dealerships, since most of the cleaning effort came from not having one.
