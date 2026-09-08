#!/usr/bin/env python3
"""Daily design-vacancy report for DOU and Djinni.

Scrapes Product Design and UI/UX vacancies from both boards, compares the
current listing against the previously stored state, and writes a dated
Markdown report describing what is new, what is still open, and what has
disappeared from the listing since the last run.

State lives in state.json (committed to the repo) so that "new vs old" is
meaningful across runs in a fresh cloud environment. Run with no arguments:

    python3 daily_report.py
"""

import datetime
import json
import os
import sys

import scrapers

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
REPORTS_DIR = os.path.join(ROOT, "reports")

SOURCE_LABEL = {"dou": "DOU", "djinni": "Djinni"}


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"updated": None, "vacancies": {}}
    with open(STATE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def key_of(vac):
    return "%s#%s" % (vac["source"], vac["id"])


def vac_line(vac, extra=""):
    label = SOURCE_LABEL.get(vac["source"], vac["source"])
    loc = vac.get("location") or "—"
    line = "- **%s** — %s · %s · [%s](%s)" % (
        vac["title"],
        vac["company"] or "—",
        loc,
        label,
        vac["url"],
    )
    return line + extra


def build_report(today, scraped, new, closed, ongoing, errors):
    dou_active = sum(1 for v in scraped.values() if v["source"] == "dou")
    dj_active = sum(1 for v in scraped.values() if v["source"] == "djinni")

    out = []
    out.append("# Design вакансії — %s" % today)
    out.append("")
    out.append(
        "Джерела: DOU (category=Design) + Djinni (primary_keyword=Design). "
        "Фільтр: Product Design, UI/UX."
    )
    out.append("")
    out.append(
        "**Активних: %d** (DOU %d, Djinni %d) · **нових: %d** · **зникло: %d**"
        % (len(scraped), dou_active, dj_active, len(new), len(closed))
    )
    out.append("")

    if errors:
        out.append("> ⚠️ Помилки скрапінгу: %s" % json.dumps(errors, ensure_ascii=False))
        out.append("")

    out.append("## 🆕 Нові (%d)" % len(new))
    out.append("")
    if new:
        for v in sorted(new, key=lambda x: (x["source"], x["title"].lower())):
            out.append(vac_line(v))
    else:
        out.append("_Нових немає._")
    out.append("")

    out.append("## ❌ Зникли з видачі (%d)" % len(closed))
    out.append("")
    if closed:
        for v in sorted(closed, key=lambda x: (x["source"], x["title"].lower())):
            seen = v.get("first_seen", "?")
            out.append(vac_line(v, extra="  _(було з %s)_" % seen))
    else:
        out.append("_Нічого не зникло._")
    out.append("")

    out.append("## 📋 Активні раніше (%d)" % len(ongoing))
    out.append("")
    if ongoing:
        for v in sorted(ongoing, key=lambda x: (x["source"], x["title"].lower())):
            seen = v.get("first_seen", "?")
            out.append(vac_line(v, extra="  _(з %s)_" % seen))
    else:
        out.append("_Порожньо._")
    out.append("")

    return "\n".join(out)


def main():
    today = datetime.date.today().isoformat()
    state = load_state()
    prev = state.get("vacancies", {})

    data = scrapers.fetch_all(relevant_only=True)
    errors = data.get("_errors", {})

    # If BOTH sources failed, do not destroy state by treating everything as
    # closed. Abort with a non-zero exit so the run is visibly broken.
    if errors and not data["dou"] and not data["djinni"]:
        sys.stderr.write("Both scrapers failed: %s\n" % errors)
        return 1

    scraped = {}
    for src in ("dou", "djinni"):
        for v in data[src]:
            scraped[key_of(v)] = v

    scraped_keys = set(scraped)
    prev_keys = set(prev)

    # A source that errored this run returned nothing; don't mark its
    # previously-seen vacancies as closed on a transient failure.
    failed_sources = set(errors)

    new_keys = scraped_keys - prev_keys
    ongoing_keys = scraped_keys & prev_keys
    closed_keys = {
        k for k in (prev_keys - scraped_keys)
        if prev[k].get("source") not in failed_sources
    }

    new = [scraped[k] for k in new_keys]
    ongoing = []
    for k in ongoing_keys:
        merged = dict(scraped[k])
        merged["first_seen"] = prev[k].get("first_seen", today)
        ongoing.append(merged)
    closed = [prev[k] for k in closed_keys]

    report = build_report(today, scraped, new, closed, ongoing, errors)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, "report-%s.md" % today)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    # Rebuild active state: everything scraped now, keeping first_seen.
    new_vacancies = {}
    for k, v in scraped.items():
        rec = dict(v)
        rec["first_seen"] = prev.get(k, {}).get("first_seen", today)
        rec["last_seen"] = today
        new_vacancies[k] = rec
    # Keep vacancies from a source that failed this run untouched.
    for k, v in prev.items():
        if v.get("source") in failed_sources and k not in new_vacancies:
            new_vacancies[k] = v

    save_state({"updated": today, "vacancies": new_vacancies})

    print("Report: %s" % report_path)
    print("Active: %d | new: %d | closed: %d | errors: %s"
          % (len(scraped), len(new), len(closed), errors or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
