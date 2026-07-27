# Swapify — Backend & Web App

Swapify is a packaged-food health scanner. Scan (or type) a product barcode and
it returns a **1–10 health score**, a nutrition breakdown, flagged ingredients,
and healthier alternatives. It's aimed at everyday Indian grocery shopping but
falls back to the global [Open Food Facts](https://world.openfoodfacts.org)
catalogue for anything it doesn't curate itself.

- **Backend:** FastAPI (Python), single app in [`src/app.py`](src/app.py), SQLite database.
- **Frontend:** a static HTML/CSS/JS app in [`static/`](static/) (no build step).
- **Live backend:** https://swapify-3.onrender.com · **live docs:** https://swapify-3.onrender.com/docs
- **Live frontend:** https://swapify-three.vercel.app

> New here? Read this file to get it running, then see **[ARCHITECTURE.md](ARCHITECTURE.md)**
> for how the pieces fit, **[DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)** for the data
> model, **[DEPLOYMENT.md](DEPLOYMENT.md)** to ship it, **[API_DOCS.md](API_DOCS.md)**
> for every endpoint, and **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)** for the honest
> list of what's fragile.

---

## What you need

- **Python 3.11+** (developed on 3.12; the repo has been run on 3.14 too).
- **pip**. A virtualenv is recommended but not required.
- Optional: **Tesseract OCR** (native) only if you want the `/ocr/*` label-scanner
  endpoints — everything else works without it.
- Optional: a free **OpenRouter** API key for real AI chat answers.

No key or external account is required to boot the app and score products.

---

## Quick start (local backend)

From the repository root:

```bash
# 1. (optional) create + activate a virtualenv
python -m venv venv
# Windows:  venv\Scripts\activate      Linux/macOS:  source venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. (optional) configure secrets/keys
cp .env.example .env        # then edit .env — every value is optional locally

# 4. run the server (from the src/ directory)
cd src
python app.py
```

The API is now at **http://127.0.0.1:8000**. Interactive docs are at
**http://127.0.0.1:8000/docs**.

`HOST`, `PORT` and `RELOAD` are read from the environment (defaults
`127.0.0.1:8000`, no reload). To auto-reload while developing, run uvicorn from
the repo root instead:

```bash
uvicorn src.app:app --host 127.0.0.1 --port 8000 --reload
```

The database (`swapify.db`) already ships with the app, seeded with **252
products**. On first boot against an empty DB, the schema is created and the
catalogue is seeded from `products.csv` automatically — you never run migrations
by hand.

### Smoke-test it

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/product/8901719113345      # Parle-G
curl "http://127.0.0.1:8000/search?q=maggi"
```

---

## Running the frontend

The frontend is plain static files — no build step. Point a static server at
`static/`:

```bash
cd static
python -m http.server 5500
# open http://127.0.0.1:5500
```

By default the frontend talks to the **live** backend
(`BACKEND_OVERRIDE_URL = 'https://swapify-3.onrender.com'` at the top of
[`static/script.js`](static/script.js)). To point it at your local backend,
change that one line to `'http://127.0.0.1:8000'` (or set it to `null` to use the
page's own origin). If you run the backend on a different origin than the page,
make sure CORS allows it — locally CORS defaults to `*`, so this just works.

---

## Configuration

All configuration is via environment variables (loaded from `.env` on startup).
See **[.env.example](.env.example)** for the complete annotated list. The ones you
are most likely to touch:

| Variable | Purpose | Default |
|----------|---------|---------|
| `OPENROUTER_API_KEY` | Real AI answers in `/chat` (else a rule-based fallback) | _unset_ |
| `GEMINI_API_KEY` | AI failover when OpenRouter is rate-limited | _unset_ |
| `USDA_API_KEY` | USDA nutrition source for auto-fill | `DEMO_KEY` |
| `SWAPIFY_EXTERNAL_SEARCH` | Merge Open Food Facts into search + categories | `1` (on) |
| `SECRET_KEY` | JWT signing key — **set a strong value in production** | `supersecretkey` |
| `ADMIN_TOKEN` | Guards admin endpoints — **set in production** | `swapify-admin-dev` |
| `CORS_ORIGINS` | Allowed frontend origins in production | `*` |
| `SWAPIFY_DB_PATH` | Database file location | `./swapify.db` |

---

## Testing

```bash
# Scoring-engine spec (pure, no server needed) — 100 assertions
python test_scoring_spec.py

# Full end-to-end HTTP suite (auto-starts the server if needed).
# Uses a local venv python at venv/Scripts/python.exe — see the script header.
bash test_api.sh            # Git Bash / WSL / Linux / macOS
./test_api.ps1              # Windows PowerShell

# Same suite pointed at the live deployment
./test_live_api.ps1
```

> ⚠️ The HTTP suites write to the database they hit (they register a throwaway
> user, record scans, and — via the Open Food Facts fallback — store a couple of
> external products into `products`). Run them against a throwaway copy of
> `swapify.db` (set `SWAPIFY_DB_PATH`) if you don't want that. See
> [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

---

## Database ops tools

The database is the source of truth; `products.csv` only seeds/syncs it.

```bash
python sync_db.py --dry-run     # preview CSV → products reconciliation
python sync_db.py               # apply it
python export_products.py       # regenerate products.csv + static CSV from the DB
python normalize_db_to_100g.py  # one-time: rescale nutrition to a per-100g basis
```

See [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) for the full data model.

---

## Documentation index

| Doc | What's in it |
|-----|--------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How frontend, API, database and the external services fit together |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | Every table, column and relationship |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Render + Vercel setup, made terminal-independent |
| [API_DOCS.md](API_DOCS.md) | Every endpoint, with request/response examples |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | The honest list: what's fragile, what's half-done |
| [ScoringLogic_Swapify.md](ScoringLogic_Swapify.md) | The health-scoring rulebook the engine implements |
