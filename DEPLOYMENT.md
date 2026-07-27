# Swapify — Deployment Guide

How Swapify is deployed, written so that **the running service never depends on a
personal machine**. The backend runs on **Render** (a managed Linux host that
restarts the process for you); the frontend is static files on **Vercel**. Once
set up, closing your laptop, logging out, or a crash have no effect — Render
respawns the worker and `GET /health` keeps reporting a climbing `uptime_seconds`.

```
Vercel (static frontend)  ──HTTPS──▶  Render (FastAPI, gunicorn+uvicorn)  ──▶  SQLite
  swapify-three.vercel.app                 swapify-3.onrender.com
```

Sections:
1. Prerequisites
2. Deploy the backend — Blueprint (recommended)
3. Deploy the backend — manual dashboard config (same result, no `render.yaml`)
4. Environment variables (authoritative list)
5. Data persistence — the one decision you must make
6. Deploy the frontend (Vercel) + CORS
7. Verify the deployment
8. Weekly-digest cron (optional)
9. Redeploy, rollback, logs
10. Keeping it awake — uptime monitoring

---

## 1. Prerequisites

- The repo pushed to **GitHub/GitLab** (Render deploys from a connected repo — this
  is what removes the personal-terminal dependency).
- A **Render** account (free tier is fine to start).
- A **Vercel** account for the frontend.
- Values ready for the secrets in §4 (`SECRET_KEY`, `ADMIN_TOKEN`, and any API keys).

The backend needs **no build tooling of yours** — Render runs
`pip install -r requirements.txt` itself.

---

## 2. Deploy the backend — Blueprint (recommended)

The repo ships **[`render.yaml`](render.yaml)** — a Render Blueprint that *is* the
service definition. Deploying from it means the configuration lives in the repo,
not in someone's memory or a dashboard only they can see.

1. Render Dashboard → **New** → **Blueprint**.
2. Connect the repository. Render reads `render.yaml` and proposes the
   `swapify-backend` web service.
3. Fill in the secret env vars it prompts for (the `sync: false` keys — see §4).
4. **Apply**. Render builds and starts the service, waiting for `/health` to return
   `200` before routing traffic to it.

From then on, **every push to the `main` branch auto-deploys**. To change build
settings, edit `render.yaml` and push — no dashboard clicking.

The service definition (already in `render.yaml`):

| Setting | Value |
|---------|-------|
| Runtime | Python 3.12 |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn --chdir src app:app -k uvicorn.workers.UvicornWorker -w 2 --bind 0.0.0.0:$PORT --timeout 120` |
| Health check path | `/health` |
| Auto-deploy | on push to `main` |

> **Why `--chdir src app:app`?** The backend is `src/app.py` and imports sibling
> modules (`ocr_label_scanner`, `observability`, `category_taxonomy`) by bare name.
> Running from inside `src/` makes those imports resolve — exactly like the local
> `cd src && python app.py` flow. `$PORT` is injected by Render.

---

## 3. Deploy the backend — manual dashboard config

If you'd rather not use the Blueprint, create the service by hand with the **same**
settings (this is the exact config the live service uses):

1. Render Dashboard → **New** → **Web Service** → connect the repo.
2. Set:
   - **Runtime:** Python 3
   - **Region:** nearest your users
   - **Branch:** `main`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:**
     `gunicorn --chdir src app:app -k uvicorn.workers.UvicornWorker -w 2 --bind 0.0.0.0:$PORT --timeout 120`
   - **Health Check Path:** `/health`
   - **Auto-Deploy:** Yes
3. Add the environment variables from §4.
4. Create the service.

---

## 4. Environment variables

Set these in **Render → your service → Environment**. Nothing here is baked into
the image, so rotating a key is just an edit + redeploy. Full annotated list is in
[`.env.example`](.env.example).

**Required in production**

| Variable | Why |
|----------|-----|
| `SECRET_KEY` | JWT signing key. Defaults to an insecure constant — **must** be a strong random value or all auth tokens are forgeable. |
| `ADMIN_TOKEN` | Guards `/admin/*` and `/experiment/logs`. Defaults to a known dev token. |
| `CORS_ORIGINS` | Comma-separated frontend origin(s), e.g. `https://swapify-three.vercel.app`. Without it CORS is `*` (fine for dev, not prod). |

**Recommended**

| Variable | Why |
|----------|-----|
| `SWAPIFY_DB_PATH` | Point the DB at a persistent disk (see §5), e.g. `/var/data/swapify.db`. |
| `OPENROUTER_API_KEY` | Real AI chat answers (else the rule-based fallback). |
| `GEMINI_API_KEY` | AI failover when OpenRouter is rate-limited. |
| `USDA_API_KEY` | Better auto-fill than the shared, rate-limited `DEMO_KEY`. |

**Optional:** `SERPAPI_KEY`, `IFCT_DATA_PATH`, the `SWAPIFY_*` timeout/budget knobs,
the digest delivery vars (`SENDGRID_API_KEY` / `SMTP_*` / `EMAIL_FROM` /
`APP_BASE_URL`), and `SENTRY_DSN`.

> 🔐 **Rotate the committed OpenRouter key.** A live `OPENROUTER_API_KEY` is present
> in the repo's `.env`. Treat it as compromised: revoke it at openrouter.ai, issue a
> new one, set it **only** as a Render env var, and keep real `.env` files out of
> git. See [KNOWN_ISSUES.md](KNOWN_ISSUES.md) → "Secrets".

---

## 5. Data persistence — the one decision you must make

Render's default web-service filesystem is **ephemeral**: it is reset on every
deploy and periodic restart. Because Swapify stores state in a **SQLite file on
disk**, that means, on the free/default setup:

- New user signups, scans, ratings, reviews, shopping lists, uploaded product
  images, and **auto-filled products** written back to `swapify.db` are **lost on
  the next deploy/restart**.
- The seeded 252-product catalogue survives (it's committed in the repo), so the
  app keeps *working* — it just forgets runtime data.

Pick one:

- **(A) Persistent disk (recommended for real use).** Attach a Render **Disk**
  mounted at `/var/data` and set `SWAPIFY_DB_PATH=/var/data/swapify.db` (already in
  `render.yaml`). The DB then survives deploys. Trade-offs: a disk pins the service
  to a **single instance** (no horizontal scaling) and is a **paid** add-on. On
  first boot with an empty disk the schema is created and seeded from `products.csv`
  automatically; to preload the shipped catalogue, copy `swapify.db` onto the disk
  once.
- **(B) Accept ephemerality** (fine for a demo). Leave the default filesystem. The
  catalogue works read-mostly; treat all user/runtime data as disposable. Consider
  moving to a managed Postgres later if persistence matters — see
  [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

---

## 6. Deploy the frontend (Vercel) + CORS

The frontend is the static files in [`static/`](static/) — no build step.

1. Vercel → **New Project** → import the repo.
2. **Root directory:** `static`. Framework preset: **Other** (no build command,
   output = the directory itself).
3. Deploy. Note the assigned origin (e.g. `https://swapify-three.vercel.app`).
4. Point the frontend at the backend: set `BACKEND_OVERRIDE_URL` at the top of
   [`static/script.js`](static/script.js) to your Render URL
   (`https://swapify-3.onrender.com`), commit, redeploy.
5. Allow that origin on the backend: set `CORS_ORIGINS` (§4) to the Vercel origin
   and redeploy the backend.

(Any static host works — GitHub Pages, Netlify, etc. Vercel is what the live site
uses.)

---

## 7. Verify the deployment

```bash
# liveness + uptime (proves it's running independently of your machine)
curl https://swapify-3.onrender.com/health

# a real scan
curl https://swapify-3.onrender.com/product/8901719113345

# the full smoke suite against the live URL (edit the base URL in the script)
./test_live_api.ps1
```

`GET /health` returns `uptime_seconds`; watch it keep climbing after you close your
laptop — that's the "no personal terminal" property, demonstrated.

---

## 8. Weekly-digest cron (optional)

The weekly digest (Feature 3) needs a scheduler and email delivery configured
(`SENDGRID_API_KEY` or the `SMTP_*` vars). Options:

- **Render Cron Job** (paid): the commented block in `render.yaml` runs
  `python cron_weekly_digest.py` on a schedule.
- **External scheduler** (works on the free tier): have any cron/uptime service
  `POST /admin/send-weekly-digests` weekly with the `X-Admin-Token` header.

Without a provider configured, digests are written to a dry-run "outbox" instead of
being sent — safe to leave as-is.

---

## 9. Redeploy, rollback, logs

- **Redeploy:** push to `main` (auto-deploy), or Render → **Manual Deploy**.
- **Rollback:** Render → **Deploys** → pick a previous successful deploy → **Rollback**.
- **Logs:** Render → **Logs** (live tail). App-level logging goes to stdout; if
  `SENTRY_DSN` is set, exceptions also go to Sentry.
- **Config change:** edit env vars or `render.yaml` and redeploy — never SSH in to
  hand-edit anything.

---

## 10. Keeping it awake — uptime monitoring

Render's **free tier spins the instance down after ~15 minutes idle**, and the next
request then pays a ~30–50 s cold start. Fix it with a free external monitor:

1. Create an [UptimeRobot](https://uptimerobot.com) (or similar) HTTP monitor.
2. URL: `https://swapify-3.onrender.com/health`. Interval: **5 minutes**.

This keeps the instance warm **and** doubles as a downtime alert. (A paid Render
instance doesn't sleep and doesn't need this.)
