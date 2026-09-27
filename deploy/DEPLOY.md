# Deploying the bot

The judge calls your endpoints over the public internet for 30-45 minutes
(testing brief §6, §12). It needs `https://<host>/v1/*` reachable, `/healthz`
answering within 30s, and context surviving between calls.

Three constraints shape the host choice:

| Constraint | Source | Consequence |
|---|---|---|
| State must persist across calls | §2.1 | A host that restarts and forgets fails warmup |
| `/healthz` must report all 255 contexts | §4 Phase 1 | Warmup gate; failure = skipped test slot |
| 3 consecutive `/healthz` failures = offline | §10 | A slow cold start costs −10 |
| No payload data may leave the test environment | §11 | **No hosted database** — state stays local |

`store.py` mirrors context and conversation state to a local SQLite file
(`VERA_DB`, default `/tmp/vera_state.db`), so a container restart reloads all 255
contexts instead of failing the gate. Verified locally: hard-kill the process and
`/healthz` still reports `5 / 50 / 200`.

Serverless platforms that scale to zero (Vercel, Netlify, default Cloud Run) are
**unusable** — each request may hit a fresh instance with an empty store.

---

## Option A — Render (recommended: free web service, no card)

Docker Spaces on Hugging Face became paid-only, so the free path there (Static)
cannot run a server. Render's free web service still can.

1. Push this repo to GitHub.
2. render.com → sign up with GitHub → **New +** → **Web Service** → pick the repo.
3. Render detects `Dockerfile` and `render.yaml`. Confirm **Instance Type: Free**.
4. Deploy. The URL is `https://vera-bot.onrender.com` (or whatever name it assigns).
5. Verify:

   ```bash
   BOT_URL=https://<your-service>.onrender.com python verify_deploy.py
   ```

**The one caveat**: free services spin down after ~15 minutes idle and cold start
takes roughly a minute, which exceeds the judge's 30s timeout. During the test
window the judge polls `/healthz` every 60s, so it will not sleep mid-test — the
exposure is the first probe if the service happens to be asleep when the window
opens. Two mitigations, use either:

- Hit `/v1/healthz` yourself a few minutes before the slot to wake it.
- Point a free uptime pinger (UptimeRobot, cron-job.org) at `/v1/healthz` every
  5-10 minutes. It touches only the health endpoint, so no payload data leaves
  the environment and §11 still holds.

## Option A2 — Hugging Face Spaces (NO LONGER FREE)

Docker and Gradio Spaces now require a paid PRO plan; only Static Spaces are
free, and those serve static files with no server process. The assembled folder
at `deploy/hf-space/` is kept in case you ever have a PRO account, but it is not
a free option today.

### (former Option A)

1. Create an account at huggingface.co, then **New Space** → SDK **Docker** →
   visibility **Public** (the judge must reach it) → name it e.g. `vera-bot`.
2. Push the contents of `deploy/hf-space/` (already assembled — Dockerfile,
   `README.md` with the required front-matter, and the 8 runtime modules):

   ```bash
   git clone https://huggingface.co/spaces/<your-username>/vera-bot
   cd vera-bot
   cp /path/to/Magicpin-Vera-AI/deploy/hf-space/* .
   git add -A && git commit -m "Vera bot" && git push
   ```

3. The Space builds (a few minutes), then your URL is:

   ```
   https://<your-username>-vera-bot.hf.space
   ```

4. Verify before submitting:

   ```bash
   export BOT_URL=https://<your-username>-vera-bot.hf.space
   curl -s $BOT_URL/v1/healthz
   curl -s $BOT_URL/v1/metadata
   python verify_deploy.py          # full contract + lifecycle against the live URL
   ```

Free CPU Spaces run continuously and only idle out after an extended quiet
period; during the test the judge polls `/healthz` every 60s, which keeps it warm.
The SQLite mirror covers a restart either way.

## Option B — Render (free web service)

No card, GitHub auto-deploy. Create a **Web Service** from the repo, Environment
**Docker**, Instance Type **Free**. Render injects `$PORT`, which the Dockerfile
already honours.

Caveat worth knowing: free services spin down after ~15 minutes idle and cold
start takes roughly a minute, which exceeds the judge's 30s timeout. During the
test window the 60s `/healthz` polling prevents sleeping, but if the bot is asleep
when the window *opens*, the first few probes can fail. Mitigate by hitting
`/healthz` yourself a few minutes before the slot, or use a free uptime pinger
(it only touches `/healthz`, so no payload data leaves the environment and §11
still holds).

## Option C — Fly.io (most robust, needs a card)

```bash
fly launch --no-deploy        # generates fly.toml from the Dockerfile
```

Then set, in `fly.toml`:

```toml
[http_service]
  auto_stop_machines = false
  min_machines_running = 1
```

That disables sleeping entirely, which removes the cold-start risk altogether.
Free allowance covers one small machine; a card is required for verification.

---

## Before you submit the URL

- [ ] `/v1/healthz` returns 200 with all four scope counts
- [ ] `/v1/metadata` has your name and contact email
- [ ] `verify_deploy.py` passes against the public URL
- [ ] the Space/service is **public**, not private
- [ ] `VERA_USE_LLM` is unset or `0` (no model is installed on the host, and the
      deterministic composer measured better anyway)
