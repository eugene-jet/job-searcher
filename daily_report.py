#!/usr/bin/env python3
"""Daily design-vacancy digest for DOU and Djinni.

Scrapes Product Design and UI/UX vacancies from both boards and writes a dated
Markdown report listing the vacancies published within the last ``WINDOW_DAYS``
days (by the board's own posting date), split into a Djinni section and a DOU
section, newest first. Run with no arguments:

    python3 daily_report.py
"""

import datetime
import json
import os
import re
import sys

import scrapers

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(ROOT, "reports")

# How many calendar days back to include, counting today. 3 = today + the two
# previous days.
WINDOW_DAYS = 3

SOURCE_LABEL = {"dou": "DOU", "djinni": "Djinni"}

# Vacancies that mention iGaming get pulled into their own block at the top.
# Only the scraped fields (title, company, location) are searched — job
# descriptions are not fetched.
_IGAMING = re.compile(r"i[\s-]?gaming", re.IGNORECASE)


def is_igaming(vac):
    text = " ".join(
        (vac.get("title", ""), vac.get("company", ""), vac.get("location", ""))
    )
    return bool(_IGAMING.search(text))


def vac_line(vac):
    label = SOURCE_LABEL.get(vac["source"], vac["source"])
    loc = vac.get("location") or "—"
    date = vac.get("date_posted") or "—"
    return "- **%s** — %s · %s · _%s_ · [%s](%s)" % (
        vac["title"],
        vac["company"] or "—",
        loc,
        date,
        label,
        vac["url"],
    )


def _section(out, heading, items):
    out.append("## %s (%d)" % (heading, len(items)))
    out.append("")
    if items:
        out.extend(vac_line(v) for v in items)
    else:
        out.append("_Немає за період._")
    out.append("")


def build_report(today, cutoff, igaming, djinni, dou, errors):
    total = len(igaming) + len(djinni) + len(dou)
    out = []
    out.append("# Design вакансії — %s" % today)
    out.append("")
    out.append(
        "Джерела: DOU (category=Design) + Djinni (primary_keyword=Design). "
        "Фільтр: Product Design, UI/UX."
    )
    out.append("")
    out.append(
        "Опубліковані за останні %d дні (з %s по %s). "
        "Всього: %d (iGaming %d, Djinni %d, DOU %d)."
        % (WINDOW_DAYS, cutoff, today, total, len(igaming), len(djinni), len(dou))
    )
    out.append("")

    if errors:
        out.append("> ⚠️ Помилки скрапінгу: %s" % json.dumps(errors, ensure_ascii=False))
        out.append("")

    # iGaming matches are pulled out of the per-source blocks and shown first.
    if igaming:
        _section(out, "🎰 iGaming", igaming)
    _section(out, "Вакансії Djinni", djinni)
    _section(out, "Вакансії DOU", dou)
    return "\n".join(out)


def main():
    today_d = datetime.date.today()
    today = today_d.isoformat()
    cutoff = (today_d - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()

    data = scrapers.fetch_all(relevant_only=True)
    errors = data.get("_errors", {})

    # If BOTH sources failed, exit non-zero so the run is visibly broken and no
    # empty report gets committed.
    if errors and not data["dou"] and not data["djinni"]:
        sys.stderr.write("Both scrapers failed: %s\n" % errors)
        return 1

    def window_sorted(source):
        items = [v for v in data[source] if (v.get("date_posted") or "") >= cutoff]
        # Newest first; alphabetical by title within the same day.
        items.sort(key=lambda x: x["title"].lower())
        items.sort(key=lambda x: x["date_posted"], reverse=True)
        return items

    djinni = window_sorted("djinni")
    dou = window_sorted("dou")

    # Pull iGaming vacancies out of both source lists into a top block. They
    # keep their source label but are not repeated in the per-source blocks.
    igaming = [v for v in djinni + dou if is_igaming(v)]
    igaming.sort(key=lambda x: x["title"].lower())
    igaming.sort(key=lambda x: x["date_posted"], reverse=True)
    djinni = [v for v in djinni if not is_igaming(v)]
    dou = [v for v in dou if not is_igaming(v)]

    report = build_report(today, cutoff, igaming, djinni, dou, errors)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, "report-%s.md" % today)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    print("Report: %s" % report_path)
    print(
        "Window %s..%s | iGaming %d | Djinni %d | DOU %d | errors: %s"
        % (cutoff, today, len(igaming), len(djinni), len(dou), errors or "none")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
