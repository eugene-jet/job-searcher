"""Unit tests for the report-rendering and Telegram-formatting helpers."""

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
