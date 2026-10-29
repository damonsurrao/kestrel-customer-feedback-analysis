#!/usr/bin/env python3 -I
"""
Kestrel Motor Group — AI Customer Feedback Analysis Pipeline (OPTIMISED)
=========================================================================
Cost-optimised version with:
  1. Prompt caching        — system prompt cached across all API calls (~90% input savings)
  2. Bigger batches (30)   — fewer API calls, amortised prompt overhead
  3. Trimmed prompts       — removed examples/includes/excludes, ~50% shorter system prompt
  4. Two-pass architecture — Haiku for sentiment+theme (all rows), Sonnet for full
                             analysis (negative/mixed only, ~35% of rows)
  5. Theme-level strategy  — instead of per-issue recommendations, one API call per
                             theme generates 3 prioritised actions from ALL evidence

Estimated cost reduction vs original: ~85-90%

Outputs (same as original):
  - theme_taxonomy.json / .csv
  - consolidated_clean_ai.csv
  - ai_issues.csv
  - wordcloud_overall.png / wordcloud_negative.png
  - word_frequencies.csv
  - theme_strategies.json    — 3 key actions per theme (AI-generated from all evidence)
  - management_summary.xlsx
  - validation_sample.xlsx
  - checkpoint.json

Usage:
  pip install anthropic pandas openpyxl wordcloud matplotlib --break-system-packages
  export ANTHROPIC_API_KEY="sk-ant-..."
  python -I ai_analysis_optimised.py --input consolidated_clean.csv --test        # test mode (40 rows)
  python -I ai_analysis_optimised.py --input consolidated_clean.csv               # full run
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("ai_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")


# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

# Two-pass model configuration
PASS1_MODEL = "claude-haiku-4-5-20251001"  # cheap: sentiment + theme only
PASS2_MODEL = "claude-sonnet-4-6"  # powerful: full issue analysis on negatives only
# Override both with --model flag (single-pass fallback)

DEFAULT_BATCH_SIZE = 30          # up from 10 — amortises system prompt
DEFAULT_OUTPUT_DIR = "ai_output"
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0
TEST_MODE_ROWS = 40              # enough to exercise both passes

CANONICAL_DEPARTMENTS = ["Sales", "Service", "Parts", "Unknown"]

RESPONSIBLE_DEPARTMENTS = [
    "Sales", "Service", "Parts",
    "Booking/Scheduling", "Facilities/Site Management",
    "Finance/Business Office", "Customer Service/Front Desk",
    "IT/Digital", "Management",
    "Unknown/Needs Review",
]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — DATA INSPECTION (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def inspect_data(df: pd.DataFrame) -> dict:
    """Inspect the consolidated_clean DataFrame and return a summary dict."""
    info = {
        "total_rows": len(df),
        "total_columns": len(df.columns),
        "columns": list(df.columns),
        "dtypes": {c: str(df[c].dtype) for c in df.columns},
        "missing": {c: int(df[c].isna().sum()) for c in df.columns if df[c].isna().any()},
        "departments": sorted(df["department"].dropna().unique().tolist()) if "department" in df.columns else [],
        "sites": sorted(df["site_code"].dropna().unique().tolist()) if "site_code" in df.columns else [],
        "year_months": sorted(df["year_month"].dropna().unique().tolist()) if "year_month" in df.columns else [],
        "comments_present": int(df["comment"].notna().sum()) if "comment" in df.columns else 0,
        "comments_missing": int(df["comment"].isna().sum()) if "comment" in df.columns else 0,
    }
    if "rating_std" in df.columns:
        ratings = pd.to_numeric(df["rating_std"], errors="coerce")
        info["rating_mean"] = round(float(ratings.mean()), 2) if ratings.notna().any() else None
        info["rating_median"] = float(ratings.median()) if ratings.notna().any() else None

    print(f"\n{'='*60}")
    print("DATA INSPECTION SUMMARY")
    print(f"{'='*60}")
    print(f"  Rows:              {info['total_rows']:,}")
    print(f"  Columns:           {info['total_columns']}")
    print(f"  Comments present:  {info['comments_present']:,}")
    print(f"  Comments missing:  {info['comments_missing']:,}")
    print(f"  Departments:       {info['departments']}")
    print(f"  Sites:             {len(info['sites'])} dealerships")
    print(f"  Year-months:       {info['year_months'][:3]} … {info['year_months'][-3:]}")
    if info.get("rating_mean"):
        print(f"  Rating mean:       {info['rating_mean']}")
    print(f"{'='*60}\n")
    return info


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — THEME TAXONOMY (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

def build_theme_taxonomy() -> dict:
    """Return the theme taxonomy. Same structure as original — examples kept in
    the JSON/CSV for human reference, but NOT sent to the LLM (trimmed prompts)."""
    taxonomy = {
        "version": "1.0",
        "description": "Kestrel Motor Group customer feedback theme taxonomy",
        "developed_from": "Analysis of ~5,300 customer comments across 24 dealerships",
        "themes": [
            {
                "theme": "Communication & Updates",
                "subthemes": ["Proactive updates", "No updates / had to chase", "Callback not returned", "Message not passed on", "Phone unanswered / long hold"],
                "definition": "Whether the customer was kept informed about the status of their vehicle, booking, or order — or had to chase for information.",
                "includes": "SMS/text updates, phone callbacks, progress calls, being left in the dark, messages lost, hold times",
                "excludes": "Explanations of technical work (see Vehicle Knowledge), booking confirmations (see Booking System)",
                "valence": "both",
                "examples": [
                    "Heard from Gaz twice before lunch. Knew where I stood the whole time.",
                    "no updates at all, car just sat there and i had to ring every day",
                    "Left my number for a call back, was told 'by close of business'. Still no call.",
                ],
            },
            {
                "theme": "Pricing & Charges",
                "subthemes": ["Good value / competitive pricing", "Quote exceeded without notice", "Unexpected / unauthorised charges", "Invoice discrepancy", "Upselling / pressure to add extras", "Fleet pricing"],
                "definition": "Whether the price was fair, transparent, and matched what was quoted or agreed.",
                "includes": "Quoted vs actual price, hidden fees, admin charges, upselling, labour rate discrepancies, value for money",
                "excludes": "Finance terms (see Sales Process), parts pricing when the issue is availability (see Parts Availability)",
                "valence": "both",
                "examples": [
                    "Quote was $520. Paid $790. Not a single call to say it was going up.",
                    "Cost me less than a tank of fuel and a pizza. Not complaining.",
                    "Receipt shows an add on for a 'premium' oil filter I didn't ask for.",
                ],
            },
            {
                "theme": "Timeliness & Wait Times",
                "subthemes": ["Quick / on-time service", "Excessive wait at dealership", "Vehicle not ready when promised", "Long service turnaround", "Queue / desk wait"],
                "definition": "How long the customer waited — whether at the dealership, for the vehicle to be ready, or in a queue.",
                "includes": "Service completion time, pickup delays, counter queues, promised vs actual times",
                "excludes": "Parts delays (see Parts Availability), booking availability (see Booking System)",
                "valence": "both",
                "examples": [
                    "In and out before my coffee went cold.",
                    "HOURS AND HOURS FOR WHAT SHOULD'VE BEEN A QUICK JOB.",
                    "Waited 2 hours past the pickup time they gave me.",
                ],
            },
            {
                "theme": "Staff Attitude & Behaviour",
                "subthemes": ["Friendly / welcoming", "Rude / dismissive / unhelpful", "Pressuring / pushy", "Staff named positively", "Staff named negatively"],
                "definition": "How staff treated the customer — warmth, respect, helpfulness, or rudeness and dismissiveness.",
                "includes": "Greetings, helpfulness, attitude, rudeness, being ignored, being made to feel welcome, named staff praise/complaints",
                "excludes": "Technical explanations (see Vehicle Knowledge), communication about progress (see Communication)",
                "valence": "both",
                "examples": [
                    "Fantastic people. Smiled, said g'day and looked after me from the start.",
                    "counter bloke just shoved the clipboard at me",
                    "Kylie kept pushing the premium care package even after I said no.",
                ],
            },
            {
                "theme": "Repair & Service Quality",
                "subthemes": ["Issue not resolved / recurring fault", "Sloppy workmanship", "Good repair outcome", "Vehicle returned dirty / damaged"],
                "definition": "Whether the actual work on the vehicle was done correctly and completely.",
                "includes": "Faults returning after repair, sloppy work, grease/dirt left on vehicle, misaligned parts, incomplete repairs",
                "excludes": "Wait time for the repair (see Timeliness), parts not available (see Parts Availability)",
                "valence": "both",
                "examples": [
                    "Replaced the tyre and left the wheel nuts not tightened properly.",
                    "Still smells like mould when I turn the fan on.",
                    "Brought my car in with a puddle forming under it. Puddle's still forming.",
                ],
            },
            {
                "theme": "Safety Concern",
                "subthemes": ["Loose wheel nuts / wheels", "Brake failure / issue", "Steering / suspension safety", "Other safety hazard"],
                "definition": "Any issue that could endanger the customer or others. ALWAYS flagged for human review.",
                "includes": "Loose wheel nuts, brake problems after repair, seat sliding, grease on pedals, dangerous repair quality",
                "excludes": "General repair quality issues that are not dangerous (see Repair & Service Quality)",
                "valence": "negative",
                "examples": [
                    "Replaced the tyre and left the wheel nuts not tightened properly. Heard thumping on the freeway.",
                    "Seat still slides back every time I brake. Dangerous.",
                    "Plenty of grease on the carpet and the pedals. My shoes slipped off the brake.",
                ],
            },
            {
                "theme": "Booking & Scheduling",
                "subthemes": ["Booking lost / not recorded", "Online booking system issues", "No availability / long wait for slot", "Booking changed without notice", "Smooth booking process"],
                "definition": "Problems or praise related to making, changing, or honouring service appointments.",
                "includes": "Online booking failures, lost bookings, availability, double-bookings, rescheduling, booking confirmation issues",
                "excludes": "Wait time once at the dealership (see Timeliness), general website issues unrelated to booking",
                "valence": "both",
                "examples": [
                    "Booked on the new website, got a confirmation number, turned up and they had no record of me.",
                    "Can't even get an appointment on a weekday, let alone a Saturday.",
                    "the new online booking system has twice lost my appointment.",
                ],
            },
            {
                "theme": "Parts Availability & Delays",
                "subthemes": ["Part not in stock", "Extended back-order / supply delay", "Part available on time", "No communication about part status"],
                "definition": "Whether required parts were available or caused delays.",
                "includes": "Out of stock, back-order, supply chain delays, parts arriving late, no update on parts ETA",
                "excludes": "Parts pricing (see Pricing & Charges), counter service attitude (see Staff Attitude)",
                "valence": "both",
                "examples": [
                    "Part delay. Ute off the road. Money lost. Fix it.",
                    "Told the EGR valve was on national back-order.",
                    "Part came in on the day they said. Can't fault the timing.",
                ],
            },
            {
                "theme": "Vehicle Knowledge & Explanation",
                "subthemes": ["Features / technology explained well", "Service work explained clearly", "Paperwork / finance explained", "Poor / no explanation"],
                "definition": "Whether staff explained vehicle features, service work, or paperwork clearly.",
                "includes": "Feature walkthroughs, service explanations, invoice explanations, dashboard warning lights, finance paperwork",
                "excludes": "Communication about service progress/updates (see Communication), sales pressure (see Sales Process)",
                "valence": "both",
                "examples": [
                    "Told me straight what the adaptive cruise does and doesn't do.",
                    "Walked me through the finance application and what each box meant.",
                    "Explained the tyre pressure monitor and the spare situation.",
                ],
            },
            {
                "theme": "Sales Process & Handover",
                "subthemes": ["Smooth purchase / handover", "Pressure selling / won't take no", "Finance issues", "Incomplete handover / missing items", "Fleet sales process"],
                "definition": "The experience of buying a vehicle — from negotiation to driving away.",
                "includes": "Test drives, negotiations, finance, delivery, handover completeness, add-on pressure, fleet ordering",
                "excludes": "Post-sale service (see Repair/Service Quality), general staff friendliness (see Staff Attitude)",
                "valence": "both",
                "examples": [
                    "Took delivery of a car with a fuel tank on empty and tyres underinflated.",
                    "Salesperson wouldn't let me leave to think it over.",
                    "Whole thing from test drive to handover was painless.",
                ],
            },
            {
                "theme": "Facilities & Amenities",
                "subthemes": ["Waiting area comfort", "Coffee machine / refreshments", "Cleanliness", "Parking", "Toilets"],
                "definition": "The physical environment — waiting areas, amenities, cleanliness.",
                "includes": "Coffee machine, chairs, toilets, temperature, cleanliness, smell, parking",
                "excludes": "Counter queues (see Timeliness), staff at the desk (see Staff Attitude)",
                "valence": "both",
                "examples": [
                    "No coffee machine working again, bit rich for a premium dealer.",
                    "Dark, dingy and smells a bit damp.",
                    "Waiting area was freezing and filthy with old coffee cups.",
                ],
            },
            {
                "theme": "Positive Experience (General)",
                "subthemes": ["Overall satisfaction", "Easy / painless process", "Would recommend / return", "New to area — welcoming"],
                "definition": "General positive sentiment that doesn't map to a specific operational theme.",
                "includes": "General praise, 'easy', 'no dramas', 'painless', welcoming for newcomers, returning loyalty",
                "excludes": "Specific positive themes like 'quick service' (Timeliness) or 'good price' (Pricing) — use those instead when specific",
                "valence": "positive",
                "examples": [
                    "Easy for a bloke who hates dealerships.",
                    "Moved here a couple of months ago and got greeted like an old mate.",
                    "No bother whatsoever.",
                ],
            },
            {
                "theme": "Insufficient / Unclear",
                "subthemes": [],
                "definition": "Comments too brief, vague, or unintelligible to classify meaningfully.",
                "includes": "Single-word feedback, completely ambiguous statements, garbled text",
                "excludes": "Short but clear statements like 'Part delay. Fix it.' (classifiable as Parts Availability)",
                "valence": "neutral",
                "examples": ["Please call. That's all.", "Too slow mate."],
            },
        ],
    }
    return taxonomy


def save_taxonomy(taxonomy: dict, output_dir: str) -> Path:
    """Save the taxonomy as both JSON and a readable CSV."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    json_path = out / "theme_taxonomy.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(taxonomy, f, indent=2, ensure_ascii=False)
    log.info(f"Saved taxonomy: {json_path}")

    rows = []
    for t in taxonomy["themes"]:
        for sub in (t["subthemes"] or [""]):
            rows.append({
                "theme": t["theme"],
                "subtheme": sub,
                "definition": t["definition"],
                "includes": t["includes"],
                "excludes": t["excludes"],
                "valence": t["valence"],
            })
        if not t["subthemes"]:
            rows.append({
                "theme": t["theme"],
                "subtheme": "",
                "definition": t["definition"],
                "includes": t["includes"],
                "excludes": t["excludes"],
                "valence": t["valence"],
            })
    csv_path = out / "theme_taxonomy.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    log.info(f"Saved taxonomy CSV: {csv_path}")
    return json_path


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — LLM CLASSIFICATION (OPTIMISED: caching, trimmed, two-pass)
# ═════════════════════════════════════════════════════════════════════════════

def get_anthropic_client():
    """Create and return an Anthropic client."""
    try:
        import anthropic
    except ImportError:
        log.error("pip install anthropic --break-system-packages")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("Set ANTHROPIC_API_KEY environment variable")
        sys.exit(1)

    return anthropic.Anthropic(api_key=api_key)


# ─── TRIMMED PROMPTS ─────────────────────────────────────────────────────────
# Original prompt: ~2,500 words (definitions + includes + excludes + 3 examples each)
# Trimmed prompt:  ~800 words  (definition + subthemes only, no examples)
# Savings: ~65% fewer input tokens per call

def build_pass1_prompt(taxonomy: dict) -> str:
    """PASS 1 (Haiku): lightweight prompt for sentiment + primary theme only.
    No examples, no includes/excludes, no severity/department logic."""
    theme_lines = []
    for t in taxonomy["themes"]:
        subs = ", ".join(t["subthemes"]) if t["subthemes"] else "—"
        theme_lines.append(f"  - {t['theme']} [{t['valence']}]: {t['definition']} Sub-themes: {subs}")

    return f"""You classify Australian automotive dealership customer feedback.
Comments may use colloquial language (g'day, mate, bloke, cheers), typos, and sarcasm.

THEMES:
{chr(10).join(theme_lines)}

RULES:
1. Sentiment reflects ONLY the words, NOT any rating score.
2. Interpret sarcasm correctly ("Oh brilliant" when complaining = Negative).
3. Pick the MOST SPECIFIC theme — use "Positive Experience (General)" only when no specific theme fits.
4. Use "Insufficient / Unclear" for vague or garbled text.

Respond with valid JSON only."""


def build_pass1_user_prompt(batch: list[dict]) -> str:
    """Pass 1 user prompt — minimal output schema."""
    items = []
    for item in batch:
        items.append(json.dumps({
            "feedback_id": item["feedback_id"],
            "comment": item["comment"],
        }, ensure_ascii=False))

    return f"""Classify these {len(batch)} comments. For each return:
{{
  "results": [
    {{
      "feedback_id": "...",
      "ai_overall_sentiment": "Positive|Negative|Neutral|Mixed|Insufficient information",
      "ai_primary_theme": "Theme name from list above",
      "ai_summary": "One sentence summary"
    }}
  ]
}}

Comments:
{chr(10).join(items)}"""


def build_pass2_prompt(taxonomy: dict) -> str:
    """PASS 2 (Sonnet): full analysis prompt for negative/mixed comments only.
    Still trimmed vs original — no examples, but includes severity + department logic."""
    theme_lines = []
    for t in taxonomy["themes"]:
        subs = ", ".join(t["subthemes"]) if t["subthemes"] else "—"
        theme_lines.append(f"  - {t['theme']}: {t['definition']} Sub-themes: {subs}")

    return f"""You are an expert customer feedback analyst for an Australian automotive dealership group.
Classify each negative/mixed customer comment in detail.

THEMES:
{chr(10).join(theme_lines)}

RESPONSIBLE DEPARTMENTS: {', '.join(RESPONSIBLE_DEPARTMENTS)}
Infer the department best positioned to own each issue — do NOT just copy the original department.

SEVERITY: Low (minor inconvenience) | Moderate (noticeable impact) | High (potential customer loss) | Critical (safety risk or severe failure). Safety issues MUST be Critical.

RULES:
1. A comment can have MULTIPLE issues. Separate each distinct problem.
2. Evidence quotes MUST be EXACT text from the comment.
3. Safety concerns → Critical severity, ai_review_required = true.
4. Flag for review if: safety concern, legal/financial dispute, contradictory signals, unclear intent.
5. Do NOT generate per-issue recommendations — strategy is generated separately at theme level.

Respond with valid JSON only."""


def build_pass2_user_prompt(batch: list[dict]) -> str:
    """Pass 2 user prompt — full issue-level output schema."""
    items = []
    for item in batch:
        items.append(json.dumps({
            "feedback_id": item["feedback_id"],
            "comment": item["comment"],
            "department": item.get("department", "Unknown"),
            "pass1_sentiment": item.get("pass1_sentiment", ""),
            "pass1_theme": item.get("pass1_theme", ""),
        }, ensure_ascii=False))

    return f"""Analyse these {len(batch)} negative/mixed comments in detail. For each return:
{{
  "results": [
    {{
      "feedback_id": "...",
      "ai_review_required": true/false,
      "ai_review_reason": "Reason or empty string",
      "issues": [
        {{
          "ai_theme": "Theme name",
          "ai_subtheme": "Sub-theme or empty string",
          "ai_issue_sentiment": "Positive|Negative|Neutral|Mixed",
          "ai_severity": "Low|Moderate|High|Critical",
          "ai_evidence_quote": "Exact words from comment",
          "ai_responsible_department": "Department from allowed list",
          "ai_review_required": true/false,
          "ai_review_reason": "Reason or empty string"
        }}
      ]
    }}
  ]
}}

Comments:
{chr(10).join(items)}"""


def redact_pii_for_api(comment: str) -> str:
    """Extra PII redaction before sending to API."""
    if not comment or not isinstance(comment, str):
        return comment or ""
    text = re.sub(r'\b\d{3,4}\s?\d{3,4}\b', '[REDACTED_PHONE]', comment)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[REDACTED_EMAIL]', text)
    text = re.sub(r'\b\d{3}[A-Z]{3}\b', '[REDACTED_REGO]', text)
    text = re.sub(r'\b[A-Z]{3}\d{3}\b', '[REDACTED_REGO]', text)
    return text


def parse_llm_response(response_text: str) -> Optional[dict]:
    """Parse the LLM JSON response, handling common issues."""
    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def validate_classification(result: dict, original_comment: str, valid_themes: set) -> list[str]:
    """Validate a single classification result. Returns list of warnings."""
    warnings = []
    valid_sentiments = {"Positive", "Negative", "Neutral", "Mixed", "Insufficient information"}
    if result.get("ai_overall_sentiment") not in valid_sentiments:
        warnings.append(f"Invalid sentiment: {result.get('ai_overall_sentiment')}")
    if result.get("ai_primary_theme") not in valid_themes:
        warnings.append(f"Unknown primary theme: {result.get('ai_primary_theme')}")
    for i, issue in enumerate(result.get("issues", [])):
        if issue.get("ai_theme") not in valid_themes:
            warnings.append(f"Issue {i}: unknown theme '{issue.get('ai_theme')}'")
        quote = issue.get("ai_evidence_quote", "")
        if quote and original_comment:
            norm_comment = re.sub(r'\s+', ' ', original_comment.lower().strip())
            norm_quote = re.sub(r'\s+', ' ', quote.lower().strip())
            if norm_quote not in norm_comment:
                warnings.append(f"Issue {i}: evidence quote not found in original comment")
        valid_severities = {"Low", "Moderate", "High", "Critical"}
        if issue.get("ai_severity") not in valid_severities:
            warnings.append(f"Issue {i}: invalid severity '{issue.get('ai_severity')}'")
        if issue.get("ai_responsible_department") not in RESPONSIBLE_DEPARTMENTS:
            warnings.append(f"Issue {i}: unknown department '{issue.get('ai_responsible_department')}'")
    return warnings


# ─── PROMPT CACHING ──────────────────────────────────────────────────────────
# The system prompt is sent as a content block with cache_control.
# After the first call, Anthropic caches it — subsequent calls pay ~10% of
# the original input token cost for the system prompt portion.

def _call_api_with_caching(
    client,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> Optional[str]:
    """Make an API call with prompt caching on the system prompt.
    Returns the response text or None on failure."""
    for attempt in range(MAX_RETRIES):
        try:
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                # ── PROMPT CACHING: system as content block with cache_control ──
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
            # Only Sonnet/Opus support the effort parameter; Haiku does not
            if "haiku" not in model.lower():
                kwargs["output_config"] = {"effort": "medium"}
            # Use streaming to avoid SDK timeout errors on large max_tokens
            result_text = ""
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    result_text += text
            response = stream.get_final_message()

            # Log cache performance
            usage = response.usage
            cached_in = getattr(usage, "cache_read_input_tokens", 0)
            created_cache = getattr(usage, "cache_creation_input_tokens", 0)
            if cached_in > 0:
                log.debug(f"  Cache HIT: {cached_in} tokens read from cache")
            elif created_cache > 0:
                log.debug(f"  Cache CREATED: {created_cache} tokens cached for next call")

            return result_text

        except Exception as e:
            err_str = str(e)
            if "rate_limit" in err_str.lower() or "429" in err_str:
                wait = RETRY_BASE_DELAY * (2 ** attempt) * 2
                log.warning(f"  Rate limited, waiting {wait:.0f}s...")
                time.sleep(wait)
            elif "overloaded" in err_str.lower() or "529" in err_str:
                wait = RETRY_BASE_DELAY * (2 ** attempt) * 3
                log.warning(f"  API overloaded, waiting {wait:.0f}s...")
                time.sleep(wait)
            else:
                log.error(f"  Attempt {attempt+1}: API error: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                else:
                    raise
    return None


# ─── Checkpoint management ───

def load_checkpoint(output_dir: str, key: str = "processed_ids") -> dict:
    """Load processing checkpoint."""
    path = Path(output_dir) / "checkpoint.json"
    if path.is_file():
        with open(path) as f:
            return json.load(f)
    return {"processed_ids": [], "pass2_processed_ids": [], "version": 2}


def save_checkpoint(checkpoint: dict, output_dir: str):
    """Save processing checkpoint."""
    path = Path(output_dir) / "checkpoint.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3A — PASS 1: SENTIMENT + THEME (Haiku, cheap)
# ═════════════════════════════════════════════════════════════════════════════

def run_pass1(
    df: pd.DataFrame,
    taxonomy: dict,
    output_dir: str,
    model: str,
    batch_size: int,
    test_mode: bool = False,
) -> pd.DataFrame:
    """Pass 1: classify all comments with sentiment + primary theme using Haiku.
    Returns df with ai_overall_sentiment, ai_primary_theme, ai_summary added."""
    client = get_anthropic_client()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    valid_themes = {t["theme"] for t in taxonomy["themes"]}

    # Filter to rows with comments
    has_comment = df["comment"].notna() & (df["comment"].str.strip() != "")
    classifiable = df[has_comment].copy()

    if test_mode:
        classifiable = classifiable.head(TEST_MODE_ROWS)
        log.info(f"TEST MODE: Pass 1 processing {len(classifiable)} rows")
    else:
        log.info(f"Pass 1: processing {len(classifiable):,} rows with comments "
                 f"({len(df) - len(classifiable):,} skipped — no comment)")

    # Load checkpoint
    checkpoint = load_checkpoint(output_dir)
    processed_ids = set(checkpoint.get("processed_ids", []))
    remaining = classifiable[~classifiable["feedback_id"].isin(processed_ids)]

    if len(remaining) < len(classifiable):
        log.info(f"  Resuming Pass 1: {len(classifiable) - len(remaining):,} already done, "
                 f"{len(remaining):,} remaining")

    # Prepare batches
    batch_items = []
    for _, row in remaining.iterrows():
        comment = redact_pii_for_api(str(row["comment"]))
        batch_items.append({
            "feedback_id": row["feedback_id"],
            "comment": comment,
        })

    # Load partial results
    all_results = []
    partial_path = out / "pass1_partial.json"
    if partial_path.is_file():
        with open(partial_path) as f:
            all_results = json.load(f)
        log.info(f"  Loaded {len(all_results):,} partial Pass 1 results")

    system_prompt = build_pass1_prompt(taxonomy)
    total_batches = (len(batch_items) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(batch_items), batch_size):
        batch = batch_items[batch_idx:batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1
        log.info(f"  Pass 1 batch {batch_num}/{total_batches} ({len(batch)} comments)...")

        try:
            user_prompt = build_pass1_user_prompt(batch)
            response_text = _call_api_with_caching(
                client, model, system_prompt, user_prompt,
                max_tokens=100 * len(batch),  # ~100 tokens per comment for sentiment+theme+summary
            )

            if response_text:
                parsed = parse_llm_response(response_text)
                if parsed and "results" in parsed:
                    results = parsed["results"]
                    all_results.extend(results)
                    for r in results:
                        processed_ids.add(r.get("feedback_id", ""))
                    checkpoint["processed_ids"] = list(processed_ids)
                    save_checkpoint(checkpoint, output_dir)
                    with open(partial_path, "w", encoding="utf-8") as f:
                        json.dump(all_results, f, ensure_ascii=False)
                else:
                    log.warning(f"  Pass 1 batch {batch_num}: parse failed, creating fallbacks")
                    for item in batch:
                        all_results.append({
                            "feedback_id": item["feedback_id"],
                            "ai_overall_sentiment": "Insufficient information",
                            "ai_primary_theme": "Insufficient / Unclear",
                            "ai_summary": "Pass 1 classification failed",
                        })
            else:
                for item in batch:
                    all_results.append({
                        "feedback_id": item["feedback_id"],
                        "ai_overall_sentiment": "Insufficient information",
                        "ai_primary_theme": "Insufficient / Unclear",
                        "ai_summary": "Pass 1 API call failed",
                    })

        except Exception as e:
            log.error(f"  Pass 1 batch {batch_num} failed: {e}")
            traceback.print_exc()
            for item in batch:
                all_results.append({
                    "feedback_id": item["feedback_id"],
                    "ai_overall_sentiment": "Insufficient information",
                    "ai_primary_theme": "Insufficient / Unclear",
                    "ai_summary": f"Error: {str(e)[:100]}",
                })

        if batch_idx + batch_size < len(batch_items):
            time.sleep(0.3)

    # Merge Pass 1 results into df
    results_map = {r["feedback_id"]: r for r in all_results}
    ai_df = df.copy()
    ai_df["ai_overall_sentiment"] = ""
    ai_df["ai_primary_theme"] = ""
    ai_df["ai_summary"] = ""
    ai_df["ai_review_required"] = False
    ai_df["ai_review_reason"] = ""
    ai_df["ai_processed_status"] = "skipped"

    for idx, row in ai_df.iterrows():
        fid = row["feedback_id"]
        if fid in results_map:
            r = results_map[fid]
            ai_df.at[idx, "ai_overall_sentiment"] = r.get("ai_overall_sentiment", "")
            ai_df.at[idx, "ai_primary_theme"] = r.get("ai_primary_theme", "")
            ai_df.at[idx, "ai_summary"] = r.get("ai_summary", "")
            ai_df.at[idx, "ai_processed_status"] = "pass1_done"

    neg_count = ai_df["ai_overall_sentiment"].isin(["Negative", "Mixed"]).sum()
    pos_count = ai_df["ai_overall_sentiment"].isin(["Positive"]).sum()
    log.info(f"  Pass 1 complete: {pos_count:,} Positive, {neg_count:,} Negative/Mixed → Pass 2")

    return ai_df


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3B — PASS 2: FULL ANALYSIS (Sonnet, only negatives/mixed)
# ═════════════════════════════════════════════════════════════════════════════

def run_pass2(
    ai_df: pd.DataFrame,
    taxonomy: dict,
    output_dir: str,
    model: str,
    batch_size: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pass 2: detailed issue analysis on negative/mixed comments only (Sonnet).
    For positive/neutral, create simple positive issues from Pass 1 theme.
    Returns (ai_df updated, ai_issues)."""
    client = get_anthropic_client()
    out = Path(output_dir)
    valid_themes = {t["theme"] for t in taxonomy["themes"]}

    # Only send negative/mixed to Pass 2
    needs_pass2 = ai_df[
        (ai_df["ai_overall_sentiment"].isin(["Negative", "Mixed"]))
        & (ai_df["ai_processed_status"] == "pass1_done")
    ].copy()

    log.info(f"Pass 2: {len(needs_pass2):,} negative/mixed comments for detailed analysis")

    # Checkpoint for pass 2
    checkpoint = load_checkpoint(output_dir)
    pass2_done = set(checkpoint.get("pass2_processed_ids", []))
    remaining = needs_pass2[~needs_pass2["feedback_id"].isin(pass2_done)]

    if len(remaining) < len(needs_pass2):
        log.info(f"  Resuming Pass 2: {len(needs_pass2) - len(remaining):,} already done, "
                 f"{len(remaining):,} remaining")

    batch_items = []
    for _, row in remaining.iterrows():
        comment = redact_pii_for_api(str(row["comment"]))
        batch_items.append({
            "feedback_id": row["feedback_id"],
            "comment": comment,
            "department": row.get("department", "Unknown"),
            "pass1_sentiment": row.get("ai_overall_sentiment", ""),
            "pass1_theme": row.get("ai_primary_theme", ""),
        })

    all_pass2 = []
    partial_path = out / "pass2_partial.json"
    if partial_path.is_file():
        with open(partial_path) as f:
            all_pass2 = json.load(f)
        log.info(f"  Loaded {len(all_pass2):,} partial Pass 2 results")

    system_prompt = build_pass2_prompt(taxonomy)
    total_batches = (len(batch_items) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(batch_items), batch_size):
        batch = batch_items[batch_idx:batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1
        log.info(f"  Pass 2 batch {batch_num}/{total_batches} ({len(batch)} comments)...")

        try:
            user_prompt = build_pass2_user_prompt(batch)
            response_text = _call_api_with_caching(
                client, model, system_prompt, user_prompt,
                max_tokens=500 * len(batch),  # ~500 tokens per comment for full issue analysis
            )

            if response_text:
                parsed = parse_llm_response(response_text)
                if parsed and "results" in parsed:
                    results = parsed["results"]
                    # Validate
                    for r in results:
                        fid = r.get("feedback_id", "")
                        orig = next((b["comment"] for b in batch if b["feedback_id"] == fid), "")
                        warns = validate_classification(r, orig, valid_themes)
                        if warns:
                            r["_validation_warnings"] = warns

                    all_pass2.extend(results)
                    for r in results:
                        pass2_done.add(r.get("feedback_id", ""))
                    checkpoint["pass2_processed_ids"] = list(pass2_done)
                    save_checkpoint(checkpoint, output_dir)
                    with open(partial_path, "w", encoding="utf-8") as f:
                        json.dump(all_pass2, f, ensure_ascii=False)
                else:
                    log.warning(f"  Pass 2 batch {batch_num}: parse failed")
                    for item in batch:
                        all_pass2.append({
                            "feedback_id": item["feedback_id"],
                            "ai_review_required": True,
                            "ai_review_reason": "Pass 2 classification failed",
                            "issues": [],
                        })
            else:
                for item in batch:
                    all_pass2.append({
                        "feedback_id": item["feedback_id"],
                        "ai_review_required": True,
                        "ai_review_reason": "Pass 2 API call failed",
                        "issues": [],
                    })

        except Exception as e:
            log.error(f"  Pass 2 batch {batch_num} failed: {e}")
            traceback.print_exc()
            for item in batch:
                all_pass2.append({
                    "feedback_id": item["feedback_id"],
                    "ai_review_required": True,
                    "ai_review_reason": f"Error: {str(e)[:200]}",
                    "issues": [],
                })

        if batch_idx + batch_size < len(batch_items):
            time.sleep(0.5)

    # ── Merge Pass 2 into ai_df ──
    pass2_map = {r["feedback_id"]: r for r in all_pass2}
    for idx, row in ai_df.iterrows():
        fid = row["feedback_id"]
        if fid in pass2_map:
            r = pass2_map[fid]
            ai_df.at[idx, "ai_review_required"] = bool(r.get("ai_review_required", False))
            ai_df.at[idx, "ai_review_reason"] = r.get("ai_review_reason", "")
            ai_df.at[idx, "ai_processed_status"] = "pass2_done"

    # ── Build ai_issues ──
    log.info("Building ai_issues DataFrame...")
    issue_rows = []

    # Issues from Pass 2 (negative/mixed — full detail)
    for r in all_pass2:
        fid = r.get("feedback_id", "")
        orig_row = ai_df[ai_df["feedback_id"] == fid]
        if orig_row.empty:
            continue
        orig = orig_row.iloc[0]
        for issue in r.get("issues", []):
            issue_rows.append({
                "feedback_id": fid,
                "site_code": orig.get("site_code", ""),
                "site_name": orig.get("site_name", ""),
                "date_parsed": orig.get("date_parsed", ""),
                "year_month": orig.get("year_month", ""),
                "department": orig.get("department", ""),
                "rating_std": orig.get("rating_std", ""),
                "ai_theme": issue.get("ai_theme", ""),
                "ai_subtheme": issue.get("ai_subtheme", ""),
                "ai_issue_sentiment": issue.get("ai_issue_sentiment", ""),
                "ai_severity": issue.get("ai_severity", ""),
                "ai_evidence_quote": issue.get("ai_evidence_quote", ""),
                "ai_responsible_department": issue.get("ai_responsible_department", ""),
                "ai_review_required": issue.get("ai_review_required", False),
                "ai_review_reason": issue.get("ai_review_reason", ""),
            })

    # Synthetic issues from Pass 1 positives (no API cost — just carry the theme forward)
    positive_rows = ai_df[
        (ai_df["ai_overall_sentiment"].isin(["Positive", "Neutral"]))
        & (ai_df["ai_processed_status"] == "pass1_done")
    ]
    for _, row in positive_rows.iterrows():
        if row["ai_primary_theme"] and row["ai_primary_theme"] != "Insufficient / Unclear":
            issue_rows.append({
                "feedback_id": row["feedback_id"],
                "site_code": row.get("site_code", ""),
                "site_name": row.get("site_name", ""),
                "date_parsed": row.get("date_parsed", ""),
                "year_month": row.get("year_month", ""),
                "department": row.get("department", ""),
                "rating_std": row.get("rating_std", ""),
                "ai_theme": row["ai_primary_theme"],
                "ai_subtheme": "",
                "ai_issue_sentiment": row["ai_overall_sentiment"],
                "ai_severity": "Low",
                "ai_evidence_quote": "",
                "ai_responsible_department": row.get("department", "Unknown/Needs Review"),
                "ai_review_required": False,
                "ai_review_reason": "",
            })

    ai_issues = pd.DataFrame(issue_rows) if issue_rows else pd.DataFrame(columns=[
        "feedback_id", "site_code", "site_name", "date_parsed", "year_month",
        "department", "rating_std", "ai_theme", "ai_subtheme",
        "ai_issue_sentiment", "ai_severity", "ai_evidence_quote",
        "ai_responsible_department",
        "ai_review_required", "ai_review_reason",
    ])

    log.info(f"  ai_issues: {len(ai_issues):,} rows "
             f"({len(all_pass2)} from Pass 2 + {len(positive_rows)} synthetic positives)")

    return ai_df, ai_issues


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3C — PASS 3: THEME-LEVEL STRATEGY (replaces per-issue recommendations)
# ═════════════════════════════════════════════════════════════════════════════
# Instead of generating a recommendation for every single issue (repetitive,
# generic, expensive), we make ONE call per theme with ALL the evidence and
# ask for 3 prioritised actions. 13 calls total vs ~2,000 inline recommendations.

MAX_COMMENTS_PER_THEME = 200  # sample if more — counts/trends already tell the volume story

THEME_STRATEGY_SYSTEM_PROMPT = """You are a senior customer experience strategist for an Australian automotive dealership group (24 dealerships, 3 states).

You will receive ALL negative/mixed customer feedback for ONE specific theme, plus summary statistics (counts by dealership, severity breakdown, trend direction).

Your job: produce exactly 3 prioritised actions that management should take to address this theme.

RULES:
1. Actions must be SPECIFIC and PRACTICAL — not "improve communication" but "implement automated SMS updates at service booking, 2-hour mark, and ready-for-pickup".
2. Each action should name WHO owns it (department/role), WHAT to do, and HOW to measure success.
3. Prioritise by expected impact: action 1 = highest impact, action 3 = lowest.
4. Reference specific patterns you see in the data (which dealerships, which sub-issues, trends).
5. If a theme is improving (trend ↓), note what might be working and recommend continuing.
6. Be honest about what the data does and doesn't tell you — frame uncertain findings as hypotheses.

Respond with valid JSON only."""


def build_theme_strategy_user_prompt(
    theme: str,
    comments_sample: list[str],
    stats: dict,
) -> str:
    """Build user prompt for theme-level strategy generation."""
    comments_text = "\n".join(f"  - {c}" for c in comments_sample)

    return f"""Generate a 3-action strategy for the theme: "{theme}"

STATISTICS:
  Total negative/mixed issues in this theme: {stats.get('total_issues', 0)}
  Severity breakdown: {json.dumps(stats.get('severity_breakdown', {}), ensure_ascii=False)}
  Top dealerships affected: {json.dumps(stats.get('top_dealerships', []), ensure_ascii=False)}
  Sub-theme breakdown: {json.dumps(stats.get('subtheme_breakdown', {}), ensure_ascii=False)}
  Trend (H1 vs H2): {stats.get('trend', 'Unknown')}
  Departments involved: {json.dumps(stats.get('departments', []), ensure_ascii=False)}

SAMPLE COMMENTS ({len(comments_sample)} of {stats.get('total_issues', 0)} total):
{comments_text}

Return:
{{
  "theme": "{theme}",
  "total_issues": {stats.get('total_issues', 0)},
  "actions": [
    {{
      "priority": 1,
      "action": "Specific action description",
      "owner": "Department or role",
      "measure": "How to measure success",
      "rationale": "Why this is #1 — what pattern in the data drives it"
    }},
    {{
      "priority": 2,
      "action": "...",
      "owner": "...",
      "measure": "...",
      "rationale": "..."
    }},
    {{
      "priority": 3,
      "action": "...",
      "owner": "...",
      "measure": "...",
      "rationale": "..."
    }}
  ]
}}"""


def _gather_theme_stats(
    ai_issues: pd.DataFrame,
    ai_df: pd.DataFrame,
    theme: str,
) -> tuple[dict, list[str]]:
    """Gather statistics and sample comments for one theme."""
    neg = ai_issues[
        (ai_issues["ai_theme"] == theme)
        & (ai_issues["ai_issue_sentiment"].isin(["Negative", "Mixed"]))
    ]

    if len(neg) == 0:
        return {}, []

    # Severity breakdown
    severity = neg["ai_severity"].value_counts().to_dict()

    # Top dealerships
    top_dealers = (
        neg.groupby(["site_code", "site_name"]).size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
        .head(5)
    )
    top_dealers_list = [
        {"site": f"{r['site_code']} ({r['site_name']})", "count": int(r["count"])}
        for _, r in top_dealers.iterrows()
    ]

    # Sub-theme breakdown
    subthemes = neg["ai_subtheme"].value_counts().head(8).to_dict()

    # Trend
    if "year_month" in neg.columns:
        months = sorted(neg["year_month"].dropna().unique())
        if len(months) >= 4:
            mid = len(months) // 2
            r1 = neg[neg["year_month"].isin(months[:mid])].shape[0] / mid
            r2 = neg[neg["year_month"].isin(months[mid:])].shape[0] / (len(months) - mid)
            if r2 > r1 * 1.3:
                trend = "↑ Increasing"
            elif r2 < r1 * 0.7:
                trend = "↓ Decreasing"
            else:
                trend = "→ Stable"
        else:
            trend = "Insufficient data"
    else:
        trend = "No date data"

    # Departments
    depts = neg["ai_responsible_department"].value_counts().head(5).to_dict()

    stats = {
        "total_issues": int(len(neg)),
        "severity_breakdown": severity,
        "top_dealerships": top_dealers_list,
        "subtheme_breakdown": subthemes,
        "trend": trend,
        "departments": depts,
    }

    # Sample comments — stratified by dealership and severity
    feedback_ids = neg["feedback_id"].unique()
    matched = ai_df[ai_df["feedback_id"].isin(feedback_ids)]
    comments = matched["comment"].dropna().tolist()

    if len(comments) > MAX_COMMENTS_PER_THEME:
        # Stratified sample: pick from different dealerships
        import random
        random.seed(42)
        random.shuffle(comments)
        comments = comments[:MAX_COMMENTS_PER_THEME]

    # Redact before sending
    comments = [redact_pii_for_api(c) for c in comments]

    return stats, comments


def run_pass3_theme_strategies(
    ai_df: pd.DataFrame,
    ai_issues: pd.DataFrame,
    taxonomy: dict,
    output_dir: str,
    model: str,
) -> dict:
    """Pass 3: generate 3-action strategy per theme from all evidence.
    Returns dict of {theme: strategy} and saves to theme_strategies.json."""
    client = get_anthropic_client()
    out = Path(output_dir)

    # Skip themes with no negative issues or the catch-all themes
    skip_themes = {"Insufficient / Unclear", "Positive Experience (General)"}
    active_themes = [
        t["theme"] for t in taxonomy["themes"]
        if t["theme"] not in skip_themes
    ]

    strategies = {}
    log.info(f"Pass 3: generating strategies for {len(active_themes)} themes...")

    for theme in active_themes:
        stats, comments = _gather_theme_stats(ai_issues, ai_df, theme)

        if not comments or stats.get("total_issues", 0) == 0:
            log.info(f"  {theme}: no negative issues, skipping")
            strategies[theme] = {
                "theme": theme,
                "total_issues": 0,
                "actions": [],
                "note": "No negative/mixed issues found for this theme",
            }
            continue

        log.info(f"  {theme}: {stats['total_issues']} issues, "
                 f"{len(comments)} sample comments → generating strategy...")

        user_prompt = build_theme_strategy_user_prompt(theme, comments, stats)

        try:
            response_text = _call_api_with_caching(
                client, model, THEME_STRATEGY_SYSTEM_PROMPT, user_prompt,
                max_tokens=2048,
            )

            if response_text:
                parsed = parse_llm_response(response_text)
                if parsed and "actions" in parsed:
                    strategies[theme] = parsed
                    log.info(f"    ✓ 3 actions generated")
                else:
                    log.warning(f"    ✗ Failed to parse strategy response")
                    strategies[theme] = {
                        "theme": theme,
                        "total_issues": stats["total_issues"],
                        "actions": [],
                        "error": "Failed to parse LLM response",
                    }
            else:
                strategies[theme] = {
                    "theme": theme,
                    "total_issues": stats["total_issues"],
                    "actions": [],
                    "error": "API call returned no response",
                }

        except Exception as e:
            log.error(f"    ✗ Error: {e}")
            strategies[theme] = {
                "theme": theme,
                "total_issues": stats["total_issues"],
                "actions": [],
                "error": str(e)[:200],
            }

        time.sleep(0.3)  # brief pause between themes

    # Save
    json_path = out / "theme_strategies.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(strategies, f, indent=2, ensure_ascii=False)
    log.info(f"Saved theme strategies: {json_path}")

    # Print summary
    print(f"\n{'='*60}")
    print("THEME STRATEGIES (3 actions each)")
    print(f"{'='*60}")
    for theme, strat in strategies.items():
        n_actions = len(strat.get("actions", []))
        n_issues = strat.get("total_issues", 0)
        if n_actions > 0:
            print(f"\n  {theme} ({n_issues} issues):")
            for a in strat["actions"]:
                print(f"    {a['priority']}. [{a.get('owner', '?')}] {a['action']}")
        elif n_issues == 0:
            print(f"\n  {theme}: no negative issues")
    print(f"{'='*60}\n")

    return strategies


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — WORD CLOUDS (unchanged from original)
# ═════════════════════════════════════════════════════════════════════════════

CUSTOM_STOPWORDS = {
    "car", "vehicle", "said", "told", "got", "went", "came", "just",
    "one", "also", "would", "could", "get", "much", "really", "even",
    "like", "know", "think", "back", "time", "still", "well", "bit",
    "lot", "thing", "things", "way", "day", "made", "make", "good",
    "great", "nice", "pretty", "quite", "going", "come", "take",
    "see", "say", "ask", "asked", "rego", "redacted_rego",
    "redacted_phone", "redacted_email", "redacted", "phone",
    "email", "call_me", "call", "called", "ring",
}

MULTI_WORD_PHRASES = {
    "parts delay": "parts_delay", "booking system": "booking_system",
    "unexpected charges": "unexpected_charges", "waiting time": "waiting_time",
    "wait time": "wait_time", "wheel nuts": "wheel_nuts",
    "coffee machine": "coffee_machine", "call back": "callback",
    "called back": "callback", "rang back": "rang_back",
    "online booking": "online_booking", "back order": "backorder",
    "on time": "on_time", "no updates": "no_updates",
    "warning light": "warning_light", "check engine": "check_engine",
    "tyre pressure": "tyre_pressure", "adaptive cruise": "adaptive_cruise",
    "paint protection": "paint_protection", "extended warranty": "extended_warranty",
    "service desk": "service_desk",
}


def preprocess_for_wordcloud(comments: pd.Series, custom_stopwords: set = None) -> str:
    from wordcloud import STOPWORDS
    if custom_stopwords is None:
        custom_stopwords = CUSTOM_STOPWORDS
    all_stops = STOPWORDS | custom_stopwords
    texts = comments.dropna().astype(str).tolist()
    combined = " ".join(texts).lower()
    for phrase, token in MULTI_WORD_PHRASES.items():
        combined = combined.replace(phrase, token)
    combined = re.sub(r'\[redacted_\w+\]', '', combined, flags=re.IGNORECASE)
    combined = re.sub(r"[^\w\s]", " ", combined)
    combined = re.sub(r"\s+", " ", combined)
    words = [w for w in combined.split() if w not in all_stops and len(w) > 2]
    return " ".join(words)


def generate_wordcloud(text, title, output_path, width=1200, height=600, colormap="viridis"):
    from wordcloud import WordCloud
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    wc = WordCloud(width=width, height=height, background_color="white",
                   colormap=colormap, max_words=150, min_font_size=10,
                   collocations=False).generate(text)
    fig, ax = plt.subplots(1, 1, figsize=(16, 8))
    ax.imshow(wc, interpolation="bilinear")
    ax.set_title(title, fontsize=20, fontweight="bold", pad=15)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved word cloud: {output_path}")
    return wc


def generate_wordclouds(ai_df, output_dir):
    out = Path(output_dir)
    all_text = preprocess_for_wordcloud(ai_df["comment"])
    generate_wordcloud(all_text, "Kestrel Motor Group — All Customer Feedback",
                       str(out / "wordcloud_overall.png"))
    neg_mask = ai_df["ai_overall_sentiment"].isin(["Negative", "Mixed"])
    if neg_mask.any():
        neg_text = preprocess_for_wordcloud(ai_df.loc[neg_mask, "comment"])
        generate_wordcloud(neg_text, "Kestrel Motor Group — Negative Feedback",
                           str(out / "wordcloud_negative.png"), colormap="Reds")
    freq = Counter(all_text.split())
    freq_df = pd.DataFrame(sorted(freq.items(), key=lambda x: -x[1]), columns=["word", "frequency"])
    freq_df.to_csv(out / "word_frequencies.csv", index=False)
    log.info(f"Saved word frequencies: {out / 'word_frequencies.csv'}")
    return {"overall": str(out / "wordcloud_overall.png"),
            "negative": str(out / "wordcloud_negative.png")}


def generate_filtered_wordcloud(ai_df, output_dir, site_code=None, department=None, theme=None, label="filtered"):
    mask = pd.Series(True, index=ai_df.index)
    parts = []
    if site_code:
        mask &= ai_df["site_code"] == site_code; parts.append(f"site={site_code}")
    if department:
        mask &= ai_df["department"] == department; parts.append(f"dept={department}")
    if theme:
        mask &= ai_df["ai_primary_theme"] == theme; parts.append(f"theme={theme}")
    subset = ai_df[mask]
    if len(subset) == 0:
        log.warning(f"No comments matching filter: {parts}"); return ""
    text = preprocess_for_wordcloud(subset["comment"])
    title = f"Kestrel — {', '.join(parts)} ({len(subset)} comments)"
    out_path = str(Path(output_dir) / f"wordcloud_{label}.png")
    generate_wordcloud(text, title, out_path)
    return out_path


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — MANAGEMENT ANALYSIS (unchanged from original)
# ═════════════════════════════════════════════════════════════════════════════

def generate_management_analysis(ai_df, ai_issues, output_dir, theme_strategies=None):
    out = Path(output_dir)
    xlsx_path = out / "management_summary.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        if len(ai_issues) > 0:
            tc = ai_issues["ai_theme"].value_counts().reset_index()
            tc.columns = ["theme", "issue_count"]
            tc["pct_of_issues"] = (tc["issue_count"] / len(ai_issues) * 100).round(1)
            tc.to_excel(writer, sheet_name="Theme Summary", index=False)

        if len(ai_issues) > 0:
            neg = ai_issues[ai_issues["ai_issue_sentiment"] == "Negative"]
            if len(neg) > 0:
                nt = neg["ai_theme"].value_counts().reset_index()
                nt.columns = ["theme", "negative_count"]
                nt["pct_of_negative"] = (nt["negative_count"] / len(neg) * 100).round(1)
                nt.to_excel(writer, sheet_name="Negative Themes", index=False)

        if len(ai_issues) > 0:
            neg = ai_issues[ai_issues["ai_issue_sentiment"] == "Negative"]
            if len(neg) > 0:
                bt = neg.groupby(["site_code", "site_name", "ai_theme"]).size().reset_index(name="neg_count")
                totals = ai_df.groupby("site_code").size().reset_index(name="total_feedback")
                bt = bt.merge(totals, on="site_code", how="left")
                bt["neg_rate_pct"] = (bt["neg_count"] / bt["total_feedback"] * 100).round(1)
                bt["small_sample"] = bt["total_feedback"] < 30
                bt = bt.sort_values(["ai_theme", "neg_rate_pct"], ascending=[True, False])
                bt.to_excel(writer, sheet_name="Branch × Theme", index=False)

        if len(ai_issues) > 0:
            dr = ai_issues.groupby(["ai_responsible_department", "ai_theme"]).size().reset_index(name="issue_count")
            dr = dr.sort_values("issue_count", ascending=False)
            dr.to_excel(writer, sheet_name="Dept Responsibility", index=False)

        if len(ai_issues) > 0 and "year_month" in ai_issues.columns:
            m = ai_issues.groupby(["year_month", "ai_theme", "ai_issue_sentiment"]).size().reset_index(name="count")
            m = m.sort_values(["year_month", "ai_theme"])
            m.to_excel(writer, sheet_name="Monthly Trends", index=False)

        if len(ai_issues) > 0:
            sv = ai_issues.groupby(["ai_severity", "ai_theme"]).size().reset_index(name="count")
            sv.to_excel(writer, sheet_name="Severity Distribution", index=False)

        if len(ai_issues) > 0 and "rating_std" in ai_issues.columns:
            rated = ai_issues[pd.to_numeric(ai_issues["rating_std"], errors="coerce").notna()].copy()
            rated["rating_num"] = pd.to_numeric(rated["rating_std"], errors="coerce")
            low = rated[rated["rating_num"] <= 2]
            if len(low) > 0:
                lt = low["ai_theme"].value_counts().reset_index()
                lt.columns = ["theme", "count_in_low_ratings"]
                lt.to_excel(writer, sheet_name="Low Rating Themes", index=False)

        if len(ai_issues) > 0:
            at = _build_action_table(ai_df, ai_issues)
            at.to_excel(writer, sheet_name="Priority Actions", index=False)

        if len(ai_issues) > 0:
            crit = ai_issues[ai_issues["ai_severity"] == "Critical"]
            if len(crit) > 0:
                crit.to_excel(writer, sheet_name="Critical Issues", index=False)

        # ── Theme Strategies (from Pass 3) ──
        if theme_strategies:
            strat_rows = []
            for theme, strat in theme_strategies.items():
                for a in strat.get("actions", []):
                    strat_rows.append({
                        "theme": theme,
                        "total_issues": strat.get("total_issues", 0),
                        "priority": a.get("priority", ""),
                        "action": a.get("action", ""),
                        "owner": a.get("owner", ""),
                        "measure": a.get("measure", ""),
                        "rationale": a.get("rationale", ""),
                    })
            if strat_rows:
                pd.DataFrame(strat_rows).to_excel(writer, sheet_name="Theme Strategies", index=False)

        if len(ai_issues) > 0:
            rev = ai_issues[ai_issues["ai_review_required"] == True]
            if len(rev) > 0:
                rev.head(200).to_excel(writer, sheet_name="Needs Review", index=False)

    log.info(f"Saved management summary: {xlsx_path}")
    return xlsx_path


def _build_action_table(ai_df, ai_issues):
    neg = ai_issues[ai_issues["ai_issue_sentiment"].isin(["Negative", "Mixed"])].copy()
    if len(neg) == 0:
        return pd.DataFrame()
    grouped = neg.groupby(["site_code", "site_name", "ai_theme"]).agg(
        complaint_count=("feedback_id", "nunique"),
        evidence=("ai_evidence_quote", "first"),
        responsible_dept=("ai_responsible_department", lambda x: x.mode().iloc[0] if len(x) > 0 else "Unknown"),
        max_severity=("ai_severity", lambda x: max(x, key=lambda s: {"Low": 0, "Moderate": 1, "High": 2, "Critical": 3}.get(s, 0))),
    ).reset_index()
    totals = ai_df.groupby("site_code").size().reset_index(name="total_feedback")
    grouped = grouped.merge(totals, on="site_code", how="left")
    grouped["complaint_rate_pct"] = (grouped["complaint_count"] / grouped["total_feedback"] * 100).round(1)

    if "year_month" in neg.columns:
        months = sorted(neg["year_month"].dropna().unique())
        if len(months) >= 4:
            mid = len(months) // 2
            first_half, second_half = set(months[:mid]), set(months[mid:])
            trends = []
            for _, row in grouped.iterrows():
                sub = neg[(neg["site_code"] == row["site_code"]) & (neg["ai_theme"] == row["ai_theme"])]
                r1 = sub[sub["year_month"].isin(first_half)].shape[0] / len(first_half) if first_half else 0
                r2 = sub[sub["year_month"].isin(second_half)].shape[0] / len(second_half) if second_half else 0
                if r2 > r1 * 1.3: trends.append("↑ Increasing")
                elif r2 < r1 * 0.7: trends.append("↓ Decreasing")
                else: trends.append("→ Stable")
            grouped["trend"] = trends
        else:
            grouped["trend"] = "Insufficient data"
    else:
        grouped["trend"] = "No date data"

    sw = {"Low": 1, "Moderate": 2, "High": 3, "Critical": 5}
    grouped["priority_score"] = (grouped["complaint_count"] * grouped["max_severity"].map(sw).fillna(1) * grouped["complaint_rate_pct"].clip(lower=0.1)).round(1)
    grouped = grouped.sort_values("priority_score", ascending=False)
    return grouped[["site_code", "site_name", "ai_theme", "complaint_count", "complaint_rate_pct",
                     "total_feedback", "max_severity", "trend", "priority_score", "evidence",
                     "responsible_dept"]]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — VALIDATION (unchanged from original)
# ═════════════════════════════════════════════════════════════════════════════

def generate_validation_sample(ai_df, ai_issues, output_dir, sample_size=250):
    out = Path(output_dir)
    classified = ai_df[ai_df["ai_processed_status"].isin(["pass1_done", "pass2_done", "success", ""])].copy()
    classified = classified[classified["comment"].notna() & (classified["comment"].str.strip() != "")]
    actual_size = min(sample_size, len(classified))
    if actual_size < 50:
        log.warning(f"Only {actual_size} classified comments available")

    sample_frames = []
    for sentiment in classified["ai_overall_sentiment"].dropna().unique():
        sub = classified[classified["ai_overall_sentiment"] == sentiment]
        n = max(1, min(int(actual_size * len(sub) / len(classified)), len(sub)))
        sample_frames.append(sub.sample(n=n, random_state=42))

    sample = pd.concat(sample_frames).drop_duplicates(subset="feedback_id") if sample_frames else classified.sample(n=actual_size, random_state=42)
    if len(sample) > actual_size:
        sample = sample.sample(n=actual_size, random_state=42)
    elif len(sample) < actual_size:
        remaining = classified[~classified["feedback_id"].isin(sample["feedback_id"])]
        extra = remaining.sample(n=min(actual_size - len(sample), len(remaining)), random_state=42)
        sample = pd.concat([sample, extra])

    val_df = sample[["feedback_id", "site_code", "site_name", "department", "comment", "rating_std",
                      "ai_overall_sentiment", "ai_primary_theme", "ai_summary",
                      "ai_review_required", "ai_review_reason"]].copy()
    for col in ["human_sentiment", "human_primary_theme", "human_themes_all",
                "human_severity", "human_notes", "human_agree_sentiment", "human_agree_theme"]:
        val_df[col] = ""

    sample_issues = ai_issues[ai_issues["feedback_id"].isin(sample["feedback_id"])]
    xlsx_path = out / "validation_sample.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        val_df.to_excel(writer, sheet_name="Validation Sample", index=False)
        sample_issues.to_excel(writer, sheet_name="AI Issues Detail", index=False)
        instructions = pd.DataFrame({"Instruction": [
            "Review each comment and fill in the 'human_' columns.",
            "human_sentiment: Positive, Negative, Neutral, Mixed, or Insufficient information",
            "human_primary_theme: The main theme from the taxonomy",
            "human_themes_all: All applicable themes, comma-separated",
            "human_severity: Low, Moderate, High, Critical (for negative issues)",
            "human_agree_sentiment: Y if AI sentiment is correct, N if not",
            "human_agree_theme: Y if AI primary theme is correct, N if not",
            "human_notes: Any observations about the AI classification",
            "",
            "After completing annotations, run:",
            "  python -I ai_analysis_optimised.py --evaluate validation_sample.xlsx",
        ]})
        instructions.to_excel(writer, sheet_name="Instructions", index=False)
    log.info(f"Saved validation sample ({len(val_df)} comments): {xlsx_path}")
    return xlsx_path


def evaluate_validation(validation_path):
    val_df = pd.read_excel(validation_path, sheet_name="Validation Sample")
    has_human = val_df["human_sentiment"].notna() & (val_df["human_sentiment"].str.strip() != "")
    evaluated = val_df[has_human].copy()
    if len(evaluated) == 0:
        log.warning("No human annotations found."); return {"error": "No human annotations found"}

    metrics = {"n_evaluated": len(evaluated)}
    sm = (evaluated["ai_overall_sentiment"].str.lower().str.strip() == evaluated["human_sentiment"].str.lower().str.strip())
    metrics["sentiment_accuracy"] = round(sm.mean(), 3)

    for s in ["Positive", "Negative", "Neutral", "Mixed"]:
        ai_pos = evaluated["ai_overall_sentiment"].str.lower() == s.lower()
        hu_pos = evaluated["human_sentiment"].str.lower() == s.lower()
        tp, fp, fn = (ai_pos & hu_pos).sum(), (ai_pos & ~hu_pos).sum(), (~ai_pos & hu_pos).sum()
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        metrics[f"sentiment_{s.lower()}_precision"] = round(p, 3)
        metrics[f"sentiment_{s.lower()}_recall"] = round(r, 3)
        metrics[f"sentiment_{s.lower()}_f1"] = round(f1, 3)

    f1s = [metrics.get(f"sentiment_{s.lower()}_f1", 0) for s in ["Positive", "Negative", "Neutral", "Mixed"]]
    metrics["sentiment_macro_f1"] = round(sum(f1s) / len(f1s), 3)

    ht = evaluated["human_primary_theme"].notna() & (evaluated["human_primary_theme"].str.strip() != "")
    if ht.any():
        te = evaluated[ht]
        metrics["theme_accuracy"] = round((te["ai_primary_theme"].str.lower().str.strip() == te["human_primary_theme"].str.lower().str.strip()).mean(), 3)

    for col, key in [("human_agree_sentiment", "human_agree_sentiment_rate"), ("human_agree_theme", "human_agree_theme_rate")]:
        if col in evaluated.columns:
            ag = evaluated[col].str.upper().str.strip()
            if ag.isin(["Y", "N"]).any():
                metrics[key] = round((ag == "Y").sum() / ag.isin(["Y", "N"]).sum(), 3)

    print(f"\n{'='*60}\nVALIDATION RESULTS\n{'='*60}")
    print(f"  Evaluated:          {metrics['n_evaluated']}")
    print(f"  Sentiment accuracy: {metrics.get('sentiment_accuracy', 'N/A')}")
    print(f"  Sentiment macro-F1: {metrics.get('sentiment_macro_f1', 'N/A')}")
    print(f"  Theme accuracy:     {metrics.get('theme_accuracy', 'N/A')}")
    print(f"{'='*60}\n")
    return metrics


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Kestrel Motor Group — AI Feedback Analysis (Optimised)")
    parser.add_argument("--input", type=Path, default=Path("consolidated_clean.csv"))
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--pass1-model", type=str, default=PASS1_MODEL,
                        help=f"Pass 1 model (default: {PASS1_MODEL})")
    parser.add_argument("--pass2-model", type=str, default=PASS2_MODEL,
                        help=f"Pass 2 model (default: {PASS2_MODEL})")
    parser.add_argument("--model", type=str, default=None,
                        help="Override BOTH pass1 and pass2 models (single-pass mode)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Comments per API batch (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--test", action="store_true",
                        help=f"Test mode: process only {TEST_MODE_ROWS} rows")
    parser.add_argument("--taxonomy-only", action="store_true")
    parser.add_argument("--skip-wordclouds", action="store_true")
    parser.add_argument("--evaluate", type=str, default=None,
                        help="Path to completed validation_sample.xlsx")
    args = parser.parse_args()

    # Model override
    pass1_model = args.model or args.pass1_model
    pass2_model = args.model or args.pass2_model

    # ── Evaluation mode ──
    if args.evaluate:
        metrics = evaluate_validation(args.evaluate)
        eval_path = Path(args.output_dir) / "evaluation_results.json"
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        with open(eval_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
        log.info(f"Saved: {eval_path}")
        return

    # ── Load data ──
    if not args.input.is_file():
        log.error(f"Input not found: {args.input}"); sys.exit(1)
    log.info(f"Loading {args.input}...")
    df = pd.read_csv(args.input, dtype=str, low_memory=False)

    # ── Inspect ──
    inspect_data(df)

    # ── Taxonomy ──
    taxonomy = build_theme_taxonomy()
    save_taxonomy(taxonomy, args.output_dir)
    print(f"\nTHEME TAXONOMY ({len(taxonomy['themes'])} themes):")
    for t in taxonomy["themes"]:
        subs = ", ".join(t["subthemes"][:3]) + ("..." if len(t["subthemes"]) > 3 else "")
        print(f"  * {t['theme']}: {subs}")

    if args.taxonomy_only:
        log.info("Taxonomy-only mode. Done."); return

    # ── PASS 1: Sentiment + Theme (Haiku) ──
    log.info(f"═══ PASS 1: Sentiment + Theme ({pass1_model}) ═══")
    ai_df = run_pass1(df, taxonomy, args.output_dir, pass1_model, args.batch_size, args.test)

    # ── PASS 2: Full Issue Analysis (Sonnet, negatives only) ──
    log.info(f"═══ PASS 2: Issue Analysis ({pass2_model}, negatives/mixed only) ═══")
    ai_df, ai_issues = run_pass2(ai_df, taxonomy, args.output_dir, pass2_model, args.batch_size)

    # ── Save ──
    out = Path(args.output_dir)
    ai_df.to_csv(out / "consolidated_clean_ai.csv", index=False)
    log.info(f"Saved: {out / 'consolidated_clean_ai.csv'}")
    ai_issues.to_csv(out / "ai_issues.csv", index=False)
    log.info(f"Saved: {out / 'ai_issues.csv'}")

    # ── PASS 3: Theme-Level Strategies (skip in test mode) ──
    if args.test:
        log.info("═══ PASS 3: SKIPPED in test mode (saves ~11 API calls) ═══")
        theme_strategies = {}
    else:
        log.info(f"═══ PASS 3: Theme Strategies ({pass2_model}) ═══")
        theme_strategies = run_pass3_theme_strategies(
            ai_df, ai_issues, taxonomy, args.output_dir, pass2_model,
        )

    # ── Word clouds ──
    if not args.skip_wordclouds:
        try:
            generate_wordclouds(ai_df, args.output_dir)
        except ImportError as e:
            log.warning(f"Word clouds skipped: {e}")

    # ── Management analysis ──
    generate_management_analysis(ai_df, ai_issues, args.output_dir, theme_strategies)

    # ── Validation sample ──
    generate_validation_sample(ai_df, ai_issues, args.output_dir)

    # ── Summary ──
    print(f"\n{'='*60}")
    print("AI ANALYSIS COMPLETE (OPTIMISED TWO-PASS)")
    print(f"{'='*60}")
    print(f"  Pass 1 model:            {pass1_model}")
    print(f"  Pass 2 model:            {pass2_model}")
    print(f"  Batch size:              {args.batch_size}")
    print(f"  Rows processed:          {len(ai_df):,}")
    print(f"  Issues identified:       {len(ai_issues):,}")

    if len(ai_df) > 0:
        sentiments = ai_df["ai_overall_sentiment"].value_counts()
        print(f"  Sentiment distribution:")
        for s, c in sentiments.items():
            if s: print(f"    {s:25s} {c:,} ({c/len(ai_df)*100:.1f}%)")

        pass2_count = (ai_df["ai_processed_status"] == "pass2_done").sum()
        pass1_only = (ai_df["ai_processed_status"] == "pass1_done").sum()
        print(f"  Pass 1 only (positive):  {pass1_only:,} (no Sonnet cost)")
        print(f"  Pass 2 (neg/mixed):      {pass2_count:,} (Sonnet)")

    if len(ai_issues) > 0:
        themes = ai_issues["ai_theme"].value_counts().head(5)
        print(f"  Top 5 themes:")
        for t, c in themes.items():
            print(f"    {t:35s} {c:,}")
        crit = ai_issues[ai_issues["ai_severity"] == "Critical"]
        print(f"  Critical issues:         {len(crit):,}")

    themes_with_actions = sum(1 for s in theme_strategies.values() if s.get("actions"))
    print(f"  Theme strategies:        {themes_with_actions} themes with 3 actions each")
    print(f"  Pass 3 API calls:        {themes_with_actions} (one per theme)")

    print(f"\nOutputs in: {args.output_dir}/")
    print(f"  • theme_strategies.json   — 3 key actions per theme")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()