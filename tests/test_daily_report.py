"""Unit tests for the report-rendering and Telegram-formatting helpers."""

import datetime
import json
import urllib.error

import daily_report as dr


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


# --- is_igaming ------------------------------------------------------------

def test_is_igaming_matches_spelling_variants():
    assert dr.is_igaming(_vac(title="Product Designer (iGaming)"))
    assert dr.is_igaming(_vac(title="Designer", description="fast-growing i-gaming studio"))
    assert dr.is_igaming(_vac(title="Designer", company="Big iGaming Co"))


def test_is_igaming_negative():
    assert not dr.is_igaming(_vac(title="Product Designer", description="fintech"))


def test_is_igaming_does_not_match_a_word_ending_in_i():
    # "gaming" preceded by another word is game development, not iGaming; only a
    # standalone "i" prefix counts.
    for text in ("AI Gaming", "Xiaomi Gaming", "Sci-Gaming", "Multi gaming platform"):
        assert not dr.is_igaming(_vac(title="Product Designer", description=text)), text


def test_is_igaming_matches_standalone_prefix_after_punctuation():
    for text in ("UI Designer — iGaming", "Designer (iGaming)"):
        assert dr.is_igaming(_vac(title=text)), text


def test_is_igaming_handles_none_fields():
    # Some listings carry location/description/company as None; is_igaming must
    # not crash on the str.join.
    assert not dr.is_igaming(
        _vac(title="Graphic Designer", company=None, location=None, description=None)
    )
    assert dr.is_igaming(
        _vac(title="iGaming Designer", company=None, location=None, description=None)
    )


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
    igaming, djinni, dou, djinni_all, dou_all = dr._prepare(
        data, datetime.date(2026, 9, 10), "2026-09-08", fast=True
    )
    assert [v["title"] for v in djinni] == ["Product Designer"]
    assert [v["title"] for v in dou] == ["UX Designer"]
    assert [v["title"] for v in djinni_all] == ["Product Designer"]
    assert [v["title"] for v in dou_all] == ["UX Designer"]
    assert igaming == []


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
    igaming = [_vac(title="iGaming Designer", source="djinni", url="https://x/ig")]
    report = dr.build_report(
        today="2026-09-14",
        cutoff="2026-09-12",
        igaming=igaming,
        djinni=djinni,
        dou=dou,
        errors={},
        sent_at="14-09-2026 21:00",
        totals={"dou": 226, "djinni": 89},
    )
    assert "# Design вакансії — 14-09-2026" in report
    assert "## 🟣 iGaming (1)" in report
    # The per-source headings show kept/scanned from totals.
    assert "## 🟠 Вакансії Djinni (1/89)" in report
    assert "## 🟢 Вакансії DOU (1/226)" in report


def test_build_report_shows_errors_and_empty_sections():
    report = dr.build_report(
        today="2026-09-14",
        cutoff="2026-09-12",
        igaming=[],
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
        igaming=[],
        djinni=[_vac()],
        dou=[],
        sent_at="14-09-2026 21:00",
        totals={"djinni": 1},
    )
    assert len(msgs) == 1
    assert "<b>Design вакансії" in msgs[0]


def test_telegram_splits_and_respects_limit():
    many = [
        _vac(title="Product Designer %d" % i, url="https://example.com/%d" % i)
        for i in range(200)
    ]
    msgs = dr.build_telegram_messages(
        today="2026-09-14",
        cutoff="2026-09-12",
        igaming=[],
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
        igaming=[],
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


# --- iGaming visibility per recipient --------------------------------------

def test_igaming_recipients_unset_is_empty(monkeypatch):
    monkeypatch.delenv("IGAMING_CHAT_IDS", raising=False)
    assert dr.igaming_recipients() == set()


def test_igaming_recipients_parses_list(monkeypatch):
    monkeypatch.setenv("IGAMING_CHAT_IDS", " 172575810 , 42 ")
    assert dr.igaming_recipients() == {"172575810", "42"}


def test_select_messages_full_when_no_restriction():
    # Empty allow-set => everyone gets the full variant.
    assert dr.select_messages("42", set(), "FULL", "REDUCED") == "FULL"


def test_select_messages_full_for_listed_chat():
    assert dr.select_messages("42", {"42"}, "FULL", "REDUCED") == "FULL"


def test_select_messages_reduced_for_other_chat():
    assert dr.select_messages("99", {"42"}, "FULL", "REDUCED") == "REDUCED"


def test_full_report_shows_igaming_block():
    ig = [_vac(source="djinni", title="Designer", company="iGaming Co")]
    dj = [_vac(source="djinni", title="Product Designer")]
    msgs = dr.build_telegram_messages(
        "2026-09-20", "2026-09-18", ig, dj, [], "20-09-2026 10:00", {}
    )
    assert "🟣 iGaming" in "\n".join(msgs)


def test_reduced_report_folds_igaming_into_sources():
    # The reduced variant is built with an empty iGaming list and the full
    # per-source lists, so the block is gone but the vacancy still shows.
    dj_all = [
        _vac(source="djinni", title="Slots Designer", company="iGaming Co"),
        _vac(source="djinni", title="Product Designer"),
    ]
    text = "\n".join(
        dr.build_telegram_messages(
            "2026-09-20", "2026-09-18", [], dj_all, [], "20-09-2026 10:00", {}
        )
    )
    assert "🟣 iGaming" not in text
    assert "Slots Designer" in text
