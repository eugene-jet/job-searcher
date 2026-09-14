"""Offline unit tests for the DOU and Djinni scraper helpers.

Everything here runs against in-memory fixture HTML/JSON — no network — so the
suite is fast and deterministic and can run in the sandboxed CI environment that
cannot reach the boards.
"""

import datetime

import scrapers


# --- relevance filter ------------------------------------------------------

def test_is_relevant_keeps_design_titles():
    kept = [
        "Product Designer",
        "Senior Product Design Lead",
        "UI/UX Designer",
        "UX/UI Designer",
        "UX Designer",
        "UI Designer",
        "User Experience Researcher",
        "User Interface Engineer",
    ]
    for title in kept:
        assert scrapers.is_relevant(title), title


def test_is_relevant_drops_unrelated_titles():
    dropped = ["Backend Engineer", "Graphic Designer", "Motion Designer", ""]
    for title in dropped:
        assert not scrapers.is_relevant(title), title


def test_is_relevant_handles_none():
    assert scrapers.is_relevant(None) is False


# --- _clean ----------------------------------------------------------------

def test_clean_strips_tags_entities_and_whitespace():
    raw = "  <b>Product</b>&nbsp;Design\n  &amp;   Research  "
    assert scrapers._clean(raw) == "Product Design & Research"


# --- _parse_dou_date -------------------------------------------------------

def test_parse_dou_date_basic():
    today = datetime.date(2026, 9, 14)
    assert scrapers._parse_dou_date("4 вересня", today) == "2026-09-04"


def test_parse_dou_date_rolls_back_to_previous_year():
    # A December list date seen in January belongs to the previous year.
    today = datetime.date(2026, 1, 5)
    assert scrapers._parse_dou_date("31 грудня", today) == "2025-12-31"


def test_parse_dou_date_rejects_garbage():
    today = datetime.date(2026, 9, 14)
    for bad in ["", "вчора", "31 foo", "notaday вересня", None]:
        assert scrapers._parse_dou_date(bad, today) is None


# --- DOU list parsing ------------------------------------------------------

DOU_FIXTURE = """
<ul>
  <li class="l-vacancy __hot">
    <div class="date"> 4 вересня </div>
    <a class="vt" href="https://jobs.dou.ua/companies/acme/vacancies/12345/?from=list">
      Senior <b>Product</b> Designer
    </a>
    <a class="company" href="https://jobs.dou.ua/companies/acme/">Acme</a>
    <span class="cities">Київ, віддалено</span>
  </li>
  <li class="l-vacancy">
    <div class="date"> 3 вересня </div>
    <a class="vt" href="https://jobs.dou.ua/companies/globex/vacancies/67890/">
      Backend Engineer
    </a>
    <a class="company" href="https://jobs.dou.ua/companies/globex/">Globex</a>
  </li>
</ul>
"""


def test_parse_dou_html_extracts_fields():
    items = scrapers._parse_dou_html(DOU_FIXTURE, today=datetime.date(2026, 9, 14))
    assert len(items) == 2

    first = items[0]
    assert first["source"] == "dou"
    assert first["id"] == "12345"
    assert first["title"] == "Senior Product Designer"
    assert first["company"] == "Acme"
    # The query string is dropped from the stored URL.
    assert first["url"] == "https://jobs.dou.ua/companies/acme/vacancies/12345/"
    assert first["location"] == "Київ, віддалено"
    assert first["date_posted"] == "2026-09-04"

    # A block without a cities span still parses, with an empty location.
    assert items[1]["id"] == "67890"
    assert items[1]["location"] == ""


# --- Djinni JSON-LD parsing ------------------------------------------------

def _djinni_html(*postings):
    import json

    blocks = "".join(
        '<script type="application/ld+json">%s</script>' % json.dumps(p)
        for p in postings
    )
    return "<html><head>%s</head></html>" % blocks


def test_parse_djinni_ld_extracts_fields_and_remote():
    posting = {
        "@type": "JobPosting",
        "title": "Product Designer",
        "url": "https://djinni.co/jobs/555-product-designer/?utm=x",
        "hiringOrganization": {"name": "Initech"},
        "jobLocationType": "TELECOMMUTE",
        "datePosted": "2026-09-08T11:33:14.49",
        "description": "We build <b>iGaming</b> products.",
    }
    items = scrapers._parse_djinni_ld(_djinni_html(posting))
    assert len(items) == 1
    v = items[0]
    assert v["source"] == "djinni"
    assert v["id"] == "555"
    assert v["title"] == "Product Designer"
    assert v["company"] == "Initech"
    assert v["url"] == "https://djinni.co/jobs/555-product-designer/"
    assert v["location"] == "Remote"
    assert v["date_posted"] == "2026-09-08"
    assert "iGaming" in v["description"]


def test_parse_djinni_ld_non_remote_uses_country():
    posting = {
        "@type": "JobPosting",
        "title": "UX Designer",
        "url": "https://djinni.co/jobs/777/",
        "hiringOrganization": {"name": "Umbrella"},
        "applicantLocationRequirements": {"address": {"addressCountry": "Ukraine"}},
        "datePosted": "2026-09-07T09:00:00",
    }
    v = scrapers._parse_djinni_ld(_djinni_html(posting))[0]
    assert v["location"] == "Ukraine"


def test_parse_djinni_ld_location_requirements_as_list():
    # schema.org permits a list of AdministrativeArea entries; the first one
    # still yields a location rather than being dropped.
    posting = {
        "@type": "JobPosting",
        "title": "UI/UX Designer",
        "url": "https://djinni.co/jobs/888/",
        "applicantLocationRequirements": [
            {"address": {"addressCountry": "Poland"}},
            {"address": {"addressCountry": "Ukraine"}},
        ],
        "datePosted": "2026-09-06T09:00:00",
    }
    v = scrapers._parse_djinni_ld(_djinni_html(posting))[0]
    assert v["location"] == "Poland"


def test_parse_djinni_ld_ignores_non_jobposting_and_bad_dates():
    good = {
        "@type": "JobPosting",
        "title": "UI Designer",
        "url": "https://djinni.co/jobs/999/",
        "datePosted": "not-a-date",
    }
    other = {"@type": "Organization", "name": "Ignore me"}
    items = scrapers._parse_djinni_ld(_djinni_html(good, other))
    assert len(items) == 1
    assert items[0]["date_posted"] is None
