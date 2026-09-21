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
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import analytics
import scrapers

# Timezone the report is written for. The GitHub runner's clock is UTC, so the
# "generated at" stamp is converted into this zone before display.
KYIV_TZ = ZoneInfo("Europe/Kyiv")

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(ROOT, "reports")

# How many calendar days back to include, counting today. 3 = today + the two
# previous days.
WINDOW_DAYS = 3

SOURCE_LABEL = {"dou": "DOU", "djinni": "Djinni"}

# Cloudflare fronts the subscriber Worker and answers the default urllib
# User-Agent with a 403, so requests to the Worker send an explicit one.
WORKER_USER_AGENT = "job-searcher-report"

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


def _section(out, heading, items, with_source, total=None):
    # ``total`` is the raw number of vacancies the board returned before the
    # relevance filter; when given, the heading shows "shown/scanned".
    if total is None:
        out.append("## %s (%d)" % (heading, len(items)))
    else:
        out.append("## %s (%d/%d)" % (heading, len(items), total))
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


def build_report(today, cutoff, igaming, djinni, dou, errors, sent_at, totals):
    out = []
    out.append("# Design вакансії — %s" % _fmt_date(today))
    out.append("")
    out.append("🕒 Згенеровано: **%s (Київ)**" % sent_at)
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
    _section(out, "🟠 Вакансії Djinni", djinni, with_source=False, total=totals.get("djinni"))
    _section(out, "🟢 Вакансії DOU", dou, with_source=False, total=totals.get("dou"))
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


def build_telegram_messages(today, cutoff, igaming, djinni, dou, sent_at, totals):
    """Render the report as one or more HTML messages under Telegram's limit."""
    lines = [
        "<b>Design вакансії 🧑‍💻✨</b>",
        "🕒 <b>%s</b> (Київ)" % _esc(sent_at),
        "За останні %d дні (%s/%s)" % (WINDOW_DAYS, _fmt_date(cutoff)[:2], _fmt_date(today)),
    ]

    def add_block(heading, items, with_source, total=None):
        if not items:
            return
        lines.append("")
        if total is None:
            lines.append("<b>%s (%d)</b>" % (heading, len(items)))
        else:
            lines.append("<b>%s (%d/%d)</b>" % (heading, len(items), total))
        last = None
        for v in items:
            date = _fmt_date(v.get("date_posted") or "—")
            if date != last:
                lines.append("⦿ <b>%s</b>" % date)
                last = date
            lines.append(_tg_vac_line(v, with_source))

    add_block("🟣 iGaming", igaming, True)
    add_block("🟠 Djinni", djinni, False, totals.get("djinni"))
    add_block("🟢 DOU", dou, False, totals.get("dou"))

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


def _deliver(token, chat_id, messages):
    """Send every chunk of one digest to a single chat via the Bot API.

    A long digest is split into several messages. One failing chunk should not
    strand the ones after it, so a plain failure is logged and the remaining
    chunks are still attempted. The return value classifies the recipient:

    * ``"ok"`` — every chunk was delivered.
    * ``"blocked"`` — the recipient is gone for good (the user blocked the bot,
      or the chat no longer exists), reported by Telegram as HTTP 403 or a 400
      "chat not found". There is no point sending the rest, so delivery stops
      and the caller can retire the recipient.
    * ``"error"`` — at least one chunk failed for some other, likely transient
      reason; the recipient is kept for the next run.
    """
    status = "ok"
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
        except urllib.error.HTTPError as exc:
            # 403 = the user blocked the bot; 400 = a malformed request, which in
            # practice here means "chat not found" for a chat that was deleted.
            # Either way this recipient is unreachable and should be retired.
            if exc.code in (400, 403):
                sys.stderr.write(
                    "Telegram %d for %s: %s\n" % (exc.code, chat_id, exc)
                )
                return "blocked"
            sys.stderr.write("Telegram send failed for %s: %s\n" % (chat_id, exc))
            status = "error"
        except Exception as exc:  # noqa: BLE001 - notification must not break the run
            sys.stderr.write("Telegram send failed for %s: %s\n" % (chat_id, exc))
            status = "error"
    return status


def send_telegram(token, chat_id, messages):
    """Send a digest to one chat, returning ``True`` only when it fully arrived.

    Thin wrapper over :func:`_deliver` kept for callers that only need the
    success flag.
    """
    return _deliver(token, chat_id, messages) == "ok"


def _worker_headers(key=None, extra=None):
    """Headers for a request to the subscriber Worker.

    Always sets an explicit ``User-Agent`` (Cloudflare answers the default one
    with a 403). When ``key`` is given it is sent as an ``Authorization: Bearer``
    token, keeping the secret out of the URL/query string; when it is ``None``
    the request relies on any key already embedded in the URL, so an unmigrated
    ``?key=`` setup keeps working.
    """
    headers = {"User-Agent": WORKER_USER_AGENT}
    if extra:
        headers.update(extra)
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def fetch_subscribers():
    """Return the active subscriber chat ids from the bot's ``/start`` list.

    The list lives outside this repository (chat ids are personal data and must
    never be committed) and is served by the Cloudflare Worker in ``trigger/``.
    ``SUBSCRIBERS_URL`` points at that endpoint. The read key is sent as a Bearer
    token when ``WORKER_API_KEY`` is set; otherwise it must be baked into the URL
    (``?key=...``). Best-effort: any failure logs and yields an empty list, so a
    Worker outage never blocks the digest to the static recipients.

    Accepts either a bare JSON array of ids or ``{"subscribers": [...]}``.
    """
    url = os.environ.get("SUBSCRIBERS_URL")
    if not url:
        return []
    try:
        req = urllib.request.Request(
            url, headers=_worker_headers(os.environ.get("WORKER_API_KEY"))
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - delivery must not break the run
        sys.stderr.write("Subscriber fetch failed: %s\n" % exc)
        return []
    if isinstance(payload, dict):
        payload = payload.get("subscribers", [])
    return [str(x).strip() for x in payload if str(x).strip()]


def collect_recipients():
    """Merge the static ``TELEGRAM_CHAT_ID`` list with the dynamic subscriber
    list, de-duplicated and preserving order (static ids first).

    ``TELEGRAM_CHAT_ID`` may hold several comma-separated ids — typically the
    owner's own DM or a channel — and always receive the digest. Subscribers who
    pressed ``/start`` are appended. The owner appearing in both lists is sent to
    only once.
    """
    recipients = []
    seen = set()
    static = os.environ.get("TELEGRAM_CHAT_ID", "")
    for chat_id in (c.strip() for c in static.split(",")):
        if chat_id and chat_id not in seen:
            seen.add(chat_id)
            recipients.append(chat_id)
    for chat_id in fetch_subscribers():
        if chat_id not in seen:
            seen.add(chat_id)
            recipients.append(chat_id)
    return recipients


def igaming_recipients():
    """Chat ids allowed to see the iGaming block, from ``IGAMING_CHAT_IDS``.

    Empty (the variable unset) means everyone gets the full report with the
    iGaming block, i.e. the original behaviour. When it lists ids, only those get
    the iGaming block; everyone else gets a report where iGaming vacancies are
    folded back into the Djinni/DOU sections.
    """
    raw = os.environ.get("IGAMING_CHAT_IDS", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def deactivate_subscribers(chat_ids):
    """Ask the Worker to retire recipients that blocked the bot or vanished.

    Posts the ids to ``DEACTIVATE_URL`` so they are marked inactive and dropped
    from future runs. This is a destructive endpoint, so the admin key is sent as
    a Bearer token from ``WORKER_ADMIN_KEY`` (falling back to ``WORKER_API_KEY``,
    then to any key baked into the URL). Best-effort: a failure is logged and
    ignored — the worst case is retrying a dead id next run.
    """
    url = os.environ.get("DEACTIVATE_URL")
    if not url or not chat_ids:
        return
    key = os.environ.get("WORKER_ADMIN_KEY") or os.environ.get("WORKER_API_KEY")
    data = json.dumps({"chat_ids": list(chat_ids)}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers=_worker_headers(key, {"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 - delivery must not break the run
        sys.stderr.write("Subscriber deactivate failed: %s\n" % exc)


def main():
    now_kyiv = datetime.datetime.now(KYIV_TZ)
    sent_at = now_kyiv.strftime("%d-%m-%Y %H:%M")
    today_d = now_kyiv.date()
    today = today_d.isoformat()
    cutoff = (today_d - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()

    data = scrapers.fetch_all(relevant_only=True)
    errors = data.get("_errors", {})
    totals = data.get("_totals", {})

    # If BOTH sources failed, exit non-zero so the run is visibly broken and no
    # empty report gets committed.
    if errors and not data["dou"] and not data["djinni"]:
        sys.stderr.write("Both scrapers failed: %s\n" % errors)
        return 1

    # Record the day's per-source scanned totals — the raw number each board
    # returns before the relevance filter, i.e. the count the site itself shows
    # (DOU's whole Design category, Djinni's Product Design + UI/UX tag listing) —
    # then regenerate the Excel workbook + chart from the running CSV. A source
    # that failed is absent from `_totals`, so `.get` yields None, which is stored
    # as a gap in the chart rather than a real zero.
    counts = {
        "dou": totals.get("dou"),
        "djinni": totals.get("djinni"),
    }
    analytics.record_day(counts, today)
    analytics.build_workbook()

    # Djinni's list only exposes the published date, but a posting can be bumped
    # afterwards. Rank by the "Оновлено" (updated) date when the vacancy page
    # exposes one so a re-bumped posting resurfaces; keep the published date
    # otherwise. Done before windowing because the update date decides the window.
    for v in data["djinni"]:
        updated = scrapers.fetch_djinni_updated(v["url"], today_d)
        if updated:
            v["date_posted"] = updated

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

    # Keep the full per-source lists (iGaming included) for the reduced report
    # variant, which folds iGaming vacancies back into Djinni/DOU with no
    # separate block.
    djinni_all = list(djinni)
    dou_all = list(dou)

    # Pull iGaming vacancies out of both source lists into a top block. They
    # keep their source label but are not repeated in the per-source blocks.
    igaming = [v for v in djinni + dou if is_igaming(v)]
    igaming.sort(key=lambda x: x["title"].lower())
    igaming.sort(key=lambda x: x["date_posted"], reverse=True)
    djinni = [v for v in djinni if not is_igaming(v)]
    dou = [v for v in dou if not is_igaming(v)]

    report = build_report(today, cutoff, igaming, djinni, dou, errors, sent_at, totals)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, "report-%s.md" % today)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    print("Report: %s" % report_path)
    print(
        "Window %s..%s | iGaming %d | Djinni %d/%s | DOU %d/%s | errors: %s"
        % (
            cutoff,
            today,
            len(igaming),
            len(djinni),
            totals.get("djinni", "?"),
            len(dou),
            totals.get("dou", "?"),
            errors or "none",
        )
    )

    # Deliver to Telegram as inline messages when configured (skipped locally).
    # Recipients are the static TELEGRAM_CHAT_ID list plus everyone who
    # subscribed to the bot with /start (fetched from the Worker); see
    # collect_recipients. Delivery is skipped locally, where no token is set.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        recipients = collect_recipients()
        if recipients:
            # Full report keeps iGaming as its own top block; the reduced report
            # (built on demand) folds iGaming vacancies back into Djinni/DOU with
            # no separate block. IGAMING_CHAT_IDS decides who gets which.
            full_messages = build_telegram_messages(
                today, cutoff, igaming, djinni, dou, sent_at, totals
            )
            reduced_messages = None
            allow_igaming = igaming_recipients()
            sent = 0
            blocked = []
            for chat_id in recipients:
                if not allow_igaming or chat_id in allow_igaming:
                    messages = full_messages
                else:
                    if reduced_messages is None:
                        reduced_messages = build_telegram_messages(
                            today, cutoff, [], djinni_all, dou_all, sent_at, totals
                        )
                    messages = reduced_messages
                status = _deliver(token, chat_id, messages)
                if status == "ok":
                    sent += 1
                elif status == "blocked":
                    blocked.append(chat_id)
                print("Telegram: %s -> %s (%d message(s))" % (status, chat_id, len(messages)))
            # Retire recipients who blocked the bot so they are not retried.
            deactivate_subscribers(blocked)
            print(
                "Telegram: delivered to %d/%d recipients, %d blocked"
                % (sent, len(recipients), len(blocked))
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
