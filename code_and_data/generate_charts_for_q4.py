#!/usr/bin/env python3
"""
Kestrel Motor Group — Generate charts (PNGs) + report stats
=============================================================
Usage:
    python generate_charts.py

Expects these files in the same directory (or edit the paths below):
    - consolidated_clean_ai.csv
    - ai_issues.csv
    - reference/monthly_revenue.csv

Outputs:
    - charts/01_top_themes.png  ... 14_safety_breakdown.png
    - report_stats.md   (all computed numbers & tables for the report)
"""

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import os
import textwrap

# ── CONFIG — edit these if your files live somewhere else ─────────────
FEEDBACK_CSV = "ai_output/consolidated_clean_ai.csv"
ISSUES_CSV   = "ai_output/ai_issues.csv"
REVENUE_CSV  = "reference/monthly_revenue.csv"
CHART_DIR    = "charts"
STATS_PATH   = "report_stats.md"
# ─────────────────────────────────────────────────────────────────────

os.makedirs(CHART_DIR, exist_ok=True)

# ── Chart style ──────────────────────────────────────────────────────
plt.rcParams.update({
    'figure.facecolor': 'white',
    'axes.facecolor':   'white',
    'font.family':      'DejaVu Sans',
    'font.size':        11,
    'axes.titlesize':   14,
    'axes.titleweight': 'bold',
    'axes.grid':        True,
    'grid.alpha':       0.3,
    'figure.dpi':       150,
})
COLORS = [
    '#E63946', '#457B9D', '#2A9D8F', '#E9C46A', '#F4A261',
    '#264653', '#A8DADC', '#6D6875', '#B5838D', '#FFB4A2',
]

# ── Load data ────────────────────────────────────────────────────────
fb     = pd.read_csv(FEEDBACK_CSV)
issues = pd.read_csv(ISSUES_CSV)
rev    = pd.read_csv(REVENUE_CSV)

fb['date_parsed']     = pd.to_datetime(fb['date_parsed'],     errors='coerce')
issues['date_parsed'] = pd.to_datetime(issues['date_parsed'], errors='coerce')

# Normalise revenue month to YYYY-MM format to match year_month
rev['month_parsed'] = pd.to_datetime(rev['month'], format='mixed', dayfirst=False, errors='coerce')
if rev['month_parsed'].isna().all():
    for fmt in ['%Y-%m', '%Y-%m-%d', '%b-%Y', '%B %Y']:
        try:
            rev['month_parsed'] = pd.to_datetime(rev['month'], format=fmt)
            if rev['month_parsed'].notna().sum() > 0:
                break
        except Exception:
            continue
rev['year_month'] = rev['month_parsed'].dt.strftime('%Y-%m')

for col in ['vehicles_sold', 'sales_revenue_aud', 'service_jobs',
            'service_revenue_aud', 'parts_transactions', 'parts_revenue_aud']:
    rev[col] = pd.to_numeric(rev[col], errors='coerce').fillna(0)

rev['total_revenue'] = rev['sales_revenue_aud'] + rev['service_revenue_aud'] + rev['parts_revenue_aud']
rev['total_transactions'] = rev['vehicles_sold'] + rev['service_jobs'] + rev['parts_transactions']

# H1 (Oct 2025 – Mar 2026) vs H2 (Apr – Sep 2026)
for df in [fb, issues]:
    df['half'] = df['year_month'].apply(
        lambda x: 'H1 (Oct 25–Mar 26)' if x <= '2026-03'
        else 'H2 (Apr–Sep 26)' if pd.notna(x) else None
    )
rev['half'] = rev['year_month'].apply(
    lambda x: 'H1 (Oct 25–Mar 26)' if x <= '2026-03'
    else 'H2 (Apr–Sep 26)' if pd.notna(x) else None
)

neg = issues[issues['ai_issue_sentiment'] == 'Negative'].copy()

# ── Revenue summaries ────────────────────────────────────────────────
rev_by_site = rev.groupby('dealer_code').agg(
    total_revenue      = ('total_revenue', 'sum'),
    sales_revenue      = ('sales_revenue_aud', 'sum'),
    service_revenue    = ('service_revenue_aud', 'sum'),
    parts_revenue      = ('parts_revenue_aud', 'sum'),
    vehicles_sold      = ('vehicles_sold', 'sum'),
    service_jobs       = ('service_jobs', 'sum'),
    parts_transactions = ('parts_transactions', 'sum'),
).reset_index()

rev_by_month = rev.groupby('year_month').agg(
    total_revenue   = ('total_revenue', 'sum'),
    service_revenue = ('service_revenue_aud', 'sum'),
    parts_revenue   = ('parts_revenue_aud', 'sum'),
    service_jobs    = ('service_jobs', 'sum'),
).reset_index().sort_values('year_month')

rev_by_half = rev.groupby('half').agg(
    total_revenue   = ('total_revenue', 'sum'),
    service_revenue = ('service_revenue_aud', 'sum'),
    parts_revenue   = ('parts_revenue_aud', 'sum'),
    service_jobs    = ('service_jobs', 'sum'),
).reset_index()

print(f"Feedback rows  : {len(fb):,}")
print(f"Issue rows     : {len(issues):,}")
print(f"Negative issues: {len(neg):,}")
print(f"Revenue rows   : {len(rev):,}")
print(f"Total revenue  : ${rev['total_revenue'].sum():,.0f}")
print()

# =====================================================================
# CHART 1 — Top drivers of negative feedback
# =====================================================================
theme_counts = neg['ai_theme'].value_counts().head(10)

fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(range(len(theme_counts)), theme_counts.values,
        color=COLORS[:len(theme_counts)])
ax.set_yticks(range(len(theme_counts)))
ax.set_yticklabels(theme_counts.index, fontsize=11)
ax.invert_yaxis()
ax.set_xlabel('Number of Negative Issues')
ax.set_title('Main Drivers of Negative Feedback\n(by AI-classified theme)')
for i, v in enumerate(theme_counts.values):
    ax.text(v + 5, i, str(v), va='center', fontsize=10, fontweight='bold')
ax.set_xlim(0, theme_counts.values[0] * 1.15)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/01_top_themes.png', bbox_inches='tight')
plt.close()
print("  01 top themes")

# =====================================================================
# CHART 2 — Theme x Department heatmap
# =====================================================================
top_themes = neg['ai_theme'].value_counts().head(8).index.tolist()
top_depts  = neg['department'].value_counts().head(6).index.tolist()
cross  = neg[neg['ai_theme'].isin(top_themes) & neg['department'].isin(top_depts)]
pivot  = (cross.pivot_table(index='ai_theme', columns='department',
                            values='feedback_id', aggfunc='count', fill_value=0)
               .reindex(index=top_themes))
pivot  = pivot[[c for c in top_depts if c in pivot.columns]]

fig, ax = plt.subplots(figsize=(10, 6))
im = ax.imshow(pivot.values, cmap='YlOrRd', aspect='auto')
ax.set_xticks(range(len(pivot.columns)))
ax.set_xticklabels(pivot.columns, rotation=30, ha='right', fontsize=10)
ax.set_yticks(range(len(pivot.index)))
ax.set_yticklabels(pivot.index, fontsize=10)
for i in range(len(pivot.index)):
    for j in range(len(pivot.columns)):
        val = pivot.values[i, j]
        if val > 0:
            c = 'white' if val > pivot.values.max() * 0.6 else 'black'
            ax.text(j, i, str(int(val)), ha='center', va='center',
                    fontsize=9, color=c, fontweight='bold')
ax.set_title('Negative Issues: Theme x Department')
fig.colorbar(im, ax=ax, shrink=0.8, label='Issue Count')
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/02_theme_department_heatmap.png', bbox_inches='tight')
plt.close()
print("  02 theme x department heatmap")

# =====================================================================
# CHART 3 — Worst dealerships
# =====================================================================
site_neg = (neg.groupby(['site_code', 'site_name']).size()
               .reset_index(name='neg_count')
               .sort_values('neg_count', ascending=False).head(12))
labels = [f"{r.site_code} ({r.site_name})" for _, r in site_neg.iterrows()]

fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(range(len(site_neg)), site_neg['neg_count'].values,
        color='#E63946', alpha=0.85)
ax.set_yticks(range(len(site_neg)))
ax.set_yticklabels(labels, fontsize=10)
ax.invert_yaxis()
ax.set_xlabel('Number of Negative Issues')
ax.set_title('Top 12 Dealerships by Negative Issue Volume')
for i, v in enumerate(site_neg['neg_count'].values):
    ax.text(v + 3, i, str(v), va='center', fontsize=10, fontweight='bold')
ax.set_xlim(0, site_neg['neg_count'].values[0] * 1.15)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/03_worst_dealerships.png', bbox_inches='tight')
plt.close()
print("  03 worst dealerships")

# =====================================================================
# CHART 4 — Theme profile for top 5 worst dealerships
# =====================================================================
top5_sites   = site_neg.head(5)['site_code'].tolist()
top6_themes  = neg['ai_theme'].value_counts().head(6).index.tolist()
site_theme   = (neg[neg['site_code'].isin(top5_sites)]
                .groupby(['site_code', 'ai_theme']).size()
                .reset_index(name='count'))
site_theme_f = site_theme[site_theme['ai_theme'].isin(top6_themes)]
pivot_st     = site_theme_f.pivot_table(
    index='site_code', columns='ai_theme', values='count', fill_value=0
)[top6_themes]

fig, ax = plt.subplots(figsize=(10, 6))
pivot_st.plot(kind='bar', stacked=True, ax=ax,
              color=COLORS[:len(top6_themes)], edgecolor='white', linewidth=0.5)
ax.set_title('Theme Profile: Top 5 Worst Dealerships')
ax.set_xlabel('')
ax.set_ylabel('Negative Issue Count')
ax.legend(title='Theme', bbox_to_anchor=(1.02, 1), loc='upper left', fontsize=8)
ax.set_xticklabels(ax.get_xticklabels(), rotation=0)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/04_dealership_theme_profile.png', bbox_inches='tight')
plt.close()
print("  04 dealership theme profile")

# =====================================================================
# CHART 5 — Monthly trend
# =====================================================================
monthly_neg   = neg.groupby('year_month').size().reset_index(name='count').sort_values('year_month')
monthly_total = fb.groupby('year_month').size().reset_index(name='total')
monthly       = monthly_neg.merge(monthly_total, on='year_month', how='left')
monthly['neg_rate'] = (monthly['count'] / monthly['total'] * 100).round(1)

fig, ax1 = plt.subplots(figsize=(12, 5))
ax1.bar(range(len(monthly)), monthly['count'], color='#E63946', alpha=0.7,
        label='Negative Issues')
ax1.set_xticks(range(len(monthly)))
ax1.set_xticklabels(monthly['year_month'], rotation=45, ha='right', fontsize=9)
ax1.set_ylabel('Negative Issue Count', color='#E63946')
ax1.set_title('Monthly Trend: Negative Feedback Volume & Rate')

ax2 = ax1.twinx()
ax2.plot(range(len(monthly)), monthly['neg_rate'],
         color='#264653', marker='o', linewidth=2, label='Neg Rate (%)')
ax2.set_ylabel('Negative Rate (%)', color='#264653')

h1_months = [i for i, m in enumerate(monthly['year_month']) if m <= '2026-03']
if h1_months:
    ax1.axvline(x=max(h1_months) + 0.5, color='gray', ls='--', alpha=0.5)
    ax1.text(max(h1_months) / 2, ax1.get_ylim()[1] * 0.95,
             'H1', ha='center', fontsize=12, color='gray', alpha=0.6)
    ax1.text((max(h1_months) + len(monthly)) / 2, ax1.get_ylim()[1] * 0.95,
             'H2', ha='center', fontsize=12, color='gray', alpha=0.6)

l1, lb1 = ax1.get_legend_handles_labels()
l2, lb2 = ax2.get_legend_handles_labels()
ax1.legend(l1 + l2, lb1 + lb2, loc='upper left')
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/05_monthly_trend.png', bbox_inches='tight')
plt.close()
print("  05 monthly trend")

# =====================================================================
# CHART 6 — H1 vs H2 comparison
# =====================================================================
h1_themes = neg[neg['half'] == 'H1 (Oct 25–Mar 26)']['ai_theme'].value_counts()
h2_themes = neg[neg['half'] == 'H2 (Apr–Sep 26)']['ai_theme'].value_counts()
compare   = pd.DataFrame({'H1': h1_themes, 'H2': h2_themes}).fillna(0).astype(int)
compare   = compare.loc[compare.sum(axis=1).sort_values(ascending=False).head(8).index]
compare['change']     = compare['H2'] - compare['H1']
compare['pct_change'] = ((compare['H2'] - compare['H1']) /
                          compare['H1'].replace(0, 1) * 100).round(0)

fig, ax = plt.subplots(figsize=(10, 6))
x = range(len(compare))
w = 0.35
ax.bar([i - w/2 for i in x], compare['H1'], w, color='#457B9D', label='H1 (Oct-Mar)')
ax.bar([i + w/2 for i in x], compare['H2'], w, color='#E63946', label='H2 (Apr-Sep)')
ax.set_xticks(x)
ax.set_xticklabels([textwrap.fill(t, 16) for t in compare.index],
                   rotation=30, ha='right', fontsize=9)
ax.set_ylabel('Negative Issue Count')
ax.set_title('H1 vs H2: Has Anything Changed?')
ax.legend()
for i, idx in enumerate(compare.index):
    chg = compare.loc[idx, 'change']
    pct = compare.loc[idx, 'pct_change']
    sign = '+' if chg > 0 else ''
    clr  = '#E63946' if chg > 0 else '#2A9D8F'
    ax.text(i, max(compare.loc[idx, 'H1'], compare.loc[idx, 'H2']) + 5,
            f'{sign}{int(chg)} ({sign}{int(pct)}%)',
            ha='center', fontsize=8, color=clr, fontweight='bold')
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/06_h1_vs_h2.png', bbox_inches='tight')
plt.close()
print("  06 H1 vs H2")

# =====================================================================
# CHART 7 — Severity distribution by theme
# =====================================================================
sev_order  = ['Critical', 'High', 'Moderate', 'Low']
sev_colors = {'Critical': '#9D0208', 'High': '#E63946',
              'Moderate': '#F4A261', 'Low': '#2A9D8F'}
top8       = neg['ai_theme'].value_counts().head(8).index.tolist()
sev_data   = (neg[neg['ai_theme'].isin(top8)]
              .groupby(['ai_theme', 'ai_severity']).size().unstack(fill_value=0))
sev_data   = sev_data.reindex(
    columns=[s for s in sev_order if s in sev_data.columns], fill_value=0
).loc[top8]

fig, ax = plt.subplots(figsize=(10, 6))
sev_data.plot(kind='barh', stacked=True, ax=ax,
              color=[sev_colors.get(s, 'gray') for s in sev_data.columns])
ax.set_xlabel('Number of Negative Issues')
ax.set_title('Severity Distribution by Theme')
ax.legend(title='Severity', bbox_to_anchor=(1.02, 1), loc='upper left')
ax.invert_yaxis()
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/07_severity_by_theme.png', bbox_inches='tight')
plt.close()
print("  07 severity by theme")

# =====================================================================
# CHART 8 — Department breakdown
# =====================================================================
dept_neg = neg['department'].value_counts().head(8)

fig, ax = plt.subplots(figsize=(8, 5))
ax.barh(range(len(dept_neg)), dept_neg.values, color='#457B9D', alpha=0.85)
ax.set_yticks(range(len(dept_neg)))
ax.set_yticklabels(dept_neg.index, fontsize=10)
ax.invert_yaxis()
ax.set_xlabel('Number of Negative Issues')
ax.set_title('Negative Issues by Department')
for i, v in enumerate(dept_neg.values):
    ax.text(v + 3, i, str(v), va='center', fontsize=10, fontweight='bold')
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/08_departments.png', bbox_inches='tight')
plt.close()
print("  08 departments")

# =====================================================================
# CHART 9 — Negative Issues per $M Revenue by Dealership
# =====================================================================
site_neg_all = neg.groupby('site_code').size().reset_index(name='neg_count')
site_rev = site_neg_all.merge(rev_by_site, left_on='site_code',
                               right_on='dealer_code', how='inner')
site_rev['neg_per_M'] = (site_rev['neg_count'] /
                          (site_rev['total_revenue'] / 1_000_000)).round(1)
site_rev = site_rev.sort_values('neg_per_M', ascending=False).head(15)

fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(range(len(site_rev)), site_rev['neg_per_M'].values,
        color='#E63946', alpha=0.85)
ax.set_yticks(range(len(site_rev)))
ax.set_yticklabels(site_rev['site_code'].values, fontsize=10)
ax.invert_yaxis()
ax.set_xlabel('Negative Issues per $1M Revenue')
ax.set_title('Complaint Intensity: Negative Issues per $1M Revenue\n(revenue-normalised — highlights underperformers relative to size)')
for i, (neg_pm, rev_tot) in enumerate(
        zip(site_rev['neg_per_M'].values, site_rev['total_revenue'].values)):
    ax.text(neg_pm + 0.3, i,
            f'{neg_pm:.1f}  (${rev_tot/1e6:.1f}M rev)',
            va='center', fontsize=9, fontweight='bold')
ax.set_xlim(0, site_rev['neg_per_M'].values[0] * 1.25)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/09_neg_per_M_revenue.png', bbox_inches='tight')
plt.close()
print("  09 neg per $M revenue")

# =====================================================================
# CHART 10 — Monthly Revenue vs Negative Issues (dual axis)
# =====================================================================
monthly_combined = monthly.merge(rev_by_month, on='year_month', how='inner')

fig, ax1 = plt.subplots(figsize=(12, 5))
x = range(len(monthly_combined))
ax1.fill_between(x, monthly_combined['total_revenue'] / 1e6,
                 color='#2A9D8F', alpha=0.25, label='Total Revenue ($M)')
ax1.plot(x, monthly_combined['total_revenue'] / 1e6, color='#2A9D8F', linewidth=2)
ax1.set_xticks(x)
ax1.set_xticklabels(monthly_combined['year_month'], rotation=45, ha='right', fontsize=9)
ax1.set_ylabel('Total Revenue ($M)', color='#2A9D8F')
ax1.set_title('Monthly Revenue vs Negative Feedback')

ax2 = ax1.twinx()
ax2.plot(x, monthly_combined['count'], color='#E63946',
         marker='s', linewidth=2.5, label='Negative Issues', zorder=5)
ax2.set_ylabel('Negative Issue Count', color='#E63946')

h1_idx = [i for i, m in enumerate(monthly_combined['year_month']) if m <= '2026-03']
if h1_idx:
    ax1.axvline(x=max(h1_idx) + 0.5, color='gray', ls='--', alpha=0.5)

l1, lb1 = ax1.get_legend_handles_labels()
l2, lb2 = ax2.get_legend_handles_labels()
ax1.legend(l1 + l2, lb1 + lb2, loc='upper left')
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/10_revenue_vs_complaints.png', bbox_inches='tight')
plt.close()
print("  10 revenue vs complaints")

# =====================================================================
# CHART 11 — Revenue at Risk: Top Dealerships
# =====================================================================
site_neg_rev = (neg.groupby(['site_code', 'site_name'])
                   .agg(neg_count=('feedback_id', 'size'), neg_comments=('feedback_id', 'nunique'))
                   .reset_index())
site_neg_rev = site_neg_rev.merge(rev_by_site, left_on='site_code',
                                   right_on='dealer_code', how='inner')
site_neg_rev['avg_service_ticket'] = (
    site_neg_rev['service_revenue'] / site_neg_rev['service_jobs'].replace(0, np.nan)
).fillna(0)
site_neg_rev['est_revenue_at_risk'] = (
    site_neg_rev['neg_comments'] * 0.15 * 3 * site_neg_rev['avg_service_ticket']
)
site_neg_rev = site_neg_rev.sort_values('est_revenue_at_risk', ascending=False).head(12)

fig, ax = plt.subplots(figsize=(10, 6))
bar_labels = [f"{r.site_code} ({r.site_name})" for _, r in site_neg_rev.iterrows()]
ax.barh(range(len(site_neg_rev)),
        site_neg_rev['est_revenue_at_risk'].values / 1000,
        color='#9D0208', alpha=0.8)
ax.set_yticks(range(len(site_neg_rev)))
ax.set_yticklabels(bar_labels, fontsize=9)
ax.invert_yaxis()
ax.set_xlabel("Estimated Annual Revenue at Risk ($'000)")
ax.set_title('Revenue at Risk from Negative Feedback\n(est. per complaining customer: 15% churn rate x 3 service visits x avg ticket)')
for i, v in enumerate(site_neg_rev['est_revenue_at_risk'].values):
    ax.text(v / 1000 + 1, i, f'${v/1000:.0f}K', va='center', fontsize=9, fontweight='bold')
ax.set_xlim(0, site_neg_rev['est_revenue_at_risk'].values[0] / 1000 * 1.2)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/11_revenue_at_risk.png', bbox_inches='tight')
plt.close()
print("  11 revenue at risk")

# =====================================================================
# CHART 12 — Neg Issues per 1000 Service Jobs by Dealership
# =====================================================================
site_svc = (neg[neg['department'].str.lower().str.contains('service', na=False)]
            .groupby('site_code').size()
            .reset_index(name='svc_neg'))
site_svc = site_svc.merge(rev_by_site[['dealer_code', 'service_jobs']],
                           left_on='site_code', right_on='dealer_code', how='inner')
site_svc['neg_per_1k_jobs'] = (
    site_svc['svc_neg'] / (site_svc['service_jobs'] / 1000)
).round(1)
site_svc = site_svc.sort_values('neg_per_1k_jobs', ascending=False).head(15)

fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(range(len(site_svc)), site_svc['neg_per_1k_jobs'].values,
        color='#457B9D', alpha=0.85)
ax.set_yticks(range(len(site_svc)))
ax.set_yticklabels(site_svc['site_code'].values, fontsize=10)
ax.invert_yaxis()
ax.set_xlabel('Negative Service Issues per 1,000 Service Jobs')
ax.set_title('Service Complaint Rate by Dealership\n(normalised by service volume — apples-to-apples comparison)')
for i, (rate, jobs) in enumerate(
        zip(site_svc['neg_per_1k_jobs'].values, site_svc['service_jobs'].values)):
    ax.text(rate + 0.2, i,
            f'{rate:.1f}  ({int(jobs):,} jobs)',
            va='center', fontsize=9, fontweight='bold')
ax.set_xlim(0, site_svc['neg_per_1k_jobs'].values[0] * 1.2)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/12_neg_per_1k_service_jobs.png', bbox_inches='tight')
plt.close()
print("  12 neg per 1k service jobs")

# =====================================================================
# CHART 13 — H1 vs H2 Revenue and Complaints side by side
# =====================================================================
h1_rev_row = rev_by_half[rev_by_half['half'] == 'H1 (Oct 25–Mar 26)']
h2_rev_row = rev_by_half[rev_by_half['half'] == 'H2 (Apr–Sep 26)']
h1_rev_total = h1_rev_row['total_revenue'].values[0] if len(h1_rev_row) else 0
h2_rev_total = h2_rev_row['total_revenue'].values[0] if len(h2_rev_row) else 0

h1_neg_count = len(neg[neg['half'] == 'H1 (Oct 25–Mar 26)'])
h2_neg_count = len(neg[neg['half'] == 'H2 (Apr–Sep 26)'])

fig, axes = plt.subplots(1, 3, figsize=(14, 5))
categories = ['Total Revenue ($M)', 'Negative Issues', 'Neg per $1M Rev']
h1_vals = [h1_rev_total / 1e6, h1_neg_count,
           h1_neg_count / (h1_rev_total / 1e6) if h1_rev_total else 0]
h2_vals = [h2_rev_total / 1e6, h2_neg_count,
           h2_neg_count / (h2_rev_total / 1e6) if h2_rev_total else 0]

for i, (cat, v1, v2) in enumerate(zip(categories, h1_vals, h2_vals)):
    ax = axes[i]
    ax.bar([0, 1], [v1, v2], color=['#457B9D', '#E63946'], width=0.5)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(['H1', 'H2'])
    ax.set_title(cat, fontsize=12, fontweight='bold')
    for j, v in enumerate([v1, v2]):
        fmt = f'${v:.1f}M' if i == 0 else (f'{int(v)}' if i == 1 else f'{v:.1f}')
        ax.text(j, v * 1.02, fmt, ha='center', fontsize=11, fontweight='bold')
    chg = (v2 - v1) / v1 * 100 if v1 else 0
    sign = '+' if chg > 0 else ''
    clr  = '#E63946' if (chg > 0 and i >= 1) else '#2A9D8F'
    if i == 0:
        clr = '#2A9D8F' if chg > 0 else '#E63946'
    ax.text(0.5, max(v1, v2) * 1.12, f'{sign}{chg:.0f}%',
            ha='center', fontsize=13, fontweight='bold', color=clr)
    ax.set_ylim(0, max(v1, v2) * 1.25)

fig.suptitle('H1 vs H2: Revenue Growth vs Complaint Growth', fontsize=14, fontweight='bold')
plt.tight_layout(rect=[0, 0, 1, 0.95])
plt.savefig(f'{CHART_DIR}/13_h1_h2_revenue_complaints.png', bbox_inches='tight')
plt.close()
print("  13 H1 vs H2 revenue & complaints")

# =====================================================================
# CHART 14 — Safety concern breakdown by sub-theme
# =====================================================================
safety     = neg[neg['ai_theme'] == 'Safety Concern']
safety_sub = safety['ai_subtheme'].value_counts()

fig, ax = plt.subplots(figsize=(9, 4))
ax.barh(range(len(safety_sub)), safety_sub.values, color='#9D0208', alpha=0.85)
ax.set_yticks(range(len(safety_sub)))
ax.set_yticklabels(safety_sub.index, fontsize=10)
ax.invert_yaxis()
ax.set_xlabel('Number of Negative Issues')
ax.set_title(f'Safety Concerns by Type ({len(safety)} issues, all Critical severity, '
             f'{safety["site_code"].nunique()} of 24 dealerships)')
for i, v in enumerate(safety_sub.values):
    ax.text(v + 1, i, str(v), va='center', fontsize=10, fontweight='bold')
ax.set_xlim(0, safety_sub.values[0] * 1.15)
plt.tight_layout()
plt.savefig(f'{CHART_DIR}/14_safety_breakdown.png', bbox_inches='tight')
plt.close()
print("  14 safety breakdown")

# =====================================================================
# COMPUTE ALL STATS & WRITE report_stats.md
# =====================================================================
print("\nComputing report stats...")

total_fb       = len(fb)
total_neg      = len(neg)
total_issues   = len(issues)
mean_rating    = fb['rating_std'].mean()
neg_rating     = fb[fb['ai_overall_sentiment'] == 'Negative']['rating_std'].mean() if 'ai_overall_sentiment' in fb.columns else float('nan')
crit_count     = int((neg['ai_severity'] == 'Critical').sum())
high_count     = int((neg['ai_severity'] == 'High').sum())
total_rev_yr   = rev['total_revenue'].sum()
total_svc_rev  = rev['service_revenue_aud'].sum()
total_parts_rev = rev['parts_revenue_aud'].sum()
total_sales_rev = rev['sales_revenue_aud'].sum()
total_svc_jobs = int(rev['service_jobs'].sum())
total_veh_sold = int(rev['vehicles_sold'].sum())
total_parts_tx = int(rev['parts_transactions'].sum())
avg_svc_ticket = total_svc_rev / total_svc_jobs if total_svc_jobs else 0

h1_count       = int(len(neg[neg['half'] == 'H1 (Oct 25–Mar 26)']))
h2_count       = int(len(neg[neg['half'] == 'H2 (Apr–Sep 26)']))
h_change_pct   = (h2_count - h1_count) / h1_count * 100 if h1_count else 0
rev_chg_pct    = (h2_rev_total - h1_rev_total) / h1_rev_total * 100 if h1_rev_total else 0
h1_neg_per_M   = h1_count / (h1_rev_total / 1e6) if h1_rev_total else 0
h2_neg_per_M   = h2_count / (h2_rev_total / 1e6) if h2_rev_total else 0
neg_per_M_chg  = (h2_neg_per_M / h1_neg_per_M - 1) * 100 if h1_neg_per_M else 0

h1_svc_rev     = h1_rev_row['service_revenue'].values[0] if len(h1_rev_row) else 0
h2_svc_rev     = h2_rev_row['service_revenue'].values[0] if len(h2_rev_row) else 0
h1_svc_jobs    = int(h1_rev_row['service_jobs'].values[0]) if len(h1_rev_row) else 0
h2_svc_jobs    = int(h2_rev_row['service_jobs'].values[0]) if len(h2_rev_row) else 0

comms_count    = int((neg['ai_theme'] == 'Communication & Updates').sum())
safety_count   = int((neg['ai_theme'] == 'Safety Concern').sum())
parts_count    = int((neg['ai_theme'] == 'Parts Availability & Delays').sum())
pricing_count  = int((neg['ai_theme'] == 'Pricing & Charges').sum())
booking_count  = int((neg['ai_theme'] == 'Booking & Scheduling').sum())
# Same churn method for every theme, per complaining customer (one comment can hold
# several issues in a theme): comments x 15% churn x 3 visits/yr x avg ticket
theme_comments  = lambda th: int(neg.loc[neg['ai_theme'] == th, 'feedback_id'].nunique())
comms_at_risk   = theme_comments('Communication & Updates') * 0.15 * 3 * avg_svc_ticket
parts_at_risk   = theme_comments('Parts Availability & Delays') * 0.15 * 3 * avg_svc_ticket
pricing_at_risk = theme_comments('Pricing & Charges') * 0.15 * 3 * avg_svc_ticket
booking_at_risk = theme_comments('Booking & Scheduling') * 0.15 * 3 * avg_svc_ticket
# Safety liability is an assumption, not calculable from this data: one serious-injury
# claim, using the TAC's average serious-injury payout (~$2.25M, 2021-22)
safety_liability = 2_250_000
combined_risk  = (comms_at_risk + parts_at_risk + pricing_at_risk + booking_at_risk
                  + safety_liability)
pct_at_risk    = combined_risk / total_rev_yr * 100 if total_rev_yr else 0

# Top 5 worst dealerships (raw count)
top5_raw = (neg.groupby(['site_code', 'site_name']).size()
               .reset_index(name='neg_count')
               .sort_values('neg_count', ascending=False).head(5))

# Top 5 worst dealerships (revenue-normalised)
site_neg_norm = neg.groupby('site_code').size().reset_index(name='neg_count')
site_neg_norm = site_neg_norm.merge(rev_by_site, left_on='site_code',
                                     right_on='dealer_code', how='inner')
site_neg_norm['neg_per_M'] = (site_neg_norm['neg_count'] /
                               (site_neg_norm['total_revenue'] / 1e6)).round(1)
top5_norm = site_neg_norm.sort_values('neg_per_M', ascending=False).head(5)

# Top 5 worst by service complaint rate
site_svc_all = (neg[neg['department'].str.lower().str.contains('service', na=False)]
                .groupby('site_code').size().reset_index(name='svc_neg'))
site_svc_all = site_svc_all.merge(rev_by_site[['dealer_code', 'service_jobs']],
                                   left_on='site_code', right_on='dealer_code', how='inner')
site_svc_all['neg_per_1k_jobs'] = (
    site_svc_all['svc_neg'] / (site_svc_all['service_jobs'] / 1000)
).round(1)
top5_svc_rate = site_svc_all.sort_values('neg_per_1k_jobs', ascending=False).head(5)

# Revenue at risk by dealership (top 10)
rar = (neg.groupby(['site_code', 'site_name'])
         .agg(neg_count=('feedback_id', 'size'), neg_comments=('feedback_id', 'nunique'))
         .reset_index())
rar = rar.merge(rev_by_site, left_on='site_code', right_on='dealer_code', how='inner')
rar['avg_ticket'] = (rar['service_revenue'] / rar['service_jobs'].replace(0, np.nan)).fillna(0)
rar['est_risk']   = rar['neg_comments'] * 0.15 * 3 * rar['avg_ticket']
rar = rar.sort_values('est_risk', ascending=False).head(10)

# Theme severity breakdown
theme_sev = (neg.groupby(['ai_theme', 'ai_severity']).size()
                .unstack(fill_value=0))
for s in ['Critical', 'High', 'Moderate', 'Low']:
    if s not in theme_sev.columns:
        theme_sev[s] = 0
theme_sev = theme_sev[['Critical', 'High', 'Moderate', 'Low']]
theme_sev['Total'] = theme_sev.sum(axis=1)
theme_sev = theme_sev.sort_values('Total', ascending=False).head(10)

# Department breakdown with revenue context
dept_counts = neg['department'].value_counts().head(8).reset_index()
dept_counts.columns = ['Department', 'Neg Issues']
dept_counts['% of Total'] = (dept_counts['Neg Issues'] / total_neg * 100).round(1)

# Monthly trend table
monthly_trend = monthly.merge(rev_by_month, on='year_month', how='inner').copy()
monthly_trend['neg_per_M_rev'] = (
    monthly_trend['count'] / (monthly_trend['total_revenue'] / 1e6)
).round(1)

# H1 vs H2 theme changes
h1_vs_h2 = compare.copy()
h1_vs_h2 = h1_vs_h2.sort_values('change', ascending=False)

# Revenue by dealership (full)
rev_dealer = rev_by_site.sort_values('total_revenue', ascending=False).copy()
rev_dealer = rev_dealer.merge(
    neg.groupby('site_code').size().reset_index(name='neg_issues'),
    left_on='dealer_code', right_on='site_code', how='left'
).fillna(0)
rev_dealer['neg_issues'] = rev_dealer['neg_issues'].astype(int)
rev_dealer['neg_per_M'] = (
    rev_dealer['neg_issues'] / (rev_dealer['total_revenue'] / 1e6)
).round(1)
rev_dealer['avg_svc_ticket'] = (
    rev_dealer['service_revenue'] / rev_dealer['service_jobs'].replace(0, np.nan)
).fillna(0).round(0).astype(int)

# ── Write report_stats.md ────────────────────────────────────────────
s = []
s.append("# Report Stats — auto-generated, copy into your report as needed\n")
s.append(f"Generated from: `{FEEDBACK_CSV}`, `{ISSUES_CSV}`, `{REVENUE_CSV}`\n")

s.append("\n---\n")
s.append("## Key Numbers\n")
s.append(f"| Metric | Value |")
s.append(f"|---|---|")
s.append(f"| Total feedback records | {total_fb:,} |")
s.append(f"| Total issues extracted | {total_issues:,} |")
s.append(f"| Negative issues | {total_neg:,} |")
s.append(f"| Critical severity | {crit_count:,} |")
s.append(f"| High severity | {high_count:,} |")
s.append(f"| Mean rating (all) | {mean_rating:.2f} / 5 |")
s.append(f"| Mean rating (negative sentiment) | {neg_rating:.2f} / 5 |")
s.append(f"| **Total group revenue** | **${total_rev_yr:,.0f}** |")
s.append(f"| Sales revenue | ${total_sales_rev:,.0f} |")
s.append(f"| Service revenue | ${total_svc_rev:,.0f} |")
s.append(f"| Parts revenue | ${total_parts_rev:,.0f} |")
s.append(f"| Vehicles sold | {total_veh_sold:,} |")
s.append(f"| Service jobs | {total_svc_jobs:,} |")
s.append(f"| Parts transactions | {total_parts_tx:,} |")
s.append(f"| **Avg service ticket** | **${avg_svc_ticket:,.0f}** |")
s.append(f"| Neg issues per $1M revenue (group) | {total_neg/(total_rev_yr/1e6):.1f} |")

s.append("\n---\n")
s.append("## H1 vs H2 Summary\n")
s.append(f"| Metric | H1 (Oct 25–Mar 26) | H2 (Apr–Sep 26) | Change |")
s.append(f"|---|---|---|---|")
s.append(f"| Total Revenue | ${h1_rev_total/1e6:.1f}M | ${h2_rev_total/1e6:.1f}M | {rev_chg_pct:+.1f}% |")
s.append(f"| Service Revenue | ${h1_svc_rev/1e6:.1f}M | ${h2_svc_rev/1e6:.1f}M | {(h2_svc_rev-h1_svc_rev)/h1_svc_rev*100 if h1_svc_rev else 0:+.1f}% |")
s.append(f"| Service Jobs | {h1_svc_jobs:,} | {h2_svc_jobs:,} | {(h2_svc_jobs-h1_svc_jobs)/h1_svc_jobs*100 if h1_svc_jobs else 0:+.1f}% |")
s.append(f"| Negative Issues | {h1_count:,} | {h2_count:,} | {h_change_pct:+.1f}% |")
s.append(f"| Neg per $1M Revenue | {h1_neg_per_M:.1f} | {h2_neg_per_M:.1f} | {neg_per_M_chg:+.1f}% |")

s.append("\n---\n")
s.append("## Top Sub-Themes per Theme\n")
for theme in theme_sev.head(5).index:
    theme_neg = neg[neg['ai_theme'] == theme]
    sub_counts = theme_neg['ai_subtheme'].value_counts().head(5)
    s.append(f"\n### {theme} ({len(theme_neg)} negative issues)\n")
    s.append(f"| Sub-Theme | Count | % of Theme |")
    s.append(f"|---|---|---|")
    for sub, cnt in sub_counts.items():
        s.append(f"| {sub} | {cnt} | {cnt/len(theme_neg)*100:.1f}% |")

s.append("\n---\n")
s.append("## Top 20 Sub-Themes Overall\n")
s.append(f"| Sub-Theme | Theme | Count | % of All Neg |")
s.append(f"|---|---|---|---|")
sub_overall = neg.groupby(['ai_subtheme', 'ai_theme']).size().reset_index(name='count')
sub_overall = sub_overall.sort_values('count', ascending=False).head(20)
for _, r in sub_overall.iterrows():
    s.append(f"| {r.ai_subtheme} | {r.ai_theme} | {int(r['count'])} | {r['count']/total_neg*100:.1f}% |")

s.append("\n---\n")
s.append("## Top 5 Themes — Report-Ready (copy-paste into report)\n")
s.append(f"| Theme | Negative Issues | % of Total | Severity Profile |")
s.append(f"|---|---|---|---|")
for idx, row in theme_sev.head(5).iterrows():
    total = int(row['Total'])
    pct   = total / total_neg * 100
    # Build severity profile string
    parts = []
    for sev in ['Critical', 'High', 'Moderate', 'Low']:
        val = int(row[sev])
        if val > 0:
            parts.append(f"{val} {sev}")
    if len(parts) == 1:
        profile = parts[0]
    elif len(parts) >= 2 and int(row['Critical']) == 0 and int(row['Low']) == 0:
        profile = f"Mostly {parts[0].split()[1]}" if int(row[parts[0].split()[1]]) > total * 0.6 else ', '.join(parts)
    else:
        profile = ', '.join(parts)
    s.append(f"| {idx} | {total} | {pct:.1f}% | {profile} |")

s.append("\n---\n")
s.append("## Top 10 Negative Themes (with severity breakdown)\n")
s.append(f"| Theme | Critical | High | Moderate | Low | Total | % of All Neg |")
s.append(f"|---|---|---|---|---|---|---|")
for idx, row in theme_sev.iterrows():
    pct = row['Total'] / total_neg * 100
    s.append(f"| {idx} | {int(row['Critical'])} | {int(row['High'])} | {int(row['Moderate'])} | {int(row['Low'])} | {int(row['Total'])} | {pct:.1f}% |")

s.append("\n---\n")
s.append("## Department Breakdown\n")
s.append(f"| Department | Neg Issues | % of Total |")
s.append(f"|---|---|---|")
for _, row in dept_counts.iterrows():
    s.append(f"| {row['Department']} | {row['Neg Issues']} | {row['% of Total']}% |")

s.append("\n---\n")
s.append("## H1 vs H2 Theme Changes (sorted by worsening)\n")
s.append(f"| Theme | H1 | H2 | Change | % Change |")
s.append(f"|---|---|---|---|---|")
for idx, row in h1_vs_h2.iterrows():
    chg = int(row['change'])
    pct = int(row['pct_change'])
    sign = '+' if chg > 0 else ''
    s.append(f"| {idx} | {int(row['H1'])} | {int(row['H2'])} | {sign}{chg} | {sign}{pct}% |")

s.append("\n---\n")
s.append("## Top 5 Worst Dealerships (raw negative count)\n")
s.append(f"| Site Code | Site Name | Neg Issues |")
s.append(f"|---|---|---|")
for _, r in top5_raw.iterrows():
    s.append(f"| {r.site_code} | {r.site_name} | {r.neg_count} |")

s.append("\n---\n")
s.append("## Top 5 Worst Dealerships (revenue-normalised: neg per $1M)\n")
s.append(f"| Site Code | Neg Issues | Total Revenue | Neg per $1M |")
s.append(f"|---|---|---|---|")
for _, r in top5_norm.iterrows():
    s.append(f"| {r.dealer_code} | {int(r.neg_count)} | ${r.total_revenue:,.0f} | {r.neg_per_M:.1f} |")

s.append("\n---\n")
s.append("## Top 5 Worst Dealerships (service complaint rate: neg per 1k jobs)\n")
s.append(f"| Site Code | Service Neg Issues | Service Jobs | Neg per 1k Jobs |")
s.append(f"|---|---|---|---|")
for _, r in top5_svc_rate.iterrows():
    s.append(f"| {r.site_code} | {int(r.svc_neg)} | {int(r.service_jobs):,} | {r.neg_per_1k_jobs:.1f} |")

s.append("\n---\n")
s.append("## Revenue at Risk — Top 10 Dealerships\n")
s.append(f"*Assumptions: per complaining customer (comment), 15% churn rate, 3 service visits/yr, site-specific avg service ticket*\n")
s.append(f"| Site Code | Site Name | Neg Issues | Neg Comments | Avg Svc Ticket | Est. Revenue at Risk |")
s.append(f"|---|---|---|---|---|---|")
for _, r in rar.iterrows():
    s.append(f"| {r.site_code} | {r.site_name} | {int(r.neg_count)} | {int(r.neg_comments)} | ${r.avg_ticket:,.0f} | ${r.est_risk:,.0f} |")

s.append("\n---\n")
s.append("## Full Dealership Revenue + Complaints Table\n")
s.append(f"| Dealer | Total Rev | Sales Rev | Service Rev | Parts Rev | Vehicles | Svc Jobs | Parts Tx | Neg Issues | Neg/$1M | Avg Svc Ticket |")
s.append(f"|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in rev_dealer.iterrows():
    s.append(f"| {r.dealer_code} | ${r.total_revenue:,.0f} | ${r.sales_revenue:,.0f} | ${r.service_revenue:,.0f} | ${r.parts_revenue:,.0f} | {int(r.vehicles_sold):,} | {int(r.service_jobs):,} | {int(r.parts_transactions):,} | {int(r.neg_issues)} | {r.neg_per_M:.1f} | ${int(r.avg_svc_ticket):,} |")

s.append("\n---\n")
s.append("## Monthly Trend Table\n")
s.append(f"| Month | Neg Issues | Total Feedback | Neg Rate % | Total Revenue | Neg per $1M Rev |")
s.append(f"|---|---|---|---|---|---|")
for _, r in monthly_trend.iterrows():
    s.append(f"| {r.year_month} | {int(r['count'])} | {int(r['total'])} | {r.neg_rate}% | ${r.total_revenue:,.0f} | {r.neg_per_M_rev:.1f} |")

s.append("\n---\n")
s.append("## Safety Concern Breakdown\n")
s.append(f"{safety_count} issues, all Critical severity, across {safety['site_code'].nunique()} of 24 dealerships. "
         f"H1: {int((safety['half'] == 'H1 (Oct 25–Mar 26)').sum())}, H2: {int((safety['half'] == 'H2 (Apr–Sep 26)').sum())}.\n")
s.append(f"| Sub-Theme | Count | % of Safety |")
s.append(f"|---|---|---|")
for sub, cnt in safety_sub.items():
    s.append(f"| {sub} | {cnt} | {cnt / safety_count * 100:.1f}% |")
s.append(f"\n| Department | Count |")
s.append(f"|---|---|")
for dept, cnt in safety['department'].value_counts().items():
    s.append(f"| {dept} | {cnt} |")
s.append(f"\n| Top Sites | Count |")
s.append(f"|---|---|")
for (code, name), cnt in safety.groupby(['site_code', 'site_name']).size().sort_values(ascending=False).head(5).items():
    s.append(f"| {code} {name} | {cnt} |")

s.append("\n---\n")
s.append("## Priority Summary (pre-formatted)\n")
s.append(f"| Priority | Focus Area | Issues | Trend | Fix Type | Est. Value at Risk |")
s.append(f"|---|---|---|---|---|---|")
s.append(f"| 1 | Safety sign-off checklist | {safety_count} | Stable (all Critical) | Process (free) | ${safety_liability:,.0f} per serious-injury claim (assumption) |")
s.append(f"| 2 | Communication automation | {comms_count} | Stable (not improving) | Systems/DMS | ${comms_at_risk:,.0f}/yr |")
s.append(f"| 3 | Parts floor stock & sharing | {parts_count} | +221% and rising | Supply chain | ${parts_at_risk:,.0f}/yr |")
s.append(f"| 4 | Pricing pre-authorisation | {pricing_count} | +41% | Process/DMS | ${pricing_at_risk:,.0f}/yr |")
s.append(f"| 5 | Booking system IT fix | {booking_count} | +47% | IT/Digital | ${booking_at_risk:,.0f}/yr |")
s.append(f"")
s.append(f"**Total group revenue: ${total_rev_yr/1e6:.1f}M** | Combined at-risk (incl. one serious-injury claim): ~${combined_risk/1e6:.2f}M ({pct_at_risk:.2f}% of annual revenue)")

with open(STATS_PATH, 'w') as f:
    f.write('\n'.join(s) + '\n')

# ── Done ─────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  14 charts saved to {CHART_DIR}/")
print(f"  Stats/tables saved to {STATS_PATH}")
print(f"{'='*60}")