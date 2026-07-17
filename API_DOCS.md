# Swapify Backend Documentation

## Live Deployment

- **Live base URL:** `https://swapify-3.onrender.com`
- **Interactive docs (live):** `https://swapify-3.onrender.com/docs`
- **Health check:** `https://swapify-3.onrender.com/health` → `{"status":"ok", "uptime_seconds": …}`

The backend runs on Render as a **persistent, managed web service** — gunicorn with
two uvicorn workers, supervised by Render's infrastructure. It is not attached to
any terminal: closing the terminal, logging out, or shutting the laptop has no
effect on it, and a crashed worker is respawned automatically. `GET /health`
reports `uptime_seconds`, which is the simplest proof — the number keeps climbing
across your machine being switched off entirely.

**Keeping it awake.** Render's free tier spins an instance down after ~15 minutes
of inactivity, and the next request then pays a ~30-50s cold start. An UptimeRobot
monitor pings `/health` every 5 minutes, which keeps the instance warm and doubles
as a downtime alert. Setup: [`DEPLOYMENT.md`](DEPLOYMENT.md) §10.

All endpoints below are relative to the base URL — use `http://127.0.0.1:8000`
locally or the live URL above. The full step-by-step deployment process
(Render / Railway / PythonAnywhere), environment variables, uptime monitoring, and
the database-first + sync workflow are documented in
[`DEPLOYMENT.md`](DEPLOYMENT.md).

**Database-first & CSV sync:** the API reads product data only from the SQLite
database. `products.csv` is used solely to *seed* a fresh database on first boot
and to *sync* updates via [`sync_db.py`](sync_db.py) (`python sync_db.py`, or
`--dry-run` to preview). No local/developer file paths are hard-coded — the DB and
CSV locations come from env vars (`SWAPIFY_DB_PATH`, `SWAPIFY_CSV_PATH`) and
otherwise resolve relative to the app.

> After deploying, update the three URLs above and share the live base URL with
> the team.

## How to run the backend
1. Open a terminal and navigate to the `server` directory.
   ```bash
   cd C:/Users/Dhruv/Documents/Intership/day_32/server
   ```
2. Install the required dependencies from `requirements.txt`:
   ```bash
   pip install -r requirements.txt
   ```
3. (Optional, for the AI Nutritionist `/chat` endpoint) Configure a free AI API
   key. Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY`, or export it
   in your shell:
   ```bash
   # PowerShell
   $env:OPENROUTER_API_KEY = "your-key-here"
   ```
   Get a free key at https://openrouter.ai/keys (OpenRouter offers free-tier
   models). Optionally also set `GEMINI_API_KEY` (free at
   https://aistudio.google.com/apikey) — Gemini is then used as an automatic
   failover when OpenRouter's free models are rate-limited. If no key is
   configured, `/chat` still works using a deterministic, food-science fallback.
4. Run the backend server from the `server/src` directory:
   ```bash
   cd src
   python app.py
   ```
   Alternatively, run it using `uvicorn` directly from the `server` directory:
   ```bash
   uvicorn src.app:app --host 127.0.0.1 --port 8000 --reload
   ```

## How to test the endpoints
Once the server is running, the API will be available at `http://127.0.0.1:8000`.

### Automated test suite
Two end-to-end smoke tests live in the `server` directory and exercise **every**
endpoint (95 sections, including the ratings, recommendations, share-card, AI
chat, barcode-validation, activity-logging, daily-digest, **weekly
challenges & leaderboard**, **smart-cart shopping-list optimization**,
**community reviews** and the **OCR label scanner** features), then verify the
writes actually persisted to `swapify.db`. They auto-start the server if it isn't
already running.

```bash
# Windows PowerShell
./test_api.ps1

# Git Bash / WSL / Linux / macOS
bash test_api.sh
```

Both print a per-request `HTTP <code>` and a final `Passed / Failed` tally. The
AI `/chat` test reports its `source` (`openrouter`/`gemini` = real AI, `fallback`
= rule-based) so you can confirm the AI key is live.

### Interactive API Documentation
FastAPI automatically generates interactive API documentation. You can access it here:
- **Swagger UI:** `http://127.0.0.1:8000/docs`
- **ReDoc:** `http://127.0.0.1:8000/redoc`

### Authentication APIs

#### Register
- **URL:** `/register`
- **Method:** `POST`
- **Request Body (JSON):**
  - `email` (string)
  - `password` (string)
  - `username` (string)

#### Login
- **URL:** `/login`
- **Method:** `POST`
- **Request Body (JSON):**
  - `email` (string)
  - `password` (string)
- **Response:** Returns a JWT `access_token`.

#### Get Profile
- **URL:** `/profile`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>`
- **Response:** Returns user details.

### 1. Get Product Details API
This endpoint returns all the detailed information (ingredients, nutritional facts) for a given product by its barcode. It also records the scan in the scan history if `device_id` is provided.

The response includes an **`ingredient_flags`** array listing every detected harmful
ingredient together with its **risk level** (`Low` / `Medium` / `High` / `Severe`).
Risk levels are stored in the database (`ingredient_rules.risk_level`) — see
[Ingredient Risk Levels](#ingredient-risk-levels) below.

If the product is not in the local database, it is fetched and scored from
**Open Food Facts** on the fly (the response then also includes `"source": "openfoodfacts"`).

The response always includes an **`image_url`** — the product's contributed image
or the shared placeholder (`/product-images/_placeholder.svg`) when it has none
(see [Product Image Upload](#28-product-image-upload-api)). Generic (non-personalized)
product details are served from a 1-hour cache (see [`PERFORMANCE.md`](PERFORMANCE.md)).

If an `Authorization: Bearer <token>` header is sent and the user has saved dietary
preferences, the `score`, `grade` and `breakdown` are **personalized** and the
response includes a `preferences_applied` object (see
[Personalized Scoring](#personalized-scoring)). Without a token the generic score
is returned.

The barcode is **validated** on the way in (see
[Barcode Validation & Correction](#20-barcode-validation--correction-api)). If it
is malformed, a `barcode_validation` object (with a suggested correction) is added
to the response — including on the `404` body, so a client can retry with the
suggestion. Valid barcodes omit this field.

- **URL:** `/product/{barcode}`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — personalize the score for the logged-in user.
- **Query Parameters:**
  - `device_id` (optional): A unique device identifier to record the scan history.

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/product/8901058005783   # Maggi noodles
```

**Expected JSON Response (200 OK):**
```json
{
  "barcode": "8901058005783",
  "product_name": "Maggi noodles",
  "brand": "Maggi",
  "category": "noodles",
  "serving_size_g": 75.0,
  "sugar_g_per_serving": 1.5,
  "saturated_fat_g_per_serving": 6.8,
  "sodium_mg_per_serving": 750.0,
  "protein_g_per_serving": 6.2,
  "fiber_g_per_serving": 2.5,
  "calories_kcal_per_serving": 288.0,
  "ingredients_text": "Maida (Refined wheat flour), Palm oil, Salt, MSG, TBHQ, Sodium benzoate, Tartrazine",
  "score": 1.0,
  "grade": "F",
  "rule_version": 1,
  "breakdown": { "...": "see Health Scoring Logic section" },
  "ingredient_flags": [
    {"name": "maida", "risk": "High"},
    {"name": "palm oil", "risk": "Medium"},
    {"name": "salt", "risk": "Medium"},
    {"name": "msg", "risk": "Medium"},
    {"name": "tbhq", "risk": "Severe"},
    {"name": "sodium benzoate", "risk": "Medium"},
    {"name": "tartrazine", "risk": "High"}
  ],
  "image_url": "/product-images/_placeholder.svg"
}
```

**Expected Error Response (404 Not Found):**
```json
{
  "error": "Product not found"
}
```

### 2. Get Product Health Score API
This endpoint returns only the health score and the corresponding grade for a given product by its barcode.

- **URL:** `/score/{barcode}`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/score/8901491101837
```

**Expected JSON Response (200 OK):**
```json
{
  "score": 6.0,
  "grade": "C",
  "breakdown": {
    "base_score": 5.0,
    "nutrient_penalties": 0.0,
    "ingredient_penalties": 0.0,
    "nutrient_bonuses": 1.0,
    "ingredient_bonuses": 0.0,
    "final_score": 6.0
  }
}
```

**Expected Error Response (404 Not Found):**
```json
{
  "error": "Product not found"
}
```

### 3. Get Product Health Score V2 API
This endpoint uses the versioned scoring engine to return the health score, grade, and the rule version applied for a given product by its barcode.

- **URL:** `/v2/score/{barcode}`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/v2/score/8901491101837
```

**Expected JSON Response (200 OK):**
```json
{
  "score": 6.0,
  "grade": "C",
  "rule_version": 1,
  "breakdown": {
    "base_score": 5.0,
    "nutrient_penalties": 0.0,
    "ingredient_penalties": 0.0,
    "nutrient_bonuses": 1.0,
    "ingredient_bonuses": 0.0,
    "final_score": 6.0
  }
}
```

**Expected Error Response (404 Not Found):**
```json
{
  "error": "Product not found"
}
```

### 4. Compare Products API
This endpoint returns the detailed information for two given products by their barcodes.

- **URL:** `/compare/{barcode1}/{barcode2}`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/compare/8901491101837/8901491101838
```

**Expected JSON Response (200 OK):**
```json
{
  "product1": {
    "barcode": "8901491101837",
    "product_name": "Lay's Classic Salted",
    "brand": "Lay's",
    "serving_size_g": 28.0,
    "sugar_g_per_serving": 0.3,
    "saturated_fat_g_per_serving": 1.8,
    "sodium_mg_per_serving": 370.0,
    "protein_g_per_serving": 2.0,
    "fiber_g_per_serving": 1.0,
    "calories_kcal_per_serving": 150.0
  },
  "product2": null
}
```

**Expected Error Response (404 Not Found):**
```json
{
  "error": "Both products not found"
}
```

### 4a. Multi-Product Comparison API (`/compare-multiple`)
Compare **multiple products (2–4)** side-by-side in a single request, so the
frontend can render a clean comparison table. Each product is resolved from the
local database first and then **Open Food Facts** (so off-catalogue barcodes
still work), scored, and returned in a flat, table-friendly shape (nutrition,
health score, grade and `ingredient_flags`).

Any barcode that cannot be resolved anywhere is returned in the `not_found`
array instead of failing the whole request. Scores are **personalized** when an
`Authorization: Bearer <token>` header is sent and the user has saved dietary
preferences (see [Personalized Scoring](#personalized-scoring)).

- **URL:** `/compare-multiple`
- **Method:** `POST`
- **Headers (optional):** `Authorization: Bearer <token>` — personalize the scores.
- **Request Body (JSON):**
  - `barcodes` (required, array of strings): 2–4 barcodes to compare. Blanks and
    duplicates are ignored.

**Example Request:**
```json
POST /compare-multiple
{
  "barcodes": ["8901058005783", "8908013479122", "8901491101837"]
}
```

**Example using `curl`:**
```bash
curl -X POST http://127.0.0.1:8000/compare-multiple \
-H "Content-Type: application/json" \
-d '{"barcodes": ["8901058005783", "8908013479122", "8901491101837"]}'
```

**Expected JSON Response (200 OK):**
```json
{
  "count": 3,
  "products": [
    {
      "barcode": "8901058005783",
      "product_name": "Maggi noodles",
      "brand": "Maggi",
      "category": "noodles",
      "score": 1.0,
      "grade": "F",
      "sugar_g": 1.5,
      "protein_g": 6.2,
      "sodium_mg": 750.0,
      "saturated_fat_g": 6.8,
      "fiber_g": 2.5,
      "calories": 288.0,
      "ingredient_flags": [
        {"name": "maida", "risk": "High"},
        {"name": "palm oil", "risk": "Medium"},
        {"name": "tbhq", "risk": "Severe"}
      ],
      "source": "database"
    }
  ],
  "not_found": []
}
```

**Response fields (per product):** `barcode`, `product_name`, `brand`,
`category`, `score`, `grade`, `sugar_g`, `protein_g`, `sodium_mg`,
`saturated_fat_g`, `fiber_g`, `calories`, `ingredient_flags`, and `source`
(`database` or `openfoodfacts`). Top-level: `count` (resolved products) and
`not_found` (unresolved barcodes).

**Expected Error Response (400 Bad Request):** fewer than 2 or more than 4 barcodes.
```json
{
  "detail": "Provide at least 2 barcodes to compare"
}
```

### 5. Similar Products API ("Better Alternatives")
This endpoint returns healthier alternatives: products in the **same category** as
the scanned product with a **higher** health score, limited to 3 items.

> **Category matching (strict).** Alternatives are drawn **only** from the exact
> same category as the scanned product, using the shared category taxonomy in
> `src/category_taxonomy.py` (also used by the CSV seed, `sync_db.py` and
> `import_data.py`, so every code path agrees). If a product's category is
> unknown/`other`, the endpoint returns an **empty list** rather than a
> grab-bag — so, for example, *Ching's Schezwan Chutney* (category `sauce`)
> never surfaces *Maggi noodles* (category `noodles`) as an "alternative". A
> genuinely category-less product yields `[]`, which is the correct answer:
> better no suggestion than a mismatched one.

**Personalization (dietary preferences):** when the request identifies a user —
either via an `Authorization: Bearer <token>` header **or** an explicit
`?user_id=` query parameter — the alternatives are tailored to that user's saved
dietary preferences (see [Personalized Scoring](#personalized-scoring)):

- All scores (the scanned product's and the candidates') are computed with the
  user's **personalized** weights, so "better" means better *for that user*.
- The results are **re-ranked** by the user's preferences: `high_protein` /
  `high_fiber` push higher-protein / higher-fiber products to the top;
  `low_sugar` / `low_sodium` / `low_fat` push lower-sugar / -sodium / -fat
  products to the top. The (personalized) health score is the final tie-breaker.
- `vegan` users have non-vegan alternatives **filtered out** when an ingredient
  list is available (products without ingredient data are not excluded).

With no preferences (or an anonymous request) the behaviour is the original
generic one: ordered by health score descending.

- **URL:** `/similar/{barcode}`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — personalize for the logged-in user.
- **Query Parameters:**
  - `user_id` (optional, int): Personalize for this user when no auth token is sent.

**Example using `curl`:**
```bash
# Generic (anonymous) — 8906127540016 = Farmley Datebites (category protein_bar)
curl http://127.0.0.1:8000/similar/8906127540016

# Personalized for a logged-in user (high-protein preference ranks protein bars first)
curl -H "Authorization: Bearer <YOUR_TOKEN>" http://127.0.0.1:8000/similar/8906127540016

# Personalized via explicit user_id
curl "http://127.0.0.1:8000/similar/8906127540016?user_id=2"

# Category-less / singleton category -> empty list (no cross-category grab-bag)
curl http://127.0.0.1:8000/similar/8901595862962   # Ching's Schezwan Chutney -> []
```

**Expected JSON Response (200 OK):**
```json
[
  {
    "barcode": "8906068720018",
    "product_name": "Max chocolate protein bar",
    "brand": "Max",
    "health_score": 5.8,
    "grade": "C",
    "sugar_g_per_serving": 9.0,
    "protein_g_per_serving": 20.0,
    "sodium_mg_per_serving": 150.0,
    "saturated_fat_g_per_serving": 5.0,
    "fiber_g_per_serving": 2.0,
    "image_url": "/product-images/_placeholder.svg"
  }
]
```

The `sugar_/protein_/sodium_/saturated_fat_/fiber_g_per_serving` fields are always
included so clients can see *why* an alternative was ranked where it was. Each
alternative also carries an `image_url` — the product's contributed image or the
shared placeholder (see [Product Image Upload](#28-product-image-upload-api)).

**Expected Error Response (404 Not Found or empty array):**
If the product does not exist, returns `404`. If no similar products with a higher score exist, returns `[]`.

### 6. Recent Scans API
This endpoint returns the last 5 scanned barcodes from memory.

- **URL:** `/recent`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/recent
```

**Expected JSON Response (200 OK):**
```json
{
  "recent": [
    "8901491101837",
    "8901030922896"
  ]
}
```

### 7. Health Check API
Liveness + readiness probe. Render polls this to decide whether an instance is
healthy (and restarts it if not), and it is the endpoint UptimeRobot monitors.

- **URL:** `/health`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/health
```

**Expected JSON Response (200 OK):**
```json
{
  "status": "ok",
  "uptime_seconds": 86412.7,
  "uptime_human": "1d 0h 0m 12s",
  "started_at": "2026-07-12T15:37:35.628068+00:00",
  "server_time": "2026-07-13T15:37:48.310000+00:00",
  "database": "ok",
  "products_loaded": 252,
  "pid": 23180
}
```

| Field | Meaning |
|---|---|
| `status` | `"ok"`, or `"degraded"` if the database probe failed. |
| `uptime_seconds` / `uptime_human` | How long this worker has been alive. **This is the proof the service is not tied to a terminal** — poll it before and after closing your terminal (or shutting the laptop) and the number will have grown, not reset. |
| `started_at` | When the worker booted (UTC). |
| `database` | `"ok"` if SQLite is readable from this worker, else `"unavailable"`. |
| `products_loaded` | Row count in `products` — catches a booted-but-empty database. |
| `pid` | Worker process ID. With `--workers 2` you will see this alternate between two values across requests; if it *changes unexpectedly*, a worker was respawned. |

A failed database probe returns `status: "degraded"` with **HTTP 200**, not a 5xx —
deliberately, so a transient DB blip doesn't cause Render to kill an otherwise
healthy instance. Only a genuinely dead process fails the health check.

`status` is still `"ok"` in the normal case, so any existing caller checking that
field keeps working unchanged.

#### Lightweight ping — `/ping`

- **URL:** `/ping`
- **Method:** `GET`

Answers without touching the database, so a monitor polling every 5 minutes adds
essentially no load to the free tier. Use `/health` when you want the full picture,
`/ping` when you only need to know the process is answering.

#### Live product count — `/product-count`

Returns the **live** "Products available" figure for the frontend, counted from
the database on every request (never hard-coded).

- **URL:** `/product-count`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/product-count
```

**Expected JSON Response (200 OK):**
```json
{
  "curated_count": 252,
  "categories": 23,
  "by_category": {
    "chocolate": 48,
    "chips": 34,
    "soft_drink": 30,
    "juice": 20,
    "biscuit": 18,
    "...": "..."
  },
  "external_source": "Open Food Facts",
  "external_coverage": "on-demand",
  "total_coverage_note": "252 products are curated in Swapify's database; any other barcode is resolved live against Open Food Facts at scan time, so total reachable products also include that external catalogue.",
  "generated_at": "2026-07-17T20:51:15.895081+00:00"
}
```

| Field | Meaning |
|---|---|
| `curated_count` | **Live** row count of Swapify's curated `products` table — the headline "Products available" number. Reflects the real architecture, not a constant. |
| `categories` / `by_category` | Number of distinct categories and the per-category breakdown (also demonstrates the count is genuine). |
| `external_source` / `external_coverage` | Swapify also resolves any barcode **not** in the curated DB against Open Food Facts at scan time (`on-demand`), so total *coverage* is far larger than `curated_count`. |
| `total_coverage_note` | A ready-to-display sentence describing curated + external coverage. |
| `generated_at` | UTC timestamp the count was computed. |

Returns **HTTP 503** if the database is unreadable. Intended for the frontend's
"Products available" widget (share `curated_count` for the headline, and
optionally `total_coverage_note` for the "+ millions via Open Food Facts" line).

```json
{ "status": "ok", "uptime_seconds": 86412.7 }
```

### 8. Get Scan History API
This endpoint returns the last 5 scanned products for the authenticated user, sorted by scan time descending.

- **URL:** `/history`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>`

**Example using `curl`:**
```bash
curl -H "Authorization: Bearer <YOUR_TOKEN>" "http://127.0.0.1:8000/history"
```

**Expected JSON Response (200 OK):**
```json
[
  {
    "barcode": "8901491101837",
    "product_name": "Lay's Classic Salted",
    "brand": "Lay's",
    "health_score": 6,
    "grade": "C",
    "image_url": null,
    "scanned_at": "2023-10-27 14:32:10"
  }
]
```

### 9. Report Missing Product API
This endpoint allows users to report a product that was not found in the database. Requires authentication.

- **URL:** `/report-missing`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>`
- **Request Body (JSON):**
  - `barcode` (required, string): The scanned barcode.
  - `product_name` (optional, string): The name of the product.
  - `comment` (optional, string): User's comment or additional info.

**Example using `curl`:**
```bash
curl -X POST http://127.0.0.1:8000/report-missing \
-H "Content-Type: application/json" \
-d '{"barcode": "123456789", "product_name": "New Snacks", "comment": "Not in DB"}'
```

**Expected JSON Response (200 OK):**
```json
{
  "status": "reported"
}
```

### 10. Offline Products API
This endpoint returns the entire product database in a lightweight JSON response for offline caching.

- **URL:** `/offline-products`
- **Method:** `GET`

**Example using `curl`:**
```bash
curl http://127.0.0.1:8000/offline-products
```

**Expected JSON Response (200 OK):**
```json
[
  {
    "barcode": "8901491101837",
    "name": "Lay's Classic Salted",
    "brand": "Lay's",
    "nutrition": {
      "sugar": 0.3,
      "saturated_fat": 1.8,
      "sodium": 370.0,
      "protein": 2.0,
      "fiber": 1.0,
      "calories": 150.0
    },
    "score": 6,
    "grade": "C"
  }
]
```

### 11. Search Products API
This endpoint searches the products table by product name or brand, with optional
filtering by brand, category, health score and grade, and returns results sorted
healthiest-first by default.

**Barcode-aware search:** when `q` looks like a barcode (digits only, ≥ 8 chars),
the query is treated as a **barcode lookup** and is
[validated](#20-barcode-validation--correction-api) first. An invalid-but-correctable
barcode (e.g. a mistyped check digit) is **auto-corrected** to its suggestion for
the lookup, so the product is still found. Barcode matches include
`"matched_by": "barcode"` and a `barcode_validation` object; text (name/brand)
searches return the shape below.

- **URL:** `/search`
- **Method:** `GET`
- **Query Parameters:** (all optional — combine any subset)
  - `q`: free text matched against `product_name` and `brand` (SQL `LIKE`), or a barcode.
  - `brand`: filter by brand (`LIKE`), e.g. `?brand=Maggi`.
  - `category`: filter by category (`LIKE`), e.g. `?category=chips`.
  - `min_score` / `max_score`: keep only products within this health-score range.
  - `grade`: keep only products with this letter grade (`A`–`F`).
  - `sort`: `score_desc` (default, healthiest first), `score_asc`, or `name`.
  - `limit`: 1–50 results per page (default 10).
  - `offset`: number of ranked results to skip, for pagination (default 0).

**Pagination:** results are scored, filtered and ranked, then the page is sliced
as `results[offset : offset + limit]`. For example `?limit=10&offset=0` is page 1
and `?limit=10&offset=10` is page 2. Each result includes an `image_url` — the
product's own uploaded image, or the shared placeholder
(`/product-images/_placeholder.svg`) when it has none (see
[Product Images](#28-product-image-upload-api)).

**Example using `curl`:**
```bash
# Text search (healthiest first)
curl "http://127.0.0.1:8000/search?q=lays"

# Filter: grade-A products in the "chips" category, cheapest-scoring first
curl "http://127.0.0.1:8000/search?category=chips&min_score=3&sort=score_desc&limit=5"

# Pagination — second page of 10
curl "http://127.0.0.1:8000/search?q=&sort=name&limit=10&offset=10"
```

**Expected JSON Response (200 OK):**
```json
[
  {
    "barcode": "8901491101837",
    "name": "Lay's Classic Salted",
    "brand": "Lay's",
    "category": "chips",
    "score": 6,
    "grade": "C",
    "image_url": "/product-images/_placeholder.svg"
  }
]
```

#### Autocomplete (typeahead) — `/search/autocomplete`
Lightweight suggestions for a search box, returned as the user types. Matches
`product_name` and `brand` with SQL `LIKE`; **prefix** matches are ranked ahead of
mid-word matches. A blank query returns an empty list.

- **URL:** `/search/autocomplete`
- **Method:** `GET`
- **Query Parameters:**
  - `q` (required): the partial text typed so far.
  - `limit`: 1–10 suggestions (default 8).

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/search/autocomplete?q=mag&limit=5"
```

**Expected JSON Response (200 OK):**
```json
{
  "query": "mag",
  "count": 2,
  "suggestions": [
    {"product_name": "Maggi noodles", "brand": "Maggi", "barcode": "8901058005783"},
    {"product_name": "Maggi masala",  "brand": "Maggi", "barcode": "8901058005784"}
  ]
}
```

### 12. Dietary Preferences API
Save and retrieve a user's dietary preferences (Low Sugar, High Protein, Vegan,
…). These drive [Personalized Scoring](#personalized-scoring) and the
preference-aware [Better Alternatives](#5-similar-products-api-better-alternatives)
ranking. Requires authentication. Preferences are stored in the
`user_preferences` table (one row per user, JSON of boolean flags).

**Recognised preference flags** (all optional booleans, default `false`):

| Flag | Effect |
|------|--------|
| `low_sugar` | Sugar nutrient + "Sugars & Sweeteners" ingredient penalties weighted **×1.75**; lower-sugar alternatives ranked first. |
| `low_sodium` | Sodium penalties weighted **×1.75**; lower-sodium alternatives ranked first. |
| `low_fat` | Saturated-fat + "Oils & Fats" penalties weighted **×1.75**; lower-fat alternatives ranked first. |
| `high_protein` | Protein bonus weighted **×2.5**; higher-protein alternatives ranked first. |
| `high_fiber` | Fiber bonus weighted **×2.5**; higher-fiber alternatives ranked first. |
| `vegan` | Cancels dairy "Protein Quality" bonuses; non-vegan alternatives filtered out of `/similar`. |

#### Get Preferences
- **URL:** `/preferences`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>`

**Expected JSON Response (200 OK):** every recognised flag is returned with a
stable shape (defaulting to `false`).
```json
{
  "user_id": 2,
  "preferences": {
    "low_sugar": true,
    "low_sodium": false,
    "low_fat": false,
    "high_protein": true,
    "high_fiber": false,
    "vegan": false
  }
}
```

#### Save Preferences
- **URL:** `/preferences`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>`
- **Request Body (JSON):**
  - `preferences` (object): a map of the flags above. Unknown keys are ignored;
    values are coerced to booleans.

**Example using `curl`:**
```bash
curl -X POST http://127.0.0.1:8000/preferences \
-H "Authorization: Bearer <YOUR_TOKEN>" \
-H "Content-Type: application/json" \
-d '{"preferences": {"low_sugar": true, "high_protein": true}}'
```

**Expected JSON Response (200 OK):**
```json
{
  "status": "preferences saved",
  "user_id": 2,
  "preferences": {
    "low_sugar": true,
    "low_sodium": false,
    "low_fat": false,
    "high_protein": true,
    "high_fiber": false,
    "vegan": false
  }
}
```

#### Update Preferences (legacy alias)
`POST /update-preferences` is kept for backwards compatibility and now **persists**
preferences (previously a no-op). It accepts either a flat body
(`{"low_sugar": true}`) or a wrapped body (`{"preferences": {...}}`) and returns
`{"status": "preferences updated", "preferences": {...}}`. New clients should use
`POST /preferences`.

### 13. AI Nutritionist Chatbot API
A **real LLM-powered** nutritionist (not rule-based) that answers free-text
questions about packaged foods, grounded in food science and in the product's
own data. It integrates with **free AI APIs** — **OpenRouter** (many free-tier
models) as the primary provider, with **Google Gemini** as an optional automatic
failover — so a rate-limited free tier still returns a genuine AI answer.

It is built to answer five kinds of questions:
1. **Product ingredients and their risks** — e.g. *"What ingredients here are
   risky and why?"* (uses the product's flagged ingredients and risk levels).
2. **Health scores and why** — e.g. *"Why did this get such a low score?"* The
   product's **actual score breakdown** (base score, each penalty/bonus, category
   caps, transparency multiplier) **and** a summary of the scoring methodology are
   passed to the model, so it explains the real math instead of guessing.
3. **Ingredient substitutions** — e.g. *"What can I use instead of sugar?"* (see
   below).
4. **Top picks from the catalogue** — e.g. *"What are the top picks from all
   products?"* or *"best chocolates"* (see **Structured top picks** below).
5. **General nutrition & food-transparency** questions — e.g. *"Why do vague
   ingredient labels matter?"* (works with no barcode).

When a `barcode` is supplied, the product's nutrition, ingredients, health score,
flagged ingredients **and score breakdown** are passed to the model as grounding
context.

#### Greeting fast-path (performance)
A bare greeting or smalltalk message (`"hi"`, `"hello"`, `"thanks"`, `"how are
you"`, …) with no barcode is answered **instantly from a canned welcome — the LLM
is never called**. This is what keeps a one-word "hi" at a few **milliseconds**
instead of the multi-second (previously ~25s) round-trip a free-tier model + its
failover chain would otherwise cost. Such a response has `source: "fast-path"`.
The match is conservative: anything beyond a plain greeting (e.g. *"hi, is Maggi
healthy?"*) still goes to the AI. Provider HTTP timeouts are also lowered and
configurable (`OPENROUTER_TIMEOUT` / `GEMINI_TIMEOUT`, default 12s) so a slow
model fails over sooner.

#### Structured top picks (7+ rule)
When the question asks for the best/top/healthiest products (*"what are the top
picks from all products"*, *"best chocolates"*, *"healthiest noodles"*), the
endpoint answers **from the real scored catalogue**, not with a generic
paragraph. It scores every product with the same engine the Home page and product
pages use and applies the **7+ rule** — a genuinely good, "Swapify Recommended"
pick scores **≥ 7/10** (grade A/B). The matched picks are:
1. passed to the LLM as grounding so the prose cites the actual products, and
2. returned as a structured **`top_picks`** array (each item has `barcode`,
   `product_name`, `brand`, `category`, `score`, `grade`, `recommended` and the
   key nutrients), plus a `top_picks_category` field naming the applied filter
   (or `null` for all products).

A category can be inferred from the question (*"best chocolates"* → the
`chocolate` category). If **no** product clears the 7+ bar (this catalogue is
packaged snacks), the array is **never empty** — it returns the highest-scoring
products instead, each flagged `recommended: false`, and the prose says so
honestly.

#### Ingredient Substitution Suggestions
When the question asks **what to use instead of an ingredient** — e.g. *"What can
I use instead of sugar?"* — the endpoint detects the substitution intent and the
named ingredient, looks up **healthier, food-science-backed alternatives** from a
curated knowledge base (cross-referenced with the beneficial ingredients in the
database: natural sweeteners, healthy oils, whole grains, …), and:
1. passes those suggestions to the LLM as grounding context (so the prose answer
   is accurate and not hallucinated), and
2. also returns them as a structured **`substitutions`** array in the response.

A few of the swaps covered: sugar → jaggery / dates / stevia / honey; palm oil →
cold-pressed / olive / rice-bran oil; maida → whole-wheat flour / oats / millets;
MSG → tomato / mushroom / herbs; artificial colours → turmeric / beetroot / paprika.

- **URL:** `/chat`
- **Method:** `POST`
- **Request Body (JSON):**
  - `question` (required, string): The user's question.
  - `barcode` (optional, string): A product barcode to use as context.

#### Configuration & rate-limit handling
Set at least one provider key in `server/.env` (see "How to run the backend"):

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | Primary provider. Free key: https://openrouter.ai/keys |
| `OPENROUTER_MODEL` | Primary model (default `openai/gpt-oss-120b:free`). |
| `OPENROUTER_FALLBACK_MODELS` | Comma-separated models tried in order if the primary is busy. |
| `GEMINI_API_KEY` | Optional second provider. Free key: https://aistudio.google.com/apikey |
| `GEMINI_MODEL` | Gemini model (default `gemini-2.0-flash`). |
| `OPENROUTER_TIMEOUT` | Per-request OpenRouter HTTP timeout in seconds (default `12`). Lower = faster failover when a model hangs. |
| `GEMINI_TIMEOUT` | Per-request Gemini HTTP timeout in seconds (default `12`). |

**Free-tier rate limits are handled gracefully.** Free models are frequently
rate-limited (HTTP 429). The backend:
- tries each OpenRouter model **in order**; a per-model 429 **skips immediately to
  the next model** (no wasted retry), other transient errors get one quick retry;
- **fails over to Gemini** when configured and every OpenRouter model is busy;
- stops calling OpenRouter at once if the **account-wide daily cap** is hit;
- only if **all** providers fail does it fall back to a deterministic,
  food-science answer (so the endpoint always returns `200`, never `500`).

The `source` field reports which provider answered (`"openrouter"`, `"gemini"`,
or `"fallback"`) and `model` reports the exact model used.

**Example using `curl` (why this score — grounded in the real breakdown):**
```bash
curl -X POST http://127.0.0.1:8000/chat \
-H "Content-Type: application/json" \
-d '{"question": "Why did this product get such a low score?", "barcode": "7622300441937"}'
```

**Expected JSON Response (200 OK):**
```json
{
  "response": "Cadbury Dairy Milk earned 1.5/10 (F): from a base of 5.0, sugar (57g) costs -2 and saturated fat (19.6g) -1; the 'Sugars & Sweeteners' and 'Oils & Fats' ingredient categories hit their caps (-2.5 and -1.7) because sugar and fractionated fat lead the ingredient list; a 0.95 transparency multiplier (vague 'flavours'/'emulsifiers') pushes it below the floor, so it clamps to 1.5.",
  "barcode": "7622300441937",
  "product_found": true,
  "source": "openrouter",
  "model": "openai/gpt-oss-120b:free",
  "ai_enabled": true,
  "product_name": "Cadbury Dairy Milk",
  "score": 1.5,
  "grade": "F",
  "ingredient_flags": [
    {"name": "sugar", "risk": "High"},
    {"name": "fractionated fat", "risk": "High"}
  ]
}
```

**Example using `curl` (substitution question, no barcode):**
```bash
curl -X POST http://127.0.0.1:8000/chat \
-H "Content-Type: application/json" \
-d '{"question": "What can I use instead of sugar?"}'
```

**Expected JSON Response (200 OK):**
```json
{
  "response": "Instead of refined sugar, try jaggery, date paste, honey, stevia or monk fruit — these add sweetness with more minerals or far fewer calories and a gentler impact on blood sugar.",
  "barcode": null,
  "product_found": false,
  "source": "openrouter",
  "model": "openai/gpt-oss-120b:free",
  "ai_enabled": true,
  "substitutions": [
    {
      "ingredient": "refined sugar",
      "alternatives": ["jaggery", "date paste", "honey", "stevia", "monk fruit"],
      "reason": "Natural sweeteners add sweetness with more minerals or far fewer calories and a gentler impact on blood sugar than refined sugar."
    }
  ]
}
```

**Example using `curl` (top picks — structured, uses the 7+ rule):**
```bash
curl -X POST http://127.0.0.1:8000/chat \
-H "Content-Type: application/json" \
-d '{"question": "what are the top picks from all products"}'
```

**Expected JSON Response (200 OK):**
```json
{
  "response": "None of the products reach the 7+/10 recommended bar, but here are the highest-scoring options:\n1. Let's Try roasted channa — 6.0/10 (grade C)\n2. Yu whole wheat noodles — 6.0/10 (grade C)\n...",
  "barcode": null,
  "product_found": false,
  "source": "openrouter",
  "model": "openai/gpt-oss-120b:free",
  "ai_enabled": true,
  "top_picks_category": null,
  "top_picks": [
    {
      "barcode": "8906161390870",
      "product_name": "Let's Try roasted channa",
      "brand": "Let's Try",
      "category": "chips",
      "score": 6.0,
      "grade": "C",
      "recommended": false,
      "sugar_g_per_serving": 1.0,
      "protein_g_per_serving": 7.0,
      "sodium_mg_per_serving": 32.0,
      "fiber_g_per_serving": 5.0,
      "image_url": "/product-images/_placeholder.svg"
    }
  ]
}
```

**Example using `curl` (greeting fast-path — instant, no LLM):**
```bash
curl -X POST http://127.0.0.1:8000/chat \
-H "Content-Type: application/json" \
-d '{"question": "hi"}'
# -> {"response": "Hi! I'm Swapify's AI nutritionist...", "source": "fast-path", ...}
```

**Response fields:**
- `response` (string): The AI-generated (or fallback) answer.
- `barcode` (string|null): Echoes the requested barcode.
- `product_found` (bool): Whether product context was located.
- `source` (string): Which provider answered — `"openrouter"`, `"gemini"`,
  `"fallback"` (deterministic) when every provider failed, or `"fast-path"` for an
  instant greeting reply.
- `model` (string|null): The exact model that answered (null on fallback/fast-path).
- `ai_enabled` (bool): Whether any AI provider key is configured.
- `fallback_reason` (string): included **only** on the fallback path, explaining
  why no provider answered.
- `substitutions` (array): included **only** when the question asks for an
  ingredient alternative; each item has `ingredient`, `alternatives` (list) and `reason`.
- `top_picks` (array) / `top_picks_category` (string|null): included **only** for
  top-picks questions; the structured, ranked list (see **Structured top picks**)
  and the category filter applied (or `null` for all products).
- `product_name`, `score`, `grade`, `ingredient_flags`: included only when a product was found.

**Expected Error Response (400 Bad Request):** when `question` is empty.
```json
{
  "detail": "question is required"
}
```

### 14. Favorites API
Manage user's favorite products. Requires authentication.

#### Add to Favorites
- **URL:** `/favorites`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>`
- **Request Body (JSON):**
  - `barcode` (required, string): The barcode of the product to favorite.

**Expected JSON Response (200 OK):**
```json
{
  "message": "Added to favorites"
}
```

#### Remove from Favorites
- **URL:** `/favorites/{barcode}`
- **Method:** `DELETE`
- **Headers:** `Authorization: Bearer <token>`

**Expected JSON Response (200 OK):**
```json
{
  "message": "Removed from favorites"
}
```

#### Get Favorites
- **URL:** `/favorites`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>`

**Expected JSON Response (200 OK):**
```json
[
  {
    "barcode": "8901491101837",
    "product_name": "Lay's Classic Salted",
    "brand": "Lay's",
    "health_score": 6,
    "grade": "C",
    "added_at": "2023-10-28 10:00:00"
  }
]
```

### 15. Weekly Summary API
Get the user's scan activity and health score trends for the past 7 days. Requires authentication.

- **URL:** `/weekly-summary`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>`

**Expected JSON Response (200 OK):**
```json
{
  "total_scans": 5,
  "average_score": 6.8,
  "daily_trends": [
    {
      "date": "2023-10-25",
      "average_score": 6.0
    },
    {
      "date": "2023-10-26",
      "average_score": 7.5
    }
  ]
}
```

### 16. Monthly Health Report API (`/monthly-report`)
Generate a **monthly summary report** from a user's scan history. The report
aggregates a single calendar month of scans into total scans, average health
score, the best- and worst-scoring products scanned, the **score trend** across
the month, and a **most-scanned category breakdown**.

- **URL:** `/monthly-report`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — used as the user when
  `user_id` is not supplied; also personalizes the scores.
- **Query Parameters:**
  - `user_id` (int): Whose history to summarise. Required unless an
    `Authorization` token is sent (the token's user is then used).
  - `month` (optional, string `YYYY-MM`): The month to report on. **Defaults to
    the current (UTC) month.**

**How values are computed:**
- **`total_scans`** — number of scans recorded in the month.
- **`average_score`** — mean health score across those scans (personalized to the
  user's dietary preferences, like the other user-scoped endpoints).
- **`best_product` / `worst_product`** — the highest- and lowest-scoring products
  scanned in the month.
- **`score_trend`** — compares the average score of the **first half** of the
  month's scans against the **second half** (chronological): `improving` when the
  later half is ≥ 0.5 higher, `declining` when ≥ 0.5 lower, else `stable`
  (`no_data` when there are no scans).
- **`category_breakdown`** — scan count per product category, most-scanned first.

**Example using `curl`:**
```bash
# Explicit user and month
curl "http://127.0.0.1:8000/monthly-report?user_id=2&month=2026-06"

# Authenticated user, current month (month defaults)
curl -H "Authorization: Bearer <YOUR_TOKEN>" "http://127.0.0.1:8000/monthly-report"
```

**Expected JSON Response (200 OK):**
```json
{
  "user_id": 2,
  "month": "2026-06",
  "total_scans": 8,
  "average_score": 4.6,
  "best_product": {
    "barcode": "8908013479122",
    "product_name": "The whole truth food protein bar",
    "brand": "The Whole Truth",
    "score": 8.0,
    "grade": "B"
  },
  "worst_product": {
    "barcode": "8901058005783",
    "product_name": "Maggi noodles",
    "brand": "Maggi",
    "score": 1.0,
    "grade": "F"
  },
  "score_trend": "improving",
  "category_breakdown": [
    {"category": "bar", "count": 3},
    {"category": "noodles", "count": 2},
    {"category": "chocolate", "count": 1}
  ],
  "daily_trends": [
    {"date": "2026-06-05", "average_score": 2.5},
    {"date": "2026-06-20", "average_score": 6.7}
  ]
}
```

**Response when the user has no scans in the month (200 OK):**
```json
{
  "user_id": 2,
  "month": "2026-01",
  "total_scans": 0,
  "average_score": 0,
  "best_product": null,
  "worst_product": null,
  "score_trend": "no_data",
  "category_breakdown": [],
  "daily_trends": []
}
```

**Expected Error Responses (400 Bad Request):**
```json
{ "detail": "user_id is required (query param or Authorization token)" }
```
```json
{ "detail": "month must be in YYYY-MM format" }
```

*(Note: These endpoints can be easily tested in your browser by simply pasting the URL in the address bar if you do not want to use `curl`).*

### 17. Crowdsourced Product Ratings API

Let users rate products on **taste**, **quality** and **value** (each **1-5
stars**) alongside the objective health score, and read the community's average
ratings for a product.

Ratings are stored per **(user, product)**: a user re-rating the same barcode
**updates** their existing rating (they never stack), so community averages are
never double-counted. Each rating row stores `user_id`, `barcode`,
`taste_rating`, `quality_rating`, `value_rating` and a `rated_at` timestamp.

#### Submit / Update a Rating
- **URL:** `/rate-product`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>` (required)
- **Request Body (JSON):**
  - `barcode` (required, string): The product being rated.
  - `taste_rating` (required, int 1-5)
  - `quality_rating` (required, int 1-5)
  - `value_rating` (required, int 1-5)

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/rate-product" \
  -H "Authorization: Bearer <YOUR_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"barcode":"8908013479122","taste_rating":5,"quality_rating":4,"value_rating":4}'
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Rating submitted",
  "barcode": "8908013479122",
  "rating": {
    "taste_rating": 5,
    "quality_rating": 4,
    "value_rating": 4
  }
}
```
> On a repeat rating of the same product the `message` becomes `"Rating updated"`.

**Error Responses:**
```json
{ "detail": "taste_rating must be an integer from 1 to 5" }   // 400 – star out of range
```
```json
{ "detail": "Invalid token" }                                  // 401 – missing/invalid auth
```

#### Get a Product's Average Ratings
- **URL:** `/product/{barcode}/ratings`
- **Method:** `GET`
- **Auth:** Not required (public).

Returns the average `taste`, `quality` and `value` ratings, an `overall`
average (mean of the three), and the `total_ratings` count.

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/product/8908013479122/ratings"
```

**Expected JSON Response (200 OK):**
```json
{
  "barcode": "8908013479122",
  "total_ratings": 2,
  "average_ratings": {
    "taste": 3.0,
    "quality": 4.0,
    "value": 3.0,
    "overall": 3.33
  }
}
```
> When a product has no ratings yet, `total_ratings` is `0` and each average is
> `null`.

#### Get the Current User's Ratings
- **URL:** `/user/ratings`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>` (required)

Returns the authenticated user's own past ratings, newest first. `product_name`
and `brand` are included when the product is in the local catalog.

**Expected JSON Response (200 OK):**
```json
{
  "user_id": 15,
  "total": 2,
  "ratings": [
    {
      "barcode": "8901491101837",
      "product_name": "Lay's Classic Salted",
      "brand": "Lay's",
      "taste_rating": 3,
      "quality_rating": 2,
      "value_rating": 3,
      "overall_rating": 2.67,
      "rated_at": "2026-07-02 15:55:04"
    }
  ]
}
```

### 18. AI-Powered Product Recommendations API

A **rule-based personalized recommendation engine**. For a logged-in user it
blends three interest signals with the health score and crowdsourced ratings:

1. **Scan history** — the user's most-scanned product categories.
2. **Dietary preferences** — the saved flags (`high_protein`, `low_sugar`,
   `vegan`, …). Scores are personalized and clearly non-vegan products are
   dropped for vegan users.
3. **Past comparisons viewed** — categories of products the user has compared
   (`/compare`, `/compare-multiple`) are logged and used as an interest signal.

Community ratings give a small boost/penalty, the personalized health score is
the base desirability signal, and already-scanned products are de-prioritized so
recommendations favour fresh discoveries. **5-10** products are returned, each
with a human-readable `reason`.

- **URL:** `/recommendations`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — used as the user
  when `user_id` is not supplied.
- **Query Parameters:**
  - `user_id` (optional, int): Whom to recommend for. Falls back to the
    authenticated user. **If neither is supplied, generic popular products are
    returned.**
  - `limit` (optional, int): Number of recommendations, **clamped to 5-10**
    (default `10`).

Each recommendation includes: `barcode`, `product_name`, `brand`,
`health_score`, `grade`, and `reason`.

**Example using `curl`:**
```bash
# Personalized (explicit user_id)
curl "http://127.0.0.1:8000/recommendations?user_id=15"

# Personalized (via token)
curl -H "Authorization: Bearer <YOUR_TOKEN>" "http://127.0.0.1:8000/recommendations"

# Generic popular products (anonymous)
curl "http://127.0.0.1:8000/recommendations"
```

**Expected JSON Response (200 OK) — personalized:**
```json
{
  "user_id": 15,
  "personalized": true,
  "count": 10,
  "based_on": {
    "top_categories": ["bar"],
    "dietary_preferences": { "high_protein": true },
    "comparisons_considered": true
  },
  "recommendations": [
    {
      "barcode": "8906068720018",
      "product_name": "Max chocolate protein bar",
      "brand": "Max protein bar",
      "health_score": 6.5,
      "grade": "C",
      "image_url": "/product-images/_placeholder.svg",
      "reason": "Recommended because it matches your most-scanned category (bar) and is high in protein."
    }
  ]
}
```

**Expected JSON Response (200 OK) — anonymous (generic popular):**
```json
{
  "user_id": null,
  "personalized": false,
  "count": 10,
  "recommendations": [
    {
      "barcode": "8908013479122",
      "product_name": "The whole truth food protein bar",
      "brand": "The whole truth",
      "health_score": 5.0,
      "grade": "C",
      "image_url": "/product-images/_placeholder.svg",
      "reason": "Recommended because it is popular with other shoppers."
    }
  ]
}
```

### 19. Shareable Score Card API

Return a product's data **formatted for a shareable image card**. Bundles the
identity fields, the health score/grade, **key warnings** and flagged
ingredients, plus a ready-to-render `card` block (grade colour, headline, labels)
so the frontend can draw the card without re-deriving any copy.

- **URL:** `/share/{barcode}`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — personalizes the
  score to the user's dietary preferences when present.

The product is resolved from the local catalog first, then Open Food Facts
(whose products also supply an `image_url`; local products have `image_url:
null`). **Key warnings** are derived from the nutrition (high sugar / sodium /
saturated fat per serving) and any High/Severe-risk flagged ingredients.

**Response fields:** `barcode`, `product_name`, `brand`, `image_url`,
`health_score`, `grade`, `warnings`, `ingredient_flags`, and a `card` object
(`title`, `subtitle`, `score_label`, `grade`, `grade_color`, `headline`,
`warning_count`, `flag_count`, `footer`).

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/share/7622300441937"
```

**Expected JSON Response (200 OK):**
```json
{
  "barcode": "7622300441937",
  "product_name": "Cadbury Dairy Milk",
  "brand": "Cadbury",
  "image_url": null,
  "health_score": 1.5,
  "grade": "F",
  "warnings": [
    "High sugar (57.0g per serving)",
    "High saturated fat (19.6g per serving)",
    "Contains sugar (high risk)",
    "Contains fractionated fat (high risk)"
  ],
  "ingredient_flags": [
    { "name": "sugar", "risk": "High" },
    { "name": "fractionated fat", "risk": "High" }
  ],
  "card": {
    "title": "Cadbury Dairy Milk",
    "subtitle": "Cadbury",
    "score_label": "1.5/10",
    "grade": "F",
    "grade_color": "#d73027",
    "headline": "Cadbury Dairy Milk scores 1.5/10 (grade F) — worth a closer look.",
    "warning_count": 4,
    "flag_count": 2,
    "footer": "Scanned with Swapify"
  },
  "source": "database"
}
```

**Error Response (404 Not Found):**
```json
{ "error": "Product not found" }
```

### 20. Barcode Validation & Correction API

Validate a barcode's **length** and **check digit** and, when it's invalid,
return a **suggested correction**. Supports the standard retail formats —
**EAN-8** (8 digits), **UPC-A** (12 digits) and **EAN-13** (13 digits) — whose
final digit is a GS1 modulo-10 check digit computed from the preceding digits.

This validation is also woven into
[`/product/{barcode}`](#1-get-product-details-api) (a `barcode_validation` object
is attached when the barcode is malformed, including on the `404`) and
[`/search`](#11-search-products-api) (a barcode-looking query is auto-corrected
for the lookup).

- **URL:** `/validate-barcode/{barcode}`
- **Method:** `GET`
- **Auth:** Not required (public).

**Response fields:**
- `barcode` (string): the trimmed input.
- `valid` (bool): `true` only for a well-formed barcode with a correct check digit.
- `format` (string|null): the detected format (`EAN-8` / `UPC-A` / `EAN-13`), or `null`.
- `suggestion` (string|null): a corrected barcode when one can be derived, else `null`.
- `message` (string): a human-readable explanation.

**Correction logic:**
- **Right length, wrong check digit** → suggests the same digits with the correct
  check digit.
- **One digit short of a format** (e.g. **12 digits** → EAN-13) → suggests the code
  with a **check digit added**.
- **Contains non-digits** → suggests the digits-only form when that is itself valid.

**Example using `curl`:**
```bash
# Valid
curl "http://127.0.0.1:8000/validate-barcode/8901491101837"

# Invalid check digit -> suggestion
curl "http://127.0.0.1:8000/validate-barcode/8901491101830"
```

**Expected JSON Response (200 OK) — valid:**
```json
{
  "barcode": "8901491101837",
  "valid": true,
  "format": "EAN-13",
  "suggestion": null,
  "message": "Valid EAN-13 barcode."
}
```

**Expected JSON Response (200 OK) — invalid, with correction:**
```json
{
  "barcode": "8901491101830",
  "valid": false,
  "format": "EAN-13",
  "suggestion": "8901491101837",
  "message": "Invalid EAN-13 check digit: expected '7', got '0'. Suggested correction: '8901491101837'."
}
```

### 21. User Activity Logging API

Track user actions — **scan**, **compare**, **share**, **rate**, **favorite** —
to understand behaviour and improve recommendations. Each action is stored in the
`user_activity` table with `user_id`, `action_type`, an optional `barcode`, an
optional JSON `metadata` blob and a `created_at` timestamp.

The `/product`, `/compare`, `/compare-multiple`, `/share`, `/rate-product` and
`POST /favorites` endpoints also **auto-log** (best-effort) the matching action
for logged-in users, so the activity stream and trends reflect real usage without
the client having to log anything explicitly.

**Recognised `action_type` values:** `scan`, `compare`, `share`, `rate`, `favorite`.

#### Log an Activity
- **URL:** `/activity`
- **Method:** `POST`
- **Headers (optional):** `Authorization: Bearer <token>` — when present, its user
  is used; otherwise `user_id` is taken from the body.
- **Request Body (JSON):**
  - `action_type` (required, string): one of the recognised values above.
  - `user_id` (optional, int): used when the request is not authenticated.
  - `barcode` (optional, string): the product the action relates to (if any).
  - `metadata` (optional, object): free-form extra context (stored as JSON).

**Example using `curl`:**
```bash
curl -X POST http://127.0.0.1:8000/activity \
-H "Authorization: Bearer <YOUR_TOKEN>" \
-H "Content-Type: application/json" \
-d '{"action_type":"scan","barcode":"8901491101837","metadata":{"src":"scanner"}}'
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Activity logged",
  "activity": {
    "id": 42,
    "user_id": 2,
    "action_type": "scan",
    "barcode": "8901491101837",
    "metadata": { "src": "scanner" },
    "created_at": "2026-07-04 08:24:39"
  }
}
```

**Expected Error Response (400 Bad Request):** unknown `action_type`.
```json
{ "detail": "action_type must be one of: scan, compare, share, rate, favorite" }
```

#### Get a User's Activity History
- **URL:** `/activity/user/{user_id}`
- **Method:** `GET`
- **Auth:** Not required.
- **Query Parameters:**
  - `action_type` (optional, string): filter to a single action type.
  - `limit` (optional, int): max rows, **clamped to 1–200** (default `50`).

Returns the user's activity newest-first plus an `action_counts` summary.

**Expected JSON Response (200 OK):**
```json
{
  "user_id": 2,
  "count": 4,
  "action_counts": { "scan": 3, "rate": 1 },
  "activities": [
    {
      "id": 42,
      "user_id": 2,
      "action_type": "scan",
      "barcode": "8901491101837",
      "metadata": { "src": "scanner" },
      "created_at": "2026-07-04 08:24:39"
    }
  ]
}
```

#### Get Overall Activity Trends *(optional analytics)*
- **URL:** `/activity/trends`
- **Method:** `GET`
- **Auth:** Not required.
- **Query Parameters:**
  - `days` (optional, int): size of the day-by-day window, **clamped to 1–90**
    (default `7`).

Returns the total number of actions, a breakdown **by action type**, a per-day
count for the last `days` days, the most-active barcodes, and the number of
distinct active users in the window.

**Expected JSON Response (200 OK):**
```json
{
  "window_days": 7,
  "total_actions": 8,
  "by_action_type": { "scan": 4, "share": 1, "rate": 1, "favorite": 1, "compare": 1 },
  "by_day": [ { "date": "2026-07-04", "count": 8 } ],
  "top_barcodes": [ { "barcode": "8901491101837", "count": 3 } ],
  "active_users": 1
}
```

### 22. Daily Digest / Notification API

Generate a **daily summary** of a user's scans to build engagement — designed to
be dropped straight into an **email or push-notification** pipeline. Summarises a
single day's scans into **total scans**, **average score**, and the **best** and
**worst** product scanned, and returns ready-to-send `notification` and `email`
blocks. Scores use the user's personalized dietary weights.

- **URL:** `/digest/{user_id}`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — personalizes the scores.
- **Query Parameters:**
  - `date` (optional, string `YYYY-MM-DD`): the day to summarise. **Defaults to the
    current (UTC) day.**

**Manual trigger vs. scheduling:** this `GET` endpoint **is** the manual trigger.
For automated daily delivery, schedule a job that calls it once a day and forwards
the `notification` / `email` blocks to your provider, e.g. a **cron** entry
(`0 8 * * * curl -s http://127.0.0.1:8000/digest/2`) or a **Windows Task
Scheduler** task running the same `curl`.

**Example using `curl`:**
```bash
# Today's digest
curl "http://127.0.0.1:8000/digest/2"

# A specific day
curl "http://127.0.0.1:8000/digest/2?date=2026-07-04"
```

**Expected JSON Response (200 OK):**
```json
{
  "user_id": 2,
  "date": "2026-07-04",
  "total_scans": 3,
  "average_score": 2.7,
  "best_product": {
    "barcode": "8908013479122",
    "product_name": "The whole truth food protein bar",
    "brand": "The whole truth",
    "score": 5.0,
    "grade": "C"
  },
  "worst_product": {
    "barcode": "8901491101837",
    "product_name": "Lay's Classic Salted",
    "brand": "Lay's",
    "score": 1.1,
    "grade": "F"
  },
  "notification": {
    "type": "daily_digest",
    "title": "Your daily scan summary — 3 scans",
    "body": "You scanned 3 products today with an average health score of 2.7/10. Best: The whole truth food protein bar (5.0/10, C). Watch out for: Lay's Classic Salted (1.1/10, F)."
  },
  "email": {
    "subject": "Your Swapify daily digest — avg 2.7/10 across 3 scans",
    "preview": "3 scans · avg 2.7/10",
    "body_text": "You scanned 3 products today with an average health score of 2.7/10. Best: The whole truth food protein bar (5.0/10, C). Watch out for: Lay's Classic Salted (1.1/10, F)."
  }
}
```

**Response when the user has no scans that day (200 OK):** a friendly nudge.
```json
{
  "user_id": 2,
  "date": "2026-07-04",
  "total_scans": 0,
  "average_score": 0,
  "best_product": null,
  "worst_product": null,
  "notification": {
    "type": "daily_digest",
    "title": "No scans yet today",
    "body": "You haven't scanned any products today. Scan a product to see how healthy it is and get better recommendations!"
  },
  "email": {
    "subject": "Your Swapify daily digest",
    "preview": "You haven't scanned any products today. Scan a product to see how healthy it is and get better recommendations!",
    "body_text": "You haven't scanned any products today. Scan a product to see how healthy it is and get better recommendations!"
  }
}
```

**Expected Error Response (400 Bad Request):** malformed `date`.
```json
{ "detail": "date must be in YYYY-MM-DD format" }
```

### 23. Weekly Challenges & Leaderboard API

A **gamification** layer: users join **weekly challenges** and see where they
**rank** against everyone else on the **leaderboard**. Progress is derived from
the existing `user_activity` stream (scans, comparisons and ratings are already
auto-logged), so **joining a challenge is the only new write** — nothing about
the scan/compare/rate flows changes. Completing a challenge earns its **badge**,
which is shown on the leaderboard.

**The four active challenges (seeded automatically):**

| # | Title | Counts | Target | Badge |
|---|-------|--------|--------|-------|
| 1 | Scan 20 products this week | `scan` actions | 20 | Scan Champion |
| 2 | Find 5 products with score > 4 | distinct scanned products scoring > 4 | 5 | Health Hunter |
| 3 | Compare 10 products | `compare` actions | 10 | Comparison Pro |
| 4 | Rate 15 products | `rate` actions | 15 | Star Reviewer |

Progress is measured over each challenge's rolling **period** window (weekly =
last 7 days). The tables are created and seeded on server startup
(`app.ensure_feature_schema`) — see also
`migrations/005_create_challenges_reviews_smartcart.sql`.

#### Get Active Challenges
- **URL:** `/challenges`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — when supplied, each
  challenge also carries whether the user has `joined` it and their live
  `progress`.

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/challenges"                              # anonymous
curl "http://127.0.0.1:8000/challenges" -H "Authorization: Bearer <TOKEN>"  # with my progress
```

**Expected JSON Response (200 OK):** *(authenticated — note `joined`/`progress`)*
```json
{
  "count": 4,
  "active_challenges": [
    {
      "id": 1,
      "code": "scan_20_weekly",
      "title": "Scan 20 products this week",
      "description": "Scan any 20 products within a week to complete this challenge.",
      "goal_type": "scan",
      "target": 20,
      "score_threshold": null,
      "period": "weekly",
      "badge": "Scan Champion",
      "participant_count": 3,
      "joined": true,
      "joined_at": "2026-07-06 14:32:12",
      "progress": { "current": 3, "target": 20, "completed": false, "percent": 15.0, "remaining": 17 }
    }
  ]
}
```

#### Join a Challenge
- **URL:** `/challenges/{id}/join`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>` (required)

Idempotent — re-joining returns the existing entry with `message: "Already joined"`.

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/challenges/1/join" \
  -H "Authorization: Bearer <TOKEN>"
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Joined challenge",
  "challenge_id": 1,
  "title": "Scan 20 products this week",
  "badge": "Scan Champion",
  "joined": true,
  "progress": { "current": 0, "target": 20, "completed": false, "percent": 0.0, "remaining": 20 }
}
```
**Error (404):** `{ "detail": "Challenge not found" }`

#### Get My Progress in a Challenge
- **URL:** `/challenges/{id}/progress`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>` (required)

Progress is computed live from the activity stream. When the target is reached
the participant row is stamped `completed_at` (badge earned — sticky thereafter).

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/challenges/1/progress" -H "Authorization: Bearer <TOKEN>"
```

**Expected JSON Response (200 OK):**
```json
{
  "challenge_id": 1,
  "title": "Scan 20 products this week",
  "description": "Scan any 20 products within a week to complete this challenge.",
  "badge": "Scan Champion",
  "period": "weekly",
  "joined": true,
  "joined_at": "2026-07-06 14:32:12",
  "completed_at": null,
  "badge_earned": false,
  "current": 3,
  "target": 20,
  "completed": false,
  "percent": 15.0,
  "remaining": 17
}
```

#### Get the Leaderboard
- **URL:** `/leaderboard`
- **Method:** `GET`
- **Auth:** Not required (public).
- **Query Parameters:**
  - `period` (optional): `weekly` (7d, default), `monthly` (30d) or `all-time`.
  - `limit` (optional, default 10, max 100).

Users are ranked by an **activity score** — a weighted sum of their actions in
the period (`scan` = 1, `compare` = 3, `rate` = 2, `share` = 1, `favorite` = 1).
Each row shows `rank`, `username`, `score`, an action `breakdown` and the
`badges` the user has earned from completed challenges.

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/leaderboard?period=weekly&limit=10"
curl "http://127.0.0.1:8000/leaderboard?period=all-time"
```

**Expected JSON Response (200 OK):**
```json
{
  "period": "weekly",
  "count": 2,
  "scoring": { "scan": 1, "compare": 3, "rate": 2, "share": 1, "favorite": 1 },
  "leaderboard": [
    {
      "rank": 1,
      "user_id": 23,
      "username": "tester_1783154991",
      "score": 15,
      "activity_count": 10,
      "activity_breakdown": { "scan": 5, "compare": 1, "rate": 3, "favorite": 1 },
      "badges": ["Scan Champion"],
      "badge_count": 1
    }
  ]
}
```
**Error (400):** `{ "detail": "period must be one of: weekly, monthly, all-time" }`

### 24. Smart Cart — Shopping List Optimization API

Let a user build a **shopping list** of products and get **healthier
alternatives** for each item. Optimization reuses the same personalized "better
alternatives" engine as `/similar`: for every item it returns the original plus
its **top 2 healthier same-category alternatives** (higher score / better
nutrition). Lists are saved, fetchable and deletable, and an item can be
**replaced** by a chosen alternative.

Items are referenced by **barcode**. When the request is authenticated the list
is tied to the user and the scores/alternatives are personalized to their
dietary preferences (and non-vegan swaps are dropped for vegan users).

#### Create a Shopping List
- **URL:** `/shopping-list`
- **Method:** `POST`
- **Headers (optional):** `Authorization: Bearer <token>` — ties the list to the
  user and personalizes scores.
- **Request Body (JSON):**
  - `items` (required, string[]): product barcodes (trimmed & de-duplicated).
  - `name` (optional, string): defaults to `"My Shopping List"`.

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/shopping-list" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name":"Weekly Groceries","items":["8901262176224","8904335602385","8901491101837"]}'
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Shopping list created",
  "id": 1,
  "user_id": 28,
  "name": "Weekly Groceries",
  "created_at": "2026-07-06 14:32:59",
  "item_count": 3,
  "items": [
    {
      "barcode": "8901262176224",
      "product_name": "Chocobar",
      "brand": "Amul",
      "category": "bar",
      "score": 2.0,
      "grade": "F",
      "sugar_g": 9.2,
      "protein_g": 1.7,
      "sodium_mg": 50.0,
      "saturated_fat_g": 7.7,
      "fiber_g": 0.0,
      "found": true
    }
  ]
}
```
**Error (400):** `{ "detail": "items must contain at least one barcode" }`

#### Get a Saved Shopping List
- **URL:** `/shopping-list/{id}`
- **Method:** `GET`

Returns the saved list with each item scored (same item shape as above).
**Error (404):** `{ "error": "Shopping list not found" }`

#### Optimize a Shopping List
- **URL:** `/shopping-list/{id}/optimize`
- **Method:** `GET`
- **Headers (optional):** `Authorization: Bearer <token>` — personalizes the ranking.

For each item returns the `original` plus its top 2 `alternatives`, the
`best_alternative_score` and the `potential_gain` (best alternative score minus
the original's). A list-level summary reports how many items have a healthier
option and the `total_potential_gain`.

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/shopping-list/1/optimize" -H "Authorization: Bearer <TOKEN>"
```

**Expected JSON Response (200 OK):**
```json
{
  "list_id": 1,
  "name": "Weekly Groceries",
  "item_count": 3,
  "items_with_alternatives": 3,
  "total_potential_gain": 7.9,
  "items": [
    {
      "original": { "barcode": "8901262176224", "product_name": "Chocobar", "score": 2.0, "grade": "F", "found": true },
      "alternatives": [
        { "barcode": "8908013479122", "product_name": "The whole truth food protein bar", "health_score": 5.0, "grade": "C", "sugar_g_per_serving": 0.0, "protein_g_per_serving": 13.0 },
        { "barcode": "8906068720018", "product_name": "Max chocolate protein bar", "health_score": 5.0, "grade": "C", "sugar_g_per_serving": 9.0, "protein_g_per_serving": 20.0 }
      ],
      "best_alternative_score": 5.0,
      "potential_gain": 3.0,
      "has_healthier_option": true
    }
  ]
}
```

#### Replace an Item
- **URL:** `/shopping-list/{id}/replace`
- **Method:** `POST`
- **Request Body (JSON):** `old_barcode` (string), `new_barcode` (string).

Swaps one item's barcode (e.g. for a healthier alternative) and returns the
updated, re-scored list.

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/shopping-list/1/replace" \
  -H "Content-Type: application/json" \
  -d '{"old_barcode":"8901262176224","new_barcode":"8908013479122"}'
```

**Expected JSON Response (200 OK):**
```json
{ "message": "Replaced 8901262176224 with 8908013479122", "id": 1, "item_count": 3, "items": [ ... ] }
```
**Error (404):** `{ "detail": "'8901262176224' is not in this list" }`

#### Delete a Shopping List
- **URL:** `/shopping-list/{id}`
- **Method:** `DELETE`

**Example using `curl`:**
```bash
curl -X DELETE "http://127.0.0.1:8000/shopping-list/1"
```
**Expected JSON Response (200 OK):** `{ "message": "Shopping list deleted", "list_id": 1 }`

### 25. Community Reviews & Discussions API

Let users leave **written reviews** (free text + a **1-5 star rating**) on a
product and **discuss** them with **upvotes/downvotes** and **threaded replies**.
A review is distinct from the structured taste/quality/value ratings in
`/rate-product` — this is the free-text discussion layer. A user can delete
**only their own** review; deleting a review cascades to its votes and replies.

#### Submit a Review
- **URL:** `/reviews`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>` (required)
- **Request Body (JSON):**
  - `barcode` (required, string)
  - `rating` (required, int 1-5)
  - `review_text` (required, string)

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/reviews" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"barcode":"8901491101837","rating":4,"review_text":"Great crunch but too salty for daily snacking."}'
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Review submitted",
  "review": {
    "id": 1,
    "user_id": 29,
    "username": "rv1_1783348415",
    "barcode": "8901491101837",
    "rating": 4,
    "review_text": "Great crunch but too salty for daily snacking.",
    "created_at": "2026-07-06 14:33:36",
    "upvotes": 0,
    "downvotes": 0,
    "vote_score": 0,
    "replies": [],
    "reply_count": 0
  }
}
```
**Error (400):** `{ "detail": "rating must be an integer from 1 to 5" }`

#### Get a Single Review
- **URL:** `/reviews/{id}`
- **Method:** `GET`
- **Auth:** Not required.

Returns the review with its vote counts and replies (see the `review` shape
above). **Error (404):** `{ "error": "Review not found" }`

#### Get All Reviews for a Product
- **URL:** `/product/{barcode}/reviews`
- **Method:** `GET`
- **Auth:** Not required.

Returns every review for the product (newest first), plus the `total_reviews`
and the `average_rating`.

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/product/8901491101837/reviews"
```

**Expected JSON Response (200 OK):**
```json
{
  "barcode": "8901491101837",
  "total_reviews": 1,
  "average_rating": 4.0,
  "reviews": [
    {
      "id": 1, "user_id": 29, "username": "rv1_1783348415", "barcode": "8901491101837",
      "rating": 4, "review_text": "Great crunch but too salty for daily snacking.",
      "created_at": "2026-07-06 14:33:36",
      "upvotes": 1, "downvotes": 0, "vote_score": 1,
      "replies": [
        { "id": 1, "user_id": 30, "reply_text": "Agreed, the sodium is high.", "created_at": "2026-07-06 14:33:37", "username": "rv2_1783348415" }
      ],
      "reply_count": 1
    }
  ]
}
```

#### Delete a Review (own only)
- **URL:** `/reviews/{id}`
- **Method:** `DELETE`
- **Headers:** `Authorization: Bearer <token>` (required)

Deletes the review **and** its votes and replies. Only the author may delete it.

**Example using `curl`:**
```bash
curl -X DELETE "http://127.0.0.1:8000/reviews/1" -H "Authorization: Bearer <TOKEN>"
```
**Expected JSON Response (200 OK):** `{ "message": "Review deleted", "review_id": 1 }`
**Error (403):** `{ "detail": "You can only delete your own review" }`

#### Upvote / Downvote a Review *(optional feature)*
- **URL:** `/reviews/{id}/vote`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>` (required)
- **Request Body (JSON):** `vote` — `"up"` or `"down"`.

Re-voting updates the user's existing vote; voting the same direction twice
**removes** it (toggle). Returns the review's updated vote tally.

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/reviews/1/vote" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"vote":"up"}'
```
**Expected JSON Response (200 OK):**
```json
{ "message": "Vote recorded", "review_id": 1, "upvotes": 1, "downvotes": 0, "vote_score": 1 }
```
**Error (400):** `{ "detail": "vote must be 'up' or 'down'" }`

#### Reply to a Review *(optional feature)*
- **URL:** `/reviews/{id}/replies`
- **Method:** `POST`
- **Headers:** `Authorization: Bearer <token>` (required)
- **Request Body (JSON):** `reply_text` (string).

**Example using `curl`:**
```bash
curl -X POST "http://127.0.0.1:8000/reviews/1/replies" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"reply_text":"Agreed, the sodium is high."}'
```
**Expected JSON Response (200 OK):**
```json
{
  "message": "Reply added",
  "reply": { "id": 1, "user_id": 30, "reply_text": "Agreed, the sodium is high.", "created_at": "2026-07-06 14:33:37", "username": "rv2_1783348415" }
}
```

### 26. Personalized Home Feed API
One call that assembles everything the app's **home screen** needs: the user's
recently scanned products, personalized recommendations, the featured weekly
challenge with progress, and the badges they've earned.

- **URL:** `/home-feed`
- **Method:** `GET`
- **Query Parameters:**
  - `user_id` (optional): whose feed to build. Falls back to the authenticated
    user (`Authorization: Bearer <token>`) when omitted.
- **Headers:** `Authorization: Bearer <token>` (optional — an alternative to `user_id`).

**Behaviour**

| Caller | `logged_in` | Content |
|--------|-------------|---------|
| Known user (`user_id` **or** token) | `true`  | last 5 **distinct** scanned products, personalized recommendations, the joined weekly challenge closest to completion (with live progress), and earned badges. |
| Anonymous (neither) | `false` | generic **popular products**, default recommendations, a **preview** of the first weekly challenge (`progress: null`), and no badges. |

**Returned fields**
- `recently_scanned` — up to 5 distinct products (most recent first), each with `barcode`, `product_name`, `brand`, `score`, `grade`, `image_url` (plus `category`, `health_score` and `scanned_at` for convenience). For anonymous callers this is drawn from the shared in-memory recent scans (see `/recent`).
- `recommendations` — same engine as [`/recommendations`](#18-ai-powered-product-recommendations-api) (personalized for a known user, popular for anonymous), each with `barcode`, `product_name`, `brand`, `score`, `grade`, `reason` and `image_url`.
- `challenge_progress` — the featured weekly challenge condensed to `{challenge_name, progress, target}`. `progress` is `0` for an anonymous preview. `null` when there is no active weekly challenge.
- `badges_earned` — the badges the user has won, each `{name, icon, earned_at}` (empty for anonymous). `earned_at` is the date the badge was first completed.

Every product image is guaranteed present: products without an image return the shared placeholder (`/product-images/_placeholder.svg`).

**Example using `curl`:**
```bash
# Personalized (via token)
curl "http://127.0.0.1:8000/home-feed" -H "Authorization: Bearer <TOKEN>"

# Personalized (via explicit user_id)
curl "http://127.0.0.1:8000/home-feed?user_id=48"

# Generic (anonymous)
curl "http://127.0.0.1:8000/home-feed"
```

**Expected JSON Response (200 OK):**
```json
{
  "user_id": 48,
  "logged_in": true,
  "personalized": true,
  "recently_scanned": [
    {"barcode": "8901491101837", "product_name": "Lay's Classic Salted", "brand": "Lay's", "score": 1.1, "grade": "F", "image_url": "/product-images/8901491101837.png"}
  ],
  "recommendations": [
    {"barcode": "8906068720018", "product_name": "Max chocolate protein bar", "brand": "Max protein bar", "score": 5.0, "grade": "C", "reason": "Recommended because it matches your most-scanned category (bar) and is a healthy choice.", "image_url": "/product-images/_placeholder.svg"}
  ],
  "challenge_progress": {
    "challenge_name": "Scan 20 products this week",
    "progress": 12,
    "target": 20
  },
  "badges_earned": [
    {"name": "Health Hunter", "icon": "🔍", "earned_at": "2026-07-08"}
  ]
}
```

> **Note:** `recently_scanned` items also include `category`, `health_score` and
> `scanned_at`, and each response carries `user_id`, `logged_in` and
> `personalized` metadata — supersets of the fields above.

---

### 27. "Swapify Recommended" Badge API
Determines whether a product qualifies for the **Swapify Recommended** badge — a
clean, genuinely healthy pick.

**Criteria**

| Criterion | Requirement | Blocks the badge? |
|-----------|-------------|-------------------|
| `health_score_above_7` | Health score **> 7** | ✅ Yes |
| `no_high_risk_ingredients` | No **Severe** or **High** [risk](#ingredient-risk-levels) flagged ingredients | ✅ Yes |
| `no_artificial_colors` | No synthetic colours (named dyes or INS/E **1xx** codes) | ✅ Yes |
| `no_preservatives` | No chemical preservatives (named, or INS/E **2xx** codes) | ⚪ Optional (reported only) |

A product is `is_recommended: true` only when all three **required** criteria pass.
Preservative-free is reported for transparency but, per spec, does **not** block
the badge on its own.

**Integration with `/product`:** every [`GET /product/{barcode}`](#1-get-product-details-api)
response now includes `is_recommended` (boolean) and a `recommended_badge` object
with the full criteria breakdown.

- **URL:** `/product/{barcode}/badge`
- **Method:** `GET`
- **Headers:** `Authorization: Bearer <token>` (optional — personalizes the score, and therefore the badge, to the user's dietary preferences).

Resolves the product from the local DB first, then Open Food Facts. Returns `404`
with `{"error": "Product not found"}` when the barcode can't be resolved anywhere.

**Example using `curl`:**
```bash
curl "http://127.0.0.1:8000/product/8908013479122/badge"
```

**Expected JSON Response (200 OK):**
```json
{
  "barcode": "8908013479122",
  "product_name": "The whole truth food protein bar",
  "brand": "The whole truth",
  "grade": "C",
  "source": "database",
  "is_recommended": false,
  "badge": null,
  "health_score": 5.0,
  "criteria": {
    "health_score_above_7": false,
    "no_high_risk_ingredients": true,
    "no_artificial_colors": true,
    "no_preservatives": true
  },
  "required_criteria": ["health_score_above_7", "no_high_risk_ingredients", "no_artificial_colors"],
  "failing_criteria": ["health_score_above_7"],
  "high_risk_ingredients": [],
  "has_artificial_colors": false,
  "has_preservatives": false
}
```

When a product **does** qualify, `is_recommended` is `true`, `badge` is
`"Swapify Recommended"` and `failing_criteria` is empty.

---

### 28. Product Image Upload API
Crowdsourced product images. Anyone can contribute a photo for a product; the
uploader is recorded when the request is authenticated. The image bytes are
stored on disk and served back under the `/product-images` static prefix — only
the **URL reference** is stored in the database (on `products.image_url` and in
the `product_images` table).

Every product-returning endpoint (`/product/{barcode}`, `/search`, `/similar`,
`/home-feed`, `/recommendations`) now includes an `image_url`. Products without a
contributed image return the shared **placeholder**
(`/product-images/_placeholder.svg`), so the client never renders an empty box.

- **URL:** `/product/image`
- **Method:** `POST`
- **Content-Type:** `multipart/form-data`
- **Headers:** `Authorization: Bearer <token>` (optional — records the uploader).
- **Form fields:**
  - `barcode` (required): the product barcode the image is for.
  - `file` (required): the image file.
- **Validation:**
  - Format must be **JPEG or PNG** (sniffed from the file's magic bytes, not just
    the declared `Content-Type`) → otherwise `400`.
  - Size must be **under 2 MB** → otherwise `413`.
  - Empty file or missing barcode → `400`.

On success the product's `image_url` is updated (when the product is in the local
catalogue) and the [product cache](PERFORMANCE.md#c-in-memory-caching) is
invalidated, so the new image appears on the very next read.

**Example using `curl`:**
```bash
# Upload a PNG for a product (authenticated)
curl -X POST "http://127.0.0.1:8000/product/image" \
  -H "Authorization: Bearer <TOKEN>" \
  -F "barcode=8901491101837" \
  -F "file=@lays.png;type=image/png"

# Then fetch the served image
curl "http://127.0.0.1:8000/product-images/8901491101837.png" --output lays.png
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Image uploaded successfully",
  "barcode": "8901491101837",
  "image_url": "/product-images/8901491101837.png",
  "product_updated": true,
  "file_size": 40213,
  "content_type": "image/png"
}
```

**Error responses**
| Status | When |
|--------|------|
| `400` | Not a JPEG/PNG, empty file, or missing `barcode`. |
| `413` | Image exceeds the 2 MB limit. |

---

### 29. OCR Label Scanner API (Proof of Concept)

Upload a photo of a product's **ingredient/nutrition label**; the server runs it
through **Tesseract OCR** ([`ocr_label_scanner.py`](src/ocr_label_scanner.py)),
extracts the ingredient list and any nutrition facts, and feeds them into the
**same scoring engine** used for catalogue products — so a label alone yields a
health score, grade and flagged ingredients, **no barcode required**.

> **Proof of concept.** OCR is an *optional* dependency (Tesseract engine +
> `pytesseract` + `Pillow`). When it isn't installed the endpoints report so and
> the rest of the API is unaffected. See [`DEPLOYMENT.md`](DEPLOYMENT.md) §9.

#### Check OCR availability
- **URL:** `/ocr/health`
- **Method:** `GET`
- **Response:**
  ```json
  { "ocr_available": true, "detail": "Tesseract 5.3.3 ready" }
  ```

#### Scan a label
- **URL:** `/ocr/scan-label`
- **Method:** `POST`
- **Body:** `multipart/form-data` with an image `file` (JPEG/PNG, ≤ 2 MB).

```bash
curl -X POST "http://127.0.0.1:8000/ocr/scan-label" \
  -F "file=@label.jpg;type=image/jpeg"
```

**Expected JSON Response (200 OK):**
```json
{
  "message": "Label scanned",
  "raw_text": "Ingredients: Sugar, Maida, Palm Oil, ...",
  "ingredients": ["Sugar", "Maida", "Palm Oil", "Milk Solids", "TBHQ", "Tartrazine"],
  "ingredients_text": "Sugar, Maida, Palm Oil, Milk Solids, TBHQ, Tartrazine",
  "nutrition": { "sugar_g_per_serving": 56.2, "sodium_mg_per_serving": 120.0, "protein_g_per_serving": 6.5 },
  "score": 1.0,
  "grade": "F",
  "rule_version": 1,
  "ingredient_flags": [
    { "name": "sugar", "risk": "High" },
    { "name": "tbhq", "risk": "Severe" },
    { "name": "tartrazine", "risk": "High" }
  ],
  "breakdown": { "...": "full score breakdown" }
}
```

**Error responses**
| Status | When |
|--------|------|
| `400` | Not a JPEG/PNG, or empty file. |
| `413` | Image exceeds the 2 MB limit. |
| `503` | OCR engine not installed (see `GET /ocr/health`). |

---

## Performance & Optimization

The API applies several optimizations (indexes, query trimming + pagination,
in-memory caching, and gzip compression). These are documented in full in
[`PERFORMANCE.md`](PERFORMANCE.md). In short:

- **Indexes** on `products(barcode, product_name, brand, category)` and a
  composite `(product_name, brand)` index, created idempotently at startup.
- **Query optimization**: `/search` selects only the columns it needs (no
  `SELECT *`) and supports `limit` + `offset` pagination.
- **Caching** (`cachetools.TTLCache`, 1-hour TTL): generic product detail scores
  and the top-100 popular-products list, invalidated on update (e.g. image
  upload). ~12× faster on a warm cache locally.
- **Gzip** compression on responses over 500 bytes.

**Benchmark it:** run [`perf_test.py`](perf_test.py) for a before/after
measurement of the search indexes (`python perf_test.py`). On a 40k-row synthetic
catalogue the `product_name` lookup is **~600× faster** with the index
(`EXPLAIN QUERY PLAN` shows `SCAN` → `SEARCH ... USING INDEX`); the script also
verifies the indexes exist and are used on the real `swapify.db`.

---

## Ingredient Risk Levels

Every harmful ingredient is classified into one of four **risk levels**. These are
stored in the database in the `ingredient_rules.risk_level` column (migration
`migrations/002_add_ingredient_risk_level.sql`, populated by
`add_ingredient_risk_levels.py`) and are the single source of truth — the API reads
them at scoring time and returns them in the `ingredient_flags` array of the
`/product`, `/score`, `/v2/score` and `/chat` responses.

| Risk Level | Meaning | Examples |
|------------|---------|----------|
| **Severe** | Banned / strongly linked to harm | TBHQ, Sodium Nitrite, Sodium Nitrate, Potassium Bromate, BHA, Partially Hydrogenated / Vanaspati (trans-fats) |
| **High**   | Refined / strongly discouraged | Maida, Refined Sugar, HFCS, Tartrazine, Sunset Yellow, Titanium Dioxide, Aspartame |
| **Medium** | Consume in moderation | Palm Oil, MSG, Sodium Benzoate, Maltodextrin, Sucralose, Caramel Color IV |
| **Low**    | Low-concern additives | Potassium Sorbate, Natural Flavour, Artificial Flavour |

**`ingredient_flags` format:**
```json
"ingredient_flags": [
  {"name": "sugar", "risk": "High"},
  {"name": "palm oil", "risk": "Medium"},
  {"name": "maida", "risk": "High"},
  {"name": "msg", "risk": "Medium"},
  {"name": "tbhq", "risk": "Severe"}
]
```

Beneficial ingredients (e.g. oats, whey protein) are not assigned a risk level and
do not appear in `ingredient_flags`.

---

### 30. Real-World Experiment Logging API

An append-only log of scans performed by real test devices in the field, plus the
analytics computed over it. Built for the "does this actually work in a shop, on a
real phone?" experiments.

**Why it is separate from the other logs.** `scan_history` is a side effect of a
successful `/product/{barcode}` lookup (catalogue products only), and `/activity`
records in-app behaviour for a *logged-in* user. An experiment log has to accept a
scan from an anonymous phone, record barcodes that aren't in the catalogue (a
failed scan is the most interesting data point), and stay stable no matter how the
product endpoints change. So it gets its own table, `experiment_scan_logs`.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /experiment/log-scan` | none (optional Bearer) | Record one scan from a test device |
| `GET /experiment/logs` | **admin** | Retrieve the log, filtered + paginated |
| `GET /experiment/analytics` | **admin** | Just the counts, without the rows |

#### Admin authentication

The two read endpoints are admin-only — the log is a device-level record. Prove
admin in **either** of two ways:

1. **Shared secret** — send the `X-Admin-Token` header. The value comes from the
   `ADMIN_TOKEN` environment variable.
   ```bash
   curl -H "X-Admin-Token: $ADMIN_TOKEN" "$BASE/experiment/logs"
   ```
2. **Admin user** — log in normally and send the JWT as `Authorization: Bearer
   <token>`. This works only if that user's email is listed in the `ADMIN_EMAILS`
   env var (comma-separated).
   ```bash
   curl -H "Authorization: Bearer $JWT" "$BASE/experiment/logs"
   ```

Anything else gets **403**. If `ADMIN_TOKEN` is not set the app falls back to the
dev token `swapify-admin-dev` and logs a warning at startup — never deploy that.

#### A. Log a scan — `POST /experiment/log-scan`

Deliberately open: a phone in a shop aisle is not logged in. If a Bearer token
*is* present the scan is additionally attributed to that user.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `barcode` | string | **yes** | The scanned code. Not validated against the catalogue — failed/unknown scans are exactly what an experiment wants to capture. |
| `device_type` | string | no | One of `mobile`, `tablet`, `desktop`, `scanner`, `unknown`. **Auto-detected from the User-Agent when omitted.** Common synonyms (`phone`, `android`, `ios`, `ipad`, `laptop`, `pc`) are folded into the canonical buckets; anything unrecognised becomes `unknown` rather than being rejected. |
| `device_info` | string *or* object | no | Free-form. A plain string (`"iPhone 14, iOS 17.4"`) or a JSON object (`{"os":"Android 14","model":"Pixel 8"}`). Objects are stored as JSON and returned as objects. |
| `timestamp` | string (ISO-8601) | no | When the scan happened on the device. Defaults to server time. Normalised to UTC on write, so a phone in another timezone still lands in the right day bucket. An unparseable value falls back to server time (and is logged) rather than failing the request. |
| `device_id` | string | no | Stable per-device ID — this is what `unique_devices` counts. When omitted, a fingerprint is derived by hashing `device_info` + User-Agent. **Send a real `device_id` if you can:** two identical phones with byte-identical User-Agents will otherwise collapse into one device. |
| `notes` | string | no | Free-text field note, e.g. `"aisle 4, poor lighting"`. |

The minimum viable call is just a barcode — everything else is inferred:

```bash
curl -X POST "$BASE/experiment/log-scan" \
  -H "Content-Type: application/json" \
  -d '{"barcode": "8901491101837"}'
```

```json
{
  "message": "Scan logged",
  "log": {
    "id": 1,
    "barcode": "8901491101837",
    "device_type": "mobile",
    "device_info": null,
    "device_id": "fp_05f02457b0badcfa",
    "user_id": null,
    "notes": null,
    "timestamp": "2026-07-13T15:38:59.712171+00:00",
    "created_at": "2026-07-13 15:38:59"
  }
}
```

`device_type` came out as `mobile` because the request carried an iPhone
User-Agent. A fully explicit call from a known device:

```bash
curl -X POST "$BASE/experiment/log-scan" \
  -H "Content-Type: application/json" \
  -d '{
        "barcode": "8901491101837",
        "device_type": "phone",
        "device_id": "dev-pixel-01",
        "device_info": {"os": "Android 14", "browser": "Chrome", "model": "Pixel 8"},
        "timestamp": "2026-07-12T09:15:00Z",
        "notes": "aisle 4, store test"
      }'
```

`"phone"` is normalised to `"mobile"`, and the `Z` timestamp is stored as
`2026-07-12T09:15:00+00:00`.

**Errors:** `400` if `barcode` is missing or empty.

#### B. Retrieve the log — `GET /experiment/logs` *(admin)*

Newest first. All query parameters are optional:

| Param | Default | Notes |
|---|---|---|
| `start_date` | — | Inclusive `YYYY-MM-DD`. |
| `end_date` | — | Inclusive — `end_date=2026-07-13` covers through 23:59:59 on the 13th. |
| `device_type` | — | Same buckets and synonyms as the write path, so `?device_type=phone` works. |
| `barcode` | — | Exact match — every scan of one product. |
| `limit` | `100` | Clamped to 1–500. |
| `offset` | `0` | For paging. |

A malformed date returns **400** (`start_date must be in YYYY-MM-DD format`)
rather than silently matching nothing.

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
  "$BASE/experiment/logs?device_type=mobile&start_date=2026-07-01&end_date=2026-07-13&limit=50"
```

```json
{
  "filters": { "start_date": "2026-07-01", "end_date": "2026-07-13", "device_type": "mobile", "barcode": null },
  "pagination": { "limit": 50, "offset": 0, "returned": 2, "matched": 2, "has_more": false },
  "analytics": {
    "total_scans": 2,
    "unique_devices": 2,
    "unique_barcodes": 1,
    "scans_by_device_type": { "mobile": 2 },
    "top_barcodes": [ { "barcode": "8901491101837", "scans": 2 } ],
    "scans_per_day": [ { "date": "2026-07-12", "scans": 1 }, { "date": "2026-07-13", "scans": 1 } ]
  },
  "logs": [
    {
      "id": 2,
      "barcode": "8901491101837",
      "device_type": "mobile",
      "device_info": { "os": "Android 14", "browser": "Chrome", "model": "Pixel 8" },
      "device_id": "dev-pixel-01",
      "user_id": null,
      "notes": "aisle 4, store test",
      "timestamp": "2026-07-12T09:15:00+00:00",
      "created_at": "2026-07-13 15:38:59"
    }
  ]
}
```

Note that `analytics` is computed over the **filtered** set, not the whole table —
so "mobile scans in the first half of July" reports its own totals, and
`pagination.matched` tells you how many rows matched regardless of `limit`.

#### C. Analytics — `GET /experiment/analytics` *(admin)*

The same three headline counts required for the experiment, without dragging back
thousands of rows to render them. Takes the identical filters as `/experiment/logs`.

| Field | Meaning |
|---|---|
| `total_scans` | Rows matching the filter. |
| `unique_devices` | Distinct `device_id` values (see the fingerprint caveat above). |
| `unique_barcodes` | Distinct products scanned. |
| `scans_by_device_type` | Scan count per device bucket. |
| `top_barcodes` | Ten most-scanned barcodes, descending. |
| `scans_per_day` | Daily scan counts, ascending — the adoption curve. |
| `first_scan_at` / `last_scan_at` | Time span the data covers. |

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" "$BASE/experiment/analytics"
```

```json
{
  "filters": { "start_date": null, "end_date": null, "device_type": null, "barcode": null },
  "first_scan_at": "2026-07-12T09:15:00+00:00",
  "last_scan_at": "2026-07-13T15:38:59.809884+00:00",
  "total_scans": 3,
  "unique_devices": 3,
  "unique_barcodes": 2,
  "scans_by_device_type": { "mobile": 2, "desktop": 1 },
  "top_barcodes": [
    { "barcode": "8901491101837", "scans": 2 },
    { "barcode": "8901719110018", "scans": 1 }
  ],
  "scans_per_day": [
    { "date": "2026-07-12", "scans": 1 },
    { "date": "2026-07-13", "scans": 2 }
  ]
}
```

> **Retention caveat.** On Render's free tier the filesystem is wiped on every
> restart and redeploy, so the log survives only as long as the instance does.
> Export anything you need to keep (`GET /experiment/logs?limit=500`) before
> redeploying. See [`DEPLOYMENT.md`](DEPLOYMENT.md) §11.

## Health Scoring Logic (V2)

The `/v2/score/{barcode}` API uses a rule-based scoring system to evaluate product
health on a scale of 1.0 to 10.0.

### Formula

```
Final Score = (5.0 - Negative Deductions + Positive Additions) x Transparency Multiplier
```

The result is clamped to the range **1.0 – 10.0**.

### 1. Nutrient Penalties & Bonuses (per serving)
- **Sugar:** >=10g (-2), 5-10g (-1)
- **Sodium:** >=400mg (-2), 200-400mg (-1)
- **Saturated Fat:** >=20g (-2), 6-10g (-2), 10-20g (-1), 0-6g (-1)
- **Protein:** >=8g (+1)
- **Fiber:** >=5g (+1)

Sugar, saturated-fat and sodium nutrient penalties are pooled into the same
categories as ingredients (Sugars & Sweeteners, Oils & Fats, Sodium) and share
their caps (see step 3).

### 2. Ingredient Penalties & Position Multipliers
Each ingredient keyword is penalized (or rewarded) and multiplied by its position
in the list:
- **Top 3 ingredients:** x1.5
- **Middle (4th–8th):** x1.0
- **9th onward / trace (index >= 8):** x0.5

**Ingredient penalties (base, before multiplier):**
- **Oils & Fats:** palm oil (-0.6), fractionated fat (-0.7)
- **Refined Carbohydrates:** maida / refined wheat flour (-0.5)
- **Sodium:** salt (-0.6)
- **Flavor Enhancers:** msg (-0.5)
- **Preservatives:** tbhq (-0.8), sodium benzoate (-0.6)
- **Artificial Colors:** tartrazine (-0.7)
- **Sugars & Sweeteners:** sugar (-0.8), corn syrup (-0.6)

**Positive additions (base, before multiplier):**
- **Healthy Fats:** peanuts (+0.4)
- **Protein Quality:** skimmed milk (+0.5), milk solids (+0.5)

Detected harmful ingredients are returned in the `ingredient_flags` list.

### 3. Category Caps (maximum deduction per category)
Caps apply to the **combined** ingredient + nutrient penalty for each category:
- Oils & Fats: -2.5
- Sugars & Sweeteners: -2.5
- Preservatives: -2.0
- Artificial Colors: -2.0
- Sodium: -2.0
- Caffeine & Stimulants: -2.0
- Flavor Enhancers: -1.5
- Emulsifiers & Stabilizers: -1.5
- Other Additives: -1.5
- Refined Carbohydrates: -1.0

Positive additions are **not** capped.

### 4. Transparency Multiplier
Applied to the subtotal before the final clamp:
- **Vague** (contains 'flavouring', 'permitted emulsifier', 'spices', 'edible vegetable oil', etc.): **x0.95**
- **Disclosed** (additives named with INS/E numbers and no vague terms): **x1.05**
- **Default** (no ingredient list / nothing special): **x1.0**

### 5. Final Grade
The clamped score (1.0 – 10.0) maps to a grade:
- **A:** 9.0 – 10.0
- **B:** 7.0 – 8.9
- **C:** 5.0 – 6.9
- **D:** 3.0 – 4.9
- **F:** 1.0 – 2.9

### Worked example — Cadbury Dairy Milk (`7622300441937`)

| Source | Item | Position | Mult | Points |
|--------|------|----------|------|--------|
| Ingredient | Sugar | 1 | x1.5 | -1.20 |
| Ingredient | Fractionated Fat | 5 | x1.0 | -0.70 |
| Nutrient | Sugar (57g) | – | x1.0 | -2.00 |
| Nutrient | Saturated Fat (19.6g) | – | x1.0 | -1.00 |
| Addition | Milk Solids | 2 | x1.5 | +0.75 |

- Sugars & Sweeteners: -1.20 + -2.00 = -3.20 → capped at **-2.50**
- Oils & Fats: -0.70 + -1.00 = **-1.70** (within cap)
- Subtotal: 5.0 - 4.20 + 0.75 = **1.55**
- Transparency: **x0.95** ("Flavours" is vague)
- **Final: 1.55 × 0.95 = 1.5 (F)**

## Personalized Scoring

The generic score above is the same for everyone. When a request is
**authenticated** (or, for `/similar`, identifies a user via `?user_id=`), the
scoring engine applies that user's saved [dietary preferences](#12-dietary-preferences-api)
as **weight multipliers**, so the same product can score differently for two
users. With no preferences the weights are all-neutral (×1.0) and the score is
identical to the generic one.

### Which endpoints are personalized?
`/product/{barcode}`, `/score/{barcode}`, `/v2/score/{barcode}` (when
authenticated), `/similar/{barcode}` (authenticated **or** `?user_id=`), and the
already user-scoped `/history`, `/favorites`, `/weekly-summary`.

The scoring function itself,
`calculate_health_score_v2(product, version=1, preferences=None, user_id=None)`,
accepts **either** an explicit `preferences` dict **or** a `user_id` (from which
it loads the stored preferences); with neither it returns the generic score.

### How preferences re-weight the score
A preference adjusts the relevant **penalty** or **bonus** (and, for penalties,
the matching category cap is scaled by the same factor so the heavier penalty is
not immediately swallowed):

| Preference | Adjustment |
|-----------|------------|
| `low_sugar` | Sugar nutrient penalty **×1.75**, "Sugars & Sweeteners" ingredient penalty + cap **×1.75** |
| `low_sodium` | Sodium nutrient penalty **×1.75**, "Sodium" ingredient penalty + cap **×1.75** |
| `low_fat` | Saturated-fat penalty **×1.75**, "Oils & Fats" ingredient penalty + cap **×1.75** |
| `high_protein` | Protein nutrient bonus **×2.5** |
| `high_fiber` | Fiber nutrient bonus **×2.5** |
| `vegan` | Dairy-derived "Protein Quality" ingredient bonuses are dropped |

Personalized responses include a `preferences_applied` object (in the `/product`
response and in the score `breakdown`) listing the active flags.

### Worked example — Cadbury Dairy Milk for a `low_sugar` user
Building on the generic example above, `low_sugar` weights the **sugar** terms by
×1.75 (and the "Sugars & Sweeteners" cap by ×1.75 → -4.375):

- Sugars & Sweeteners: (-1.20 + -2.00) × 1.75 = **-5.60** → capped at **-4.375**
- Oils & Fats: **-1.70** (unchanged, within cap)
- Subtotal: 5.0 - 6.075 + 0.75 = **-0.325**
- Transparency: **×0.95**, then clamped to the 1.0 floor
- **Final: 1.0 (F)** — down from the generic 1.5, reflecting the user's stricter
  view of sugar.

