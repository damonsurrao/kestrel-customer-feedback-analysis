"""
Kestrel Motor Group — Feedback Analysis Charts
================================================
Generates an Excel workbook with 6 chart tabs from AI analysis output.

Usage:
    python generate_charts.py --issues ai_issues.csv --main consolidated_clean_ai.csv -o kestrel_feedback_charts.xlsx

Requirements:
    pip install pandas openpyxl
"""
import argparse
import pandas as pd
import numpy as np
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference, Series, BubbleChart
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import (
    Paragraph, ParagraphProperties, CharacterProperties,
    Font as DrawingFont,
)
from openpyxl.chart.axis import NumericAxis, TextAxis
from openpyxl.chart.layout import Layout, ManualLayout
import warnings

warnings.filterwarnings("ignore")


def fix_axes(chart):
    """Ensure axis tick labels and lines are visible on all charts."""
    chart.x_axis.tickLblPos = "low"
    chart.y_axis.tickLblPos = "low"
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    # Push axis titles away from tick labels so they don't overlap
    if chart.x_axis.title:
        chart.x_axis.title.layout = Layout(
            manualLayout=ManualLayout(y=0.98)
        )
    if chart.y_axis.title:
        chart.y_axis.title.layout = Layout(
            manualLayout=ManualLayout(x=0.01)
        )


# ── Styling constants ─────────────────────────────────────────────────
HEADER_FILL = PatternFill(start_color="1B2A4A", end_color="1B2A4A", fill_type="solid")
HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
DATA_FONT = Font(name="Arial", size=10)
TITLE_FONT = Font(name="Arial", bold=True, size=14, color="1B2A4A")
SUBTITLE_FONT = Font(name="Arial", size=11, color="555555")
THIN_BORDER = Border(bottom=Side(style="thin", color="DDDDDD"))

# Categorical palette (accessibility-checked)
COLORS = [
    "2563EB", "DC2626", "D97706", "059669", "7C3AED",
    "DB2777", "0891B2", "CA8A04", "4F46E5", "BE123C", "0D9488",
]


# ── Helpers ───────────────────────────────────────────────────────────
def style_header_row(ws, row, max_col):
    for col in range(1, max_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def style_data_rows(ws, start_row, end_row, max_col):
    for row in range(start_row, end_row + 1):
        for col in range(1, max_col + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")


def auto_width(ws, max_col, min_width=10, max_width=35):
    for col in range(1, max_col + 1):
        letter = get_column_letter(col)
        best = min_width
        for row in ws.iter_rows(min_col=col, max_col=col, values_only=False):
            for cell in row:
                if cell.value:
                    best = max(best, min(len(str(cell.value)) + 2, max_width))
        ws.column_dimensions[letter].width = best


def make_chart_font(size=10, bold=False, color="333333"):
    return CharacterProperties(
        sz=size * 100, b=bold, solidFill=color,
        latin=DrawingFont(typeface="Arial"),
    )


# ── Chart builders ────────────────────────────────────────────────────
def build_bubble_chart(wb, issues):
    """Sheet 1: Bubble chart — negative themes by reach and severity."""
    sev_map = {"Low": 1, "Moderate": 2, "High": 3, "Critical": 4}
    issues = issues.copy()
    issues["severity_num"] = issues["ai_severity"].map(sev_map).fillna(1)

    neg = issues[issues["ai_issue_sentiment"].isin(["Negative", "Mixed"])]
    bubble = (
        neg.groupby("ai_theme")
        .agg(
            customers_impacted=("feedback_id", "nunique"),
            departments_impacted=("department", "nunique"),
            avg_severity=("severity_num", "mean"),
            total_issues=("feedback_id", "count"),
            critical_count=("severity_num", lambda x: (x >= 3).sum()),
        )
        .reset_index()
        .sort_values("total_issues", ascending=False)
    )

    ws = wb.active
    ws.title = "Theme Bubble Chart"
    ws.sheet_properties.tabColor = "2563EB"

    ws["A1"] = "Negative Feedback: Theme Impact Analysis"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Each bubble = one theme.  Size = avg severity.  Position = reach across customers & departments."
    ws["A2"].font = SUBTITLE_FONT
    ws.merge_cells("A2:F2")

    headers = ["Theme", "Departments Impacted", "Customers Impacted",
               "Avg Severity", "Total Issues", "Critical+High Count"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=4, column=i, value=h)
    style_header_row(ws, 4, len(headers))

    for idx, row in enumerate(bubble.itertuples(), 5):
        ws.cell(row=idx, column=1, value=row.ai_theme)
        ws.cell(row=idx, column=2, value=row.departments_impacted)
        ws.cell(row=idx, column=3, value=row.customers_impacted)
        ws.cell(row=idx, column=4, value=round(row.avg_severity, 2))
        ws.cell(row=idx, column=5, value=row.total_issues)
        ws.cell(row=idx, column=6, value=row.critical_count)

    end = 4 + len(bubble)
    style_data_rows(ws, 5, end, len(headers))
    auto_width(ws, len(headers))

    chart = BubbleChart()
    chart.title = "Theme Impact: Departments vs Customers (bubble = severity)"
    chart.style = 2
    chart.width = 28
    chart.height = 18
    chart.x_axis.title = "Departments Impacted"
    chart.y_axis.title = "Customers Impacted"

    for i in range(len(bubble)):
        r = 5 + i
        xv = Reference(ws, min_col=2, min_row=r, max_row=r)
        yv = Reference(ws, min_col=3, min_row=r, max_row=r)
        bv = Reference(ws, min_col=4, min_row=r, max_row=r)
        s = Series(yv, xv, bv, title=bubble.iloc[i]["ai_theme"])
        c = COLORS[i % len(COLORS)]
        s.graphicalProperties.solidFill = c
        s.graphicalProperties.line.solidFill = c
        chart.series.append(s)

    fix_axes(chart)
    ws.add_chart(chart, f"A{end + 2}")


def build_sentiment_by_dealership(wb, main):
    """Sheet 2: Stacked bar — sentiment counts per dealership."""
    ws = wb.create_sheet("Sentiment by Dealership")
    ws.sheet_properties.tabColor = "DC2626"

    cross = main.groupby(["site_name", "ai_overall_sentiment"]).size().unstack(fill_value=0)
    for c in ["Positive", "Neutral", "Mixed", "Negative"]:
        if c not in cross.columns:
            cross[c] = 0
    cross = cross[["Positive", "Neutral", "Mixed", "Negative"]]
    cross["Total"] = cross.sum(axis=1)
    cross["Negative %"] = cross["Negative"] / cross["Total"]
    cross = cross.sort_values("Negative %", ascending=False)

    ws["A1"] = "Sentiment Distribution by Dealership"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Sorted by % negative feedback."
    ws["A2"].font = SUBTITLE_FONT

    headers = ["Dealership", "Positive", "Neutral", "Mixed", "Negative", "Total", "Negative %"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=4, column=i, value=h)
    style_header_row(ws, 4, len(headers))

    for idx, (site, row) in enumerate(cross.iterrows(), 5):
        ws.cell(row=idx, column=1, value=site)
        for j, col in enumerate(["Positive", "Neutral", "Mixed", "Negative", "Total"], 2):
            ws.cell(row=idx, column=j, value=int(row[col]))
        ws.cell(row=idx, column=7, value=row["Negative %"])
        ws.cell(row=idx, column=7).number_format = "0.0%"

    end = 4 + len(cross)
    style_data_rows(ws, 5, end, len(headers))
    auto_width(ws, len(headers))

    chart = BarChart()
    chart.type = "bar"
    chart.grouping = "stacked"
    chart.title = "Sentiment Breakdown by Dealership"
    chart.width = 28
    chart.height = max(14, len(cross) * 0.7)
    chart.style = 2

    cats = Reference(ws, min_col=1, min_row=5, max_row=end)
    bar_colors = ["059669", "9CA3AF", "D97706", "DC2626"]
    for i in range(4):
        vals = Reference(ws, min_col=i + 2, min_row=4, max_row=end)
        chart.add_data(vals, titles_from_data=True)
    chart.set_categories(cats)
    for i, c in enumerate(bar_colors):
        chart.series[i].graphicalProperties.solidFill = c
        chart.series[i].graphicalProperties.line.solidFill = c

    chart.y_axis.title = "Dealership"
    chart.x_axis.title = "Number of Reviews"
    fix_axes(chart)
    ws.add_chart(chart, f"A{end + 2}")


def build_theme_trends(wb, issues):
    """Sheet 3: Line chart — top 8 themes over time."""
    ws = wb.create_sheet("Theme Trends")
    ws.sheet_properties.tabColor = "D97706"

    top = issues["ai_theme"].value_counts().head(8).index.tolist()
    pivot = (
        issues[issues["ai_theme"].isin(top)]
        .groupby(["year_month", "ai_theme"]).size()
        .unstack(fill_value=0)
        .sort_index()
    )

    ws["A1"] = "Issue Theme Trends Over Time"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Top 8 themes by total issue count, monthly."
    ws["A2"].font = SUBTITLE_FONT

    headers = ["Month"] + list(pivot.columns)
    for i, h in enumerate(headers, 1):
        ws.cell(row=4, column=i, value=h)
    style_header_row(ws, 4, len(headers))

    for idx, (month, row) in enumerate(pivot.iterrows(), 5):
        ws.cell(row=idx, column=1, value=month)
        for j, theme in enumerate(pivot.columns):
            ws.cell(row=idx, column=j + 2, value=int(row[theme]))

    end = 4 + len(pivot)
    style_data_rows(ws, 5, end, len(headers))
    auto_width(ws, len(headers))

    chart = LineChart()
    chart.title = "Monthly Issue Trends by Theme"
    chart.width = 28
    chart.height = 16
    chart.style = 2
    chart.y_axis.title = "Issue Count"
    chart.x_axis.title = "Month"

    cats = Reference(ws, min_col=1, min_row=5, max_row=end)
    for i in range(len(pivot.columns)):
        vals = Reference(ws, min_col=i + 2, min_row=4, max_row=end)
        chart.add_data(vals, titles_from_data=True)
    chart.set_categories(cats)

    for i, s in enumerate(chart.series):
        s.graphicalProperties.line.solidFill = COLORS[i % len(COLORS)]
        s.graphicalProperties.line.width = 22000
        s.smooth = False

    fix_axes(chart)
    ws.add_chart(chart, f"A{end + 2}")


def build_heatmap(wb, issues):
    """Sheet 4: Colour-scaled matrix — department × theme (negative only)."""
    ws = wb.create_sheet("Dept × Theme Heatmap")
    ws.sheet_properties.tabColor = "7C3AED"

    neg = issues[issues["ai_issue_sentiment"].isin(["Negative", "Mixed"])]
    cross = neg.groupby(["department", "ai_theme"]).size().unstack(fill_value=0)
    cross = cross[cross.sum().sort_values(ascending=False).index]

    ws["A1"] = "Negative Issues: Department × Theme Matrix"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Conditional colour scale highlights hotspots (white → red)."
    ws["A2"].font = SUBTITLE_FONT
    ws.merge_cells("A2:G2")

    headers = ["Department"] + list(cross.columns)
    for i, h in enumerate(headers, 1):
        ws.cell(row=4, column=i, value=h)
    style_header_row(ws, 4, len(headers))

    mx = cross.values.max()
    for idx, (dept, row) in enumerate(cross.iterrows(), 5):
        ws.cell(row=idx, column=1, value=dept)
        ws.cell(row=idx, column=1).font = Font(name="Arial", bold=True, size=10)
        for j, theme in enumerate(cross.columns):
            val = int(row[theme])
            cell = ws.cell(row=idx, column=j + 2, value=val)
            cell.font = DATA_FONT
            cell.alignment = Alignment(horizontal="center")
            if val > 0 and mx > 0:
                intensity = val / mx
                g = b = int(255 * (1 - intensity * 0.7))
                hx = f"FF{g:02X}{b:02X}"
                cell.fill = PatternFill(start_color=hx, end_color=hx, fill_type="solid")
                if intensity > 0.6:
                    cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")

    auto_width(ws, len(headers), min_width=12)


def build_severity(wb, issues):
    """Sheet 5: Stacked column — severity levels per department."""
    ws = wb.create_sheet("Severity Analysis")
    ws.sheet_properties.tabColor = "059669"

    cross = issues.groupby(["department", "ai_severity"]).size().unstack(fill_value=0)
    for s in ["Low", "Moderate", "High", "Critical"]:
        if s not in cross.columns:
            cross[s] = 0
    cross = cross[["Low", "Moderate", "High", "Critical"]]

    ws["A1"] = "Issue Severity by Department"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "AI-classified severity levels across departments."
    ws["A2"].font = SUBTITLE_FONT

    headers = ["Department", "Low", "Moderate", "High", "Critical", "Total", "High+Critical %"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=4, column=i, value=h)
    style_header_row(ws, 4, len(headers))

    for idx, (dept, row) in enumerate(cross.iterrows(), 5):
        ws.cell(row=idx, column=1, value=dept)
        total = hc = 0
        for j, sev in enumerate(["Low", "Moderate", "High", "Critical"]):
            v = int(row[sev])
            ws.cell(row=idx, column=j + 2, value=v)
            total += v
            if sev in ("High", "Critical"):
                hc += v
        ws.cell(row=idx, column=6, value=total)
        ws.cell(row=idx, column=7, value=hc / total if total else 0)
        ws.cell(row=idx, column=7).number_format = "0.0%"

    end = 4 + len(cross)
    style_data_rows(ws, 5, end, len(headers))
    auto_width(ws, len(headers))

    chart = BarChart()
    chart.type = "col"
    chart.grouping = "stacked"
    chart.title = "Issue Severity by Department"
    chart.width = 22
    chart.height = 14
    chart.style = 2

    cats = Reference(ws, min_col=1, min_row=5, max_row=end)
    sev_colors = ["9CA3AF", "D97706", "DC2626", "7F1D1D"]
    for i in range(4):
        vals = Reference(ws, min_col=i + 2, min_row=4, max_row=end)
        chart.add_data(vals, titles_from_data=True)
    chart.set_categories(cats)
    for i, c in enumerate(sev_colors):
        chart.series[i].graphicalProperties.solidFill = c
        chart.series[i].graphicalProperties.line.solidFill = c

    chart.x_axis.title = "Department"
    chart.y_axis.title = "Issue Count"
    fix_axes(chart)
    ws.add_chart(chart, f"A{end + 2}")


def build_monthly_summary(wb, main):
    """Sheet 6: Volume bars + negative-rate line, plus avg ratings."""
    ws = wb.create_sheet("Monthly Summary")
    ws.sheet_properties.tabColor = "0891B2"

    monthly = (
        main.groupby("year_month")
        .agg(
            total_reviews=("feedback_id", "count"),
            avg_rating=("rating_std", "mean"),
            negative_count=("ai_overall_sentiment", lambda x: (x == "Negative").sum()),
            positive_count=("ai_overall_sentiment", lambda x: (x == "Positive").sum()),
        )
        .reset_index()
        .sort_values("year_month")
    )
    monthly["negative_pct"] = monthly["negative_count"] / monthly["total_reviews"]
    monthly["positive_pct"] = monthly["positive_count"] / monthly["total_reviews"]

    ws["A1"] = "Monthly Feedback Summary"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Volume, ratings and sentiment over time."
    ws["A2"].font = SUBTITLE_FONT

    headers = ["Month", "Total Reviews", "Avg Rating", "Positive", "Negative",
               "Positive %", "Negative %"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=4, column=i, value=h)
    style_header_row(ws, 4, len(headers))

    for idx, row in enumerate(monthly.itertuples(), 5):
        ws.cell(row=idx, column=1, value=row.year_month)
        ws.cell(row=idx, column=2, value=int(row.total_reviews))
        ws.cell(row=idx, column=3, value=round(row.avg_rating, 2))
        ws.cell(row=idx, column=4, value=int(row.positive_count))
        ws.cell(row=idx, column=5, value=int(row.negative_count))
        ws.cell(row=idx, column=6, value=row.positive_pct)
        ws.cell(row=idx, column=6).number_format = "0.0%"
        ws.cell(row=idx, column=7, value=row.negative_pct)
        ws.cell(row=idx, column=7).number_format = "0.0%"

    end = 4 + len(monthly)
    style_data_rows(ws, 5, end, len(headers))
    auto_width(ws, len(headers))

    chart_bar = BarChart()
    chart_bar.type = "col"
    chart_bar.title = "Monthly Feedback Volume & Negative Rate"
    chart_bar.width = 26
    chart_bar.height = 14
    chart_bar.style = 2
    chart_bar.y_axis.title = "Review Count"

    cats = Reference(ws, min_col=1, min_row=5, max_row=end)
    vals_total = Reference(ws, min_col=2, min_row=4, max_row=end)
    chart_bar.add_data(vals_total, titles_from_data=True)
    chart_bar.set_categories(cats)
    chart_bar.series[0].graphicalProperties.solidFill = "2563EB"
    chart_bar.series[0].graphicalProperties.line.solidFill = "2563EB"

    chart_line = LineChart()
    chart_line.y_axis.title = "Negative %"
    chart_line.y_axis.axId = 200
    chart_line.y_axis.numFmt = "0%"

    vals_neg = Reference(ws, min_col=7, min_row=4, max_row=end)
    chart_line.add_data(vals_neg, titles_from_data=True)
    chart_line.set_categories(cats)
    chart_line.series[0].graphicalProperties.line.solidFill = "DC2626"
    chart_line.series[0].graphicalProperties.line.width = 28000

    chart_bar.y_axis.crosses = "min"
    chart_bar += chart_line

    fix_axes(chart_bar)
    ws.add_chart(chart_bar, f"A{end + 2}")


# ── Main ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Generate Kestrel feedback charts")
    parser.add_argument("--issues", required=True, help="Path to ai_issues.csv")
    parser.add_argument("--main", required=True, help="Path to consolidated_clean_ai.csv")
    parser.add_argument("-o", "--output", default="kestrel_feedback_charts.xlsx",
                        help="Output Excel path (default: kestrel_feedback_charts.xlsx)")
    args = parser.parse_args()

    print("Loading data...")
    issues = pd.read_csv(args.issues)
    main_df = pd.read_csv(args.main)
    print(f"  Issues: {len(issues):,} rows  |  Main: {len(main_df):,} rows")

    wb = Workbook()

    print("Building charts...")
    build_bubble_chart(wb, issues)
    build_sentiment_by_dealership(wb, main_df)
    build_theme_trends(wb, issues)
    build_heatmap(wb, issues)
    build_severity(wb, issues)
    build_monthly_summary(wb, main_df)

    wb.save(args.output)
    print(f"\nSaved → {args.output}")
    print(f"Sheets: {wb.sheetnames}")


if __name__ == "__main__":
    main()