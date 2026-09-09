// Cloudflare Worker that triggers the daily report workflow on a punctual cron.
//
// GitHub Actions' own `schedule:` is best-effort and routinely fires 30 minutes
// to several hours late (or is dropped entirely under load), which is why the
// report sometimes never arrives. Cloudflare's cron triggers fire within about
// a minute of the scheduled time, so this Worker becomes the reliable clock: on
// each tick it calls the GitHub REST API to dispatch the `daily.yml` workflow,
// exactly as pressing "Run workflow" in the Actions tab would.
//
// Secrets (set with `wrangler secret put`, never committed):
//   GH_TOKEN        fine-grained PAT for eugene-jet/job-searcher with
//                   "Actions: Read and write" permission.
//   TRIGGER_SECRET  optional shared secret; when set, the manual HTTP endpoint
//                   requires `?key=<TRIGGER_SECRET>` so the URL cannot be used
//                   by anyone who stumbles onto it.

const OWNER = "eugene-jet";
const REPO = "job-searcher";
const WORKFLOW = "daily.yml";
const REF = "main";

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

export default {
  // Fired by the crons declared in wrangler.toml. A successful dispatch returns
  // HTTP 204 with an empty body.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  // Manual trigger for testing: open the Worker URL in a browser or curl it.
  // Guarded by TRIGGER_SECRET when that secret is configured.
  async fetch(request, env) {
    const url = new URL(request.url);
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
