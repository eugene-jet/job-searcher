# job-searcher

Daily report of Product Design and UI/UX vacancies from
[DOU](https://jobs.dou.ua/vacancies/?category=Design) and
[Djinni](https://djinni.co/jobs/?primary_keyword=Design).

Each run scrapes both boards and writes a dated Markdown report to `reports/`
listing the vacancies **published within the last three days** (by each board's
own posting date), split into a **Djinni** section and a **DOU** section,
newest first. Vacancies that mention **iGaming** are pulled into a separate
block above the Djinni section — the match looks at the title, company,
location, and the full job description (Djinni descriptions come with the
listing; each DOU vacancy in the window is opened to read its body).

## How it works

- `scrapers.py` — fetches and normalizes vacancies from both boards using only
  the Python standard library (no third-party dependencies, so it runs in a
  clean environment without `pip install`). DOU is paginated through its
  `xhr-load` endpoint and its list date (a Ukrainian "day month" string) is
  parsed; Djinni is read from the `JobPosting` JSON-LD on each results page,
  including its `datePosted`. Only titles matching the Product Design / UI/UX
  filter are kept.
- `daily_report.py` — runs the scrapers, keeps the vacancies whose posting date
  falls within the window (`WINDOW_DAYS`, default 3 = today and the two previous
  days), groups them by source, sorts newest first, and writes
  `reports/report-YYYY-MM-DD.md`.

If a source fails to load, the report notes the error; if both fail the script
exits non-zero so no empty report is committed.

## Run it locally

```bash
python3 daily_report.py
```

The report is printed to `reports/` and a one-line summary to stdout.

## Automation

A GitHub Actions workflow ([.github/workflows/daily.yml](.github/workflows/daily.yml))
runs `daily_report.py` twice a day, at 10:00 and 21:00 Europe/Kyiv (cron
`0 7 * * *` and `0 18 * * *` UTC; the schedule is fixed to UTC, so the local
times shift by an hour across daylight-saving changes). Instead of pushing to
`main` directly, each run opens a pull request with the new report on a
short-lived branch and merges it automatically — `main` is protected so every
change lands through a PR. GitHub-hosted runners have full outbound network
access; the scraping runs there because the Claude Code cloud sandbox blocks
outbound connections to the job boards by organization egress policy.

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
