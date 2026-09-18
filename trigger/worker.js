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
  "Він приходитиме двічі на день. Щоб відписатися — надішли /stop.";
const STOP_REPLY =
  "Ти відписаний — дайджест більше не надходитиме. Щоб повернутися, надішли /start.";
const HELP_REPLY =
  "Я надсилаю дайджест вакансій Product Design та UI/UX. " +
  "Команди: /start — підписатися, /stop — відписатися.";

async function dispatch(env) {
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
      body: JSON.stringify({ ref: REF }),
    },
  );
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function authorized(url, env) {
  return env.API_KEY && url.searchParams.get("key") === env.API_KEY;
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
  return json({ deactivated: ids.length });
}

export default {
  // Fired by the crons declared in wrangler.toml. A successful dispatch returns
  // HTTP 204 with an empty body.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Telegram webhook. The path ends in WEBHOOK_SECRET so only Telegram, which
    // was told this URL, can reach it; handleWebhook also checks the header.
    if (env.WEBHOOK_SECRET && path === `/telegram/${env.WEBHOOK_SECRET}`) {
      return handleWebhook(request, env);
    }

    // JSON endpoints for the daily report, all guarded by API_KEY.
    if (path === "/subscribers") {
      if (!authorized(url, env)) return new Response("forbidden\n", { status: 403 });
      return json(await listSubscribers(env));
    }
    if (path === "/stats") {
      if (!authorized(url, env)) return new Response("forbidden\n", { status: 403 });
      return json(await computeStats(env));
    }
    if (path === "/deactivate") {
      if (!authorized(url, env)) return new Response("forbidden\n", { status: 403 });
      if (request.method !== "POST") return new Response("method not allowed\n", { status: 405 });
      return handleDeactivate(request, env);
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
