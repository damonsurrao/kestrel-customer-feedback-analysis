# Issues & Decisions Log (DRAFT)

Status: **Open** = needs profiling or input | **Proposed** = my recommendation | **Decided** = made by data owner in inventory notes

## Coverage

| Group | Files |
|---|---|
| Multi-file sites: Box Hill 4, Chermside 5, Chatswood 4, Liverpool 4, Parramatta 12, Ringwood 4, Wollongong 2, Toowoomba 2 | 37 |
| Single-file sites (14): Ballarat, Bendigo, Dandenong, Essendon, Frankston, Geelong, Ipswich, Noosa, Penrith, Springwood, Southport, Tamworth, Shepparton, Newcastle (as Hunter Motors) | 14 |
| Rockhampton + Townsville (shared file) | 1 |
| **Total** | **52** (matches README) |

All 24 sites have files. Newcastle's own file is missing, but the Hunter Motors file has been identified as Newcastle (see D1).

## Decisions made (from data owner notes)

| # | Decision | Site | Consequence / validation needed |
|---|---|---|---|
| D1 | Hunter Motors = Newcastle (NSW05) | NSW05 | Must validate: check Dealer values, brands, region, dates against dealerships.csv. If Dealer has multiple values, it may cover more than one site. Revenue data may be under 'Newcastle' or absent |
| D2 | Exclude Toowoomba Q4 2025 summary | QLD06 | No Toowoomba data for Oct-Dec 2025. Report as a known gap |
| D3 | Frankston subtotals used for verification, then excluded | VIC04 | Compare avg rating + count vs computed values per month; log comparison; then drop from final table |
| D4 | Southport Google reviews flagged via binary `from_google_southport` column | QLD04 | 1 for Google review rows from Southport sheet, 0 for all other rows across all sites |
| D5 | Dandenong Sheet3 is out of scope (probably Oct 2026) | VIC01 | Skip empty sheet; confirm by checking sheet order and name |

## Issues

| # | Site(s) | Issue | Severity | Proposed decision | Status |
|---|---|---|---|---|---|
| 1 | NSW05 | Hunter Motors assumed = Newcastle. Not in dealerships.csv; Dealer column may hold surprises | High | Validate before trusting: profile Dealer values, compare brands/region to dealerships.csv, check revenue file | Open |
| 2 | VIC02 Ringwood | Scale changes mid-file: 1-5 → 1-10 from Feb 2026 (confirmed). Marker on row 36, new header row 37 in Q1 2026 | High | Split into segments by header detection. Rating conversion approach: linear rescale (raw/2) or compare on % top-box / bottom-box. **Warning:** naive merge creates a fake jump in Feb 2026 | Open |
| 3 | VIC04 Frankston | Two layouts in one sheet (rows 1-157, then ~160+). Monthly subtotal rows | High | Split on repeated header. Check 'Rating' vs 'CSAT' scale. Reconcile subtotals (D3), then exclude them | Decided (partially) |
| 4 | VIC04 Frankston | New layout has `Source` column | Med | Map to `source_detail`. Profile distinct values before interpreting | Open |
| 5 | QLD04 Southport | Google reviews in second sheet; no dept; `When` possibly relative text | Med | Load with `from_google_southport=1`, dept Unknown. Keep separate from headline satisfaction metrics | Decided |
| 6 | VIC01 Dandenong | 12 sheets; header on R4 except May 26 (R5, extra NOTE row); banner year unreliable | Med | Detect header by content. Don't trust banner year. Profile all sheets | Open |
| 7 | VIC01, NSW01 | May 2026 booking system events at Dandenong and Parramatta | Info | Annotations stored. Hypothesis to test in the analysis, not an assumption | Open |
| 8 | NSW01 Parramatta | Schema drift: Booking ref from May 2026 only | Low | Optional column; per-file header diff in profiling | Proposed |
| 9 | VIC06 Geelong | Inverted scale (1 = Excellent, 5 = Poor) | High | `rating_std = 6 - rating_raw`; test post-flip distribution vs peers | Proposed |
| 10 | VIC07 Ballarat | Banner row, possibly merged cells; "How satisfied? Comments" ambiguous; scale unknown | Med | Skip banner in loader; never edit source; profile rating values | Proposed |
| 11 | QLD01 Chermside | FINAL vs FINAL v2 for Q1 2026. FORMAT CONFLICT: V1 inventory says CSV, V6 says XLSX. Expected files: Chermside Q4 2025, Q1 2026 FINAL, Q1 2026 FINAL v2, Q2 2026, Q3 2026 (5 total). Loader found only 1 of 5 on first run — likely a format mismatch | High | Verify real file extension. If .xlsx fails, try .csv. Diff Q1 FINAL vs FINAL v2 on response_id; load one only; log which and why | Open |
| 12 | QLD06 Toowoomba | No Q4 2025 data (confirmed) | Med | Report as a known coverage gap | Decided |
| 13 | QLD07/08 | Shared file for two sites. Site column contains variant abbreviations: ROK, ROCK, ROCKY for Rockhampton; TSV/TOWNSVILLE expected for Townsville (verify). 12 rows were unmapped on first run due to ROK not being in the match pattern | Med | Ingest once; split on Site using case-insensitive pattern match covering all known abbreviations (ROCK\|ROCKY\|ROK\|ROCKHAMPTON for QLD07, TSV\|TOWNSVILLE for QLD08); assert zero unmapped after routing | Proposed |
| 14 | QLD03 Ipswich | Site column in a single-site file | Low | Assert == Ipswich on every row | Open |
| 15 | VIC03 Essendon | No department column | High | Unknown unless inferred from text (label as inferred, with confidence) | Open |
| 16 | VIC07, VIC08, VIC03, NSW07, VIC04 | Rating scale/type unstated ("How satisfied?", "Stars", "Score", "HOW WAS IT", "CSAT") | High | Profile distinct values; map text via documented lookup; escalate non-1-5 numeric | Open |
| 17 | VIC09 Shepparton | Closed site | Low | Keep history, flag status, exclude from forward-looking recommendations | Proposed |
| 18 | NSW06 Wollongong | Date-range filenames, possible overlap/gap | Med | Parse range from name; compare with min/max date inside | Open |
| 19 | NSW03 Chatswood | Semicolon delimiter; possible decimal commas in Score | Med | Explicit delimiter; verify Score parses as number | Open |
| 20 | All | Date formats (AU dd/mm vs ISO vs Excel serials vs text) | High | Per-site rules; don't guess ambiguous dates; cross-check filename period | Open |
| 21 | Quarterly sites | Q1-Q4: calendar or financial year? | Low | Ringwood evidence (Feb 2026 inside Q1 2026) supports calendar quarters; confirm others | Proposed |
| 22 | All | Department label variation | High | Build `department_map` lookup from profiled distinct values; unmapped → Unknown, reported | Open |
| 23 | Multiple | Duplicate rows across files/exports | Med | Dedup key: site + date + dept + rating + comment hash (+ response_id if present) | Open |
| 24 | Multiple | PII: names, phones, emails, rego, DMS/customer nos, staff names, Google reviewer names, PII in comments | High | Classify; drop/hash in analytics layer; scan comments for patterns before any LLM call | Proposed |
| 25 | QLD05 Noosa | Paper cards from staff | Low | Note selection bias in caveats | Proposed |
| 26 | VIC01, VIC02, VIC03 | Action columns differ (free text vs Y/N flag) | Low | Keep as `action_taken` with type noted | Proposed |
| 27 | All | Non-data rows (banners, notes, totals, markers) | Med | Drop by rule into `rejects` table with reason; never silently discard | Proposed |
| 28 | QLD04 Southport | 'When' column for Google reviews may be relative text ('3 weeks ago') | Med | Attempt parse; if relative, compute approximate date from file modified date or last Feedback-sheet date; flag as approximate; null if unparseable | Open |

## Cross-cutting assumptions (to confirm)
1. Canonical scale: 1-5, 5 = best (except Geelong inverted, Ringwood new template 1-10).
2. UTF-8 unless decode fails (then cp1252).
3. Site code from registry, not from file, except shared North QLD file.
4. Raw files untouched. All fixes in code, traceable via `source_file` + `source_sheet` + `source_row`.
5. Quarters are calendar quarters (supported by Ringwood evidence).
6. `from_google_southport` = 1 only for Southport's Google reviews sheet; 0 everywhere else.

## Profiling pass (next step): what to capture per file/sheet/segment
- Row count
- Detected header row (vs registry hint)
- Column names found (vs registry column_map): flag any unexpected or missing columns
- Date min/max, format, parse failures
- Null rate per column
- Distinct department values
- Distinct rating values, min/max, type (numeric vs text)
- Duplicate count (on dedup key)
- Subtotal reconciliation (Frankston)
- Comment length distribution
- PII pattern hits in comments (phone, email, rego patterns)
- Dealer distinct values (Newcastle/Hunter Motors)
- Site distinct values (Ipswich, Rockhampton/Townsville)
- Diff all results against `source_registry.yaml`; flip `verified` where everything matches
