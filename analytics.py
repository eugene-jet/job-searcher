"""Time-series analytics for the DOU + Djinni vacancy counts.

Keeps a running daily count of relevant (Product Design / UI/UX) vacancies per
board in a CSV that acts as the source of truth, and regenerates an Excel
workbook with a line chart from that CSV. Everything but the workbook writer is
standard library; the workbook needs openpyxl because the standard library
cannot produce an ``.xlsx`` — let alone one with an embedded chart.

The count granularity is one row per calendar day: a second run on the same day
overwrites that day's values with the latest numbers.
"""

import csv
import datetime
import os

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
REPORTS_DIR = os.path.join(ROOT, "reports")
CSV_PATH = os.path.join(DATA_DIR, "vacancy_counts.csv")
XLSX_PATH = os.path.join(REPORTS_DIR, "vacancy-analytics.xlsx")

# CSV columns, in order. The two source columns hold the relevant-vacancy count
# each board carried that day, or an empty cell when the scrape failed.
SOURCES = ("dou", "djinni")
FIELDNAMES = ("date",) + SOURCES

# Column headers shown in the workbook (and used as the chart's series names).
HEADERS = {"dou": "DOU", "djinni": "Djinni"}


def _read_rows(csv_path):
    """Return ``{date: {"dou": str, "djinni": str}}`` from the CSV, or ``{}``.

    Values are kept as the raw strings the file holds (possibly ``""``) so a
    blank cell round-trips as a gap rather than being coerced to ``0``.
    """
    if not os.path.exists(csv_path):
        return {}
    rows = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            date = (row.get("date") or "").strip()
            if not date:
                continue
            rows[date] = {s: (row.get(s) or "").strip() for s in SOURCES}
    return rows


def _as_int(value):
    """Turn a CSV cell into an int, or ``None`` when blank/non-numeric (a gap)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_day(counts, day, csv_path=CSV_PATH):
    """Upsert one day's per-source counts into the CSV source of truth.

    ``counts`` maps each source to an int, or to ``None`` when that source
    failed to scrape this run. A ``None`` never overwrites an existing value for
    the day and is written as an empty cell when no prior value exists, so a
    failed scrape reads as a gap in the chart, not a real zero. Returns the CSV
    path.
    """
    rows = _read_rows(csv_path)
    row = rows.get(day, {s: "" for s in SOURCES})
    for s in SOURCES:
        value = counts.get(s)
        if value is not None:
            row[s] = str(value)
        # value is None: keep whatever the day already had (possibly "").
    rows[day] = row

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for date in sorted(rows):
            writer.writerow({"date": date, **rows[date]})
    return csv_path


def build_workbook(csv_path=CSV_PATH, xlsx_path=XLSX_PATH):
    """Regenerate the ``.xlsx`` (data + line chart) from the CSV. Returns its path.

    Safe when the CSV is absent or header-only: it still writes a valid workbook
    with just the header row and no chart.
    """
    rows = _read_rows(csv_path)
    dates = sorted(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Counts"
    ws.append(["date"] + [HEADERS[s] for s in SOURCES])
    for date in dates:
        row = rows[date]
        # Blank cells stay blank (None) so the chart draws a gap, not a zero.
        ws.append([date] + [_as_int(row.get(s)) for s in SOURCES])

    if dates:
        last_row = 1 + len(dates)  # header is row 1, data rows follow
        chart = LineChart()
        chart.title = "DOU + Djinni — Product Design / UI/UX вакансії"
        chart.x_axis.title = "Дата"
        chart.y_axis.title = "Кількість"
        chart.height = 10
        chart.width = 24
        # Series come from the two count columns (B, C); the header row supplies
        # their names and the date column supplies the categories.
        data = Reference(ws, min_col=2, max_col=1 + len(SOURCES), min_row=1, max_row=last_row)
        cats = Reference(ws, min_col=1, min_row=2, max_row=last_row)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws.add_chart(chart, "E2")

    # Deterministic document properties: identical data yields a byte-stable
    # file, so a no-change run does not churn Git with a meaningless binary diff.
    stamp = (
        datetime.datetime.strptime(dates[-1], "%Y-%m-%d")
        if dates
        else datetime.datetime(1970, 1, 1)
    )
    wb.properties.creator = "job-searcher"
    wb.properties.lastModifiedBy = "job-searcher"
    wb.properties.created = stamp
    wb.properties.modified = stamp

    os.makedirs(os.path.dirname(xlsx_path), exist_ok=True)
    wb.save(xlsx_path)
    return xlsx_path


if __name__ == "__main__":
    build_workbook()
    print("Wrote %s" % XLSX_PATH)
