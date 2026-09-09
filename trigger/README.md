# External cron trigger (Cloudflare Worker)

GitHub Actions' `schedule:` runs on a best-effort basis: it commonly fires tens
of minutes — sometimes hours — late, and under load the run is dropped
altogether. That is why the daily report has been arriving at random times or
not at all.

This directory holds a tiny Cloudflare Worker that replaces GitHub as the clock.
Cloudflare cron triggers fire within about a minute of the scheduled time. On
each tick the Worker calls the GitHub REST API to dispatch the existing
`daily.yml` workflow (the same effect as pressing **Run workflow** in the
Actions tab). The scraping and Telegram delivery still run inside GitHub
Actions; only the *trigger* moves out.

## One-time setup

1. **Create a GitHub token.** In GitHub → Settings → Developer settings →
   Fine-grained tokens, create a token scoped to the `job-searcher` repository
   with **Repository permissions → Actions: Read and write**. Copy it.

2. **Install and log in to Wrangler** (Cloudflare's CLI):

   ```bash
   npm install -g wrangler
   wrangler login
   ```

3. **Store the secrets** (they are encrypted in your Cloudflare account and are
   never written to this repository):

   ```bash
   cd trigger
   wrangler secret put GH_TOKEN        # paste the GitHub token
   wrangler secret put TRIGGER_SECRET  # optional: any random string
   ```

4. **Deploy:**

   ```bash
   wrangler deploy
   ```

   Wrangler prints the Worker URL, e.g.
   `https://job-searcher-trigger.<your-subdomain>.workers.dev`.

## Test it

Trigger a run by hand and confirm the report lands in Telegram:

```bash
curl "https://job-searcher-trigger.<your-subdomain>.workers.dev/?key=<TRIGGER_SECRET>"
```

A `dispatched` response means the workflow was started; check the Actions tab
and Telegram. (If you did not set `TRIGGER_SECRET`, drop the `?key=` part.)

## The Worker is the only scheduler

[`.github/workflows/daily.yml`](../.github/workflows/daily.yml) no longer
declares a `schedule:` — this Worker is the sole timed trigger, so there are no
duplicate runs. The workflow keeps `workflow_dispatch: {}`, which is the entry
point the Worker uses and also lets you run the report manually from the Actions
tab. **Deploy this Worker before relying on the schedule**: with the GitHub cron
gone, nothing fires the report until the Worker is live.
