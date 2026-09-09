# job-searcher

**A twice-daily Telegram digest of fresh Product Design and UI/UX jobs — scraped
from [DOU](https://jobs.dou.ua/vacancies/?category=Design) and
[Djinni](https://djinni.co/jobs/?primary_keyword=Design), delivered on time.**

job-searcher watches two Ukrainian job boards, keeps only the Product Design /
UI/UX vacancies posted in the last three days, and pushes a tidy, grouped digest
to a Telegram chat every morning and evening. The same digest is archived as a
dated Markdown file in [`reports/`](reports/), so there is a permanent,
browsable history. There are no servers to run and nothing to `pip install`: the
scraping runs on GitHub Actions, and the punctual twice-a-day schedule is driven
by a small Cloudflare Worker.

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
> • Graphic Designer/UX Designer — Invictus
> • Senior Graphic Designer/UX Designer — Invictus
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
- **Zero dependencies.** Pure Python standard library — it runs in a clean
  environment with no install step.
- **Archived history.** Every run lands a dated Markdown report through an
  auto-merged pull request, so the archive builds itself.

## Run it locally

```bash
python3 daily_report.py
```

This writes `reports/report-YYYY-MM-DD.md` and prints a one-line summary to
stdout. Telegram delivery is skipped locally — it only sends when the
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables are set.

## How it works

- **`scrapers.py`** fetches and normalizes vacancies from both boards using only
  the Python standard library. DOU is paginated through its `xhr-load` endpoint
  and its Ukrainian "day month" list date is parsed; Djinni is read from the
  `JobPosting` JSON-LD on each results page, including its `datePosted`. Only
  titles matching the Product Design / UI/UX filter are kept.
- **`daily_report.py`** runs the scrapers, keeps the vacancies whose posting date
  falls within the window (`WINDOW_DAYS`, default 3 = today and the two previous
  days), groups them by source, sorts newest first, stamps the generation time in
  Europe/Kyiv, writes the Markdown report, and sends the Telegram message(s). If
  a source fails, the report notes the error; if both fail it exits non-zero so
  no empty report is committed.
- **`trigger/`** is a Cloudflare Worker that acts as the clock. Its cron calls
  the workflow's `workflow_dispatch` entry point on time — see
  [`trigger/README.md`](trigger/README.md) for a one-time deploy.

## Automation

The report runs as a GitHub Actions workflow
([`.github/workflows/daily.yml`](.github/workflows/daily.yml)). It is triggered
twice a day — 07:00 and 18:00 UTC (10:00 and 21:00 Europe/Kyiv in summer) — by
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
notification.

Trigger a run by hand any time from the Actions tab (**Run workflow**) or with:

```bash
gh workflow run "Daily design vacancy report"
```

## Filter

The relevance filter (in `scrapers.py`, `RELEVANT`) matches Product Design and
UI/UX titles: `product design`, `ui/ux`, `ux/ui`, `ux designer`, `ui designer`,
`user experience`, `user interface`. Adjust that regular expression to widen or
narrow the report.
