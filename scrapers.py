"""Scrapers for DOU and Djinni design vacancies.

Standard library only (urllib) so the script runs in a fresh cloud environment
without installing any dependencies. Each scraper returns a list of normalized
vacancy dicts with the keys: source, id, title, company, url, location.
"""

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

# One <li class="l-vacancy"> block: the title anchor, the company anchor and
# the optional cities span. The markup puts class before href and wraps
# attributes across lines, so DOTALL matching is required.
_DOU_ITEM = re.compile(
    r'<a class="vt" href="(?P<url>https://jobs\.dou\.ua/[^"?]+/vacancies/(?P<id>\d+)/[^"]*)"\s*>'
    r"(?P<title>.*?)</a>"
    r'.*?<a\s+class="company"[^>]*>(?P<company>.*?)</a>'
    r'(?:.*?<span class="cities[^"]*">(?P<city>[^<]*)</span>)?',
    re.S,
)


def _parse_dou_html(html):
    items = []
    for m in _DOU_ITEM.finditer(html):
        items.append(
            {
                "source": "dou",
                "id": m.group("id"),
                "title": _clean(m.group("title")),
                "company": _clean(m.group("company")),
                "url": m.group("url").split("?")[0],
                "location": _clean(m.group("city") or ""),
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
            items.append(
                {
                    "source": "djinni",
                    "id": m.group(1) if m else url,
                    "title": _clean(j.get("title", "")),
                    "company": _clean(org.get("name", "") if isinstance(org, dict) else ""),
                    "url": url,
                    "location": "Remote" if j.get("jobLocationType") == "TELECOMMUTE" else loc,
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
