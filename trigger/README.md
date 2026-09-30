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
digest. Telegram sends every `/start`, `/pause` and `/stop` to the Worker's
webhook, which records the chat in a KV namespace; the daily report reads the
active list and
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
23:30 Kyiv — 30 ticks a day, of which two are the real report (11:00 and 20:00)
and 28 are refreshes — so a new subscriber normally sees vacancies at most half
an hour old. The refresh span is a summer time: the cron runs on UTC, so in
winter the ticks run an hour earlier by the Kyiv clock (8:00 to 22:30), and the
digest page derives its footer from the UTC span to match. The report does not
move: the Worker picks the two report ticks by their Kyiv time, so they land on
11:00 and 20:00 in both seasons (08:00 and 17:00 UTC in summer, 09:00 and 18:00
UTC in winter).

If a `/start` finds the stored digest older than half an hour anyway — before the
day's first tick, or after a failed refresh — it is still served immediately, and
a refresh is dispatched in the background for whoever writes next. That
background dispatch is capped at one per 15 minutes so a burst of `/start`
messages cannot pile up runs.

Both report variants are stored, because the Worker decides which one a chat may
see. It decides with the list of chats allowed the iGaming block that the report
sends along with the digest — the very `IGAMING_CHAT_IDS` its scheduled delivery
reads — so `/start` and the twice-daily digest always agree. An empty list means
no restriction and everybody gets the full variant.

The Worker used to keep its own copy of that list as a secret, entered by hand.
The two copies drifted, and `/start` withheld the block from a chat the scheduled
digest showed it to. A Worker secret named `IGAMING_CHAT_IDS` is now read only as
a fallback for a digest stored before the list travelled with it, and can be
deleted.

If nothing is stored yet — the first deploy, or a cleared namespace — the Worker
falls back to the old path and dispatches `daily.yml` with `only_chat_id`, which
scrapes fresh for that one chat and takes about twenty seconds.

The reply to `/start` depends on where the chat stands: a first subscription
gets a welcome with the delivery times, a chat that is already subscribed is told
so and gets the digest with the time it was collected, and a chat returning after
`/stop` is welcomed back. The delivery times are 11:00 and 20:00 Kyiv all year
round, the same `REPORT_HOURS_KYIV` that picks the report ticks.

`/start` is rate-limited to **one digest per chat per 10 minutes**, counted
from the moment that chat's last digest was sent: a digest at 22:27 permits the
next at 22:37. A `/start` in between gets no second digest; the reply points to
the one already in the chat and says how many minutes remain. Pressing again
while waiting does not push the time back.

A chat returning after `/stop` gets the digest with its welcome back even inside
those ten minutes, since it has just chosen to subscribe again — but only once
per span, and the span restarts from that digest. A second return inside it
still restores the subscription, and the reply explains that it has been
switched off and on several times in a row and when the next digest can come.
However fast someone toggles `/stop` and `/start`, that caps them at two digests
in any ten minutes. Subscribing and unsubscribing themselves are never limited. See `ONLY_CHAT_ID`,
`REFRESH_ONLY` and `DIGEST_URL` in
[`daily.yml`](../.github/workflows/daily.yml).

### Bot replies

Every message the bot sends apart from the digest itself. Parts in *italics* are
filled in when the reply is sent:

| When | Reply | Digest follows |
| --- | --- | --- |
| `/start` — first subscription | Вітаю! Тепер свіжі вакансії Product Design та UI/UX приходитимуть тобі двічі на день, о *11:00* і *20:00*. Перший дайджест одразу нижче. Якщо набридне пиши /stop. | yes |
| `/start` — already subscribed | Ти вже з нами 🙂 Тримай свіжий дайджест, зібраний о *22:10*. Регулярні о *11:00* і *20:00*. | yes |
| `/start` — again within 10 minutes of the last digest | Попередній дайджест вже вище в чаті. Новий можна отримувати раз на 10 хв, тож чекаємо на тебе через *6* хв 🤖 | no |
| `/start` — returning after `/stop` | З поверненням! Підписку відновлено – дайджест знову приходитиме о *11:00* і *20:00*. Ось актуальний. | yes, once per 10 minutes |
| `/start` — returning again within those 10 minutes | Підписку знову відновлено! Схоже, вона кілька разів поспіль вмикалась і вимикалась 😞. Новий дайджест буде за *8* хв, а попередній вище в чаті | no |
| `/start` — on `/pause` | Паузу знято! Дайджест знову приходитиме о *11:00* і *20:00*. Ось актуальний. | yes |
| `/start` — on `/pause`, within 10 minutes of the last digest | Паузу знято – дайджест знову приходитиме о *11:00* і *20:00*. Попередній вище в чаті, а новий можна отримати через *6* хв. | no |
| `/digest` — subscribed | Тримай свіжий дайджест, зібраний о *22:10*. | yes |
| `/digest` — on `/pause` | Тримай свіжий дайджест, зібраний о *22:10*. Регулярні на паузі до *7 жовтня*. | yes |
| `/digest` — within 10 minutes of the last digest | the same reply as `/start` gets then | no |
| `/digest` — not subscribed | Дайджест приходить підписникам. Щоб підписатися й одразу отримати свіжий, надішли /start. | — |
| `/pause` — subscribed | На скільки поставити дайджест на паузу? Підписка залишиться, а дайджести повернуться самі. Buttons: 7 днів · 14 днів · 30 днів · Скасувати | — |
| `/pause` — already on pause | Дайджест на паузі до *7 жовтня*. Можна поставити нову паузу, рахуючи від сьогодні, або зняти її. Buttons: 7 днів · 14 днів · 30 днів · Зняти паузу | — |
| `/pause`, or a button, when not subscribed | Підписки зараз немає, тож і ставити на паузу нічого. Щоб підписатися, надішли /start. | — |
| button: 7, 14 or 30 днів | Готово, дайджест на паузі. Він повернеться *7 жовтня* о *11:00*. Якщо захочеш раніше, надішли /start. | — |
| button: Зняти паузу | Паузу знято, дайджест знову приходитиме о *11:00* і *20:00*. | — |
| button: Скасувати | Гаразд, дайджест приходитиме як і раніше. | — |
| `/feedback` alone | Напиши наступним повідомленням, що варто покращити або що не так. Відгук отримає автор бота. | — |
| `/feedback` with text, or the next message after it | Дякую, відгук надіслано 🙌 | — |
| `/feedback` — more than 3 in an hour | Цей відгук не надіслано: за годину можна надіслати до 3. Спробуй трохи згодом. | — |
| `/stop` | Підписку скасовано, дайджест більше не надходитиме 😭. Якщо щось було не так, розкажи через /feedback. Щоб повернутися, надішли /start. | — |
| any other text — subscribed | Я надсилаю дайджест вакансій Product Design та UI/UX. Команди: /digest – свіжий дайджест, /pause – поставити на паузу, /feedback – написати відгук, /stop – відписатися | — |
| any other text — not subscribed | Я надсилаю дайджест вакансій Product Design та UI/UX. Щоб підписатися, надішли /start. Відгук чи ідею можна надіслати через /feedback. | — |

- **Delivery times** come from `REPORT_HOURS_KYIV` in [`worker.js`](worker.js):
  11:00 and 20:00 Kyiv, the same in summer and in winter.
- **The collection time** is when the stored digest was built; the clause is
  left out if that is unknown.
- **The minutes** are how long remains of the ten counted from the chat's last
  digest, from 1 to 10.
- **The date** is the day the digest comes back after a pause.

A button's reply replaces the `/pause` message it was pressed on, so the buttons
go away with it.

The replies are defined in [`worker.js`](worker.js) — `startReply()` for the
`/start` variants, `digestReply()` for `/digest`, `pausePrompt()` and
`applyPause()` for `/pause` and its buttons, the `FEEDBACK_` constants for
`/feedback`, and `DIGEST_NOT_SUBSCRIBED`, `PAUSE_NOT_SUBSCRIBED`, `STOP_REPLY`,
`HELP_REPLY` and `HELP_REPLY_UNSUBSCRIBED` for the rest.
That file is the source of truth; change a reply there and update this table
with it.

### Pause

`/pause` holds the digest for 7, 14 or 30 days without unsubscribing, for a
holiday or a search that is over for now. The chat picks the length with a
button, and the Worker stores the day the digest comes back as `paused_until`
on the chat's `sub:` record, as a Kyiv calendar date. Until that date
`/subscribers` leaves the chat out, so the report sends it nothing; on that date
it gets both digests as usual. Nothing needs to run for the pause to end.

A paused chat is still a subscriber. It counts as active in `/stats`, on the
dashboard and in the bot's description, and `/stats` also reports how many of the
active chats are paused. It still receives a `/broadcast`, which is kept for
announcements.

The pause ends early when the chat sends `/start`, which serves the digest as
usual, or presses **Зняти паузу**, which `/pause` offers instead of **Скасувати**
while a pause is on. Choosing a length again replaces the pause, counted from
that day. `/stop` unsubscribes whether or not the chat is paused. `/digest`
serves the current digest without touching the pause.

The buttons arrive as `callback_query` updates. Telegram sends those to the
webhook unless `allowed_updates` was narrowed when the webhook was set; see
step 4 of the setup below.

### Feedback

`/feedback` passes a note from any chat, subscribed or not, to the bot's owner.
The note can follow the command (`/feedback додайте фільтр`, also on the next
line), or the command can come alone, and then the chat's next message that is
not a command, within 10 minutes, is taken as the note. The help reply and the
`/stop` reply both mention it, since someone who has just unsubscribed is the one
whose reasons are most worth hearing. Only subscribers see it in the command
menu.

The Worker sends each note to `FEEDBACK_CHAT_ID` as plain text, headed with who
sent it and where their subscription stands:

> 💬 Відгук від @username (Ім'я, id 123456789) · підписка активна
>
> Додайте фільтр по Senior

The status reads *підписка активна*, *на паузі до 7 жовтня*, *підписку
скасовано*, *бот заблоковано* or *без підписки*.

Each note is also stored in KV under `feedback:<time>:<chat_id>` for 90 days, so
one is not lost if it could not be delivered, or arrived before
`FEEDBACK_CHAT_ID` was set. The record holds the sender's username and name,
which are personal data, and that is why it expires. List them with:

```bash
wrangler kv key list --binding SUBSCRIBERS --prefix feedback: --remote
```

A chat may send 3 notes an hour; a note is cut at 1,000 characters. Only text is
accepted: photos, stickers and files are ignored, as they are everywhere else in
the bot.

### Bot description

The description is the text Telegram shows in an empty chat above the **Start**
button, so it is the first thing someone arriving from a shared link reads. The
Worker owns it: on every cron tick it builds the text in `botDescription()` and
calls `setMyDescription` whenever the result differs from the text it last set
(kept in KV under `bot:description`). This overwrites any description entered
by hand in @BotFather, so edit it in [`worker.js`](worker.js) instead.

> Свіжі вакансії Product Design та UI/UX з DOU і Djinni двічі на день, о *11:00*
> і *20:00*, прямо в особисті. Тільки за останні три дні, найновіші зверху.
>
> 📋 Вакансій за останні 7 днів: *38*
> 👥 Уже підписалися: *57*
>
> Натисни Start, і актуальний дайджест прийде одразу. Відписатися можна будь-коли
> командою /stop.

- **The vacancy count** is the number of Product Design and UI/UX vacancies
  dated within the last seven days, counting today, on both boards together.
  The report works it out in `week_count()` in
  [`daily_report.py`](../daily_report.py), with the same title filter and the
  same dates as the digest, Djinni bumps included, and sends it to `/digest`
  as `week_count`. A vacancy posted on both boards counts twice, as it is
  listed twice in the digest. The line is left out when either board failed to
  scrape, since one board alone would understate the week, and when the count
  is zero.
- **The subscriber count** is the number of active subscribers, the same figure the
  dashboard shows as Active. It appears only once there are at least
  `DESCRIPTION_COUNT_MIN` (30) of them; below that the line is left out,
  because a count of a handful would put people off rather than draw them in.
  The count comes from the snapshot the tick records anyway, so it follows the
  half-hourly cron and stands still overnight, when the cron does not run.
- **The delivery times** come from the same `REPORT_HOURS_KYIV` as the `/start`
  replies, 11:00 and 20:00 Kyiv all year round.

### Bot short description

The short description is the Info text on the bot's profile page, and Telegram
also sends it along with the link when someone shares the bot. It is capped at
120 characters, so it says only what the bot sends and how to stop it. The
Worker sets it the same way as the description: `botShortDescription()` builds
the text, and the cron tick calls `setMyShortDescription` whenever it differs
from the text last set (kept in KV under `bot:short_description`). Edit it in
[`worker.js`](worker.js), not in @BotFather.

> Привіт! Щодня о *11:00* і *20:00* надсилаю свіжі вакансії Product Design та
> UI/UX з DOU і Djinni. Набридне, пиши /stop

### Bot commands

The commands are what Telegram lists behind the **Menu** button beside the
message field and suggests when someone types `/`. A chat sees one of two lists,
depending on whether it is subscribed. Edit both in [`worker.js`](worker.js).

Everybody who is not subscribed — which in practice means someone who has sent
`/stop`, because a chat that has never pressed **Start** shows that button
instead of the menu — sees the bot's own list, `BOT_COMMANDS`. The Worker sets it
with `setMyCommands` on the same tick and under the same rule as the description
(kept in KV under `bot:commands`):

| Command  | Description                              |
| -------- | ---------------------------------------- |
| `/start` | Підписатися й отримати свіжий дайджест   |

A subscribed chat is given its own list, `SUBSCRIBER_COMMANDS`, which Telegram
shows in that chat instead:

| Command     | Description                    |
| ----------- | ------------------------------ |
| `/digest`   | Свіжий дайджест зараз          |
| `/pause`    | Поставити дайджест на паузу    |
| `/feedback` | Написати відгук                |
| `/stop`     | Відписатися від дайджесту      |

`/digest` sends the current digest, under the same ten-minute limit as `/start`,
but leaves the subscription as it is. It is the Telegram command, not the
`/digest` endpoint the report posts to. `/start` keeps working for subscribers
too; it only leaves their menu, where "subscribe" would read oddly.

The Worker sets a chat's own list with `setMyCommands` and a `chat` scope on every
`/start`, and removes it with `deleteMyCommands` on `/stop`, which brings back the
bot's list. What Telegram last accepted is kept on the chat's `sub:` record as
`menu`, holding the `MENU_VERSION` the chat got. On each cron tick, while
counting subscribers, the Worker picks out the chats whose `menu` does not match
where they stand and puts up to 20 of them right: a call that failed, a chat
that subscribed before the menu existed, or a new `MENU_VERSION`. Raise
`MENU_VERSION` whenever `SUBSCRIBER_COMMANDS` changes, and the tick rolls the new
list out to every subscriber, 20 chats per tick. Blocked chats are skipped.

Telegram clients may keep showing the old list until the chat is reopened.

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
   wrangler secret put FEEDBACK_CHAT_ID    # optional: your own chat id, for /feedback
   ```

   `WEBHOOK_SECRET` proves an incoming update really came from Telegram. `API_KEY`
   is the **read** key (`/subscribers`, `/stats`); `ADMIN_KEY` is the **admin**
   key that the destructive endpoints (`/broadcast`, `/deactivate`) require, so a
   leak of the widely-used read key cannot spam or wipe subscribers. Until
   `ADMIN_KEY` is set the admin endpoints fall back to accepting `API_KEY`, so you
   can add it later without an outage.

   `FEEDBACK_CHAT_ID` is the chat that receives `/feedback` notes, normally your
   own private chat with the bot. Its id is the one in the report's
   `TELEGRAM_CHAT_ID` secret if you send the digest to yourself, or the key of
   your own `sub:` record once you have pressed `/start`. Without it, notes are
   only stored in KV.

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

   The command above leaves `allowed_updates` at Telegram's default, which
   includes the button presses `/pause` relies on. If the webhook was ever set
   with a narrower `allowed_updates`, the buttons stop responding; check with
   `getWebhookInfo`, and set the webhook again without it if `callback_query` is
   missing from the list it reports.

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
| `/telegram/<WEBHOOK_SECRET>` | POST | secret path | Telegram webhook: handles `/start`, `/digest`, `/pause`, `/feedback`, `/stop` and the `/pause` buttons. |
| `/subscribers` | GET | read | Chat ids to send the digest to — active subscribers not on pause: `{"subscribers": [...]}`. |
| `/stats` | GET | read | Counts: `{"total", "active", "blocked", "stopped", "paused"}`; `paused` is part of `active`. |
| `/deactivate` | POST | admin | Retire ids that blocked the bot: `{"chat_ids": [...]}`. |
| `/broadcast` | POST | admin | Send one message to every active subscriber: `{"text": "..."}`. |
| `/digest` | POST | admin | Store the rendered digest for `/start` to serve: `{"date", "sent_at", "full": [...], "reduced": [...] \| null, "week_count": n \| null}`. |
| `/latest` | GET | public | The stored digest as a web page — what `/start` sends. Reduced variant; the admin key shows the full one. |
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

### Digest page

The digest the bot serves on `/start` is also readable in a browser at
[`/latest`](https://job-searcher-trigger.evnikmoroz.workers.dev/latest). It
renders whatever is currently stored, which the report refreshes every half
hour, so the link always shows the same vacancies a new subscriber would get.

Visitors see the reduced variant — the one a chat outside `IGAMING_CHAT_IDS`
receives. Presenting the admin key shows the full one:

```bash
curl -s -H "Authorization: Bearer <ADMIN_KEY>" \
  "https://job-searcher-trigger.<your-subdomain>.workers.dev/latest"
```

No key is needed otherwise, and nothing personal is exposed — the page lists
vacancies, not subscribers.

### Dashboard

A shareable page charting active/total subscribers over time lives at
[`/dashboard`](https://job-searcher-trigger.evnikmoroz.workers.dev/dashboard). It
reads `/history`, which covers every day since the first subscription.

Days from the time the cron started recording come from the aggregate snapshot
the Worker stores once a day. Days before that — and any day the cron missed —
are rebuilt from the dates each subscriber record keeps (`first_seen`,
`stopped_at`, `blocked_at`), and the chart draws them as hollow points so they
are not mistaken for measurements. A chat that stopped and later pressed
`/start` again keeps that stretch in a `gaps` list on its record, so rebuilt
days count it as away for exactly those days. Returns made before `gaps` was
introduced left no trace and count as active throughout; none had happened.

No key is needed and no chat ids are exposed — only counts.

## The Worker is the only scheduler

[`.github/workflows/daily.yml`](../.github/workflows/daily.yml) no longer
declares a `schedule:` — this Worker is the sole timed trigger, so there are no
duplicate runs. The workflow keeps `workflow_dispatch: {}`, which is the entry
point the Worker uses and also lets you run the report manually from the Actions
tab. **Deploy this Worker before relying on the schedule**: with the GitHub cron
gone, nothing fires the report until the Worker is live.
