# Kestrel Motor Group — Customer Feedback: Data & AI Challenge

Kestrel's head office had 52 customer-feedback spreadsheets from 24 dealerships (Oct 2025 – Sep 2026) and no common template, so it couldn't answer basic questions about customer experience. This repo turns those files into a single traceable dataset, uses AI to turn the free-text comments into themes, sentiment and severity, and answers the Head of Customer Experience's three questions.

**Headline:** 7,045 feedback records produced 3,231 negative issues. Communication is the biggest driver by volume (26%), but Safety is the No.1 priority: 131 issues, all Critical, where one serious incident could cost more than everything else combined. Parts Availability complaints tripled between H1 and H2. Complaints grew faster than revenue: negative issues per $1M revenue went up 21%.

**How this README is organised**
1. [Codebase overview](#codebase-overview): where things live and how to run them
2. [Approach, assumptions & solution](#approach-assumptions--solution), including tech choices
3. [Observations about data quality](#observations-about-data-quality)
4. [Answers for the Head of Customer Experience](#kestrel-motor-group--answers-for-the-head-of-customer-experience)
5. [Insights & recommendations across dealerships](#insights--recommendations-across-dealerships)
6. [AI usage overview](#ai-usage-overview)

Sections 2–4 also exist as standalone files: [`approach_overview.md`](approach_overview.md), [`data_quality_observations.md`](data_quality_observations.md) and [`business_answers_q4.md`](business_answers_q4.md).

---

# Codebase overview

```
.
├── README.md                      ← this report
├── approach_overview.md           ← approach, assumptions, tech choices
├── data_quality_observations.md   ← data quality findings
├── business_answers_q4.md         ← answers to the business questions
├── charts/                        ← 14 PNG charts used in the report
└── code_and_data/                 ← all code, inputs and outputs (run everything from here)
    ├── feedback/                  ← the 52 original source files (unchanged)
    ├── reference/                 ← dealerships.csv, monthly_revenue.csv
    ├── docs/                      ← source_registry.yaml (per-file manifest), issues_and_decisions.md
    ├── consolidate.py             ← 1. stack all 52 files into one raw table with provenance
    ├── validate_consolidation.py  ← 1b. check file/sheet coverage and row counts vs source
    ├── clean.py                   ← 2. 16-step cleaning, validation, PII redaction
    ├── validate_clean.py          ← 2b. check row accounting, integrity, encoding
    ├── diagnose_unmapped.py       ← helper: list source columns that weren't mapped
    ├── ai_analysis_optimised.py   ← 3. AI classification (Haiku → Sonnet) + theme strategies
    ├── generate_charts_for_q4.py  ← 4. charts + report_stats.md (all computed numbers)
    ├── generate_excel.py          ← 4b. Excel workbook of charts (kestrel_feedback_charts.xlsx)
    ├── consolidated_raw.csv       ← output of step 1
    ├── consolidated_clean.csv     ← output of step 2
    ├── data_quality_errors.csv    ← every logged data quality issue
    ├── rejects.csv                ← removed rows, with reason codes
    ├── validation_report.txt      ← output of step 1b
    ├── report_stats.md            ← output of step 4
    └── ai_output/                 ← AI results, taxonomy, word clouds, validation sample
```

The file table in the Approach section lists paths relative to `code_and_data/`.

### How to run

Requirements are Python 3 with `pandas`, `numpy`, `openpyxl`, `matplotlib`, `wordcloud` and `anthropic`. The AI step also needs `ANTHROPIC_API_KEY` set as an environment variable. All paths are relative, so run every command from inside `code_and_data/`. The `-X utf8` flag stops the scripts crashing on Windows consoles when they print characters like →:

```bash
cd code_and_data
python -X utf8 consolidate.py --feedback-dir feedback --output consolidated_raw.csv
python -X utf8 validate_consolidation.py --consolidated consolidated_raw.csv --feedback-dir feedback --dealerships reference/dealerships.csv
python -X utf8 clean.py --input consolidated_raw.csv --output consolidated_clean.csv
python -X utf8 validate_clean.py --raw consolidated_raw.csv --clean consolidated_clean.csv --rejects rejects.csv --errors data_quality_errors.csv
python -X utf8 -I ai_analysis_optimised.py --input consolidated_clean.csv --test   # 40-row dry run
python -X utf8 -I ai_analysis_optimised.py --input consolidated_clean.csv          # full run (paid API calls)
python -X utf8 generate_charts_for_q4.py
python -X utf8 generate_excel.py --issues ai_output/ai_issues.csv --main ai_output/consolidated_clean_ai.csv
```

Note: `generate_charts_for_q4.py` writes its PNGs to `code_and_data/charts/`. If you rerun it, copy them to the top-level `charts/` folder so the report picks up the new versions.

---

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


---

# Observations about Data Quality

## The shape of the problem

52 files, zero shared template. Every dealership evolved its own spreadsheet over time, so the incoming data varied in column names, column order, rating scales, date formats, sheet structure, and what counted as a "row." The source registry documents the per-file reality; the cleaning pipeline logged 674 data quality errors and rejected 32 rows.

## Structural inconsistencies across files

No two dealerships used exactly the same schema. Specific issues:

- **Dandenong (VIC01)** — header row shifts down by one in the May sheet due to an extra annotation row.
- **Essendon (VIC03)** — no department column; instead each department (Sales, Service, Parts) is on its own sheet, so department was taken from the sheet name.
- **Frankston (VIC04)** — subtotal rows embedded in data ("Total Mar 2026", "Grand Total") that parse as valid records. 12 of these were caught and rejected.
- **Ringwood (VIC02)** — template changes mid-file at row 36 in Q1 2026. Column names change, and the rating scale jumps from 1–5 to 1–10.
- **North QLD (QLD07/QLD08)** — two dealerships share one file, distinguished by a "Site" column with inconsistent abbreviations (ROK, ROCK, ROCKY, TSV, T'VILLE, etc.).
- **Chermside (QLD01)** — two versions of the Q1 2026 file exist (FINAL and FINAL v2). Kept v2, dropped FINAL.
- **Parramatta (NSW01)** — schema drifts across quarters with columns appearing and disappearing.
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

- **Geelong (VIC06)** inverted it: 1 = Excellent, 5 = Poor. A banner row in the spreadsheet says so. Normalised via `6 - raw`.
- **Ringwood (VIC02)** switched to 1–10 from February 2026 onwards. Normalised via `ceil(raw / 2)`.
- **Tamworth (NSW07)** used text ratings ("Good", "OK", "Bad") instead of numbers. 10 rows had no rating at all — the highest count of missing ratings for any site.
- **Southport (QLD04)** Google reviews used star emoji (★★★½☆). Parsed to numeric.
- **Shepparton (VIC09):** 1 missing rating.

11 total `RATING_MISSING` errors (10 Tamworth, 1 Shepparton).

## Department mapping

~40 raw department variants across all sites, mapped to four canonical values: Sales, Service, Parts, Unknown.

21 `DEPT_MISSING` errors:
- **Frankston (VIC04):** 12 — all from the subtotal rows (no department on a "Grand Total" line).
- **Southport (QLD04):** 8 — Google reviews have no department context.
- **Shepparton (VIC09):** 1.

Essendon (VIC03) has no department column, but its workbook has one sheet per department, so every row still gets a department from its sheet name. The only `Unknown` departments in the clean data are the 8 Southport Google reviews.

## PII in free-text comments

The largest category: **611 instances** of personally identifiable information found inside comment text. This includes names, phone numbers, email addresses, and registration plates that customers or staff typed into the free-text field.

23 of 24 sites had at least some PII leakage. The worst offenders:

| Site | PII instances |
|---|---|
| Essendon (VIC03) | 67 |
| Dandenong (VIC01) | 54 |
| Parramatta (NSW01) | 46 |
| Penrith (NSW02) | 44 |
| Ringwood (VIC02) | 43 |
| Box Hill (VIC05) | 35 |
| Ballarat (VIC07) | 35 |
| Chatswood (NSW03) | 35 |
| Newcastle (NSW05) | 33 |

All PII was redacted before any data left the pipeline — name/phone/email/rego columns were SHA-256 hashed, and comments were regex-scanned and scrubbed before being sent to the AI classification API. One gap was caught late: 55 comments at Essendon and Ipswich began with the customer's full name, which the original patterns missed. These were only redacted after the AI run (see AI usage overview).

This is logged as severity `info` (not a data error, but a privacy hygiene finding). All 611 instances were actioned.

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

A further 216 rows were removed as duplicates: 112 from Chermside's superseded Q1 FINAL file, and 104 exact duplicates across sites (dedup counts are printed by `clean.py` and checked by `validate_clean.py`).

## Coverage gaps

- **Toowoomba (QLD06):** Q4 2025 data exists only as a pivot-table summary — no individual feedback records. This creates a known gap for Oct–Dec 2025.
- **Southport (QLD04):** Google reviews are a different population (self-selected public reviews vs dealership-collected feedback). Kept but flagged with `from_google_southport = True` so they can be filtered in analysis.

## Summary

Of the 674 logged errors, 640 (95%) are severity `info` (mostly PII redactions — things handled automatically). Only 34 (5%) are `warning` severity, covering unparseable or approximate dates, missing ratings, and missing departments. The data is messy but tractable — the problems are well-understood and concentrated in a handful of sites.


---

# Kestrel Motor Group — Answers for the Head of Customer Experience

## Business context

Kestrel Motor Group runs 24 dealerships across Victoria, New South Wales and Queensland, selling and servicing several vehicle brands. Over the year it sold 16,011 vehicles and completed 200,249 service jobs, for \$838.7M in revenue. Around 89% of that revenue comes from vehicle sales, but the service relationship is what brings customers back. Each dealership records feedback in its own format, so head office has had no group-wide view of what customers are saying. This analysis brings that feedback together to show what drives dissatisfaction, how it has changed, and where to act first.

**How priorities are ranked:** Safety first, then financial impact, then customer loyalty, then efficiency.

> **Dataset:** 7,045 feedback records across 24 dealerships (Oct 2025 - Sep 2026), yielding 3,231 negative issues classified by AI into themes and severity.
>
> **Revenue context:** $838,685,990 total revenue across the group, with a total of 200,249 service jobs, avg service ticket $416.

---

## 1. What are the main drivers of negative feedback?

![Top Themes](charts/01_top_themes.png)

The top five drivers account for **~72%** of all negative issues:

| Theme | Negative Issues | % of Total | Severity Profile |
|---|---|---|---|
| Communication & Updates | 854 | 26.4% | 255 High, 572 Moderate, 27 Low |
| Timeliness & Wait Times | 425 | 13.2% | 68 High, 325 Moderate, 32 Low |
| Repair & Service Quality | 377 | 11.7% | 25 Critical, 261 High, 87 Moderate, 4 Low |
| Pricing & Charges | 366 | 11.3% | 2 Critical, 250 High, 113 Moderate, 1 Low |
| Parts Availability & Delays | 299 | 9.3% | 2 Critical, 95 High, 195 Moderate, 7 Low |

**Communication is the biggest driver of negative feedback by volume**, but it is not the No.1 priority (see Safety below). The single biggest sub-theme across the entire dataset is "No updates / had to chase" (389 issues) — customers are universally describing the same failure: zero proactive outreach, with every update requiring customer-initiated contact. "Callback not returned" (282) and "Message not passed on" (107) compound this further.

**Safety Concern (131 issues) is smaller in volume but 100% Critical severity** — loose wheel nuts, unsecured bonnets, greasy brake pedals. While the themes above are the leading causes of negative sentiment, the analysis has surfaced a potentially serious business risk: safety issues that could be life-threatening to customers and other road users. These carry serious legal, compliance and reputational risk, from compensation claims and penalties to, in the worst case, the shutdown of the business. The volume is small, but with 131 reported in a year (2–3 a week) the probability of a serious incident is high, the impact is very high, and the failures are preventable. **This makes Safety the No.1 priority (see section 3).**

### How do drivers vary across dealerships?

![Worst Dealerships](charts/03_worst_dealerships.png)

The **top 5 worst dealerships** generate 34% of all negative issues:

| Site Code | Site Name | Neg Issues |
|---|---|---|
| VIC01 | Dandenong | 257 |
| NSW01 | Parramatta | 231 |
| QLD01 | Chermside | 224 |
| VIC02 | Ringwood | 193 |
| VIC03 | Essendon | 186 |
<br>

![Dealership Theme Profile](charts/04_dealership_theme_profile.png)

#### Revenue-normalised view: who's really underperforming?

Raw complaint counts favour large dealerships. When we normalise by revenue, the picture shifts:

![Neg per $M Revenue](charts/09_neg_per_M_revenue.png)

**QLD07** leads with 8.7 negative issues per $1M revenue; even though it may not top the raw-count list, it has the worst complaint-to-revenue ratio. This is a more actionable metric because it answers: *"Where are we losing the most customer goodwill relative to business we're doing?"*

![Neg per 1k Service Jobs](charts/12_neg_per_1k_service_jobs.png)

Normalising by service volume tells a similar story: some mid-sized dealerships have a worse complaint rate per 1,000 service jobs than the high-volume sites, suggesting systemic process issues rather than just scale effects.

### How do drivers vary across departments?

![Theme x Department Heatmap](charts/02_theme_department_heatmap.png)

**Service is the epicentre**: it generates 80%+ of negative issues across Communication (715), Repair Quality (369), Timeliness (368), and Pricing (304). Parts and Customer Service/Front Desk are secondary hotspots, particularly for Communication failures (the front desk is the structural chokepoint where messages die).

![Department Breakdown](charts/08_departments.png)

---

## 2. Has anything changed over the year?

**Yes, and not for the better.** H2 (Apr–Sep 2026) is **28.8% worse** than H1 (Oct 2025–Mar 2026): 1,819 negative issues vs 1,412.

![Monthly Trend](charts/05_monthly_trend.png)

### Revenue context: complaints are growing faster than revenue

| Metric | H1 (Oct 25 - Mar 26) | H2 (Apr - Sep 26) | Change |
|---|---|---|---|
| Total Revenue | $407.0M | $431.7M | +6.1% |
| Service Revenue | $40.9M | $42.3M | +3.3% |
| Service Jobs | 98,220 | 102,029 | +3.9% |
| Negative Issues | 1,412 | 1,819 | +28.8% |
| Neg per $1M Revenue | 3.5 | 4.2 | +21.4% |

**Complaints are outpacing business growth.** Even after accounting for any revenue increase, the complaint rate per dollar of revenue is rising. This is genuine service deterioration, not just volume scaling.

![H1 vs H2 Revenue and Complaints](charts/13_h1_h2_revenue_complaints.png)

![Revenue vs Complaints](charts/10_revenue_vs_complaints.png)

![H1 vs H2 Comparison](charts/06_h1_vs_h2.png)

### What's getting worse:

| Theme | H1 | H2 | Change | % Change |
|---|---|---|---|---|
| Parts Availability & Delays | 71 | 228 | +157 | +221% |
| Pricing & Charges | 152 | 214 | +62 | +41% |
| Booking & Scheduling | 88 | 129 | +41 | +47% |
| Timeliness & Wait Times | 197 | 228 | +31 | +16% |
| Safety Concern | 62 | 69 | +7 | +11% |
| Repair & Service Quality | 186 | 191 | +5 | +3% |
| Staff Attitude & Behaviour | 109 | 112 | +3 | +3% |
| Communication & Updates | 432 | 422 | -10 | -2% |


**Parts Availability is the alarm bell.** It tripled from H1 to H2. Multiple long-tenure customers (20+ years) specifically note this is a recent deterioration, suggesting a systemic change in ordering policy or supply chain. Routine consumables such as brake pads, oil filters, cabin filters, spark plugs are unavailable at franchise dealerships for cars they sell.

### What's stable (i.e., not improving either):
- Communication & Updates (~430/half — the largest problem isn't getting better)
- Safety Concerns (+11%, still all Critical)
- Staff Attitude & Behaviour (stable — no self-correction occurring)

---

## 3. Where should Kestrel focus first, and roughly what is it worth?

![Severity by Theme](charts/07_severity_by_theme.png)

![Revenue at Risk](charts/11_revenue_at_risk.png)

### Priority 1: Eliminate Safety failures (131 issues — 100% Critical, liability exposure)

![Safety Breakdown](charts/14_safety_breakdown.png)

Safety issues come from 23 of 24 dealerships, so this is a group-wide process gap, not one bad workshop. 90% (118) are in Service. Half are "other" hazards, such as headlights left aimed into the trees, cruise control cutting out on the highway, or overheating after a service. The rest are steering/suspension (33), brakes (20) and loose wheel nuts (12). They are not falling: 62 in H1, 69 in H2.

> **Potential impact of one serious incident.** A single injury caused by a service failure could cost more than every other issue in this report combined.
> - **Lawsuit / compensation:** Victoria's TAC paid an average of about \$2.25M per serious-injury claim in 2021–22 ([Law Partners, citing TAC data](https://lawpartners.com.au/blog/tac-compensation-calculator)).
> - **Legal and compliance:** under the Australian Consumer Law, services must be provided with due care and skill, and customers can claim compensation when they are not ([ACCC](https://www.accc.gov.au/consumers/buying-products-and-services/consumer-rights-and-guarantees)). ACL penalties for companies now reach the greatest of \$50M, three times the benefit gained, or 30% of adjusted turnover ([Gadens](https://www.gadens.com/insights/increased-competition-and-consumer-law-penalties-now-take-effect)).
> - **Brand reputation:** about half of consumers avoid a business after reading negative reviews (BrightLocal 2025, [via Review42](https://resources.review42.com/online-reputation-management-statistics/)). A safety incident at one site would damage trust in all 24.

**What to do:** Mandatory pre-handover safety sign-off checklist (wheel nuts torqued, bonnet latched, caps secured, road test). This is a 5-minute process step that prevents catastrophic outcomes.

**What it's worth:** We assume one serious-injury claim at the TAC average, **~\$2.25M** (assumption; the data cannot predict whether an incident will happen). That alone is over seven times the at-risk revenue of the other four priorities combined (~\$302K/yr). The fix costs essentially nothing (a printed checklist and 5 minutes per vehicle).

### Priority 2: Fix Communication (854 issues — largest volume, solvable with automation)

**What to do:** Deploy automated SMS/email notifications at four service milestones (checked in → diagnosis → approved → ready). This is a systems fix, not a people fix.

**What it's worth:** Communication failures drive customer churn. With 854 negative communication issues from 778 customers and a group average service ticket of \$416, a conservative 15% churn assumption (each lost customer = 3 annual visits) suggests **\$145k in at-risk annual service revenue**. The fix (DMS notification automation) costs ~$15–30K (assumption) to configure across 24 sites — a payback period measured in weeks.

### Priority 3: Stop the Parts Availability blowout (299 issues, +221% and accelerating)

**What to do:** Mandatory floor stock levels for the top 200 high-velocity parts per platform. Cross-dealership stock-sharing before interstate orders. Pre-service parts checks 48 hours before appointments.

**What it's worth:** Every parts delay cascades into a Timeliness complaint, a Communication complaint, and often a repeat visit. Parts delays are a multiplier of pain. The group's parts revenue is \$11.56M/yr. With 299 direct issues from 275 customers (plus 21 timeliness issues in the same comments), the same churn method gives **~$51K/year** in at-risk service revenue. This excludes the cost of vehicles off the road for fleet customers (assumed $500–1,500/day), which falls on the customer rather than Kestrel.

### Summary: Where to start

| Priority | Focus Area | Issues | Trend | Fix Type | Estimated Value at Risk |
|---|---|---|---|---|---|
| 🔴 1 | Safety sign-off checklist | 131 | Stable (all Critical) | Process (free) | $2,250,000 per serious-injury claim (assumption) |
| 🟠 2 | Communication automation | 854 | Stable (not improving) | Systems/DMS | $145,477/yr |
| 🟡 3 | Parts floor stock & sharing | 299 | **+221% and rising** | Supply chain | $51,422/yr |
| 🔵 4 | Pricing pre-authorisation | 366 | +41% | Process/DMS | $67,129/yr |
| ⚪ 5 | Booking system IT fix | 217 | +47% | IT/Digital | $37,585/yr |

**Total group revenue: $838.7M.** The combined estimated revenue at risk across these five areas is approximately **\$2.55M (0.30% of group revenue)**: one serious-injury claim for Safety (\$2.25M) plus ~\$302K a year across the other four.  While modest relative to total revenue, this exposure warrants attention given the potential for preventable customer churn, deteriorating parts availability, and safety-related liabilities.

**Start with Safety, then Communication.** Safety has the highest potential impact and costs almost nothing to fix; Communication has the largest volume and a fast payback. Parts Availability requires structural supply chain work but is the fastest-deteriorating problem and will get worse every month without intervention.

---

*Analysis based on AI-classified themes from 7,045 feedback records. Revenue figures from monthly_revenue.csv. Sentiment classification validated against rating scores (negative sentiment aligns with mean rating of 1.82/5). Revenue-at-risk estimates count each complaining customer once and use a conservative 15% churn assumption and 3x annual visit frequency — they should be validated against Kestrel's actual margin and retention data. The Safety liability (one serious-injury claim at the TAC average), DMS fix cost and fleet downtime figures are assumptions, not calculated from the data.*

---

# Insights & recommendations across dealerships

### Insights

- **Communication is a group-wide problem, not a few bad sites.** It is the #1 negative theme at 23 of 24 dealerships, making up 20–39% of each site's negative issues. Noosa is the only exception. A problem this consistent points to a shared process or system, so it should be fixed centrally, not site by site.
- **The Parts blowout is mostly a Victorian problem.** Negative Parts issues went from 28 → 115 in VIC (4×), compared with 21 → 63 in NSW and 22 → 50 in QLD. Dandenong alone went from 3 → 25. A rise concentrated in one state suggests a supplier or distribution change there, not 9 separate dealership failures.
- **Dandenong (VIC01) and Essendon (VIC03) are genuine underperformers.** They are in the top 5 both by raw count and by complaints per 1,000 service jobs (18.1 and 19.2), so size alone doesn't explain it.
- **The regional QLD sites (Rockhampton, Townsville, Toowoomba) look worst per $1M revenue.** That's partly because they are service-heavy and sales-light, which inflates the per-revenue figure. Rockhampton is still in the top 5 per 1,000 service jobs (14.8), so its problem is real. For Townsville and Toowoomba, the revenue figure overstates it.
- **Safety issues are spread across many sites.** The highest counts are Dandenong 12, Parramatta 11 and Box Hill 10. This isn't one rogue workshop, which supports a group-wide handover checklist.
- **Don't treat Noosa as best practice yet.** It has only 49 records, 4% negative and a mean rating of 4.6/5. It collects feedback on cards, so unhappy customers may simply not be filling them in. Springwood (QLD02) is a more credible benchmark: it has normal volume (296 records) and the lowest negative rate of any normal-volume site (18%).

### Recommendations

1. **Fix Safety first, then Communication and Pricing authorisation, centrally**, through a standard safety handover checklist and DMS notifications, rolled out to all 24 sites at once (see the priority table above).
2. **Investigate Victorian parts supply specifically.** Ask what changed around April 2026 (supplier, warehouse, ordering policy), starting with Dandenong, Essendon and Ringwood.
3. **Run deep-dives at Dandenong and Essendon**, and compare their processes with Springwood's.
4. **Standardise feedback collection.** Use one template with a mandatory department, a 1–5 scale (5 = best), real dates, a site code, and a warning against putting personal details in the comment field. Most of the cleaning effort in this project exists because there was no template.
5. **Track one monthly KPI: negative issues per 1,000 service jobs, by site.** It adjusts for volume, so sites can be compared fairly, and the trend shows whether fixes are working.

### Limitations

- The theme and sentiment labels come from AI. Every extracted issue carries an evidence quote taken from the customer's own comment, so each label can be traced back to what was actually said. I reviewed the output by hand, and the labels agree with the ratings (negative comments average 1.82/5).
- The dollar values are rough estimates built on stated assumptions (15% churn, 3 visits per year). They are not forecasts.
- Toowoomba has no Q4 2025 records. Liverpool has 6 records dated Oct–Dec 2026, outside the reporting period, which are probably day/month swaps.

---

# AI usage overview

**How I used AI to build**
- **Writing code, mainly.** I used an AI coding assistant for most of the code: consolidation, cleaning, validation, AI classification and charts.
- **No raw data went into the chat.** I only gave it the column headers from each file, never the feedback rows, because the data contains personal information that should be treated as real.
- **Drafting the report.** I also used AI to help draft these write-ups from the numbers my scripts produced (`report_stats.md`).

**Key interaction: the source registry**
Before asking for any code, I opened every file myself and wrote a long `.txt` description of what was in each one: structure, headers, quirks and scales, with nothing personal. I gave that to the AI to produce `source_registry.yaml`, which the consolidation code is built on. Grounding it in my own notes, rather than letting it guess, is why I was fairly confident it wouldn't hallucinate much. I still found things I had missed along the way and added them as I went.

**Where it went wrong**
Because the AI never saw the actual data, it sometimes made wrong assumptions. For example, it didn't know how many sheets a workbook had, so sheets got missed until I corrected it. I fixed these problems as they came up.

**A privacy miss I caught**
The comment redaction in `clean.py` only removed phone numbers, emails and registrations. In a final review I found 55 staff-transcribed comments at Essendon and Ipswich that began with the customer's full name ("Name - said…"). Those names were never redacted, so they were sent to the Anthropic API. To fix it, I:
- added a rule to `clean.py` that removes a name at the start of a comment
- reran the cleaning: only those 55 comments changed, and validation still passes
- removed the names from every AI output file

The same review showed the AI had misread some of these customers as staff in its summaries ("Staff member [name] praised…"). The theme and sentiment labels weren't affected.

**How I verified its output**
- **Validation scripts.** `validate_consolidation.py` and `validate_clean.py` independently check the AI-written code's output against the source files: row counts, sheet coverage, row accounting and value round-trips.
- **Manual checks.** I compared the generated files with the source files by hand. For example, I confirmed in Excel that exactly one row had a blank rating, comment and date, and that the script removed exactly that one.
- **Reading the AI's output.** I went over the AI classification results myself. I also checked the sentiment labels against the star ratings, and kept the 600 rows the model flagged as uncertain (`ai_review_required`) visible instead of hiding them.

**AI inside the solution**
The Anthropic API classifies the comments: Haiku labels sentiment and theme for every comment, Sonnet extracts detailed issues from the negative and mixed ones, and Sonnet then writes per-theme actions. Phone numbers, emails and registrations are removed from comments before they are sent to the API, and names at the start of a comment now are too (see the miss above).
