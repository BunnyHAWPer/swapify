# Performance Test Report

**Date:** 2026-07-14 · **Build:** local `main` (not yet deployed) · **DB:** SQLite, 102 products

Reproduce with:

```bash
python perf_endpoints.py --base http://127.0.0.1:8000   # endpoint latency + cache proof
python perf_test.py                                     # index behaviour in isolation
python validate_barcodes.py                             # catalogue integrity
```

---

## Summary

Three real problems found and fixed. Two were invisible in ordinary testing because the
catalogue is small — they only bite as data grows, which is exactly when you can't afford
them.

| # | Problem | Fix | Result |
| --- | --- | --- | --- |
| 1 | `scan_history` and `favorites` had **no indexes at all** | Migration 007 | `/history` **54x** faster; popularity query **9.6x** |
| 2 | `/leaderboard` ran an **N+1** (a query per user) and was the slowest endpoint by 10x | Batched query + 60s cache | **29ms → 0.002ms** server-side (**33.7x** over HTTP) |
| 3 | Cache health was **unmeasurable** — no hit/miss counters | `/cache-stats` + `/admin/cache-clear` | Hit rate now **98.1%**, verified |

All 13 measured endpoints are under 300ms p95. Nothing is currently slow enough to be
worth further optimisation.

---

## 1. The indexes were on the wrong table

Migration 006 indexed `products` — a table that holds ~100 rows and is rebuilt from a CSV.
Indexing it is nearly free but also nearly pointless: SQLite scans 100 rows faster than it
can consult an index.

Meanwhile **`scan_history` — which gains a row on every single scan, forever — had no index
at all**, and neither did `favorites`. So the two tables that actually grow were the two
tables doing full scans.

`EXPLAIN QUERY PLAN` before the fix:

```
GET /history      SCAN h                                    <-- full table scan
                  USE TEMP B-TREE FOR ORDER BY              <-- and then sorts it all
popular/home-feed SEARCH h USING AUTOMATIC COVERING INDEX   <-- see below
```

`AUTOMATIC COVERING INDEX` is SQLite telling you it is **building a missing index from
scratch, on every single request**, then throwing it away. It is the query planner working
around a schema gap at runtime.

Measured at **200,000 `scan_history` rows** (`bench_idx`, 200 iterations, p50):

| Query | Before | After | Speedup |
| --- | --- | --- | --- |
| `GET /history` (per user) | 18.73 ms | 0.35 ms | **54.3x** |
| popularity join (`/home-feed`, `/recommendations`) | 260.13 ms | 27.12 ms | **9.6x** |
| `GET /favorites` (per user) | 1.49 ms | 0.82 ms | 1.8x |

Cost: 642 ms one-time index build; +22 MB on a 200k-row DB. Both trivially worth it.

The composite `(user_id, scanned_at DESC)` is what removes the temp B-tree — it covers the
`WHERE` and the `ORDER BY` together, so the rows come out of the index already sorted.

**Applied via `migrations/007_index_growth_tables.sql`, and automatically at app start by
`ensure_performance_and_image_schema()` — so the deploy needs no manual step.**

---

## 2. `/leaderboard` — an N+1, then a cache

`/leaderboard` was **~10x slower than any other endpoint** (52–65 ms p50 over HTTP; 27.9 ms
of genuine server time). Two causes:

**The N+1.** It fetched the top N users in one query, then looped over them issuing *another
query per user* for their activity breakdown. At the default `limit=10` that is 10 extra
round-trips to compute numbers a single `GROUP BY user_id, action_type` produces at once.
Fixed by batching.

**The badges.** `get_user_badges()` opens its **own database connection per user**, and
evaluates live challenge progress inside — a second N+1 nested in the challenge logic.
Profiling put this at **48% of the endpoint**:

```
leaderboard total     : 26.887 ms
get_user_badges x10   : 13.006 ms   <- 48% of the endpoint
```

Untangling that means restructuring `compute_challenge_progress`, which is a risky refactor
for a modest win. A leaderboard is a slowly-changing aggregate read far more often than it
changes — the textbook case for a cache. A 60-second TTL makes it never more than a minute
stale (invisible to a user) and turns the endpoint into a dict lookup:

| | cold (cache miss) | warm (cache hit) | speedup |
| --- | --- | --- | --- |
| `get_leaderboard()` server-side | 29.08 ms | **0.002 ms** | 14,539x |
| `GET /leaderboard` over HTTP | 53.91 ms | **1.60 ms** | 33.7x |

Verified the cached payload is **byte-identical** to a freshly computed one, for both
`all-time` and `weekly` — a performance fix that changes results is just a bug.

The badges N+1 is still there behind the cache. It is documented rather than hidden: if the
board ever needs a sub-second TTL, that is the thing to fix next.

---

## 3. Caching: is it actually working?

It was impossible to say. The caches had no counters, and **a fast endpoint proves nothing**
— it might simply be fast. A cache that never hits and a cache that always hits return
identical response bodies.

Added `GET /cache-stats` (hit/miss/hit-rate/entries per cache) and `POST /admin/cache-clear`
(admin-gated), which lets `perf_endpoints.py` force a *genuinely* cold cache and then confirm
the warm request was served by a real **hit**, not a lucky fast miss.

| Path | Cache | Cold | Warm | Speedup | Real cache hit? |
| --- | --- | --- | --- | --- | --- |
| `/product/{barcode}` | `product_cache` | 17.97 ms | 10.20 ms | 1.8x | **yes** |
| `/home-feed` | `popular_cache` | 11.51 ms | 30.39 ms | — | **yes** |
| `/leaderboard` | `leaderboard_cache` | 53.91 ms | 1.60 ms | 33.7x | **yes** |
| `/score/{barcode}` | *(none)* | 19.03 ms | 4.09 ms | — | **no — not cached** |

**Product cache hit rate: 98.1%** (155 hits / 3 misses), 0.002 ms when warm. The caches work.

Two honest caveats in that table:

- **`/home-feed` warm looks slower than cold.** It isn't — the popular-cache *hit* is
  confirmed. The number is drowned in HTTP measurement noise (see below); server-side,
  `get_popular_products_cached()` is 0.002 ms warm.
- **`/score` is not cached at all.** It re-reads the DB and recomputes the full score on
  every call, even though a cached scored payload for that exact barcode already exists
  (populated by `/product`). At ~4 ms it is not a bottleneck, so it is **reported, not
  fixed** — the tidy fix has to respect that `/score` is preference-aware and currently
  404s for products only found in Open Food Facts, and neither subtlety is worth risking
  for 4 ms.

---

## Endpoint latency (all 13 measured)

50 iterations each, local loopback, gzip enabled.

| Endpoint | p50 | p95 | Bytes |
| --- | --- | --- | --- |
| `/ping` | 15.12 | 25.97 | 38 |
| `/health` | 15.34 | 28.49 | 236 |
| `/product/{barcode}` | 15.34 | 29.91 | 931 |
| `/product` (payload fallback) | 14.57 | 28.76 | 966 |
| `/score/{barcode}` | 3.94 | 28.64 | 578 |
| `/search?q=` (text) | 13.79 | 25.20 | 334 |
| `/search?q=` (barcode) | 3.42 | 28.46 | 294 |
| `/search/autocomplete` | 9.38 | 26.58 | 277 |
| `/similar/{barcode}` | 3.89 | 26.46 | 2 |
| `/home-feed` | 30.65 | 33.32 | 631 |
| `/recommendations` | 15.31 | 22.74 | 590 |
| **`/leaderboard`** | **1.60** | **15.92** | 301 |
| `/offline-products` | 30.42 | 32.52 | 3689 |

**Read these numbers with care.** `/ping` does no work at all and still measures ~15 ms p50 /
~26 ms p95 — that floor is per-request TCP connect overhead on Windows loopback (the harness
opens a fresh connection each call, no keep-alive), *not* server time. It swamps everything
below ~30 ms. The server-side profile is the honest view:

```
get_leaderboard (cold)          27.943 ms   <- was the only real hotspot
search_products('chocolate')     1.652 ms
get_user_badges (one user)       1.294 ms
generic_scored_product (warm)    0.002 ms   <- cache working
get_popular_products_cached      0.002 ms   <- cache working
```

---

## Known remaining costs (deliberately not fixed)

1. **`/search` text queries are a full table scan.** `LIKE '%term%'` has a leading wildcard,
   which no B-tree index can serve — `idx_products_name_brand` cannot help it. At 102
   products this is 1.65 ms and irrelevant. If the catalogue reaches tens of thousands, the
   answer is an FTS5 virtual table, not another index.
2. **The badges N+1 inside `/leaderboard`** (48% of the uncached cost), now masked by the
   60s cache. Fix if the board ever needs a shorter TTL.
3. **`/score` bypasses the product cache** (~4 ms). See above.
4. **`similar/{barcode}` returned an empty array** for the test barcode. Not a performance
   issue, but worth a correctness look — it may be a category with only one member.
5. **Cache counters are per-worker.** Production runs 2 gunicorn workers, so `/cache-stats`
   reflects whichever worker answered. Read it as a sample, not a global total.

---

## Caveat on scale

The catalogue is 102 products, so most endpoint timings are dominated by fixed overhead
rather than data volume. The index results were therefore measured against a **synthetic
200,000-row `scan_history`**, because that is the table that actually grows in production and
the only way to see what these queries cost once real usage accumulates. The numbers in §1
are the ones that will matter in six months; the numbers in §4 mostly measure the network.
