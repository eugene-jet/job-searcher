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
//    `/pause` holds its digest for a few days without removing it,
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
//   FEEDBACK_CHAT_ID    optional; the owner's chat id, where /feedback notes
//                       are sent. Without it they are only kept in KV.
//   OWNER_CHAT_ID       optional; the owner's own Telegram id, the only chat
//                       the /stats command answers. Kept as a secret rather
//                       than in the code, because the repository is public.
//   IGAMING_CHAT_IDS    no longer needed: the report now sends the list with
//                       the stored digest. Read only as a fallback for a
//                       digest stored before that, and safe to delete.
//
// Bindings (in wrangler.toml):
//   SUBSCRIBERS         KV namespace holding one `sub:<chat_id>` record each.

const OWNER = "eugene-jet";
const REPO = "job-searcher";
const WORKFLOW = "daily.yml";
const REF = "main";

// How long after /start sends a chat the stored digest before it will send
// that chat another, counted from the moment the last one went out. Each
// resend spends a share of the bot's sending capacity, which every chat
// shares, so a repeated /start inside this span gets an explanation instead.
const WELCOME_WINDOW_SEC = 600;

// Minutes this chat still has to wait before /start may send it another
// digest; 0 means it may have one now, and in that case the send is recorded
// here. The span starts when the chat's own last digest was sent, so a digest
// at 22:27 permits the next at 22:37. The limiter this replaced used windows
// fixed to the clock, which let 22:29 and 22:30 both through and made "once
// every ten minutes" untrue. Like that limiter it lives in eventually
// consistent KV, so it is soft — enough to stop repeated presses, which is its
// whole job.
//
// `returning` marks a chat coming back after /stop. It has just chosen to
// subscribe again, and the digest is what shows that worked, so it may have one
// inside the span — but only once: the send is recorded as that one-off, and a
// second return inside the new span waits like anyone else. Toggling /stop and
// /start as fast as one likes therefore still yields at most two digests in
// any ten minutes, one ordinary and one for returning.
async function welcomeWait(env, chatId, now = Date.now(), returning = false) {
  const key = `welcome:${chatId}`;
  const { at: last, returned } = parseWelcome(await env.SUBSCRIBERS.get(key));
  const remaining = last + WELCOME_WINDOW_SEC * 1000 - now;
  const oneOff = remaining > 0 && returning && !returned;
  if (remaining > 0 && !oneOff) return Math.max(1, Math.ceil(remaining / 60000));
  // Kept a minute past the span; KV will not expire anything sooner than 60 s.
  await env.SUBSCRIBERS.put(key, JSON.stringify({ at: now, returned: oneOff }), {
    expirationTtl: WELCOME_WINDOW_SEC + 60,
  });
  return 0;
}

// The stored `welcome:` value: when the last digest went out, and whether it
// was the one-off for a returning chat. A bare number is the earlier format,
// written before the one-off existed; it expires within minutes of deploy, but
// reads correctly meanwhile.
function parseWelcome(raw) {
  if (!raw) return { at: 0, returned: false };
  try {
    const v = JSON.parse(raw);
    if (v && typeof v === "object") return { at: Number(v.at) || 0, returned: Boolean(v.returned) };
    return { at: Number(v) || 0, returned: false };
  } catch {
    return { at: 0, returned: false };
  }
}

// The reply to /start, worded for where the chat stands. `state` comes from
// subscribe(): "new" for a first subscription, "active" for a chat that is
// already subscribed, "resumed" for one whose digest was on /pause, and
// "returning" for one that had stopped or blocked the bot.
// `waitMin` is the minutes left before this chat may have another digest (see
// welcomeWait); when it is above zero none follows and the reply has to say so
// — the single reply this replaced promised a digest "in a minute" that then
// never came.
function startReply(state, waitMin, record) {
  const [first, second] = reportTimesKyiv();
  if (waitMin > 0) {
    // "хв" rather than the full word, so the count needs no plural form.
    if (state === "returning") {
      // A returning chat only waits when it already had its one-off digest
      // inside this span, i.e. it has been toggling /stop and /start. The
      // subscription is restored all the same; the reply says so, and why no
      // digest follows. Phrased without gendered verb forms.
      return (
        "Підписку знову відновлено! Схоже, вона кілька разів поспіль вмикалась " +
        `і вимикалась 😞. Новий дайджест буде за ${waitMin} хв, а попередній вище в чаті`
      );
    }
    if (state === "resumed") {
      return (
        `Паузу знято – дайджест знову приходитиме о ${first} і ${second}. ` +
        `Попередній вище в чаті, а новий можна отримати через ${waitMin} хв.`
      );
    }
    return (
      "Попередній дайджест вже вище в чаті. Новий можна отримувати раз на 10 хв, " +
      `тож чекаємо на тебе через ${waitMin} хв 🤖`
    );
  }
  if (state === "new") {
    return (
      "Вітаю! Тепер свіжі вакансії Product Design та UI/UX приходитимуть тобі " +
      `двічі на день, о ${first} і ${second}. Перший дайджест одразу нижче. ` +
      "Якщо набридне пиши /stop."
    );
  }
  if (state === "returning") {
    return (
      `З поверненням! Підписку відновлено – дайджест знову приходитиме о ${first} і ${second}. ` +
      "Ось актуальний."
    );
  }
  if (state === "resumed") {
    return `Паузу знято! Дайджест знову приходитиме о ${first} і ${second}. Ось актуальний.`;
  }
  // sent_at is stored as "dd-mm-yyyy HH:MM" (Kyiv); the clock is its tail.
  const at = record && record.sent_at ? `, зібраний о ${record.sent_at.slice(-5)}` : "";
  return `Ти вже з нами 🙂 Тримай свіжий дайджест${at}. Регулярні о ${first} і ${second}.`;
}

// The reply to /digest. `digest` is the stored digest and `sub` the chat's own
// record; a paused chat is reminded that its regular digests are on hold. A
// chat that has to wait gets the same reply as a repeated /start.
function digestReply(waitMin, digest, sub) {
  if (waitMin > 0) return startReply("active", waitMin, digest);
  const at = digest && digest.sent_at ? `, зібраний о ${digest.sent_at.slice(-5)}` : "";
  const paused = isPaused(sub) ? ` Регулярні на паузі до ${dayMonth(sub.paused_until)}.` : "";
  return `Тримай свіжий дайджест${at}.${paused}`;
}

// "9:00", not "09:00": the hour is written as people write it.
const KYIV_CLOCK = new Intl.DateTimeFormat("uk-UA", {
  timeZone: "Europe/Kyiv",
  hour: "numeric",
  minute: "2-digit",
  hourCycle: "h23",
});

// The report hours as people read them, "11:00" and "20:00". REPORT_HOURS_KYIV
// is already a Kyiv wall clock, so the text is the same all year round.
function reportTimesKyiv() {
  return REPORT_HOURS_KYIV.map((h) => `${h}:00`);
}

// The hour a tick falls on by the Kyiv clock, 0–23. Kyiv is a whole number of
// hours from UTC, so the minute needs no conversion and scheduled() reads it
// straight from the UTC time.
function kyivHour(at) {
  return Number(KYIV_CLOCK.formatToParts(at).find((p) => p.type === "hour").value);
}


const STOP_REPLY =
  "Підписку скасовано, дайджест більше не надходитиме 😭. " +
  "Якщо щось було не так, розкажи через /feedback. Щоб повернутися, надішли /start.";
// The reply to anything the bot does not understand, one for a subscriber and
// one for everybody else, matching the menu each is shown.
const HELP_REPLY =
  "Я надсилаю дайджест вакансій Product Design та UI/UX. " +
  "Команди: /digest – свіжий дайджест, /pause – поставити на паузу, " +
  "/feedback – написати відгук, /stop – відписатися";
const HELP_REPLY_UNSUBSCRIBED =
  "Я надсилаю дайджест вакансій Product Design та UI/UX. " +
  "Щоб підписатися, надішли /start. Відгук чи ідею можна надіслати через /feedback.";
const DIGEST_NOT_SUBSCRIBED =
  "Дайджест приходить підписникам. Щоб підписатися й одразу отримати свіжий, надішли /start.";

// The bot's description: the text Telegram shows in an empty chat above the
// Start button, so it is the first thing someone arriving from a shared link
// reads. The Worker owns it and rewrites it on the cron tick (see
// updateBotProfile), which replaces whatever was set by hand in @BotFather.
//
// It says how many vacancies the last seven days brought, which the report
// counts and sends along with the digest (week_count in daily_report.py). The
// line is left out when that count is missing — a board failed to scrape, or
// the digest was stored by a report that did not send one — or is zero.
//
// Once DESCRIPTION_COUNT_MIN chats are subscribed, the text also says how many.
// Below that the line is left out: a count of a handful reads as "nobody uses
// this" and would put people off rather than draw them in. The figure is the
// active subscribers, the same one the dashboard shows as Active.
const DESCRIPTION_COUNT_MIN = 30;

function botDescription(active, week) {
  const [first, second] = reportTimesKyiv();
  // Each figure follows a colon rather than sitting in a sentence, so it
  // needs no plural form.
  const lines = [];
  if (week > 0) lines.push(`📋 Вакансій за останні 7 днів: ${week}`);
  if (active >= DESCRIPTION_COUNT_MIN) lines.push(`👥 Уже підписалися: ${active}`);
  const counts = lines.length ? lines.join("\n") + "\n\n" : "";
  return (
    "Свіжі вакансії Product Design та UI/UX з DOU і Djinni двічі на день, " +
    `о ${first} і ${second}, прямо в особисті. Тільки за останні три дні, найновіші зверху.\n\n` +
    counts +
    "Натисни Start, і актуальний дайджест прийде одразу. " +
    "Відписатися можна будь-коли командою /stop."
  );
}

// The bot's short description: the Info text on its profile page, also sent
// along with the link when someone shares the bot. Telegram caps it at 120
// characters, so it says only what the bot sends and how to stop it.
function botShortDescription() {
  const [first, second] = reportTimesKyiv();
  return (
    `Привіт! Щодня о ${first} і ${second} надсилаю свіжі вакансії Product Design ` +
    "та UI/UX з DOU і Djinni. Набридне, пиши /stop"
  );
}

// The commands Telegram lists in the Menu button beside the message field and
// suggests when someone types "/". There are two lists. BOT_COMMANDS is the
// bot's own, which every chat sees unless it has one of its own; it offers only
// /start, since nothing else is of use before subscribing. A subscribed chat is
// given SUBSCRIBER_COMMANDS as its own list instead (see setChatMenu), where
// /start, which would read "subscribe", gives way to /digest.
const BOT_COMMANDS = [
  { command: "start", description: "Підписатися й отримати свіжий дайджест" },
];
const SUBSCRIBER_COMMANDS = [
  { command: "digest", description: "Свіжий дайджест зараз" },
  { command: "pause", description: "Поставити дайджест на паузу" },
  { command: "feedback", description: "Написати відгук" },
  { command: "stop", description: "Відписатися від дайджесту" },
];
// The owner's chat, OWNER_CHAT_ID, is given SUBSCRIBER_COMMANDS with /stats
// added at the end. The list is set per chat, so no other chat sees it.
const STATS_COMMAND = { command: "stats", description: "Статистика підписників" };
// Raise by one whenever SUBSCRIBER_COMMANDS or STATS_COMMAND changes. A chat's
// record keeps the version it was last given, and the cron tick resends the
// list to every subscriber whose version differs.
const MENU_VERSION = 2;

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

// Call a Bot API method with a JSON body.
async function telegram(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Send a plain reply. `extra` adds fields to the sendMessage body, such as the
// buttons /pause offers.
async function reply(env, chatId, text, extra = {}) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
    ...extra,
  });
}

// Set the bot's description, short description and command list when they have
// changed: a count moved or crossed the threshold, or the wording or report
// hours were changed in the code. What was last set is kept in KV, so an
// unchanged tick costs four KV reads (the three settings and the stored digest,
// which carries the week's vacancy count) and no call to Telegram. A failed
// call is not recorded, and the next tick tries again.
const DESCRIPTION_KEY = "bot:description";
const SHORT_DESCRIPTION_KEY = "bot:short_description";
const COMMANDS_KEY = "bot:commands";

async function updateBotProfile(env, active) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  const digest = await env.SUBSCRIBERS.get(DIGEST_KEY, "json");
  await setIfChanged(env, "setMyDescription", DESCRIPTION_KEY, {
    description: botDescription(active, digest && digest.week_count),
  });
  await setIfChanged(env, "setMyShortDescription", SHORT_DESCRIPTION_KEY, {
    short_description: botShortDescription(),
  });
  await setIfChanged(env, "setMyCommands", COMMANDS_KEY, { commands: BOT_COMMANDS });
}

// Call a Telegram set* method with `body`, unless the body last sent, kept in
// KV under `key`, is the same.
async function setIfChanged(env, method, key, body) {
  const payload = JSON.stringify(body);
  if ((await env.SUBSCRIBERS.get(key)) === payload) return;
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
    },
  );
  if (res.ok) await env.SUBSCRIBERS.put(key, payload);
}

// Give a chat the menu that fits it: SUBSCRIBER_COMMANDS as its own list once
// it subscribes, and on /stop no list of its own, which brings back the bot's
// BOT_COMMANDS. The chat's record keeps what Telegram last accepted, as `menu`
// (the MENU_VERSION it got, or absent), so the cron tick can find the chats
// whose menu is out of date — a call that failed, a chat that subscribed before
// the menu existed, or a new MENU_VERSION — and put them right.
//
// The owner's chat gets /stats in its list too. A change of OWNER_CHAT_ID alone
// does not make any menu out of date, so after setting it the owner sends
// /start, which sets the menu afresh. On /stop the owner's list is removed like
// anyone's, and /stats then leaves the menu, though it still answers.
async function setChatMenu(env, chatId, subscribed) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  const scope = { type: "chat", chat_id: chatId };
  const commands = isOwnerChat(env, String(chatId))
    ? [...SUBSCRIBER_COMMANDS, STATS_COMMAND]
    : SUBSCRIBER_COMMANDS;
  const res = subscribed
    ? await telegram(env, "setMyCommands", { commands, scope })
    : await telegram(env, "deleteMyCommands", { scope });
  if (!res.ok) return;
  const key = `sub:${chatId}`;
  const record = await env.SUBSCRIBERS.get(key, "json");
  if (!record) return;
  if (subscribed) record.menu = MENU_VERSION;
  else delete record.menu;
  await env.SUBSCRIBERS.put(key, JSON.stringify(record));
}

// A chat whose menu does not match where it stands. A blocked chat is left
// alone: it cannot see the menu, and a /start when it comes back sets it anyway.
function menuOutOfDate(record) {
  if (record.active) return record.menu !== MENU_VERSION;
  return !record.blocked && record.menu !== undefined;
}

// How many chats one cron tick may bring up to date. Each costs one call to
// Telegram, and a tick shares Cloudflare's cap of 50 outgoing requests per
// invocation with the dispatch and the bot's profile. The rest wait for the
// next tick, half an hour later.
const MENU_SYNC_PER_TICK = 20;

async function syncChatMenus(env, records) {
  for (const record of records.slice(0, MENU_SYNC_PER_TICK)) {
    await setChatMenu(env, record.id, record.active);
  }
}

// --- Stored digest ---------------------------------------------------------
//
// The daily report posts its rendered Telegram messages here (see
// publish_digest in daily_report.py) and /start serves them straight from KV.
// Answering a /start by dispatching a workflow run and re-scraping both boards
// took some twenty seconds; reading a KV key takes a moment. The stored value
// holds both report variants, because the Worker, not the report, decides which
// one a given chat may see.

const DIGEST_KEY = "digest:latest";
// How old the stored digest may get before a /start also kicks off a refresh in
// the background. Matches the half-hourly cron, so the debounce fires only when
// a tick was missed or the day's first /start lands before the first tick
// (9:00 Kyiv in summer, 8:00 in winter). The runs
// it starts are capped below.
const DIGEST_MAX_AGE_SEC = 1800;

async function storeDigest(request, env) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response("bad request\n", { status: 400 });
  }
  if (!Array.isArray(payload.full) || payload.full.length === 0) {
    return new Response("digest must carry a non-empty full variant\n", { status: 400 });
  }
  const record = {
    date: payload.date || null,
    sent_at: payload.sent_at || null,
    full: payload.full,
    // Absent when the iGaming block is unrestricted and everybody gets `full`.
    reduced: Array.isArray(payload.reduced) ? payload.reduced : null,
    // The chats allowed the iGaming block, sent by the report from the same
    // list its scheduled delivery uses. Null when an older report sent none.
    igaming_chat_ids: Array.isArray(payload.igaming_chat_ids)
      ? payload.igaming_chat_ids.map(String)
      : null,
    // Vacancies of the last seven days, for the bot's description. Null when
    // the report could not count them or is too old to send the count.
    week_count: Number.isInteger(payload.week_count) ? payload.week_count : null,
    stored_at: new Date().toISOString(),
  };
  await env.SUBSCRIBERS.put(DIGEST_KEY, JSON.stringify(record));
  return json({ stored: true, messages: record.full.length, date: record.date });
}

// Whether a chat may see the iGaming block. The list comes with the stored
// digest, so /start decides exactly as the scheduled delivery did. The Worker
// once kept its own copy in an IGAMING_CHAT_IDS secret, entered by hand; it
// drifted from the report's, and /start withheld the block from a chat the
// scheduled digest showed it to. That secret is now only a fallback for a
// digest stored by an older report that did not send the list. An empty list
// means no restriction.
function igamingAllowed(env, chatId, record) {
  let ids;
  if (record && Array.isArray(record.igaming_chat_ids)) {
    ids = record.igaming_chat_ids;
  } else {
    ids = (env.IGAMING_CHAT_IDS || "").split(",");
  }
  ids = ids.map((s) => String(s).trim()).filter(Boolean);
  return ids.length === 0 || ids.includes(String(chatId));
}

// Serve the stored digest to one chat. Returns false when there is nothing
// stored yet, so the caller can fall back to dispatching a live run.
async function sendStoredDigest(env, chatId, stored) {
  const record = stored !== undefined ? stored : await env.SUBSCRIBERS.get(DIGEST_KEY, "json");
  if (!record || !Array.isArray(record.full) || record.full.length === 0) return false;
  const messages =
    record.reduced && !igamingAllowed(env, chatId, record) ? record.reduced : record.full;
  for (const text of messages) {
    // One failing chunk should not strand the ones after it, and a chat that
    // blocked the bot cannot receive the rest either way.
    if ((await sendTo(env, chatId, text, true)) === "blocked") break;
  }
  return true;
}

// True when the stored digest is missing or older than DIGEST_MAX_AGE_SEC.
async function digestIsStale(env) {
  const record = await env.SUBSCRIBERS.get(DIGEST_KEY, "json");
  if (!record || !record.stored_at) return true;
  const age = (Date.now() - Date.parse(record.stored_at)) / 1000;
  return !Number.isFinite(age) || age > DIGEST_MAX_AGE_SEC;
}

// Serve the stored digest after the reply to /start or /digest.
async function serveDigest(env, ctx, chatId, record) {
  const served = await sendStoredDigest(env, chatId, record);
  if (!served) {
    // Nothing stored yet — the first deploy, or KV cleared. Fall back to
    // the old path: a live run scoped to this chat.
    await dispatch(env, { only_chat_id: chatId });
  } else if (await digestIsStale(env)) {
    // Served, but past its hour. Warm the next one in the background so
    // whoever writes next gets something fresher; this chat is already
    // answered and waits for nothing. Capped in case /start arrives in
    // bursts while a refresh is still running.
    if (!(await rateLimited(env, "refresh", 1, 900))) {
      ctx.waitUntil(dispatch(env, { refresh_only: "true" }));
    }
  }
}

async function subscribe(env, chatId, from) {
  const key = `sub:${chatId}`;
  const existing = await env.SUBSCRIBERS.get(key, "json");
  const now = new Date().toISOString();
  // A chat coming back after /stop or a block closes a gap in its subscription.
  // The record is rebuilt below without stopped_at and blocked_at, so the gap
  // is kept here, in `gaps`, or the dashboard could never show it: rebuilding
  // a past day's count needs to know the chat was away then.
  const gaps = (existing && Array.isArray(existing.gaps) && existing.gaps) || [];
  if (existing && !existing.active) {
    const since = existing.blocked ? existing.blocked_at : existing.stopped_at;
    if (since) gaps.push({ from: since, to: now, blocked: Boolean(existing.blocked) });
  }
  // The record is rebuilt without paused_until as well, so a /start lifts any
  // pause the chat had set.
  const record = {
    id: chatId,
    active: true,
    blocked: false,
    first_seen: (existing && existing.first_seen) || now,
    last_start: now,
    username: (from && from.username) || null,
    first_name: (from && from.first_name) || null,
    gaps,
  };
  await env.SUBSCRIBERS.put(key, JSON.stringify(record));
  // Where the chat stood before this /start, so the reply can match it.
  if (!existing) return "new";
  if (!existing.active) return "returning";
  return isPaused(existing) ? "resumed" : "active";
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

// --- Pause -----------------------------------------------------------------
//
// /pause holds a chat's digest for a few days without unsubscribing it: a
// holiday, or a search that is over for now. The chat stays active, so it
// still counts as a subscriber in the stats and the bot's description, but
// /subscribers leaves it out and the report sends it nothing until the pause
// ends. The pause ends by itself, or earlier with /start or the button /pause
// offers to a paused chat.
//
// The end is kept as a Kyiv calendar date, `paused_until`, rather than as a
// moment. The digest goes out at fixed hours, so the date the digest comes
// back is all the chat needs to know: it gets nothing before that date and
// both digests on it.

// The lengths /pause offers, in days. Each reads "N днів", the right plural
// form for all three; a length such as 3 or 21 would need "дні" or "день".
const PAUSE_DAYS = [7, 14, 30];

const PAUSE_NOT_SUBSCRIBED =
  "Підписки зараз немає, тож і ставити на паузу нічого. Щоб підписатися, надішли /start.";

const KYIV_DATE = new Intl.DateTimeFormat("en-US", {
  timeZone: "Europe/Kyiv",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

// Today's date by the Kyiv clock, as "yyyy-mm-dd".
function kyivToday(now = new Date()) {
  const part = Object.fromEntries(KYIV_DATE.formatToParts(now).map((p) => [p.type, p.value]));
  return `${part.year}-${part.month}-${part.day}`;
}

// The Kyiv date `days` after today, as "yyyy-mm-dd".
function kyivDateAfter(days, now = new Date()) {
  const [y, m, d] = kyivToday(now).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

const DAY_MONTH = new Intl.DateTimeFormat("uk-UA", {
  timeZone: "UTC",
  day: "numeric",
  month: "long",
});

// "7 жовтня" for "2026-10-07".
function dayMonth(date) {
  return DAY_MONTH.format(new Date(`${date}T12:00:00Z`));
}

// Whether a chat's digest is on hold today.
function isPaused(record, now = new Date()) {
  return Boolean(record && record.paused_until) && kyivToday(now) < record.paused_until;
}

// The /pause message and its buttons. A chat already on pause is told when
// the digest comes back, and offered to lift the pause instead of cancelling.
function pausePrompt(record) {
  const lengths = PAUSE_DAYS.map((n) => ({ text: `${n} днів`, callback_data: `pause:${n}` }));
  if (isPaused(record)) {
    return {
      text:
        `Дайджест на паузі до ${dayMonth(record.paused_until)}. ` +
        "Можна поставити нову паузу, рахуючи від сьогодні, або зняти її.",
      keyboard: [lengths, [{ text: "Зняти паузу", callback_data: "pause:off" }]],
    };
  }
  return {
    text:
      "На скільки поставити дайджест на паузу? Підписка залишиться, " +
      "а дайджести повернуться самі.",
    keyboard: [lengths, [{ text: "Скасувати", callback_data: "pause:cancel" }]],
  };
}

async function sendPausePrompt(env, chatId) {
  const record = await env.SUBSCRIBERS.get(`sub:${chatId}`, "json");
  if (!record || !record.active) {
    await reply(env, chatId, PAUSE_NOT_SUBSCRIBED);
    return;
  }
  const prompt = pausePrompt(record);
  await reply(env, chatId, prompt.text, {
    reply_markup: { inline_keyboard: prompt.keyboard },
  });
}

// Carry out a choice from the /pause buttons (a length in days, "off" or
// "cancel") and return the text that replaces the prompt.
async function applyPause(env, chatId, choice) {
  const key = `sub:${chatId}`;
  const record = await env.SUBSCRIBERS.get(key, "json");
  // The chat may have sent /stop since the prompt went out.
  if (!record || !record.active) return PAUSE_NOT_SUBSCRIBED;
  const [first, second] = reportTimesKyiv();
  const days = Number(choice);
  if (PAUSE_DAYS.includes(days)) {
    record.paused_until = kyivDateAfter(days);
    await env.SUBSCRIBERS.put(key, JSON.stringify(record));
    return (
      `Готово, дайджест на паузі. Він повернеться ${dayMonth(record.paused_until)} о ${first}. ` +
      "Якщо захочеш раніше, надішли /start."
    );
  }
  if (choice === "off" && record.paused_until) {
    delete record.paused_until;
    await env.SUBSCRIBERS.put(key, JSON.stringify(record));
    return `Паузу знято, дайджест знову приходитиме о ${first} і ${second}.`;
  }
  return "Гаразд, дайджест приходитиме як і раніше.";
}

// A press on an inline button. Telegram keeps the button spinning until
// answerCallbackQuery, so that goes first whatever the press was. The message
// is then rewritten to say what was done, which also takes its buttons away,
// so an old prompt cannot be pressed again.
async function handleCallback(env, query) {
  if (!env.TELEGRAM_BOT_TOKEN) return;
  await telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
  const message = query.message;
  const data = typeof query.data === "string" ? query.data : "";
  if (!message || !message.chat || !data.startsWith("pause:")) return;
  const chatId = String(message.chat.id);
  const text = await applyPause(env, chatId, data.slice("pause:".length));
  await telegram(env, "editMessageText", {
    chat_id: chatId,
    message_id: message.message_id,
    text,
  });
}

// --- Feedback --------------------------------------------------------------
//
// /feedback passes a note to the bot's owner at FEEDBACK_CHAT_ID. The note can
// follow the command ("/feedback текст"), or the command can come alone, and
// then the chat's next plain message within FEEDBACK_WAIT_SEC is the note.
//
// Each note is also kept in KV under `feedback:` for FEEDBACK_KEEP_SEC, so one
// is not lost when the owner's chat cannot be reached or FEEDBACK_CHAT_ID is
// not set. It carries the sender's username and name, which are personal data,
// hence the expiry.

const FEEDBACK_WAIT_SEC = 600;
const FEEDBACK_WAIT_PREFIX = "feedback_wait:";
const FEEDBACK_KEEP_SEC = 90 * 86400;
// Long enough for a paragraph or two, short enough that a paste of something
// else does not flood the owner's chat.
const FEEDBACK_MAX_CHARS = 1000;
const FEEDBACK_PER_HOUR = 3;

const FEEDBACK_PROMPT =
  "Напиши наступним повідомленням, що варто покращити або що не так. " +
  "Відгук отримає автор бота.";
const FEEDBACK_THANKS = "Дякую, відгук надіслано 🙌";
const FEEDBACK_LIMITED =
  `Цей відгук не надіслано: за годину можна надіслати до ${FEEDBACK_PER_HOUR}. ` +
  "Спробуй трохи згодом.";

async function askFeedback(env, chatId) {
  await env.SUBSCRIBERS.put(`${FEEDBACK_WAIT_PREFIX}${chatId}`, "1", {
    expirationTtl: FEEDBACK_WAIT_SEC,
  });
  await reply(env, chatId, FEEDBACK_PROMPT);
}

// Where the chat stands, for the owner reading its note: a note sent after
// /stop reads differently from one sent by a subscriber.
function subscriptionStatus(record) {
  if (!record) return "без підписки";
  if (record.active) {
    return isPaused(record) ? `на паузі до ${dayMonth(record.paused_until)}` : "підписка активна";
  }
  return record.blocked ? "бот заблоковано" : "підписку скасовано";
}

// "💬 Відгук від @user (Ім'я, id 123) · підписка активна", then the note.
// Sent as plain text, so nothing in the note needs escaping.
function feedbackMessage(entry) {
  const who = [entry.first_name, `id ${entry.chat_id}`].filter(Boolean).join(", ");
  const name = entry.username ? `@${entry.username} (${who})` : who;
  return `💬 Відгук від ${name} · ${entry.status}\n\n${entry.text}`;
}

async function takeFeedback(env, msg, note) {
  const chatId = String(msg.chat.id);
  await env.SUBSCRIBERS.delete(`${FEEDBACK_WAIT_PREFIX}${chatId}`);
  if (await rateLimited(env, `feedback:${chatId}`, FEEDBACK_PER_HOUR, 3600)) {
    await reply(env, chatId, FEEDBACK_LIMITED);
    return;
  }
  const from = msg.from || {};
  const record = await env.SUBSCRIBERS.get(`sub:${chatId}`, "json");
  const entry = {
    at: new Date().toISOString(),
    chat_id: chatId,
    username: from.username || null,
    first_name: from.first_name || null,
    status: subscriptionStatus(record),
    text: note.length > FEEDBACK_MAX_CHARS ? `${note.slice(0, FEEDBACK_MAX_CHARS)}…` : note,
  };
  await env.SUBSCRIBERS.put(`feedback:${entry.at}:${chatId}`, JSON.stringify(entry), {
    expirationTtl: FEEDBACK_KEEP_SEC,
  });
  if (env.FEEDBACK_CHAT_ID && env.TELEGRAM_BOT_TOKEN) {
    await telegram(env, "sendMessage", {
      chat_id: env.FEEDBACK_CHAT_ID,
      text: feedbackMessage(entry),
      disable_web_page_preview: true,
    });
  }
  await reply(env, chatId, FEEDBACK_THANKS);
}

// --- Owner stats -----------------------------------------------------------
//
// /stats gives the bot's owner the subscriber counts in Telegram, without
// opening the dashboard, along with what changed over the last
// STATS_WINDOW_DAYS. It answers only in OWNER_CHAT_ID; any other chat gets
// the help reply, as for a command the bot does not know, and so does every
// chat while OWNER_CHAT_ID is not set. FEEDBACK_CHAT_ID is not used for this:
// it may one day point at a group, and the counts are for the owner alone. The reply carries
// counts only, never chat ids or names. Each call walks the whole sub: prefix,
// one KV read per chat, which is fine for a command only the owner sends.

// The reply reads "N днів", the right plural form for 7; a window such as 3 or
// 21 would need "дні" or "день".
const STATS_WINDOW_DAYS = 7;

function isOwnerChat(env, chatId) {
  return Boolean(env.OWNER_CHAT_ID) && chatId === String(env.OWNER_CHAT_ID).trim();
}

// A chat counts as joined in the window when it first subscribed then; a chat
// that stopped earlier and came back in the window does not. It counts as left
// when it is inactive now and stopped or blocked the bot in the window, the
// same moment computeStats goes by: blocked_at for a blocked chat, stopped_at
// for the rest.
async function ownerStats(env, now = Date.now()) {
  const since = now - STATS_WINDOW_DAYS * 86400000;
  const recent = (iso) => Boolean(iso) && Date.parse(iso) >= since;
  let joined = 0;
  let left = 0;
  const totals = await computeStats(env, (record) => {
    if (recent(record.first_seen)) joined += 1;
    if (!record.active && recent(record.blocked ? record.blocked_at : record.stopped_at)) left += 1;
  });
  // A note's key is feedback:<ISO time>:<chat id>, so the time is read from
  // the key without fetching the note. The time has colons of its own, so it
  // is everything between the prefix and the last colon.
  let feedback = 0;
  let cursor;
  do {
    const page = await env.SUBSCRIBERS.list({ prefix: "feedback:", cursor });
    for (const entry of page.keys) {
      const at = entry.name.slice("feedback:".length, entry.name.lastIndexOf(":"));
      if (recent(at)) feedback += 1;
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return { ...totals, joined, left, feedback };
}

// Each figure follows a colon, so it needs no plural form.
function ownerStatsMessage(s) {
  return (
    "📊 Підписники\n" +
    `Активні: ${s.active} (на паузі: ${s.paused})\n` +
    `Скасували підписку: ${s.stopped}\n` +
    `Заблокували бота: ${s.blocked}\n` +
    `Усього за весь час: ${s.total}\n\n` +
    `За останні ${STATS_WINDOW_DAYS} днів\n` +
    `Нові: ${s.joined}\n` +
    `Пішли: ${s.left}\n` +
    `Відгуки: ${s.feedback}`
  );
}

async function handleWebhook(request, env, ctx) {
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
  if (update.callback_query) {
    await handleCallback(env, update.callback_query);
  }
  const msg = update.message || update.edited_message;
  if (msg && msg.chat && typeof msg.text === "string") {
    const chatId = String(msg.chat.id);
    const text = msg.text.trim();
    if (text === "/start" || text.startsWith("/start ")) {
      const state = await subscribe(env, chatId, msg.from);
      // Sent every time, not only on a first subscription: it costs one call,
      // and it puts right a menu that an earlier call failed to set.
      ctx.waitUntil(setChatMenu(env, chatId, true));
      // Serve the digest the last report or refresh run stored, which arrives
      // in about a second — at most once per chat per WELCOME_WINDOW_SEC, so a
      // repeated /start cannot be used to fan out messages. The wait is worked
      // out before replying, because the reply explains it when it applies.
      //
      // A chat returning after /stop gets its digest even inside the span, once;
      // see welcomeWait for how repeated toggling is kept in check.
      const waitMin = await welcomeWait(env, chatId, Date.now(), state === "returning");
      const record = waitMin ? null : await env.SUBSCRIBERS.get(DIGEST_KEY, "json");
      await reply(env, chatId, startReply(state, waitMin, record));
      if (!waitMin) await serveDigest(env, ctx, chatId, record);
    } else if (text === "/digest" || text.startsWith("/digest ")) {
      // The current digest between the regular ones, under the same ten-minute
      // limit as /start. Unlike /start it leaves the subscription as it is, so
      // a paused chat stays paused.
      const sub = await env.SUBSCRIBERS.get(`sub:${chatId}`, "json");
      if (!sub || !sub.active) {
        await reply(env, chatId, DIGEST_NOT_SUBSCRIBED);
      } else {
        const waitMin = await welcomeWait(env, chatId);
        const record = waitMin ? null : await env.SUBSCRIBERS.get(DIGEST_KEY, "json");
        await reply(env, chatId, digestReply(waitMin, record, sub));
        if (!waitMin) await serveDigest(env, ctx, chatId, record);
      }
    } else if (text === "/stop" || text.startsWith("/stop ")) {
      await unsubscribe(env, chatId);
      ctx.waitUntil(setChatMenu(env, chatId, false));
      await reply(env, chatId, STOP_REPLY);
    } else if (text === "/pause" || text.startsWith("/pause ")) {
      await sendPausePrompt(env, chatId);
    } else if (/^\/feedback(?:\s|$)/.test(text)) {
      // Any whitespace ends the command, not only a space: a note typed
      // straight after it often starts on a new line.
      const note = text.slice("/feedback".length).trim();
      if (note) await takeFeedback(env, msg, note);
      else await askFeedback(env, chatId);
    } else if ((text === "/stats" || text.startsWith("/stats ")) && isOwnerChat(env, chatId)) {
      await reply(env, chatId, ownerStatsMessage(await ownerStats(env)));
    } else if (!text.startsWith("/") && (await env.SUBSCRIBERS.get(`${FEEDBACK_WAIT_PREFIX}${chatId}`))) {
      await takeFeedback(env, msg, text);
    } else {
      const sub = await env.SUBSCRIBERS.get(`sub:${chatId}`, "json");
      await reply(env, chatId, sub && sub.active ? HELP_REPLY : HELP_REPLY_UNSUBSCRIBED);
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

// The chats the report sends the digest to: every active subscriber except
// those on /pause.
async function listSubscribers(env) {
  const ids = [];
  await eachSubscriber(env, (record) => {
    if (record.active && !isPaused(record)) ids.push(record.id);
  });
  return { subscribers: ids };
}

// `paused` is a part of `active`, not a fourth kind: a paused chat is still
// subscribed.
async function computeStats(env, onRecord) {
  const totals = { total: 0, active: 0, blocked: 0, stopped: 0, paused: 0 };
  await eachSubscriber(env, (record) => {
    if (onRecord) onRecord(record);
    totals.total += 1;
    if (record.active) {
      totals.active += 1;
      if (isPaused(record)) totals.paused += 1;
    } else if (record.blocked) {
      totals.blocked += 1;
    } else {
      totals.stopped += 1;
    }
  });
  return totals;
}

// Store one aggregate snapshot per day (keyed by date) so the dashboard can
// chart growth over time. Only counts are stored, never chat ids. Called on the
// cron tick; a second call the same day just overwrites that day's point.
// Returns the counts, so the tick can reuse them for the bot's description.
// `onRecord` sees every subscriber record on the way, so the tick can check
// the menus without walking the list a second time.
async function recordSnapshot(env, onRecord) {
  const date = new Date().toISOString().slice(0, 10);
  const counts = await computeStats(env, onRecord);
  await env.SUBSCRIBERS.put(`stat:${date}`, JSON.stringify({ date, ...counts }));
  return counts;
}

// The stored daily snapshots, oldest first, plus a live "current" reading. A
// point for today is always present so the chart is never empty, even before
// the day's first cron snapshot has been written.
async function history(env) {
  const stored = new Map();
  let cursor;
  do {
    const page = await env.SUBSCRIBERS.list({ prefix: "stat:", cursor });
    for (const entry of page.keys) {
      const record = await env.SUBSCRIBERS.get(entry.name, "json");
      if (record) stored.set(record.date, record);
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);

  const records = [];
  await eachSubscriber(env, (record) => records.push(record));
  const current = await computeStats(env);
  const today = new Date().toISOString().slice(0, 10);

  // The stored snapshots only begin on the day the cron first recorded one,
  // but the subscriber records remember when each chat first subscribed, and
  // when it stopped or was blocked. So the days before the first snapshot —
  // and any day the cron happened to miss — are rebuilt from those dates,
  // back to the first subscription ever. A stored snapshot always wins where
  // one exists, because it was measured on the day.
  //
  // A chat that stopped and later came back keeps that closed gap in `gaps`
  // (see subscribe()), so rebuilt days count it as away for the right stretch.
  // Returns made before `gaps` existed left no trace and count as active
  // throughout. Each rebuilt point is flagged so the chart can draw it
  // differently.
  const firsts = records.map((r) => (r.first_seen || "").slice(0, 10)).filter(Boolean);
  const start = [...firsts, ...stored.keys()].sort()[0] || today;
  const points = [];
  for (let t = Date.parse(start + "T00:00:00Z"); ; t += 86400000) {
    const day = new Date(t).toISOString().slice(0, 10);
    if (day > today) break;
    if (day === today) {
      points.push({ date: today, ...current });
    } else if (stored.has(day)) {
      points.push(stored.get(day));
    } else {
      points.push({ date: day, ...countOn(records, day), reconstructed: true });
    }
  }
  return { history: points, current };
}

// Subscriber counts as they stood at the end of `day` (YYYY-MM-DD), derived
// from the dates kept on each record. Mirrors computeStats: a blocked chat
// counts as blocked even if it had also stopped. A closed gap counts the chat
// as away from the day it left up to, but not including, the day it returned;
// one that left and came back the same day was subscribed at the end of it.
function countOn(records, day) {
  const on = (iso) => Boolean(iso) && iso.slice(0, 10) <= day;
  const totals = { total: 0, active: 0, blocked: 0, stopped: 0 };
  for (const r of records) {
    if (!on(r.first_seen)) continue;
    totals.total += 1;
    const away = (r.gaps || []).find((g) => on(g.from) && g.to.slice(0, 10) > day);
    if (on(r.blocked_at) || (away && away.blocked)) totals.blocked += 1;
    else if (on(r.stopped_at) || away) totals.stopped += 1;
    else totals.active += 1;
  }
  return totals;
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
async function sendTo(env, chatId, text, html = false) {
  const body = {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  };
  // The digest is rendered as Telegram HTML by daily_report.py; without the
  // parse mode its links and bold headings would arrive as literal tags. The
  // plain notices this also sends carry no markup and pass either way.
  if (html) body.parse_mode = "HTML";
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (res.ok) return "ok";
  if (res.status === 403 || res.status === 400) return "blocked";
  return "error";
}

// Send a one-off message to every active subscriber. Retires chats that blocked
// the bot, so it doubles as a liveness check.
//
// Small lists only. Sends are paced to stay under Telegram's bulk rate of about
// 30 a second, but a harder ceiling sits in front of that: every send is a
// fetch, and on Cloudflare's free plan one Worker invocation may make at most 50
// of them. A broadcast to a list much past 40 would fail part-way. The daily
// digest does not go through here — it is sent from GitHub Actions — so this
// limits only the admin broadcast.
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
    // 20 a second: under Telegram's bulk rate, and waiting costs no CPU time.
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (status === "ok") sent += 1;
    else if (status === "blocked") blocked.push(id);
  }
  await deactivateIds(env, blocked);

  return json({ recipients: ids.length, sent, blocked: blocked.length });
}

// Public page showing the digest the bot would send right now, so the same
// thing /start delivers can be opened as a link and shared. It renders whatever
// is in KV, which the report refreshes every half hour, and shows the reduced
// variant — the one a chat outside IGAMING_CHAT_IDS receives — unless the admin
// key is presented, since the full variant is restricted for a reason.
//
// The stored messages are already Telegram HTML (<b>, <a>, <i>), which browsers
// render as-is; only the line breaks need turning into markup. Nothing is
// escaped here on purpose: this text was written by the report through an
// admin-only endpoint, not by a visitor.
function digestPage(record, variant, messages, now = new Date()) {
  // The refresh span as a Kyiv clock for today. The cron is UTC, so the span
  // is 9:00–23:30 in summer and 8:00–22:30 in winter; a fixed "9:00" in the
  // footer would be wrong for half the year.
  const y = now.getUTCFullYear(), m = now.getUTCMonth(), d = now.getUTCDate();
  const [from, to] = TICK_SPAN_UTC.map(([h, min]) =>
    KYIV_CLOCK.format(new Date(Date.UTC(y, m, d, h, min))),
  );
  const body = messages.join("\n\n").split("\n").join("<br>\n");
  const when = record.sent_at ? `Зібрано ${record.sent_at} (Київ)` : "";
  const stored = record.stored_at
    ? ` · оновлено ${new Date(record.stored_at).toISOString().slice(11, 16)} UTC`
    : "";
  return `<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Design вакансії — дайджест</title>
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
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px 16px;
  }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 2px; }
  .sub { color: var(--muted); margin: 0 0 20px; font-size: 13px; }
  .sub a { color: var(--accent); text-decoration: none; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 20px; overflow-wrap: anywhere;
  }
  .card a { color: var(--accent); text-decoration: none; }
  .card a:hover { text-decoration: underline; }
  .foot { color: var(--muted); font-size: 12px; margin-top: 16px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Design вакансії</h1>
  <p class="sub">${when}${stored} · варіант: ${variant} · те саме надсилає <a href="https://t.me/JobsbroBot">@JobsbroBot</a> на /start</p>
  <div class="card">
${body}
  </div>
  <p class="foot">Оновлюється кожні 30 хвилин, з ${from} до ${to} за Києвом.</p>
</div>
</body>
</html>`;
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
    const rebuilt = hist.filter(function (p) { return p.reconstructed; });
    document.getElementById('foot').textContent = 'Updated ' + new Date().toLocaleString() +
      (rebuilt.length
        ? ' · Hollow points (' + rebuilt[0].date + ' to ' + rebuilt[rebuilt.length - 1].date +
          ') are rebuilt from subscription dates; filled points were recorded on the day.'
        : '');
    const labels = hist.map(function (p) { return p.date; });
    const active = hist.map(function (p) { return p.active; });
    const total = hist.map(function (p) { return p.total; });
    const fill = hist.map(function (p) { return p.reconstructed ? 'transparent' : '#2563eb'; });
    const muted = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();
    // Monotone interpolation keeps the curve smooth without overshooting: a
    // plain tension spline swings past its points, which on a head count draws
    // dips and peaks — a subscriber lost, then regained — that never happened.
    new Chart(document.getElementById('chart'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          { label: 'Active', data: active, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.15)', fill: true, tension: .3, cubicInterpolationMode: 'monotone', pointRadius: 3, pointBackgroundColor: fill, pointBorderColor: '#2563eb' },
          { label: 'Total', data: total, borderColor: muted || '#94a3b8', borderDash: [4, 4], fill: false, tension: .3, cubicInterpolationMode: 'monotone', pointRadius: 0 }
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

// The two hours that carry the real report, on the hour exactly, by the Kyiv
// clock rather than UTC. Cloudflare's cron only speaks UTC, so it ticks every
// half hour and scheduled() picks the report ticks by their Kyiv time; that way
// the report stays at 11:00 and 20:00 across daylight saving, which falls on
// 08:00 and 17:00 UTC in summer and on 09:00 and 18:00 UTC in winter. Every
// other tick only refreshes the stored digest.
//
// Any hour here must lie inside TICK_SPAN_UTC in both seasons, or no tick
// lands on it and the report silently never goes out.
const REPORT_HOURS_KYIV = [11, 20];

// The first and last tick of the day in UTC, as [hour, minute] — the span of
// the "0,30 6-20 * * *" cron in wrangler.toml, which is where to change it.
const TICK_SPAN_UTC = [[6, 0], [20, 30]];

export default {
  // Fired by the crons declared in wrangler.toml. A successful dispatch returns
  // HTTP 204 with an empty body. It also records the day's subscriber snapshot
  // and keeps the bot's description, which carries the count, up to date, along
  // with its short description and command list, and the menus of up to
  // MENU_SYNC_PER_TICK chats whose own command list is out of date.
  //
  // Two ticks a day dispatch the full run: scrape, send to every subscriber,
  // commit the report and the analytics row. The other 28 dispatch a refresh,
  // which scrapes and updates the stored digest and does nothing else — no
  // message, no commit, no analytics — so that /start always has something at
  // most half an hour old to serve.
  //
  // The minute is part of the test, not only the hour: ticks land on the hour
  // and on the half hour, and 11:30 is a refresh even though 11:00 is a report.
  async scheduled(event, env, ctx) {
    const at = new Date(event.scheduledTime);
    const isReport = at.getUTCMinutes() === 0 && REPORT_HOURS_KYIV.includes(kyivHour(at));
    const inputs = isReport ? undefined : { refresh_only: "true" };
    ctx.waitUntil(dispatch(env, inputs));
    const stale = [];
    const collect = (record) => {
      if (menuOutOfDate(record)) stale.push(record);
    };
    ctx.waitUntil(
      recordSnapshot(env, collect).then(async (counts) => {
        await updateBotProfile(env, counts.active);
        await syncChatMenus(env, stale);
      }),
    );
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Telegram webhook. The path ends in WEBHOOK_SECRET so only Telegram, which
    // was told this URL, can reach it; handleWebhook also checks the header.
    if (env.WEBHOOK_SECRET && path === `/telegram/${env.WEBHOOK_SECRET}`) {
      return handleWebhook(request, env, ctx);
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
    if (path === "/digest") {
      if (!authorizedAdmin(request, url, env)) return new Response("forbidden\n", { status: 403 });
      if (request.method !== "POST") return new Response("method not allowed\n", { status: 405 });
      return storeDigest(request, env);
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
    // Public, key-free: the digest itself, which carries no personal data — it
    // is the same list of vacancies the bot posts to anyone who subscribes.
    // Visitors get the reduced variant; the admin key unlocks the full one.
    if (path === "/latest") {
      const record = await env.SUBSCRIBERS.get(DIGEST_KEY, "json");
      if (!record || !Array.isArray(record.full) || record.full.length === 0) {
        return new Response("no digest stored yet\n", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      }
      const full = authorizedAdmin(request, url, env);
      const messages = !full && record.reduced ? record.reduced : record.full;
      return new Response(digestPage(record, full ? "повний" : "звичайний", messages), {
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          // Short cache: the digest changes twice an hour at most, and a stale
          // page for a minute is better than a KV read for every visitor.
          "Cache-Control": "public, max-age=60",
        },
      });
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
