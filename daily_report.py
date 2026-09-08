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
import urllib.parse
import urllib.request

import scrapers

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(ROOT, "reports")

# How many calendar days back to include, counting today. 3 = today + the two
# previous days.
WINDOW_DAYS = 3

SOURCE_LABEL = {"dou": "DOU", "djinni": "Djinni"}

# Vacancies that mention iGaming get pulled into their own block at the top.
# Djinni descriptions arrive with the listing; DOU descriptions are fetched per
# vacancy in main() and stashed under "description" before this runs.
_IGAMING = re.compile(r"i[\s-]?gaming", re.IGNORECASE)


def is_igaming(vac):
    text = " ".join(
        (
            vac.get("title", ""),
            vac.get("company", ""),
            vac.get("location", ""),
            vac.get("description", ""),
        )
    )
    return bool(_IGAMING.search(text))


def _fmt_date(iso):
    """Format an ISO date as dd-mm-yyyy for display; pass through anything else."""
    try:
        return datetime.datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return iso or "—"


def vac_line(vac, with_source):
    line = "- [%s](%s) — %s" % (vac["title"], vac["url"], vac["company"] or "—")
    loc = vac.get("location")
    if loc:
        line += " · %s" % loc
    if with_source:
        line += " [%s]" % SOURCE_LABEL.get(vac["source"], vac["source"])
    return line


def _section(out, heading, items, with_source):
    out.append("## %s (%d)" % (heading, len(items)))
    out.append("")
    if not items:
        out.append("_Немає за період._")
        out.append("")
        return
    # Group by date (items arrive newest-first, title-sorted within a day) and
    # print the date once as a sub-heading instead of on every line.
    last = None
    for v in items:
        date = _fmt_date(v.get("date_posted") or "—")
        if date != last:
            if last is not None:
                out.append("")  # close the previous date's list
            out.append("⦿ **%s**" % date)
            out.append("")  # blank line so the bullets render as a list
            last = date
        out.append(vac_line(v, with_source))
    out.append("")


def build_report(today, cutoff, igaming, djinni, dou, errors):
    out = []
    out.append("# Design вакансії — %s" % _fmt_date(today))
    out.append("")
    out.append("Джерела: DOU + Djinni. Фільтр: Product Design, UI/UX.")
    out.append("")
    out.append(
        "Опубліковані за останні %d дні (з %s по %s)."
        % (WINDOW_DAYS, _fmt_date(cutoff), _fmt_date(today))
    )
    out.append("")

    if errors:
        out.append("> ⚠️ Помилки скрапінгу: %s" % json.dumps(errors, ensure_ascii=False))
        out.append("")

    # iGaming matches are pulled out of the per-source blocks and shown first.
    if igaming:
        _section(out, "🟣 iGaming", igaming, with_source=True)
    _section(out, "🟠 Вакансії Djinni", djinni, with_source=False)
    _section(out, "🟢 Вакансії DOU", dou, with_source=False)
    return "\n".join(out)


# --- Telegram delivery -----------------------------------------------------

TELEGRAM_LIMIT = 3800  # Telegram's hard limit is 4096; leave room for tags.


def _esc(text):
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _tg_vac_line(vac, with_source):
    line = '• <a href="%s">%s</a> — %s' % (
        vac["url"],
        _esc(vac["title"]),
        _esc(vac["company"] or "—"),
    )
    loc = vac.get("location")
    if loc:
        line += " · %s" % _esc(loc)
    if with_source:
        line += " [%s]" % SOURCE_LABEL.get(vac["source"], vac["source"])
    return line


def build_telegram_messages(today, cutoff, igaming, djinni, dou):
    """Render the report as one or more HTML messages under Telegram's limit."""
    lines = [
        "<b>Design вакансії 🧑‍💻✨</b>",
        "За останні %d дні (%s/%s)" % (WINDOW_DAYS, _fmt_date(cutoff)[:2], _fmt_date(today)),
    ]

    def add_block(heading, items, with_source):
        if not items:
            return
        lines.append("")
        lines.append("<b>%s (%d)</b>" % (heading, len(items)))
        last = None
        for v in items:
            date = _fmt_date(v.get("date_posted") or "—")
            if date != last:
                lines.append("⦿ <b>%s</b>" % date)
                last = date
            lines.append(_tg_vac_line(v, with_source))

    add_block("🟣 iGaming", igaming, True)
    add_block("🟠 Djinni", djinni, False)
    add_block("🟢 DOU", dou, False)

    if not igaming and not djinni and not dou:
        lines.append("")
        lines.append("Немає вакансій за період.")

    # Pack lines into chunks that each stay under the limit.
    messages, chunk = [], ""
    for line in lines:
        piece = (line + "\n")
        if len(chunk) + len(piece) > TELEGRAM_LIMIT and chunk:
            messages.append(chunk.rstrip("\n"))
            chunk = ""
        chunk += piece
    if chunk.strip():
        messages.append(chunk.rstrip("\n"))
    return messages


def send_telegram(token, chat_id, messages):
    """Send each message via the Telegram Bot API. Best-effort; logs failures."""
    for msg in messages:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=data), timeout=30
            ) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001 - notification must not break the run
            sys.stderr.write("Telegram send failed: %s\n" % exc)
            return False
    return True


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

    # DOU listings carry no description, so open each DOU vacancy in the window
    # and stash its body text — but skip the fetch when the title/company
    # already flags it as iGaming.
    for v in dou:
        if not is_igaming(v):
            v["description"] = scrapers.fetch_dou_description(v["url"])

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

    # Deliver to Telegram as inline messages when configured (skipped locally).
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        messages = build_telegram_messages(today, cutoff, igaming, djinni, dou)
        ok = send_telegram(token, chat_id, messages)
        print("Telegram: sent %d message(s), ok=%s" % (len(messages), ok))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
