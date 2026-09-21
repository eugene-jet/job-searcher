// Cloudflare Worker with two jobs: it is the punctual cron that triggers the
// daily-report workflow, and it is the Telegram bot's back end that keeps the
// list of subscribers.
//
// 1. Cron clock. GitHub Actions' own `schedule:` is best-effort and routinely
//    fires 30 minutes to several hours late (or is dropped entirely under
//    load). Cloudflare's cron triggers fire within about a minute, so on each
//    tick this Worker calls the GitHub REST API to dispatch `daily.yml`,
//    exactly as pressing "Run workflow" in the Actions tab would.
//
// 2. Subscriptions. Telegram delivers bot updates to the webhook endpoint
//    below. `/start` adds the chat to the subscriber list, `/stop` removes it,
//    and the daily report reads the active list (and reports blocked chats back)
//    over the JSON endpoints. Chat ids are personal data and live only in KV,
//    never in the repository.
//
// Secrets (set with `wrangler secret put`, never committed):
//   GH_TOKEN            fine-grained PAT for eugene-jet/job-searcher with
//                       "Actions: Read and write" permission.
//   TELEGRAM_BOT_TOKEN  the bot token from @BotFather, used to reply to /start.
//   WEBHOOK_SECRET      the `secret_token` passed to setWebhook; also the last
//                       path segment of the webhook URL. Rejects any request
//                       that is not Telegram.
//   API_KEY             guards the /subscribers, /deactivate and /stats
//                       endpoints; the daily report passes it as `?key=...`.
//   TRIGGER_SECRET      optional; when set, the manual dispatch endpoint
//                       requires `?key=<TRIGGER_SECRET>`.
//
// Bindings (in wrangler.toml):
//   SUBSCRIBERS         KV namespace holding one `sub:<chat_id>` record each.

const OWNER = "eugene-jet";
const REPO = "job-searcher";
const WORKFLOW = "daily.yml";
const REF = "main";

const START_REPLY =
  "Готово! Ти підписаний на дайджест свіжих вакансій Product Design та UI/UX. " +
  "Він приходитиме двічі на день. Актуальний дайджест надішлю за хвилину. " +
  "Щоб відписатися — надішли /stop.";
const STOP_REPLY =
  "Ти відписаний — дайджест більше не надходитиме. Щоб повернутися, надішли /start.";
const HELP_REPLY =
  "Я надсилаю дайджест вакансій Product Design та UI/UX. " +
  "Команди: /start — підписатися, /stop — відписатися.";

async function dispatch(env, inputs) {
  const body = { ref: REF };
  if (inputs) body.inputs = inputs;
  return fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub rejects API requests without a User-Agent.
        "User-Agent": "job-searcher-cron-trigger",
      },
      body: JSON.stringify(body),
    },
  );
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Read the presented credential from the Authorization: Bearer header, falling
// back to the ?key= query for backwards compatibility. Prefer the header: query
// strings leak into logs, browser history and Referer.
function credential(request, url) {
  const header = request.headers.get("Authorization") || "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  if (match) return match[1];
  return url.searchParams.get("key");
}

// Read access (/subscribers, /stats): the read key or the admin key.
function authorizedRead(request, url, env) {
  const key = credential(request, url);
  if (env.API_KEY && key === env.API_KEY) return true;
  return Boolean(env.ADMIN_KEY) && key === env.ADMIN_KEY;
}

// Admin/destructive access (/broadcast, /deactivate): the admin key only. Until
// ADMIN_KEY is configured it falls back to API_KEY, so deploying this before the
// key is provisioned does not lock the daily report out of /deactivate.
function authorizedAdmin(request, url, env) {
  const key = credential(request, url);
  if (env.ADMIN_KEY) return key === env.ADMIN_KEY;
  return Boolean(env.API_KEY) && key === env.API_KEY;
}

// --- Telegram bot ----------------------------------------------------------

async function reply(env, chatId, text) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        disable_web_page_preview: true,
      }),
    },
  );
}

async function subscribe(env, chatId, from) {
  const key = `sub:${chatId}`;
  const existing = await env.SUBSCRIBERS.get(key, "json");
  const now = new Date().toISOString();
  const record = {
    id: chatId,
    active: true,
    blocked: false,
    first_seen: (existing && existing.first_seen) || now,
    last_start: now,
    username: (from && from.username) || null,
    first_name: (from && from.first_name) || null,
  };
  await env.SUBSCRIBERS.put(key, JSON.stringify(record));
}

// Soft unsubscribe: the record is kept (marked inactive) so the stats still
// reflect that this chat was once a subscriber.
async function unsubscribe(env, chatId) {
  const key = `sub:${chatId}`;
  const existing = await env.SUBSCRIBERS.get(key, "json");
  if (!existing) return;
  existing.active = false;
  existing.stopped_at = new Date().toISOString();
  await env.SUBSCRIBERS.put(key, JSON.stringify(existing));
}

async function handleWebhook(request, env) {
  // Telegram sends the configured secret_token in this header; reject anything
  // that does not carry it, even though the URL already embeds the secret.
  if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
    return new Response("forbidden\n", { status: 403 });
  }
  let update;
  try {
    update = await request.json();
  } catch {
    return new Response("bad request\n", { status: 400 });
  }
  const msg = update.message || update.edited_message;
  if (msg && msg.chat && typeof msg.text === "string") {
    const chatId = String(msg.chat.id);
    const text = msg.text.trim();
    if (text === "/start" || text.startsWith("/start ")) {
      await subscribe(env, chatId, msg.from);
      await reply(env, chatId, START_REPLY);
      // Send a fresh digest scoped to just this chat by dispatching the workflow
      // with only_chat_id. Rate-limited so repeated /start cannot fan out many
      // heavy scrapes (one welcome run per chat per 10 minutes).
      if (!(await rateLimited(env, "welcome:" + chatId, 1, 600))) {
        await dispatch(env, { only_chat_id: chatId });
      }
    } else if (text === "/stop" || text.startsWith("/stop ")) {
      await unsubscribe(env, chatId);
      await reply(env, chatId, STOP_REPLY);
    } else {
      await reply(env, chatId, HELP_REPLY);
    }
  }
  // Telegram only needs a 200 to mark the update delivered.
  return new Response("ok\n", { status: 200 });
}

// Walk the whole `sub:` prefix, calling `onRecord` for each stored record.
async function eachSubscriber(env, onRecord) {
  let cursor;
  do {
    const page = await env.SUBSCRIBERS.list({ prefix: "sub:", cursor });
    for (const entry of page.keys) {
      const record = await env.SUBSCRIBERS.get(entry.name, "json");
      if (record) onRecord(record);
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
}

async function listSubscribers(env) {
  const ids = [];
  await eachSubscriber(env, (record) => {
    if (record.active) ids.push(record.id);
  });
  return { subscribers: ids };
}

async function computeStats(env) {
  const totals = { total: 0, active: 0, blocked: 0, stopped: 0 };
  await eachSubscriber(env, (record) => {
    totals.total += 1;
    if (record.active) totals.active += 1;
    else if (record.blocked) totals.blocked += 1;
    else totals.stopped += 1;
  });
  return totals;
}

// Store one aggregate snapshot per day (keyed by date) so the dashboard can
// chart growth over time. Only counts are stored, never chat ids. Called on the
// cron tick; a second call the same day just overwrites that day's point.
async function recordSnapshot(env) {
  const date = new Date().toISOString().slice(0, 10);
  const counts = await computeStats(env);
  await env.SUBSCRIBERS.put(`stat:${date}`, JSON.stringify({ date, ...counts }));
}

// The stored daily snapshots, oldest first, plus a live "current" reading. A
// point for today is always present so the chart is never empty, even before
// the day's first cron snapshot has been written.
async function history(env) {
  const points = [];
  let cursor;
  do {
    const page = await env.SUBSCRIBERS.list({ prefix: "stat:", cursor });
    for (const entry of page.keys) {
      const record = await env.SUBSCRIBERS.get(entry.name, "json");
      if (record) points.push(record);
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  points.sort((a, b) => (a.date < b.date ? -1 : 1));
  const current = await computeStats(env);
  const today = new Date().toISOString().slice(0, 10);
  if (!points.length || points[points.length - 1].date !== today) {
    points.push({ date: today, ...current });
  }
  return { history: points, current };
}

// Mark chats inactive+blocked so they drop out of future sends. Shared by the
// daily report's /deactivate callback and the broadcast's own retiring of chats
// that blocked the bot.
async function deactivateIds(env, ids) {
  const now = new Date().toISOString();
  for (const id of ids) {
    const key = `sub:${String(id)}`;
    const record = await env.SUBSCRIBERS.get(key, "json");
    if (record) {
      record.active = false;
      record.blocked = true;
      record.blocked_at = now;
      await env.SUBSCRIBERS.put(key, JSON.stringify(record));
    }
  }
  return ids.length;
}

// Retire the chats the daily report could not reach (they blocked the bot or
// the chat was deleted).
async function handleDeactivate(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("bad request\n", { status: 400 });
  }
  const ids = (body && body.chat_ids) || [];
  const deactivated = await deactivateIds(env, ids);
  return json({ deactivated });
}

// Send one message to one chat, classifying the outcome like the daily report
// does: "ok", "blocked" (403/400 — user blocked the bot or the chat is gone),
// or "error" (some other, likely transient failure).
async function sendTo(env, chatId, text) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        disable_web_page_preview: true,
      }),
    },
  );
  if (res.ok) return "ok";
  if (res.status === 403 || res.status === 400) return "blocked";
  return "error";
}

// Send a one-off message to every active subscriber. Retires chats that blocked
// the bot, so it doubles as a liveness check. Small lists only: Telegram caps
// broadcasts near 30 messages/second, which this does not throttle for.
async function handleBroadcast(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    return json({ error: "TELEGRAM_BOT_TOKEN not set" }, 500);
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("bad request\n", { status: 400 });
  }
  const text = body && typeof body.text === "string" ? body.text.trim() : "";
  if (!text) return json({ error: "text required" }, 400);

  const ids = [];
  await eachSubscriber(env, (record) => {
    if (record.active) ids.push(record.id);
  });

  let sent = 0;
  const blocked = [];
  for (const id of ids) {
    const status = await sendTo(env, id, text);
    if (status === "ok") sent += 1;
    else if (status === "blocked") blocked.push(id);
  }
  await deactivateIds(env, blocked);

  return json({ recipients: ids.length, sent, blocked: blocked.length });
}

// Public dashboard page. Reads /history (aggregate counts only, no chat ids)
// and draws the subscriber trend. Served at /dashboard with no key so the link
// can be shared; nothing personal is exposed.
const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobsbroBot — subscribers</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #f6f7f9; --card: #ffffff; --fg: #0f172a; --muted: #64748b;
    --border: #e2e8f0; --accent: #2563eb;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0b1220; --card: #131c2e; --fg: #e5edff; --muted: #93a4c3;
      --border: #24314d; --accent: #5b8cff;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px 16px;
  }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 2px; }
  .sub { color: var(--muted); margin: 0 0 20px; }
  .sub a { color: var(--accent); text-decoration: none; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px;
  }
  .card .n { font-size: 28px; font-weight: 700; }
  .card .l { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card.accent .n { color: var(--accent); }
  .chart-box {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; height: 320px;
  }
  .foot { color: var(--muted); font-size: 12px; margin-top: 16px; text-align: center; }
  @media (max-width: 520px) { .cards { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<div class="wrap">
  <h1>JobsbroBot — subscribers</h1>
  <p class="sub">Live count of people subscribed to the design-jobs digest · <a href="https://t.me/JobsbroBot">t.me/JobsbroBot</a></p>
  <div class="cards">
    <div class="card accent"><div class="n" id="active">–</div><div class="l">Active</div></div>
    <div class="card"><div class="n" id="total">–</div><div class="l">Total</div></div>
    <div class="card"><div class="n" id="stopped">–</div><div class="l">Stopped</div></div>
    <div class="card"><div class="n" id="blocked">–</div><div class="l">Blocked</div></div>
  </div>
  <div class="chart-box"><canvas id="chart"></canvas></div>
  <p class="foot" id="foot">Loading…</p>
</div>
<script>
async function load() {
  try {
    const res = await fetch('/history', { cache: 'no-store' });
    const data = await res.json();
    const hist = data.history || [];
    const cur = data.current || { total: 0, active: 0, blocked: 0, stopped: 0 };
    document.getElementById('active').textContent = cur.active;
    document.getElementById('total').textContent = cur.total;
    document.getElementById('stopped').textContent = cur.stopped;
    document.getElementById('blocked').textContent = cur.blocked;
    document.getElementById('foot').textContent = 'Updated ' + new Date().toLocaleString();
    const labels = hist.map(function (p) { return p.date; });
    const active = hist.map(function (p) { return p.active; });
    const total = hist.map(function (p) { return p.total; });
    const muted = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();
    new Chart(document.getElementById('chart'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          { label: 'Active', data: active, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.15)', fill: true, tension: .3, pointRadius: 3 },
          { label: 'Total', data: total, borderColor: muted || '#94a3b8', borderDash: [4, 4], fill: false, tension: .3, pointRadius: 0 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        plugins: { legend: { labels: { boxWidth: 12 } } }
      }
    });
  } catch (e) {
    document.getElementById('foot').textContent = 'Failed to load stats.';
  }
}
load();
</script>
</body>
</html>`;

// Approximate fixed-window rate limit backed by KV. KV is eventually
// consistent, so the cap is soft — good enough to blunt abuse without a Durable
// Object. Returns true when the caller should be rejected. Uses the SUBSCRIBERS
// namespace with an rl: prefix, which does not collide with sub:/stat: keys.
async function rateLimited(env, bucket, limit, windowSec) {
  const slot = Math.floor(Date.now() / 1000 / windowSec);
  const key = `rl:${bucket}:${slot}`;
  const count = parseInt((await env.SUBSCRIBERS.get(key)) || "0", 10) + 1;
  await env.SUBSCRIBERS.put(key, String(count), { expirationTtl: windowSec * 2 });
  return count > limit;
}

// Serve a response from Cloudflare's edge cache when possible, so repeated hits
// on the public endpoints do not re-run KV work every time. On a miss it
// produces the response, caches it for `ttl` seconds, and returns it.
async function cachedResponse(request, ctx, ttl, produce) {
  const cache = caches.default;
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await produce();
  res.headers.set("Cache-Control", `public, max-age=${ttl}`);
  ctx.waitUntil(cache.put(request, res.clone()));
  return res;
}

export default {
  // Fired by the crons declared in wrangler.toml. A successful dispatch returns
  // HTTP 204 with an empty body. It also records the day's subscriber snapshot.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
    ctx.waitUntil(recordSnapshot(env));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Telegram webhook. The path ends in WEBHOOK_SECRET so only Telegram, which
    // was told this URL, can reach it; handleWebhook also checks the header.
    if (env.WEBHOOK_SECRET && path === `/telegram/${env.WEBHOOK_SECRET}`) {
      return handleWebhook(request, env);
    }

    // Read endpoints: the read key (or the admin key).
    if (path === "/subscribers") {
      if (!authorizedRead(request, url, env)) return new Response("forbidden\n", { status: 403 });
      return json(await listSubscribers(env));
    }
    if (path === "/stats") {
      if (!authorizedRead(request, url, env)) return new Response("forbidden\n", { status: 403 });
      return json(await computeStats(env));
    }
    // Destructive endpoints: the admin key only.
    if (path === "/deactivate") {
      if (!authorizedAdmin(request, url, env)) return new Response("forbidden\n", { status: 403 });
      if (request.method !== "POST") return new Response("method not allowed\n", { status: 405 });
      return handleDeactivate(request, env);
    }
    if (path === "/broadcast") {
      if (!authorizedAdmin(request, url, env)) return new Response("forbidden\n", { status: 403 });
      if (request.method !== "POST") return new Response("method not allowed\n", { status: 405 });
      // A broadcast fans out real Telegram messages, so cap it hard: even with
      // the admin key, no more than 3 per 5 minutes.
      if (await rateLimited(env, "broadcast", 3, 300)) {
        return new Response("rate limited\n", { status: 429 });
      }
      return handleBroadcast(request, env);
    }

    // Public, key-free: aggregate counts only (no chat ids), for the dashboard.
    // Served from the edge cache so hammering them does not re-run KV work.
    if (path === "/history") {
      return cachedResponse(request, ctx, 30, async () => json(await history(env)));
    }
    if (path === "/dashboard") {
      return cachedResponse(
        request,
        ctx,
        300,
        () =>
          new Response(DASHBOARD_HTML, {
            headers: { "Content-Type": "text/html; charset=utf-8" },
          }),
      );
    }

    // Anything else is the manual dispatch trigger, guarded by TRIGGER_SECRET
    // when that secret is configured: open the URL in a browser or curl it.
    if (env.TRIGGER_SECRET && url.searchParams.get("key") !== env.TRIGGER_SECRET) {
      return new Response("forbidden\n", { status: 403 });
    }
    const res = await dispatch(env);
    if (res.ok) {
      return new Response("dispatched\n", { status: 200 });
    }
    return new Response(`failed: ${res.status}\n${await res.text()}`, { status: 502 });
  },
};
