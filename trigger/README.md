# External cron trigger (Cloudflare Worker)

GitHub Actions' `schedule:` runs on a best-effort basis: it commonly fires tens
of minutes — sometimes hours — late, and under load the run is dropped
altogether. That is why the daily report has been arriving at random times or
not at all.

This directory holds a small Cloudflare Worker with two jobs.

**As the clock**, it replaces GitHub as the scheduler. Cloudflare cron triggers
fire within about a minute of the scheduled time. On each tick the Worker calls
the GitHub REST API to dispatch the existing `daily.yml` workflow (the same
effect as pressing **Run workflow** in the Actions tab). The scraping and
Telegram delivery still run inside GitHub Actions; only the *trigger* moves out.

**As the bot's back end**, it keeps the list of people who subscribed to the
digest. Telegram sends every `/start` and `/stop` to the Worker's webhook, which
records the chat in a KV namespace; the daily report reads the active list and
reports back anyone who blocked the bot. This is optional — without it the
report still goes to whatever `TELEGRAM_CHAT_ID` lists — and it is what makes
the subscriber count meaningful. Chat ids are personal data and live only in KV,
never in this repository. See [Subscription bot](#subscription-bot) below.

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

## Subscription bot

This turns the one-way digest into a bot people can subscribe to. Skip it if you
only push to a fixed chat or channel.

### Welcome digest on `/start`

When a chat sends `/start`, the Worker replies with the confirmation and then
serves the digest straight from KV, which takes about a second.

That digest is kept ready in advance. The report posts its rendered Telegram
messages to `/digest` at the end of every run, and a refresh run (`refresh_only`,
dispatched by the Worker's own cron) re-scrapes and updates the stored copy
without sending anything to anyone. The cron ticks every half hour from 9:00 to
23:30 Kyiv — 30 ticks a day, of which two are the real report (12:00 and 21:00)
and 28 are refreshes — so a new subscriber normally sees vacancies at most half
an hour old.

If a `/start` finds the stored digest older than half an hour anyway — before the
day's first tick, or after a failed refresh — it is still served immediately, and
a refresh is dispatched in the background for whoever writes next. That
background dispatch is capped at one per 15 minutes so a burst of `/start`
messages cannot pile up runs.

Both report variants are stored, because the Worker decides which one a chat may
see: set `IGAMING_CHAT_IDS` on the Worker to the same list the report uses, or
leave it unset and everybody gets the full variant.

If nothing is stored yet — the first deploy, or a cleared namespace — the Worker
falls back to the old path and dispatches `daily.yml` with `only_chat_id`, which
scrapes fresh for that one chat and takes about twenty seconds.

`/start` is rate-limited to **one digest per chat per 10 minutes**; a `/start`
inside that window still gets the confirmation reply but no second digest. See
`ONLY_CHAT_ID`, `REFRESH_ONLY` and `DIGEST_URL` in
[`daily.yml`](../.github/workflows/daily.yml).

### One-time setup

1. **Create the KV namespace** that stores the subscriber list, then paste the
   id it prints into `wrangler.toml` under the `SUBSCRIBERS` binding:

   ```bash
   cd trigger
   wrangler kv namespace create SUBSCRIBERS
   ```

2. **Store the bot secrets** (in addition to `GH_TOKEN` from the cron setup):

   ```bash
   wrangler secret put TELEGRAM_BOT_TOKEN  # the @BotFather token
   wrangler secret put WEBHOOK_SECRET      # any long random string
   wrangler secret put API_KEY             # read key: any long random string
   wrangler secret put ADMIN_KEY           # admin key: a different random string
   ```

   `WEBHOOK_SECRET` proves an incoming update really came from Telegram. `API_KEY`
   is the **read** key (`/subscribers`, `/stats`); `ADMIN_KEY` is the **admin**
   key that the destructive endpoints (`/broadcast`, `/deactivate`) require, so a
   leak of the widely-used read key cannot spam or wipe subscribers. Until
   `ADMIN_KEY` is set the admin endpoints fall back to accepting `API_KEY`, so you
   can add it later without an outage.

3. **Deploy** so the new routes and binding go live:

   ```bash
   wrangler deploy
   ```

4. **Point Telegram at the webhook.** Tell Telegram to deliver updates to the
   Worker, using the same `WEBHOOK_SECRET` both in the URL path and as the
   `secret_token`:

   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     -d "url=https://job-searcher-trigger.<your-subdomain>.workers.dev/telegram/<WEBHOOK_SECRET>" \
     -d "secret_token=<WEBHOOK_SECRET>"
   ```

   Open a chat with the bot and send `/start`; it should reply and appear in the
   list (step below).

5. **Let the report read the list.** Add repository secrets in GitHub → Settings
   → Secrets and variables → Actions and wire them into the workflow env. The
   report sends the key as a Bearer token, so the URLs stay keyless:

   - `SUBSCRIBERS_URL` → `https://…workers.dev/subscribers`
   - `DEACTIVATE_URL` → `https://…workers.dev/deactivate`
   - `WORKER_API_KEY` → the `API_KEY` value (read)
   - `WORKER_ADMIN_KEY` → the `ADMIN_KEY` value (for `/deactivate`)

   With `SUBSCRIBERS_URL` set, the report sends to every active subscriber (plus
   any static `TELEGRAM_CHAT_ID`); with `DEACTIVATE_URL` set, chats that blocked
   the bot are retired automatically. (A legacy `?key=<API_KEY>` baked into the
   URL still works if you leave the key vars unset, but the Bearer token keeps the
   secret out of request logs.)

### Endpoints

Authenticated routes take the key as `Authorization: Bearer <key>` (preferred) or
a `?key=<key>` query (legacy fallback — avoid, it leaks into logs). **read** routes
accept `API_KEY` or `ADMIN_KEY`; **admin** routes require `ADMIN_KEY`. **public**
routes return only aggregate counts (no personal data), so the dashboard link can
be shared freely.

| Route | Method | Access | Purpose |
| --- | --- | --- | --- |
| `/telegram/<WEBHOOK_SECRET>` | POST | secret path | Telegram webhook: handles `/start` and `/stop`. |
| `/subscribers` | GET | read | Active subscriber chat ids: `{"subscribers": [...]}`. |
| `/stats` | GET | read | Counts: `{"total", "active", "blocked", "stopped"}`. |
| `/deactivate` | POST | admin | Retire ids that blocked the bot: `{"chat_ids": [...]}`. |
| `/broadcast` | POST | admin | Send one message to every active subscriber: `{"text": "..."}`. |
| `/digest` | POST | admin | Store the rendered digest for `/start` to serve: `{"date", "sent_at", "full": [...], "reduced": [...] \| null}`. |
| `/history` | GET | public | Daily count snapshots + live current, for the dashboard. |
| `/dashboard` | GET | public | HTML page charting subscribers over time. |

Check how many people use the bot at any time:

```bash
curl -H "Authorization: Bearer <API_KEY>" \
  "https://job-searcher-trigger.<your-subdomain>.workers.dev/stats"
```

### Broadcast

Send a one-off message (an announcement, or just a liveness ping) to every
active subscriber:

```bash
curl -X POST "https://job-searcher-trigger.<your-subdomain>.workers.dev/broadcast" \
  -H "Authorization: Bearer <ADMIN_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello from the jobs bot!"}'
```

It returns `{"recipients", "sent", "blocked"}` — how many were targeted, how many
received it, and how many had blocked the bot (those are retired automatically,
so the count self-heals). The text is sent as-is (no Markdown/HTML parsing).

`/broadcast` is rate limited to 3 calls per 5 minutes (returns `429` when
exceeded), since each call fans out real Telegram messages. The per-recipient
send itself is not throttled, so keep the audience modest — Telegram caps
broadcasts near 30 messages per second.

The public `/history` and `/dashboard` responses are served from Cloudflare's
edge cache (30s and 300s respectively), so hammering them does not re-run the KV
work on every request. Counts on the dashboard can therefore lag by up to 30s.

### Dashboard

A shareable page charting active/total subscribers over time lives at
[`/dashboard`](https://job-searcher-trigger.evnikmoroz.workers.dev/dashboard). It
reads `/history`, which the Worker fills with one aggregate snapshot per day on
each cron tick, so the chart accrues history from the first run after deploy. No
key is needed and no chat ids are exposed — only counts.

## The Worker is the only scheduler

[`.github/workflows/daily.yml`](../.github/workflows/daily.yml) no longer
declares a `schedule:` — this Worker is the sole timed trigger, so there are no
duplicate runs. The workflow keeps `workflow_dispatch: {}`, which is the entry
point the Worker uses and also lets you run the report manually from the Actions
tab. **Deploy this Worker before relying on the schedule**: with the GitHub cron
gone, nothing fires the report until the Worker is live.
