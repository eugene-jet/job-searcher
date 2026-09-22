# job-searcher

**A twice-daily Telegram digest of fresh Product Design and UI/UX jobs — scraped
from [DOU](https://jobs.dou.ua/vacancies/?category=Design) and
[Djinni](https://djinni.co/jobs/?search_type=basic-search&primary_keyword=Product%20Design&primary_keyword=UI%20UX),
delivered on time.**

job-searcher watches two Ukrainian job boards, keeps only the Product Design /
UI/UX vacancies posted in the last three days, and pushes a tidy, grouped digest
to a Telegram chat every morning and evening. The same digest is archived as a
dated Markdown file in [`reports/`](reports/), so there is a permanent,
browsable history. There are no servers to run and nothing to `pip install`: the
scraping runs on GitHub Actions, and the punctual twice-a-day schedule is driven
by a small Cloudflare Worker.

**Subscribe:** open [@JobsbroBot](https://t.me/JobsbroBot) in Telegram and press
*Start*. You get the current digest within a minute, and the twice-daily digest
in your own direct messages from then on.

## Sample output

Each run posts a message like this to Telegram — the same content is saved as a
dated file such as
[`reports/report-2026-09-09.md`](reports/report-2026-09-09.md):

> **Design вакансії 🧑‍💻✨**
> 🕒 **09-09-2026 21:00** (Київ)
> За останні 3 дні (07/09-09-2026)
>
> **🟣 iGaming (1)**
> ⦿ 08-09-2026
> • Senior Product Designer (4069) — Ciklum · Київ, Львів, віддалено [DOU]
>
> **🟠 Djinni (4)**
> ⦿ 08-09-2026
> • UX Designer — Invictus
> • Senior Product Designer — Invictus
>
> **🟢 DOU (9)**
> ⦿ 09-09-2026
> • …

<!-- Prefer a real screenshot? Add a Telegram capture under docs/ and link it here. -->

## Features

- **Two boards, one digest.** DOU and Djinni are scraped, normalized, and merged
  into a single message split into per-source sections, newest first.
- **Only what's relevant.** A title filter keeps Product Design and UI/UX roles
  and drops the rest (see [Filter](#filter)).
- **Only what's fresh.** A rolling three-day window by each board's own posting
  date, so you never re-read yesterday's list.
- **iGaming surfaced.** Vacancies mentioning iGaming — matched across title,
  company, location, and the full job description — are pulled into their own
  block at the top.
- **Punctual delivery.** A Cloudflare Worker cron fires within about a minute of
  the scheduled time; GitHub Actions' own scheduler was dropped because it ran
  best-effort and often fired hours late.
- **Analytics over time.** Each run records the day's scanned vacancy total per
  board — the count the board itself reports — and regenerates an Excel workbook
  with a line chart, committed to the repo (see [Analytics](#analytics)).
- **Almost no dependencies.** The scraper and the report are pure Python standard
  library. The only dependency is `openpyxl`, used solely to write the Excel
  analytics workbook (the standard library cannot produce an `.xlsx` with a
  chart).
- **Archived history.** Every run lands a dated Markdown report through an
  auto-merged pull request, so the archive builds itself.

## Analytics

Alongside the daily digest, every run records the scanned vacancy total each
board carried that day — the raw count the board itself shows, before the
relevance filter (so DOU's whole Design category and Djinni's Product Design +
UI/UX + Graphic Design tag listing) — and keeps a running time series:

- **[`data/vacancy_counts.csv`](data/vacancy_counts.csv)** is the source of
  truth — one row per calendar day with the columns `date`, `dou`, `djinni`. A
  second run on the same day overwrites that day's numbers with the latest; a
  source that failed to scrape is left blank (a gap), not recorded as `0`.
- **[Git activity — the repository's commit graph](https://github.com/eugene-jet/job-searcher/graphs/commit-activity)**
  shows how the history builds up over time as each run lands its report and
  updated counts.
- **[Live chart on Google Sheets](https://docs.google.com/spreadsheets/d/1fcVnr2D4TvZEHOT95skfsjcpe_AlilVbNe3SeTN2Cm0/edit)**
  — a hosted view of the same data. A bound Apps Script pulls the CSV above from
  the repo on a daily trigger and redraws the chart, so the sheet tracks the
  history without a download.

Both files are committed by the same auto-merged pull request as the Markdown
report, so the history builds itself. The workbook's document properties are set
deterministically from the data, so a run whose counts did not change produces no
spurious binary diff.

## Run it locally

```bash
pip install -r requirements.txt
python3 daily_report.py
```

This writes `reports/report-YYYY-MM-DD.md`, updates
`data/vacancy_counts.csv` and `reports/vacancy-analytics.xlsx`, and prints a
short summary to stdout. Telegram delivery is skipped locally — it only sends
when the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables are
set.

## Running the tests

The parsing, report-rendering and analytics helpers are covered by an offline
unit-test suite in [`tests/`](tests/). The tests run against in-memory fixtures
and never touch the network, so they are fast and reproducible:

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

The same suite runs in CI on every push and pull request
([`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

## How it works

- **`scrapers.py`** fetches and normalizes vacancies from both boards using only
  the Python standard library. DOU is paginated through its `xhr-load` endpoint
  and its Ukrainian "day month" list date is parsed; Djinni is read from the
  `JobPosting` JSON-LD on each results page, including its `datePosted`. Only
  titles matching the Product Design / UI/UX filter are kept.
- **`daily_report.py`** runs the scrapers and, for each kept Djinni vacancy, reads
  the vacancy page's "Оновлено" (updated) date and uses it in place of the
  published date when present, so a re-bumped posting resurfaces. It keeps the
  vacancies whose date falls within the window (`WINDOW_DAYS`, default 3 = today
  and the two previous days), groups them by source, sorts newest first, stamps
  the generation time in
  Europe/Kyiv, writes the Markdown report, and sends the Telegram message(s). If
  a source fails, the report notes the error; if both fail it exits non-zero so
  no empty report is committed.
- **`analytics.py`** upserts the day's per-source scanned totals (the raw count
  each board returns before the relevance filter) into
  `data/vacancy_counts.csv` and regenerates `reports/vacancy-analytics.xlsx`
  (the `Counts` sheet plus a line chart) with openpyxl. See
  [Analytics](#analytics).
- **`trigger/`** is a Cloudflare Worker that acts as the clock. Its cron calls
  the workflow's `workflow_dispatch` entry point on time — see
  [`trigger/README.md`](trigger/README.md) for a one-time deploy.

## Automation

The report runs as a GitHub Actions workflow
([`.github/workflows/daily.yml`](.github/workflows/daily.yml)). It is triggered
twice a day — 09:00 and 18:00 UTC (12:00 and 21:00 Europe/Kyiv in summer) — by
the Cloudflare Worker in [`trigger/`](trigger/), which calls the workflow's
`workflow_dispatch` entry point. GitHub Actions' own `schedule:` was removed: it
ran on a best-effort basis and routinely fired tens of minutes to several hours
late (or was dropped under load), so the punctual Worker cron replaced it.

Each run sends the digest to Telegram and, when the report changed, lands the
Markdown file in `reports/` through an auto-merged pull request on a short-lived
branch rather than pushing to `main` directly. GitHub-hosted runners have the
outbound network access the boards need; the scraping runs there because the
Claude Code cloud sandbox blocks those connections by egress policy.

Telegram delivery is enabled by two repository secrets, `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`; without them the script just writes the report and skips the
notification. `TELEGRAM_CHAT_ID` may list several comma-separated destinations
(for example a personal DM alongside a channel).

Optionally the digest can run as a subscription bot: people open
[@JobsbroBot](https://t.me/JobsbroBot) and press `/start`, and each receives the
report in their own DM. Pressing `/start` also sends the **current digest right
away** — the Worker dispatches a one-off, freshly scraped run scoped to just that
chat (rate-limited to one per chat every 10 minutes), so a new subscriber does
not wait for the next scheduled run. Subscribers are stored by the Cloudflare
Worker in [`trigger/`](trigger/), and the report reads the active list (via the
`SUBSCRIBERS_URL` secret) on top of any static `TELEGRAM_CHAT_ID`, retiring
anyone who blocked the bot (via `DEACTIVATE_URL`). This is what makes the user
count meaningful — see [`trigger/README.md`](trigger/README.md) for the setup and
the `/stats` endpoint that reports how many people use the bot.

A shareable **[dashboard](https://job-searcher-trigger.evnikmoroz.workers.dev/dashboard)**
charts active and total subscribers over time. It shows only aggregate counts
(no chat ids), so the link is safe to share.

Trigger a run by hand any time from the Actions tab (**Run workflow**) or with:

```bash
gh workflow run "Daily design vacancy report"
```

## Filter

The relevance filter (in `scrapers.py`, `RELEVANT`) matches Product Design,
UI/UX and Graphic Design titles in English and Ukrainian: `product design`,
`ui/ux`, `ux/ui`, `ux designer`, `ui designer`, `user experience`,
`user interface`, `graphic design` (which also catches `graphic designer`), plus
Ukrainian forms such as `графічний дизайнер`, `продуктовий дизайнер`,
`продакт-дизайнер`, `UX-дизайнер`, `UI-дизайнер` and `дизайнер інтерфейсів`. The
Ukrainian rules anchor on the specialty word, so a bare `дизайнер` is not
matched. Adjust that regular expression to widen or narrow the report.

The digest itself is currently narrower than the scraper. `RELEVANT` is built
from two patterns — `PRODUCT_UI_UX` and the graphic-design one — and
`daily_report._prepare` keeps only the titles matching `PRODUCT_UI_UX`, so
graphic-design vacancies are scraped and counted in the scanned totals but are
not sent out. A title naming both families (`Graphic/UX Designer`) still counts
as UI/UX and is sent. Dropping that filter in `_prepare` restores the
graphic-design roles to the digest.
