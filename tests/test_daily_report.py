"""Unit tests for the report-rendering and Telegram-formatting helpers."""

import datetime
import io
import json
import urllib.error

import pytest

import daily_report as dr


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record pauses instead of taking them, so delivery tests stay instant."""
    waits = []
    monkeypatch.setattr(dr.time, "sleep", waits.append)
    return waits


def _vac(**over):
    base = {
        "source": "djinni",
        "title": "Product Designer",
        "company": "Initech",
        "url": "https://example.com/1",
        "location": "Remote",
        "date_posted": "2026-09-10",
        "description": "",
    }
    base.update(over)
    return base


# --- _fmt_date -------------------------------------------------------------

def test_fmt_date_reformats_iso():
    assert dr._fmt_date("2026-09-04") == "04-09-2026"


def test_fmt_date_passes_through_non_iso():
    assert dr._fmt_date("whenever") == "whenever"
    assert dr._fmt_date(None) == "—"


# --- is_highlight ----------------------------------------------------------

# A made-up pattern: the real one is deployment configuration, not code.
_PATTERN = r"\bfintech\b|банк"


def test_is_highlight_is_off_without_a_pattern(monkeypatch):
    monkeypatch.delenv("HIGHLIGHT_PATTERN", raising=False)
    assert not dr.is_highlight(_vac(title="Fintech Designer"))


def test_is_highlight_matches_every_field(monkeypatch):
    monkeypatch.setenv("HIGHLIGHT_PATTERN", _PATTERN)
    assert dr.is_highlight(_vac(title="Product Designer (Fintech)"))
    assert dr.is_highlight(_vac(title="Designer", company="Big Fintech Co"))
    assert dr.is_highlight(_vac(title="Designer", location="fintech hub"))
    assert dr.is_highlight(_vac(title="Designer", description="Онлайн-банк"))


def test_is_highlight_negative(monkeypatch):
    monkeypatch.setenv("HIGHLIGHT_PATTERN", _PATTERN)
    assert not dr.is_highlight(_vac(title="Product Designer", description="healthcare"))


def test_is_highlight_handles_none_fields(monkeypatch):
    # Some listings carry location/description/company as None; is_highlight
    # must not crash on the str.join.
    monkeypatch.setenv("HIGHLIGHT_PATTERN", _PATTERN)
    assert not dr.is_highlight(
        _vac(title="Graphic Designer", company=None, location=None, description=None)
    )
    assert dr.is_highlight(
        _vac(title="Fintech Designer", company=None, location=None, description=None)
    )


def test_highlight_label_defaults_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("HIGHLIGHT_LABEL", raising=False)
    assert dr.highlight_label() == dr.DEFAULT_HIGHLIGHT_LABEL
    monkeypatch.setenv("HIGHLIGHT_LABEL", "⭐ Spotlight")
    assert dr.highlight_label() == "⭐ Spotlight"


# --- _prepare --------------------------------------------------------------

def test_prepare_drops_graphic_design_vacancies():
    data = {
        "djinni": [
            _vac(title="Product Designer", url="https://x/1"),
            _vac(title="Graphic Designer", url="https://x/2"),
        ],
        "dou": [
            _vac(title="Графічний дизайнер", source="dou", url="https://x/3"),
            _vac(title="UX Designer", source="dou", url="https://x/4"),
        ],
    }
    # fast=True keeps the helper offline: no Djinni "Оновлено" or DOU
    # description fetches.
    highlight, djinni, dou, djinni_all, dou_all = dr._prepare(
        data, datetime.date(2026, 9, 10), "2026-09-08", fast=True
    )
    assert [v["title"] for v in djinni] == ["Product Designer"]
    assert [v["title"] for v in dou] == ["UX Designer"]
    assert [v["title"] for v in djinni_all] == ["Product Designer"]
    assert [v["title"] for v in dou_all] == ["UX Designer"]
    assert highlight == []


# --- vac_line --------------------------------------------------------------

def test_vac_line_markdown_with_and_without_source():
    v = _vac(title="UX Designer", url="https://x/2", company="Acme", location="Kyiv", source="dou")
    assert dr.vac_line(v, with_source=False) == "- [UX Designer](https://x/2) — Acme · Kyiv"
    assert dr.vac_line(v, with_source=True).endswith(" [DOU]")


def test_vac_line_without_location():
    v = _vac(location="")
    assert " · " not in dr.vac_line(v, with_source=False)


# --- _esc ------------------------------------------------------------------

def test_esc_escapes_html_specials_in_order():
    assert dr._esc("A & B <tag>") == "A &amp; B &lt;tag&gt;"
    assert dr._esc(None) == ""


# --- build_report ----------------------------------------------------------

def test_build_report_structure_and_counts():
    djinni = [_vac(title="Product Designer", source="djinni")]
    dou = [_vac(title="UX Designer", source="dou", url="https://x/9")]
    report = dr.build_report(
        today="2026-09-14",
        cutoff="2026-09-12",
        djinni=djinni,
        dou=dou,
        errors={},
        sent_at="14-09-2026 21:00",
        totals={"dou": 226, "djinni": 89},
    )
    assert "# Design вакансії — 14-09-2026" in report
    # The per-source headings show kept/scanned from totals.
    assert "## 🔵 Вакансії Djinni (1/89)" in report
    assert "## 🟢 Вакансії DOU (1/226)" in report
    # Each source heading links to the board's full listing.
    assert (
        "## 🔵 Вакансії Djinni (1/89) · [🗂️ Всі вакансії](%s)"
        % dr.scrapers.DJINNI_URLS[0]
    ) in report
    assert (
        "## 🟢 Вакансії DOU (1/226) · [🗂️ Всі вакансії](%s)" % dr.scrapers.DOU_URL
    ) in report


def test_build_report_shows_errors_and_empty_sections():
    report = dr.build_report(
        today="2026-09-14",
        cutoff="2026-09-12",
        djinni=[],
        dou=[],
        errors={"dou": "TimeoutError: timed out"},
        sent_at="14-09-2026 21:00",
        totals={},
    )
    assert "⚠️ Помилки скрапінгу" in report
    assert "TimeoutError" in report
    assert "_Немає за період._" in report


# --- build_telegram_messages ----------------------------------------------

def test_telegram_single_message_for_small_input():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 1},
    )
    assert len(msgs) == 1
    assert "<b>Design вакансії" in msgs[0]


def test_telegram_source_heading_links_to_all_vacancies():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[_vac(title="Fintech Designer", url="https://x/hl")],
        djinni=[_vac()],
        dou=[_vac(source="dou", url="https://x/9")],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 59, "dou": 218},
    )
    body = "\n".join(msgs)
    # The "&" between the Djinni query parameters is escaped for Telegram HTML.
    djinni_href = dr.scrapers.DJINNI_URLS[0].replace("&", "&amp;")
    assert (
        '<b>🔵 Djinni (1/59)</b> · <b><a href="%s">🗂️ Всі вакансії</a></b>' % djinni_href
    ) in body
    assert (
        '<b>🟢 DOU (1/218)</b> · <b><a href="%s">🗂️ Всі вакансії</a></b>'
        % dr.scrapers.DOU_URL
    ) in body
    assert "<b>%s (1)</b>\n" % dr.DEFAULT_HIGHLIGHT_LABEL in body


def test_telegram_shows_empty_scanned_source_with_nothing_new_line():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 59, "dou": 218},
    )
    body = "\n".join(msgs)
    assert (
        '<b>🟢 DOU (0/218)</b> · <b><a href="%s">🗂️ Всі вакансії</a></b>\n'
        "<i>Нових вакансій за %d дні немає</i>"
        % (dr.scrapers.DOU_URL, dr.WINDOW_DAYS)
    ) in body
    assert "Немає вакансій за період." not in body


def test_telegram_hides_source_that_was_not_scanned():
    # A failed fetch leaves the source out of totals; "nothing new" would be a
    # claim the run cannot make.
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 59},
    )
    body = "\n".join(msgs)
    assert "DOU" not in body
    assert "Нових вакансій за" not in body


def test_telegram_hides_source_whose_vacancies_are_all_highlighted():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[_vac(source="dou", url="https://x/hl")],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 59, "dou": 218},
    )
    body = "\n".join(msgs)
    assert "🟢 DOU" not in body
    assert "Нових вакансій за" not in body


def test_telegram_all_sources_empty_shows_each_block_once():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 59, "dou": 218},
    )
    body = "\n".join(msgs)
    assert body.count("<i>Нових вакансій за %d дні немає</i>" % dr.WINDOW_DAYS) == 2
    assert "Немає вакансій за період." not in body


def test_telegram_falls_back_when_no_block_is_drawn():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={},
    )
    assert "Немає вакансій за період." in "\n".join(msgs)


def test_telegram_splits_and_respects_limit():
    many = [
        _vac(title="Product Designer %d" % i, url="https://example.com/%d" % i)
        for i in range(200)
    ]
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=many,
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": len(many)},
    )
    assert len(msgs) >= 2
    assert all(len(m) <= dr.TELEGRAM_LIMIT for m in msgs)


def test_telegram_escapes_html_in_titles():
    v = _vac(title="Designer <script> & co", company="Me & You")
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[v],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 1},
    )
    body = "\n".join(msgs)
    assert "&lt;script&gt;" in body
    assert "Me &amp; You" in body
    assert "<script>" not in body


# --- send_telegram ---------------------------------------------------------

class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def _patch_urlopen(monkeypatch, behaviour):
    """Record every request and drive urlopen from ``behaviour(index, request)``.

    ``behaviour`` returns ``None`` to succeed or an exception instance to raise
    for that call, letting a test simulate a single failing chunk.
    """
    calls = []

    def fake_urlopen(request, timeout=None):
        exc = behaviour(len(calls), request)
        calls.append(request)
        if exc is not None:
            raise exc
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_send_telegram_all_succeed(monkeypatch):
    calls = _patch_urlopen(monkeypatch, lambda i, req: None)
    ok = dr.send_telegram("token", "chat", ["a", "b", "c"])
    assert ok is True
    assert len(calls) == 3


def test_send_telegram_continues_after_a_failing_chunk(monkeypatch):
    # The second chunk fails; the third must still be attempted, and the overall
    # result reports the failure.
    def behaviour(i, req):
        return urllib.error.URLError("boom") if i == 1 else None

    calls = _patch_urlopen(monkeypatch, behaviour)
    ok = dr.send_telegram("token", "chat", ["a", "b", "c"])
    assert ok is False
    assert len(calls) == 3


# --- _deliver classification -----------------------------------------------

def test_deliver_ok_when_every_chunk_arrives(monkeypatch):
    calls = _patch_urlopen(monkeypatch, lambda i, req: None)
    assert dr._deliver("token", "chat", ["a", "b"]) == "ok"
    assert len(calls) == 2


def test_deliver_blocked_on_403_and_stops(monkeypatch):
    # A 403 means the user blocked the bot: the recipient is retired and the
    # remaining chunks are not attempted.
    def behaviour(i, req):
        return urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    calls = _patch_urlopen(monkeypatch, behaviour)
    assert dr._deliver("token", "chat", ["a", "b", "c"]) == "blocked"
    assert len(calls) == 1


def test_deliver_blocked_on_400(monkeypatch):
    def behaviour(i, req):
        return urllib.error.HTTPError("u", 400, "Bad Request", {}, None)

    _patch_urlopen(monkeypatch, behaviour)
    assert dr._deliver("token", "chat", ["a"]) == "blocked"


def test_deliver_error_continues_and_keeps_recipient(monkeypatch):
    # A transient failure on one chunk is reported as "error" (not "blocked"),
    # and the later chunks are still attempted.
    def behaviour(i, req):
        return urllib.error.URLError("boom") if i == 1 else None

    calls = _patch_urlopen(monkeypatch, behaviour)
    assert dr._deliver("token", "chat", ["a", "b", "c"]) == "error"
    assert len(calls) == 3


def _http_429(retry_after=None):
    body = {"ok": False, "error_code": 429, "description": "Too Many Requests"}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return urllib.error.HTTPError(
        "https://api.telegram.org", 429, "Too Many Requests", None,
        io.BytesIO(json.dumps(body).encode()),
    )


def test_deliver_pauses_after_every_message(monkeypatch, slept):
    # The pause is what keeps a long subscriber list under Telegram's bulk rate.
    _patch_urlopen(monkeypatch, lambda i, req: None)
    assert dr._deliver("token", "chat", ["a", "b", "c"]) == "ok"
    assert slept == [dr.TELEGRAM_SEND_INTERVAL_SEC] * 3


def test_deliver_retries_after_429_and_waits_as_asked(monkeypatch, slept):
    calls = _patch_urlopen(monkeypatch, lambda i, req: _http_429(2) if i == 0 else None)
    assert dr._deliver("token", "chat", ["a"]) == "ok"
    assert len(calls) == 2  # the 429, then the retry that got through
    assert slept == [2, dr.TELEGRAM_SEND_INTERVAL_SEC]


def test_deliver_gives_up_after_repeated_429_but_keeps_recipient(monkeypatch):
    # Rate limiting is transient: the chunk fails, the chat is not retired.
    calls = _patch_urlopen(monkeypatch, lambda i, req: _http_429(1))
    assert dr._deliver("token", "chat", ["a"]) == "error"
    assert len(calls) == dr.TELEGRAM_RETRIES_ON_429 + 1


def test_retry_after_defaults_and_caps():
    assert dr._retry_after(_http_429()) == 1
    assert dr._retry_after(_http_429(3600)) == dr.TELEGRAM_MAX_RETRY_WAIT_SEC
    bare = urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)
    assert dr._retry_after(bare) == 1


# --- subscriber list -------------------------------------------------------

class _JsonResponse:
    """Context-manager stand-in for urlopen that returns a fixed JSON payload."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_fetch_subscribers_empty_when_url_unset(monkeypatch):
    monkeypatch.delenv("SUBSCRIBERS_URL", raising=False)
    assert dr.fetch_subscribers() == []


def test_fetch_subscribers_parses_bare_list(monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_URL", "https://worker/subscribers?key=k")
    monkeypatch.setattr(dr.urllib.request, "urlopen", lambda *a, **k: _JsonResponse([1, 2, "3"]))
    assert dr.fetch_subscribers() == ["1", "2", "3"]


def test_fetch_subscribers_sets_user_agent(monkeypatch):
    # Cloudflare 403s the default urllib User-Agent, so the request must carry an
    # explicit one.
    monkeypatch.setenv("SUBSCRIBERS_URL", "https://worker/subscribers?key=k")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        return _JsonResponse([1])

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.fetch_subscribers()
    assert captured["ua"] == dr.WORKER_USER_AGENT


def test_fetch_subscribers_sends_bearer_when_key_set(monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_URL", "https://worker/subscribers")
    monkeypatch.setenv("WORKER_API_KEY", "readkey")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        return _JsonResponse([1])

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.fetch_subscribers()
    assert captured["auth"] == "Bearer readkey"


def test_fetch_subscribers_no_auth_header_without_key(monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_URL", "https://worker/subscribers?key=baked")
    monkeypatch.delenv("WORKER_API_KEY", raising=False)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        return _JsonResponse([1])

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.fetch_subscribers()
    assert captured["auth"] is None


def test_fetch_subscribers_parses_wrapped_object(monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_URL", "https://worker/subscribers")
    monkeypatch.setattr(
        dr.urllib.request, "urlopen", lambda *a, **k: _JsonResponse({"subscribers": [10, 20]})
    )
    assert dr.fetch_subscribers() == ["10", "20"]


def test_fetch_subscribers_empty_on_failure(monkeypatch):
    monkeypatch.setenv("SUBSCRIBERS_URL", "https://worker/subscribers")

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(dr.urllib.request, "urlopen", boom)
    assert dr.fetch_subscribers() == []


# --- collect_recipients ----------------------------------------------------

def test_collect_recipients_merges_and_dedups(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 10 , 20 ")
    monkeypatch.setattr(dr, "fetch_subscribers", lambda: ["20", "30"])
    # Static ids first, subscriber ids appended, the shared "20" listed once.
    assert dr.collect_recipients() == ["10", "20", "30"]


def test_collect_recipients_subscribers_only(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(dr, "fetch_subscribers", lambda: ["7", "8"])
    assert dr.collect_recipients() == ["7", "8"]


# --- mask_chat_ids ---------------------------------------------------------

def test_mask_chat_ids_registers_each_id_in_actions(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    dr.mask_chat_ids(["123456789", "987654321"])
    assert capsys.readouterr().out == "::add-mask::123456789\n::add-mask::987654321\n"


def test_mask_chat_ids_prints_nothing_outside_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    dr.mask_chat_ids(["123456789"])
    assert capsys.readouterr().out == ""


# --- deactivate_subscribers ------------------------------------------------

def test_deactivate_posts_ids(monkeypatch):
    monkeypatch.setenv("DEACTIVATE_URL", "https://worker/deactivate?key=k")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["ua"] = request.get_header("User-agent")
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.deactivate_subscribers(["1", "2"])
    assert captured["url"].startswith("https://worker/deactivate")
    assert json.loads(captured["data"]) == {"chat_ids": ["1", "2"]}
    assert captured["ua"] == dr.WORKER_USER_AGENT


def test_deactivate_sends_admin_bearer(monkeypatch):
    monkeypatch.setenv("DEACTIVATE_URL", "https://worker/deactivate")
    monkeypatch.setenv("WORKER_ADMIN_KEY", "adminkey")
    monkeypatch.setenv("WORKER_API_KEY", "readkey")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.deactivate_subscribers(["1"])
    # The admin key wins over the read key for a destructive call.
    assert captured["auth"] == "Bearer adminkey"


def test_deactivate_falls_back_to_api_key(monkeypatch):
    monkeypatch.setenv("DEACTIVATE_URL", "https://worker/deactivate")
    monkeypatch.delenv("WORKER_ADMIN_KEY", raising=False)
    monkeypatch.setenv("WORKER_API_KEY", "readkey")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.deactivate_subscribers(["1"])
    assert captured["auth"] == "Bearer readkey"


def test_deactivate_noop_when_url_unset(monkeypatch):
    monkeypatch.delenv("DEACTIVATE_URL", raising=False)
    called = []
    monkeypatch.setattr(dr.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    dr.deactivate_subscribers(["1"])
    assert called == []


def test_deactivate_noop_when_no_ids(monkeypatch):
    monkeypatch.setenv("DEACTIVATE_URL", "https://worker/deactivate")
    called = []
    monkeypatch.setattr(dr.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    dr.deactivate_subscribers([])
    assert called == []


# --- _prepare bump lookback ------------------------------------------------

def test_prepare_only_checks_bumps_within_the_lookback(monkeypatch):
    # The "Оновлено" check opens one page per vacancy, so it is limited to
    # postings recent enough that a bump could bring them into the window.
    today = datetime.date(2026, 9, 22)
    fresh = _vac(title="Product Designer", url="https://x/fresh", date_posted="2026-09-20")
    midway = _vac(title="UX Designer", url="https://x/midway", date_posted="2026-09-15")
    ancient = _vac(title="UI Designer", url="https://x/ancient", date_posted="2026-08-20")
    asked = []

    def fake_updated(url, today_d=None):
        asked.append(url)
        return None

    monkeypatch.setattr(dr.scrapers, "fetch_djinni_updated", fake_updated)
    monkeypatch.setattr(dr.scrapers, "fetch_dou_description", lambda url: "")
    dr._prepare({"djinni": [fresh, midway, ancient], "dou": []}, today, "2026-09-20")
    assert asked == ["https://x/fresh", "https://x/midway"]


def test_prepare_bump_pulls_an_older_vacancy_into_the_window(monkeypatch):
    today = datetime.date(2026, 9, 22)
    bumped = _vac(title="Product Designer", url="https://x/bumped", date_posted="2026-09-16")

    monkeypatch.setattr(dr.scrapers, "fetch_djinni_updated", lambda url, today_d=None: "2026-09-22")
    monkeypatch.setattr(dr.scrapers, "fetch_dou_description", lambda url: "")
    _, djinni, _, _, _ = dr._prepare({"djinni": [bumped], "dou": []}, today, "2026-09-20")
    assert [v["title"] for v in djinni] == ["Product Designer"]
    assert djinni[0]["date_posted"] == "2026-09-22"


def test_prepare_skips_dou_descriptions_without_a_pattern(monkeypatch):
    # The description is read only to match the highlight pattern, so with no
    # pattern there is nothing to fetch it for.
    monkeypatch.delenv("HIGHLIGHT_PATTERN", raising=False)
    fetched = []
    monkeypatch.setattr(dr.scrapers, "fetch_djinni_updated", lambda url, today_d=None: None)
    monkeypatch.setattr(dr.scrapers, "fetch_dou_description", fetched.append)
    dou = [_vac(title="UX Designer", source="dou", url="https://x/d", date_posted="2026-09-22")]
    dr._prepare({"djinni": [], "dou": dou}, datetime.date(2026, 9, 22), "2026-09-20")
    assert fetched == []


def test_prepare_highlights_a_dou_vacancy_by_its_description(monkeypatch):
    monkeypatch.setenv("HIGHLIGHT_PATTERN", _PATTERN)
    fetched = []

    def fake_description(url):
        fetched.append(url)
        return "we build a fintech app" if url == "https://x/hit" else "healthcare"

    monkeypatch.setattr(dr.scrapers, "fetch_djinni_updated", lambda url, today_d=None: None)
    monkeypatch.setattr(dr.scrapers, "fetch_dou_description", fake_description)
    dou = [
        _vac(title="UX Designer", source="dou", url="https://x/hit", date_posted="2026-09-22"),
        _vac(title="UI Designer", source="dou", url="https://x/miss", date_posted="2026-09-22"),
        # Already matched by its company, so its page is not opened.
        _vac(title="Product Designer", source="dou", company="Fintech Co",
             url="https://x/company", date_posted="2026-09-22"),
    ]
    highlight, _, dou_kept, _, dou_all = dr._prepare(
        {"djinni": [], "dou": dou}, datetime.date(2026, 9, 22), "2026-09-20"
    )
    assert sorted(fetched) == ["https://x/hit", "https://x/miss"]
    assert sorted(v["url"] for v in highlight) == ["https://x/company", "https://x/hit"]
    assert [v["url"] for v in dou_kept] == ["https://x/miss"]
    assert len(dou_all) == 3


# --- week_count ------------------------------------------------------------

def test_week_count_covers_seven_days_and_the_digest_filter():
    today = datetime.date(2026, 9, 22)
    data = {
        "djinni": [
            _vac(title="Product Designer", date_posted="2026-09-22"),
            # The first day of the week, counting today.
            _vac(title="UX Designer", date_posted="2026-09-16"),
            # A day too old.
            _vac(title="UI Designer", date_posted="2026-09-15"),
            # Scraped, but not in the family the digest ships.
            _vac(title="Graphic Designer", date_posted="2026-09-21"),
            # No date at all cannot be placed in the week.
            _vac(title="Product Designer", date_posted=None),
        ],
        "dou": [_vac(title="UX/UI дизайнер", source="dou", date_posted="2026-09-20")],
        "_errors": {},
    }
    assert dr.week_count(data, today) == 3


def test_week_count_is_none_when_a_board_failed():
    # One board's count alone would understate the week.
    data = {
        "djinni": [_vac(date_posted="2026-09-22")],
        "dou": [],
        "_errors": {"dou": "URLError: down"},
    }
    assert dr.week_count(data, datetime.date(2026, 9, 22)) is None


# --- publish_digest --------------------------------------------------------

def test_publish_digest_posts_both_variants(monkeypatch):
    monkeypatch.setenv("DIGEST_URL", "https://worker/digest")
    monkeypatch.setenv("WORKER_ADMIN_KEY", "admin")
    monkeypatch.setenv("HIGHLIGHT_CHAT_IDS", " 22, 11 ")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["type"] = request.get_header("Content-type")
        captured["body"] = json.loads(request.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    assert dr.publish_digest("2026-09-22", "22-09-2026 21:00", ["a", "b"], ["a"]) is True
    assert captured["url"] == "https://worker/digest"
    assert captured["auth"] == "Bearer admin"
    assert captured["type"] == "application/json"
    assert captured["body"] == {
        "date": "2026-09-22",
        "sent_at": "22-09-2026 21:00",
        "full": ["a", "b"],
        "reduced": ["a"],
        # The same list the scheduled delivery uses, so /start cannot drift
        # from it; normalised the way highlight_recipients reads it.
        "highlight_chat_ids": ["11", "22"],
        # No count given: the Worker leaves the description's line out.
        "week_count": None,
    }


def test_publish_digest_carries_the_week_count(monkeypatch):
    monkeypatch.setenv("DIGEST_URL", "https://worker/digest")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.publish_digest("2026-09-22", "21:00", ["a"], None, 38)
    assert captured["body"]["week_count"] == 38


def test_publish_digest_sends_an_empty_list_when_unrestricted(monkeypatch):
    # No HIGHLIGHT_CHAT_IDS means no restriction; an empty list tells the Worker
    # exactly that, rather than leaving it to guess from a missing field.
    monkeypatch.setenv("DIGEST_URL", "https://worker/digest")
    monkeypatch.delenv("HIGHLIGHT_CHAT_IDS", raising=False)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.publish_digest("2026-09-22", "21:00", ["a"], None)
    assert captured["body"]["highlight_chat_ids"] == []


def test_publish_digest_carries_a_null_reduced_variant(monkeypatch):
    # No HIGHLIGHT_CHAT_IDS means everybody gets the full variant and the report
    # never builds a reduced one; the Worker must be told so explicitly.
    monkeypatch.setenv("DIGEST_URL", "https://worker/digest")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    dr.publish_digest("2026-09-22", "21:00", ["a"], None)
    assert captured["body"]["reduced"] is None


def test_publish_digest_noop_when_url_unset(monkeypatch):
    monkeypatch.delenv("DIGEST_URL", raising=False)

    def boom(*a, **k):
        raise AssertionError("must not call the network")

    monkeypatch.setattr(dr.urllib.request, "urlopen", boom)
    assert dr.publish_digest("2026-09-22", "21:00", ["a"], None) is False


def test_publish_digest_survives_a_failure(monkeypatch):
    # A stale stored digest is a far smaller problem than a digest that never
    # goes out, so a failure here is swallowed.
    monkeypatch.setenv("DIGEST_URL", "https://worker/digest")

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(dr.urllib.request, "urlopen", boom)
    assert dr.publish_digest("2026-09-22", "21:00", ["a"], None) is False


def test_highlight_recipients_unset_is_empty(monkeypatch):
    monkeypatch.delenv("HIGHLIGHT_CHAT_IDS", raising=False)
    assert dr.highlight_recipients() == set()


def test_highlight_recipients_parses_list(monkeypatch):
    monkeypatch.setenv("HIGHLIGHT_CHAT_IDS", " 11 , 42 ")
    assert dr.highlight_recipients() == {"11", "42"}


def test_select_messages_full_when_no_restriction():
    # Empty allow-set => everyone gets the full variant.
    assert dr.select_messages("42", set(), "FULL", "REDUCED") == "FULL"


def test_select_messages_full_for_listed_chat():
    assert dr.select_messages("42", {"42"}, "FULL", "REDUCED") == "FULL"


def test_select_messages_reduced_for_other_chat():
    assert dr.select_messages("99", {"42"}, "FULL", "REDUCED") == "REDUCED"


def test_full_report_shows_highlight_block(monkeypatch):
    monkeypatch.setenv("HIGHLIGHT_LABEL", "⭐ Spotlight")
    hl = [_vac(source="djinni", title="Designer", company="Fintech Co")]
    dj = [_vac(source="djinni", title="Product Designer")]
    msgs = dr.build_telegram_messages(
        "2026-09-20", "2026-09-18", hl, dj, [], "20-09-2026 10:00", {}
    )
    assert "⭐ Spotlight" in "\n".join(msgs)


def test_reduced_report_folds_highlight_into_sources(monkeypatch):
    # The reduced variant is built with an empty highlight list and the full
    # per-source lists, so the block is gone but the vacancy still shows.
    monkeypatch.setenv("HIGHLIGHT_LABEL", "⭐ Spotlight")
    dj_all = [
        _vac(source="djinni", title="Wallet Designer", company="Fintech Co"),
        _vac(source="djinni", title="Product Designer"),
    ]
    text = "\n".join(
        dr.build_telegram_messages(
            "2026-09-20", "2026-09-18", [], dj_all, [], "20-09-2026 10:00", {}
        )
    )
    assert "⭐ Spotlight" not in text
    assert "Wallet Designer" in text


def test_telegram_notice_is_the_first_line_and_escaped():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 1},
        notice="⚠️ Djinni <was> down & is back",
    )
    lines = msgs[0].split("\n")
    assert lines[0] == "⚠️ Djinni &lt;was&gt; down &amp; is back"
    assert lines[1] == ""
    assert lines[2].startswith("<b>Design вакансії")


def test_telegram_without_notice_starts_with_the_heading():
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        highlight=[],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 1},
    )
    assert msgs[0].startswith("<b>Design вакансії")
