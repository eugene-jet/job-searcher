# job-searcher

Daily report of Product Design and UI/UX vacancies from
[DOU](https://jobs.dou.ua/vacancies/?category=Design) and
[Djinni](https://djinni.co/jobs/?primary_keyword=Design).

Each run scrapes both boards, compares the current listing against the
previously stored state, and writes a dated Markdown report to `reports/`
describing what is **new**, what is **still open**, and what has **disappeared**
from the listing since the previous run.

## How it works

- `scrapers.py` — fetches and normalizes vacancies from both boards using only
  the Python standard library (no third-party dependencies, so it runs in a
  clean environment without `pip install`). DOU is paginated through its
  `xhr-load` endpoint; Djinni is read from the `JobPosting` JSON-LD on each
  results page. Only titles matching the Product Design / UI/UX filter are kept.
- `daily_report.py` — loads `state.json`, runs the scrapers, computes the diff,
  writes `reports/report-YYYY-MM-DD.md`, and rewrites `state.json` with the
  current active listing (preserving each vacancy's `first_seen` date).
- `state.json` — the persistent memory of previously seen vacancies. It is
  committed to the repository so that "new vs old" stays meaningful across runs
  in a fresh cloud environment.

If a source fails to load on a given run, its previously seen vacancies are kept
untouched rather than being reported as closed, so a transient network error
does not produce a misleading report.

## Run it locally

```bash
python3 daily_report.py
```

The report is printed to `reports/` and a one-line summary to stdout.

## Automation

A GitHub Actions workflow ([.github/workflows/daily.yml](.github/workflows/daily.yml))
runs `daily_report.py` once a day at 09:00 Europe/Kyiv (cron `0 6 * * *` UTC),
then commits and pushes the new report and updated `state.json` back to this
repository. GitHub-hosted runners have full outbound network access; the
scraping runs there because the Claude Code cloud sandbox blocks outbound
connections to the job boards by organization egress policy.

Trigger a run by hand any time from the Actions tab (**Run workflow**) or with:

```bash
gh workflow run "Daily design vacancy report"
```

To read the latest report, open the newest file in `reports/` on GitHub or pull
the repository locally:

```bash
git pull
```

## Filter

The relevance filter (in `scrapers.py`, `RELEVANT`) matches Product Design and
UI/UX titles: `product design`, `ui/ux`, `ux/ui`, `ux designer`, `ui designer`,
`user experience`, `user interface`. Adjust that regular expression to widen or
narrow the report.
