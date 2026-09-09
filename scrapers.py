"""Scrapers for DOU and Djinni design vacancies.

Standard library only (urllib) so the script runs in a fresh cloud environment
without installing any dependencies. Each scraper returns a list of normalized
vacancy dicts with the keys: source, id, title, company, url, location,
date_posted (an ISO ``YYYY-MM-DD`` string, or ``None`` when the board does not
expose a parseable date).
"""

import datetime
import http.cookiejar
import json
import re
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

DOU_URL = "https://jobs.dou.ua/vacancies/?category=Design"
DOU_XHR = "https://jobs.dou.ua/vacancies/xhr-load/?category=Design"
DJINNI_URL = "https://djinni.co/jobs/?primary_keyword=Design"

# Titles we care about: Product Design and UI/UX families.
RELEVANT = re.compile(
    r"product\s*design"
    r"|ui\s*/?\s*ux"
    r"|ux\s*/?\s*ui"
    r"|\bux\s*designer\b"
    r"|\bui\s*designer\b"
    r"|user\s*experience"
    r"|user\s*interface",
    re.IGNORECASE,
)


def is_relevant(title):
    return bool(RELEVANT.search(title or ""))


def _build_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener, jar


def _clean(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


# --- DOU -------------------------------------------------------------------

# DOU shows the date as a Ukrainian day + genitive month, e.g. "4 вересня",
# with no year. Map the month names so the list dates can be parsed.
_UA_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def _parse_dou_date(text, today):
    """Turn "4 вересня" into an ISO date string, or return None.

    DOU omits the year, so assume the current year and roll back to the
    previous one when the month lies in the future (a December listing seen
    in January).
    """
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return None
    try:
        day = int(parts[0])
    except ValueError:
        return None
    month = _UA_MONTHS.get(parts[1].lower())
    if not month:
        return None
    year = today.year
    if month > today.month + 1:
        year -= 1
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


# Each <li class="l-vacancy"> block holds the date div, the title anchor, the
# company anchor and an optional cities span. Attributes wrap across lines, so
# every sub-pattern matches with DOTALL.
_DOU_LI = re.compile(r'<li class="l-vacancy.*?</li>', re.S)
_DOU_DATE = re.compile(r'<div class="date">\s*([^<]+?)\s*</div>')
_DOU_VT = re.compile(
    r'<a class="vt" href="(https://jobs\.dou\.ua/[^"?]+/vacancies/(\d+)/[^"]*)"\s*>(.*?)</a>',
    re.S,
)
_DOU_COMPANY = re.compile(r'<a\s+class="company"[^>]*>(.*?)</a>', re.S)
_DOU_CITY = re.compile(r'<span class="cities[^"]*">([^<]*)</span>')


def _parse_dou_html(html, today=None):
    today = today or datetime.date.today()
    items = []
    for block in _DOU_LI.finditer(html):
        chunk = block.group(0)
        vt = _DOU_VT.search(chunk)
        if not vt:
            continue
        date_div = _DOU_DATE.search(chunk)
        company = _DOU_COMPANY.search(chunk)
        city = _DOU_CITY.search(chunk)
        items.append(
            {
                "source": "dou",
                "id": vt.group(2),
                "title": _clean(vt.group(3)),
                "company": _clean(company.group(1)) if company else "",
                "url": vt.group(1).split("?")[0],
                "location": _clean(city.group(1)) if city else "",
                "date_posted": _parse_dou_date(date_div.group(1) if date_div else "", today),
            }
        )
    return items


def fetch_dou(max_pages=40):
    opener, jar = _build_opener()

    with opener.open(DOU_URL, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    items = _parse_dou_html(html)

    csrf = None
    for c in jar:
        if c.name == "csrftoken":
            csrf = c.value

    count = 20
    for _ in range(max_pages):
        if not csrf:
            break
        data = urllib.parse.urlencode(
            {"csrfmiddlewaretoken": csrf, "count": count, "category": "Design"}
        ).encode()
        req = urllib.request.Request(
            DOU_XHR,
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": DOU_URL,
                "User-Agent": USER_AGENT,
            },
        )
        with opener.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        items.extend(_parse_dou_html(payload.get("html", "")))
        count = payload.get("num", count + 20)
        if payload.get("last"):
            break

    # De-duplicate by vacancy id (pages can overlap).
    seen, unique = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        unique.append(it)
    return unique


# The DOU vacancy body lives in a "b-typo vacancy-section" div and ends where
# the reply/apply block begins.
_DOU_DESC = re.compile(
    r'class="b-typo vacancy-section">(.*?)<div class="reply', re.S
)


def fetch_dou_description(url):
    """Return the plain-text body of a single DOU vacancy page, or "".

    Used to detect keywords (e.g. iGaming) that appear in the description but
    not in the title. Network or parse failures degrade to an empty string.
    """
    try:
        opener, _ = _build_opener()
        with opener.open(url, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - detection is best-effort
        return ""
    m = _DOU_DESC.search(html)
    if m:
        return _clean(m.group(1))
    # Fallback: a bounded slice from the section start if the reply anchor moved.
    start = html.find("b-typo vacancy-section")
    return _clean(html[start:start + 12000]) if start != -1 else ""


# --- Djinni ----------------------------------------------------------------

_LD_BLOCK = re.compile(
    r'<script type="application/ld\+json">\s*(.*?)</script>', re.S
)
_DJINNI_ID = re.compile(r"/jobs/(\d+)")


def _parse_djinni_ld(html):
    items = []
    for block in _LD_BLOCK.finditer(html):
        try:
            data = json.loads(block.group(1))
        except json.JSONDecodeError:
            continue
        entries = data if isinstance(data, list) else [data]
        for j in entries:
            if not isinstance(j, dict) or j.get("@type") != "JobPosting":
                continue
            url = (j.get("url") or "").split("?")[0]
            m = _DJINNI_ID.search(url)
            org = j.get("hiringOrganization") or {}
            loc = ""
            req = j.get("applicantLocationRequirements") or {}
            if isinstance(req, dict):
                addr = req.get("address") or {}
                loc = addr.get("addressCountry", "") if isinstance(addr, dict) else ""
            # datePosted is an ISO datetime like "2026-09-08T11:33:14.49"; keep
            # the date part when it is well formed.
            posted = (j.get("datePosted") or "")[:10]
            if not re.match(r"\d{4}-\d{2}-\d{2}$", posted):
                posted = None
            items.append(
                {
                    "source": "djinni",
                    "id": m.group(1) if m else url,
                    "title": _clean(j.get("title", "")),
                    "company": _clean(org.get("name", "") if isinstance(org, dict) else ""),
                    "url": url,
                    "location": "Remote" if j.get("jobLocationType") == "TELECOMMUTE" else loc,
                    "date_posted": posted,
                    # The list JSON-LD already carries the full description, so
                    # Djinni needs no per-vacancy fetch to search its text.
                    "description": _clean(j.get("description", "")),
                }
            )
    return items


def fetch_djinni(max_pages=20):
    opener, _ = _build_opener()
    items = []
    for page in range(1, max_pages + 1):
        url = "%s&page=%d" % (DJINNI_URL, page)
        with opener.open(url, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
        page_items = _parse_djinni_ld(html)
        if not page_items:
            break
        items.extend(page_items)
        if 'rel="next"' not in html and "rel=next" not in html:
            break

    seen, unique = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        unique.append(it)
    return unique


# The list JSON-LD only carries datePosted (the "Опубліковано" date). The
# vacancy detail page additionally shows an "Оновлено <day> <month>" line when a
# posting was bumped after publication, in the same year-less Ukrainian format
# DOU uses. Match the day + genitive month that follows the word.
_DJINNI_UPDATED = re.compile(r"Оновлено\s+(\d{1,2}\s+[а-яіїєґ']+)", re.IGNORECASE)


def fetch_djinni_updated(url, today=None):
    """Return the ISO "Оновлено" (updated) date for a Djinni vacancy, or None.

    Djinni omits the line entirely for a posting that was never updated, so a
    None result means the caller should keep the published date. Network or
    parse failures degrade to None for the same reason.
    """
    today = today or datetime.date.today()
    try:
        opener, _ = _build_opener()
        with opener.open(url, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - best-effort, fall back to datePosted
        return None
    m = _DJINNI_UPDATED.search(html)
    if not m:
        return None
    return _parse_dou_date(m.group(1), today)


def fetch_all(relevant_only=True):
    """Return {'dou': [...], 'djinni': [...]} of vacancies.

    Each source is fetched independently; a failure in one does not abort the
    other. Errors are attached under the '_errors' key.
    """
    result = {"dou": [], "djinni": [], "_errors": {}}
    for name, fn in (("dou", fetch_dou), ("djinni", fetch_djinni)):
        try:
            found = fn()
            if relevant_only:
                found = [v for v in found if is_relevant(v["title"])]
            result[name] = found
        except Exception as exc:  # noqa: BLE001 - report, don't crash the run
            result["_errors"][name] = "%s: %s" % (type(exc).__name__, exc)
    return result


if __name__ == "__main__":
    data = fetch_all()
    for src in ("dou", "djinni"):
        print("== %s: %d relevant ==" % (src, len(data[src])))
        for v in data[src]:
            print("  [%s] %s — %s (%s)" % (v["id"], v["title"], v["company"], v["location"]))
    if data["_errors"]:
        print("ERRORS:", data["_errors"])
