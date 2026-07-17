#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$SCRIPT_DIR"
BASE="${BASE_URL:-http://127.0.0.1:8000}"
PY="$SERVER_DIR/venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python"        
DB="$SERVER_DIR/swapify.db"
DELAY="${DELAY:-0}"
STARTED_SERVER=0
SERVER_PID=""
LOG="$SERVER_DIR/.test_server.log"
IMG_DIR="$SERVER_DIR/.test_images"     # throwaway images for the upload tests

# ---- colors -----------------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; RESET=$'\e[0m'
  RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BLUE=$'\e[34m'; CYAN=$'\e[36m'; MAGENTA=$'\e[35m'
else
  BOLD=""; DIM=""; RESET=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; MAGENTA=""
fi

PASS=0; FAIL=0

# ---- helpers ----------------------------------------------------------------
banner() {
  echo
  echo "${BOLD}${MAGENTA}==============================================================================${RESET}"
  echo "${BOLD}${MAGENTA}  $1${RESET}"
  echo "${BOLD}${MAGENTA}==============================================================================${RESET}"
}

section() {   # section <n> <title>
  echo
  echo "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo "${BOLD}${CYAN}  [$1]  $2${RESET}"
  echo "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

# pretty-print JSON (jq not installed, so use python). Optional 2nd arg = show
# only the first N items of a top-level array (keeps huge lists readable).
SHOW_JSON_PROG='
import sys, json
raw = sys.stdin.read()
head = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    d = json.loads(raw)
except Exception:
    print(raw); sys.exit()
if head and isinstance(d, list):
    n = int(head)
    print(f"[array with {len(d)} items - showing first {n}]")
    print(json.dumps(d[:n], indent=2, ensure_ascii=False))
else:
    print(json.dumps(d, indent=2, ensure_ascii=False))
'
show_json() {
  # NOTE: use `python -c` (not a heredoc) so the piped body stays on stdin.
  # A heredoc would redirect stdin and the body would be lost.
  printf '%s' "$1" | "$PY" -c "$SHOW_JSON_PROG" "${2:-}" 2>/dev/null || printf '%s\n' "$1"
}

# core request runner.
#   request <METHOD> <PATH> <DATA|""> <auth|noauth> [headN] [expectPrefix]
# stores raw response body in global LAST_BODY and status in LAST_CODE.
# expectPrefix defaults to "2" (any 2xx = pass); pass "4" for tests that are
# meant to return a client error (e.g. rating validation) so they still count
# as a pass when the expected status is returned.
LAST_BODY=""; LAST_CODE=""
request() {
  local method="$1" path="$2" data="$3" auth="$4" head="${5:-}" expect="${6:-2}"
  local url="$BASE$path"
  local -a args=(-s -S -X "$method" -m 60 -w $'\n__HTTP__%{http_code}')
  local shown="curl -X $method '$url'"

  if [ "$auth" = "auth" ]; then
    args+=(-H "Authorization: Bearer $TOKEN")
    shown="$shown \\
       -H 'Authorization: Bearer <TOKEN>'"
  fi
  if [ -n "$data" ]; then
    args+=(-H "Content-Type: application/json" -d "$data")
    shown="$shown \\
       -H 'Content-Type: application/json' \\
       -d '$data'"
  fi

  echo "${DIM}\$ $shown${RESET}"
  local raw; raw="$(curl "${args[@]}" "$url")"
  LAST_CODE="${raw##*__HTTP__}"
  LAST_BODY="${raw%$'\n'__HTTP__*}"

  local col="$GREEN"
  case "$LAST_CODE" in
    2*) col="$GREEN" ;;
    4*) col="$YELLOW" ;;
    *)  col="$RED" ;;
  esac
  echo "${col}${BOLD}HTTP $LAST_CODE${RESET}"
  show_json "$LAST_BODY" "$head"

  # tally: a status matching the expected prefix (default 2xx) is a pass. Tests
  # that deliberately expect a client error pass "4" as the expected prefix.
  case "$LAST_CODE" in
    ${expect}*) PASS=$((PASS+1)) ;;
    *)          FAIL=$((FAIL+1)) ;;
  esac
  [ "$DELAY" != "0" ] && sleep "$DELAY"
  return 0
}

json_get() {  # json_get <field> <<< body   -> extract top-level string/number
  local field="$1"
  "$PY" -c "import sys,json;
d=json.load(sys.stdin);
v=d.get('$field') if isinstance(d,dict) else None;
print('' if v is None else v)"
}

# multipart/form-data upload runner (Task 2C — image upload). The generic
# `request` helper only sends JSON, so image uploads use this. We cd into the
# file's directory and pass a *relative* filename so this works on both Git Bash
# (where curl.exe can't open an MSYS /c/... path inside a -F @file;type= arg) and
# native Linux/macOS.
#   request_upload <path> <barcode> <file> <content_type> <auth|noauth> [expectPrefix]
request_upload() {
  local path="$1" bc="$2" file="$3" ctype="$4" auth="$5" expect="${6:-2}"
  local url="$BASE$path" dir base
  dir="$(dirname "$file")"; base="$(basename "$file")"
  local -a hdr=()
  [ "$auth" = "auth" ] && hdr=(-H "Authorization: Bearer $TOKEN")
  echo "${DIM}\$ curl -X POST '$url' \\
       -F 'barcode=$bc' \\
       -F 'file=@$base;type=$ctype'${RESET}"
  local raw
  raw="$(cd "$dir" && curl -s -S -m 60 -w $'\n__HTTP__%{http_code}' "${hdr[@]}" \
         -F "barcode=$bc" -F "file=@$base;type=$ctype" "$url")"
  LAST_CODE="${raw##*__HTTP__}"; LAST_BODY="${raw%$'\n'__HTTP__*}"
  local col="$GREEN"
  case "$LAST_CODE" in 2*) col="$GREEN";; 4*) col="$YELLOW";; *) col="$RED";; esac
  echo "${col}${BOLD}HTTP $LAST_CODE${RESET}"
  show_json "$LAST_BODY"
  case "$LAST_CODE" in ${expect}*) PASS=$((PASS+1));; *) FAIL=$((FAIL+1));; esac
}

# Gzip check (Task 1D). Passes when the server returns Content-Encoding: gzip for
# a large response requested with Accept-Encoding: gzip.
check_gzip() {  # check_gzip <path>
  local url="$BASE$1"
  echo "${DIM}\$ curl -H 'Accept-Encoding: gzip' -D - -o /dev/null '$url'${RESET}"
  local enc
  enc="$(curl -s -H 'Accept-Encoding: gzip' -D - -o /dev/null "$url" \
         | tr -d '\r' | grep -i '^content-encoding:' | awk '{print tolower($2)}')"
  if [ "$enc" = "gzip" ]; then
    echo "${GREEN}${BOLD}Content-Encoding: gzip  ✓  (response compressed)${RESET}"
    PASS=$((PASS+1))
  else
    echo "${RED}${BOLD}Expected gzip, got: '${enc:-none}'${RESET}"
    FAIL=$((FAIL+1))
  fi
}

# image_url presence check on a product-list response (Task 2B). <mode> is
# 'array' (list of results) or 'field' (single object). Passes when every item
# carries a non-empty image_url.
check_image_url() {  # check_image_url <mode>
  printf '%s' "$LAST_BODY" | "$PY" -c "
import sys,json
mode=sys.argv[1]
d=json.load(sys.stdin)
items=d if isinstance(d,list) else [d]
miss=[i for i,it in enumerate(items) if not (isinstance(it,dict) and it.get('image_url'))]
if not items:
    print('   (no items to check)'); sys.exit(0)
if miss:
    print('   MISSING image_url on items:',miss); sys.exit(1)
print('   image_url present on all %d item(s), e.g. %s' % (len(items), items[0].get('image_url')))
" "$1" && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
}

# Generate throwaway test images once: a valid 1x1 PNG, a text file with a .png
# name (rejected on content), and a >2 MB file (rejected on size).
make_test_images() {
  mkdir -p "$IMG_DIR"
  "$PY" - "$IMG_DIR" <<'PYEOF'
import sys, struct, zlib, os
d = sys.argv[1]
def chunk(t, x):
    c = t + x
    return struct.pack('>I', len(x)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
png = (b'\x89PNG\r\n\x1a\n'
       + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00'))
       + chunk(b'IEND', b''))
open(os.path.join(d, 'valid.png'), 'wb').write(png)
open(os.path.join(d, 'not_image.png'), 'wb').write(b'this is plain text, definitely not an image')
open(os.path.join(d, 'too_big.png'), 'wb').write(png + b'\x00' * (2 * 1024 * 1024 + 64))
print('test images ready in', d)
PYEOF
}

db_query() {  # db_query "<SQL>"  -> prints rows
  "$PY" - "$DB" "$1" <<'PYEOF'
import sys, sqlite3
db, sql = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
try:
    for r in con.execute(sql):
        print("   " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
except Exception as e:
    print("   (query error:", e, ")")
con.close()
PYEOF
}

cleanup() {
  rm -rf "$IMG_DIR" 2>/dev/null    # remove throwaway upload test images
  if [ "$STARTED_SERVER" = "1" ] && [ -n "$SERVER_PID" ]; then
    echo
    echo "${DIM}Stopping test server (pid $SERVER_PID)...${RESET}"
    kill "$SERVER_PID" 2>/dev/null
    # give it a moment, then hard-kill any survivor on the port
    ( sleep 1; kill -9 "$SERVER_PID" 2>/dev/null ) >/dev/null 2>&1 &
  fi
}
trap cleanup EXIT

# =============================================================================
banner "SWAPIFY API TEST SUITE"
echo "Base URL : $BASE"
echo "Python   : $PY"
echo "Database : $DB"

# ---- make sure the server is running ----------------------------------------
if curl -s -m 3 "$BASE/health" >/dev/null 2>&1; then
  echo "${GREEN}Server already running.${RESET}"
else
  if [ "${NO_AUTOSTART:-0}" = "1" ]; then
    echo "${RED}Server not reachable at $BASE and NO_AUTOSTART=1. Start it first:${RESET}"
    echo "   cd server/src && ../venv/Scripts/python.exe -m uvicorn app:app --port 8000"
    exit 1
  fi
  echo "${YELLOW}Server not running — starting it...${RESET}"
  ( cd "$SERVER_DIR/src" && "$PY" -m uvicorn app:app --host 127.0.0.1 --port 8000 ) \
      >"$LOG" 2>&1 &
  SERVER_PID=$!
  STARTED_SERVER=1
  # wait for /health (up to ~25s)
  for i in $(seq 1 50); do
    if curl -s -m 2 "$BASE/health" >/dev/null 2>&1; then
      echo "${GREEN}Server is up (pid $SERVER_PID).${RESET}"
      break
    fi
    sleep 0.5
    if [ "$i" = "50" ]; then
      echo "${RED}Server failed to start. Last log lines:${RESET}"
      tail -n 30 "$LOG"
      exit 1
    fi
  done
fi

# capture DB baseline counts (to prove writes later)
BASE_USERS=$(db_query "SELECT COUNT(*) AS c FROM users" | grep -o 'c=[0-9]*' | cut -d= -f2)
BASE_SCANS=$(db_query "SELECT COUNT(*) AS c FROM scan_history" | grep -o 'c=[0-9]*' | cut -d= -f2)
BASE_REPORTS=$(db_query "SELECT COUNT(*) AS c FROM missing_reports" | grep -o 'c=[0-9]*' | cut -d= -f2)

# =============================================================================
#  TEST DATA
# =============================================================================
STAMP=$(date +%s)
EMAIL="tester_${STAMP}@example.com"
USERNAME="tester_${STAMP}"
PASSWORD="Passw0rd!"

# real barcodes present in swapify.db
BC_UNHEALTHY="8901491101837"   # Lay's Classic Salted
BC_HEALTHY="8908013479122"     # The Whole Truth protein bar
BC_BAR="8906127540016"         # Farmley Datebites (protein_bar -> has same-cat alternatives)
BC_BAR2="8904335602385"        # Yoga bar protein bar
BC_SAUCE="8901595862962"       # Ching's Schezwan Chutney (sauce -> no cross-cat noodles!)
BC_OFF="3017620422003"         # Nutella -> NOT in DB, tests Open Food Facts fallback

# =============================================================================
section 0 "HEALTH CHECK  (GET /health)"
request GET "/health" "" noauth

section "0b" "PRODUCT COUNT  (GET /product-count)  ->  live curated count + coverage (Task 3)"
request GET "/product-count" "" noauth

section 1 "REGISTER USER  (POST /register)  ->  writes users table"
request POST "/register" "{\"email\":\"$EMAIL\",\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" noauth

section 2 "LOGIN  (POST /login)  ->  returns JWT access_token"
request POST "/login" "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" noauth
TOKEN="$(printf '%s' "$LAST_BODY" | json_get access_token)"
if [ -n "$TOKEN" ]; then
  echo "${GREEN}Got token:${RESET} ${TOKEN:0:32}...(${#TOKEN} chars)"
else
  echo "${RED}NO TOKEN — authenticated tests below will fail.${RESET}"
fi

section 3 "PROFILE  (GET /profile)  [auth]"
request GET "/profile" "" auth
USER_ID="$(printf '%s' "$LAST_BODY" | json_get id)"
echo "${BLUE}user_id = $USER_ID${RESET}"

section 4 "PRODUCT LOOKUP (local DB)  (GET /product/{barcode})  [auth -> records scan]"
echo "${DIM}# Scanning 3 products while authenticated so they land in scan_history${RESET}"
request GET "/product/$BC_UNHEALTHY" "" auth
request GET "/product/$BC_HEALTHY"   "" auth
request GET "/product/$BC_BAR"       "" auth

section 5 "PRODUCT LOOKUP (Open Food Facts fallback)  (GET /product/{barcode})"
echo "${DIM}# $BC_OFF is NOT in the local DB -> server fetches live from Open Food Facts${RESET}"
request GET "/product/$BC_OFF" "" auth

section 6 "HEALTH SCORE v1  (GET /score/{barcode})"
request GET "/score/$BC_UNHEALTHY" "" noauth

section 7 "HEALTH SCORE v2  (GET /v2/score/{barcode})  [personalized when auth]"
request GET "/v2/score/$BC_HEALTHY" "" auth

section 8 "BETTER ALTERNATIVES  (GET /similar/{barcode})  [personalized]"
request GET "/similar/$BC_BAR" "" auth
echo "${DIM}All alternatives above must share the SAME category as the scanned product (Task 2).${RESET}"

section "8b" "BETTER ALTERNATIVES — category match (Task 2)  (GET /similar/{sauce})  ->  NO noodles"
echo "${DIM}Schezwan Chutney (category 'sauce') must NOT return Maggi (noodles). It has no${RESET}"
echo "${DIM}same-category peer, so the correct answer is an empty list — never a grab-bag.${RESET}"
request GET "/similar/$BC_SAUCE" "" noauth

section 9 "SET PREFERENCES  (POST /preferences)  [auth]  ->  writes user_preferences"
request POST "/preferences" '{"preferences":{"high_protein":true,"low_sugar":true,"vegan":false}}' auth

section 10 "GET PREFERENCES  (GET /preferences)  [auth]"
request GET "/preferences" "" auth

section 11 "UPDATE PREFERENCES (alias)  (POST /update-preferences)  [auth]"
request POST "/update-preferences" '{"low_sodium":true,"high_fiber":true}' auth

section 12 "BETTER ALTERNATIVES — re-ranked by NEW preferences  (GET /similar/{barcode})"
request GET "/similar/$BC_BAR" "" auth

section 13 "ADD FAVORITE  (POST /favorites)  [auth]  ->  writes favorites"
request POST "/favorites" "{\"barcode\":\"$BC_HEALTHY\"}" auth

section 14 "LIST FAVORITES  (GET /favorites)  [auth]"
request GET "/favorites" "" auth

section 15 "REMOVE FAVORITE  (DELETE /favorites/{barcode})  [auth]"
request DELETE "/favorites/$BC_HEALTHY" "" auth

section 16 "SCAN HISTORY  (GET /history)  [auth]  <- proves scans from step 4 saved"
request GET "/history" "" auth

section 17 "WEEKLY SUMMARY  (GET /weekly-summary)  [auth]"
request GET "/weekly-summary" "" auth

section 18 "MONTHLY REPORT  (GET /monthly-report)  [auth]"
request GET "/monthly-report" "" auth

section 19 "RECENT SCANS (in-memory)  (GET /recent)"
request GET "/recent" "" noauth

section 20 "COMPARE TWO PRODUCTS  (GET /compare/{b1}/{b2})"
request GET "/compare/$BC_UNHEALTHY/$BC_HEALTHY" "" noauth

section 21 "COMPARE MULTIPLE (2-4)  (POST /compare-multiple)"
request POST "/compare-multiple" "{\"barcodes\":[\"$BC_BAR\",\"$BC_HEALTHY\",\"$BC_BAR2\",\"$BC_OFF\"]}" auth

section 22 "OFFLINE PRODUCTS (full catalogue)  (GET /offline-products)"
request GET "/offline-products" "" noauth 2

section 23 "SEARCH  (GET /search?q=protein)"
request GET "/search?q=protein" "" noauth 3

section 24 "REPORT MISSING PRODUCT  (POST /report-missing)  [auth]  ->  writes missing_reports"
request POST "/report-missing" '{"barcode":"0000000000000","product_name":"Mystery Snack","comment":"Not in DB, please add"}' auth

section 25 "AI NUTRITIONIST — general question  (POST /chat)  [uses OpenRouter key]"
request POST "/chat" '{"question":"Is a diet high in saturated fat bad for my heart?"}' noauth
CHAT_SOURCE="$(printf '%s' "$LAST_BODY" | json_get source)"
echo "${BLUE}chat source = $CHAT_SOURCE  (openrouter = real AI, fallback = rule-based)${RESET}"

section 26 "AI NUTRITIONIST — with product context  (POST /chat + barcode)"
request POST "/chat" "{\"question\":\"Should I eat this often?\",\"barcode\":\"$BC_UNHEALTHY\"}" noauth

section 27 "AI NUTRITIONIST — ingredient substitution  (POST /chat)  -> substitutions[]"
request POST "/chat" '{"question":"What can I use instead of sugar in baking?"}' noauth

section "27a" "AI CHAT — greeting fast-path (Task 1)  (POST /chat 'hi')  ->  source 'fast-path', instant"
echo "${DIM}A bare greeting must NOT hit the LLM (no ~25s wait). Expect source=fast-path and a${RESET}"
echo "${DIM}sub-second response — watch the HTTP timing.${RESET}"
request POST "/chat" '{"question":"hi"}' noauth
echo "${BLUE}fast-path source = $(printf '%s' "$LAST_BODY" | json_get source)  (expected: fast-path)${RESET}"

section "27b" "AI CHAT — structured top picks (Task 4)  (POST /chat)  ->  top_picks[] via 7+ rule"
echo "${DIM}Must return a structured top_picks[] array (score/grade/recommended/category) built${RESET}"
echo "${DIM}from the real scored catalogue — not a generic paragraph.${RESET}"
request POST "/chat" '{"question":"what are the top picks from all products"}' noauth

section "27c" "AI CHAT — top picks by category (Task 4)  (POST /chat 'best chocolates')"
request POST "/chat" '{"question":"what are the best chocolates"}' noauth

# =============================================================================
#  TASK 1 — CROWDSOURCED PRODUCT RATINGS
# =============================================================================
section 28 "SUBMIT RATING  (POST /rate-product)  [auth]  ->  writes product_ratings"
request POST "/rate-product" "{\"barcode\":\"$BC_HEALTHY\",\"taste_rating\":5,\"quality_rating\":4,\"value_rating\":4}" auth

section 29 "SUBMIT RATING — 2nd product  (POST /rate-product)  [auth]"
request POST "/rate-product" "{\"barcode\":\"$BC_UNHEALTHY\",\"taste_rating\":3,\"quality_rating\":2,\"value_rating\":3}" auth

section 30 "UPDATE RATING — re-rate same product  (POST /rate-product)  [auth]  ->  \"Rating updated\""
echo "${DIM}# Re-rating $BC_HEALTHY overwrites the previous rating (never double-counts)${RESET}"
request POST "/rate-product" "{\"barcode\":\"$BC_HEALTHY\",\"taste_rating\":4,\"quality_rating\":5,\"value_rating\":5}" auth

section 31 "RATING VALIDATION — star out of range  (POST /rate-product)  [auth]  ->  expect HTTP 400"
echo "${DIM}# taste_rating=9 is invalid (must be 1-5) — the endpoint should reject it${RESET}"
request POST "/rate-product" "{\"barcode\":\"$BC_HEALTHY\",\"taste_rating\":9,\"quality_rating\":3,\"value_rating\":3}" auth "" 4

section 32 "PRODUCT AVERAGE RATINGS  (GET /product/{barcode}/ratings)  [public]"
request GET "/product/$BC_HEALTHY/ratings" "" noauth

section 33 "USER'S OWN RATINGS  (GET /user/ratings)  [auth]  <- proves ratings from 28-30 saved"
request GET "/user/ratings" "" auth

# =============================================================================
#  TASK 2 — AI-POWERED PRODUCT RECOMMENDATIONS
# =============================================================================
section 34 "RECOMMENDATIONS — personalized  (GET /recommendations)  [auth]"
echo "${DIM}# Uses this user's scan history, preferences, comparisons + community ratings${RESET}"
request GET "/recommendations" "" auth 3
request GET "/recommendations?limit=5" "" auth 3

section 35 "RECOMMENDATIONS — generic popular  (GET /recommendations)  [anonymous]"
request GET "/recommendations" "" noauth 3

# =============================================================================
#  TASK 3 — SHAREABLE SCORE CARD
# =============================================================================
section 36 "SHARE CARD  (GET /share/{barcode})  [local product]"
request GET "/share/$BC_UNHEALTHY" "" noauth

section 37 "SHARE CARD  (GET /share/{barcode})  [Open Food Facts fallback -> has image_url]"
request GET "/share/$BC_OFF" "" noauth

# =============================================================================
#  TASK — PRODUCT BARCODE VALIDATION & CORRECTION
# =============================================================================
section 38 "VALIDATE BARCODE — valid EAN-13  (GET /validate-barcode/{barcode})"
request GET "/validate-barcode/$BC_UNHEALTHY" "" noauth

section 39 "VALIDATE BARCODE — invalid check digit  (GET /validate-barcode/{barcode})  -> suggestion"
echo "${DIM}# 8901491101830 has a wrong check digit; the API suggests 8901491101837${RESET}"
request GET "/validate-barcode/8901491101830" "" noauth

section 40 "VALIDATE BARCODE — non-numeric  (GET /validate-barcode/{barcode})"
request GET "/validate-barcode/abc123" "" noauth

section 41 "SEARCH BY BARCODE — auto-corrects a mistyped check digit  (GET /search?q=)"
echo "${DIM}# q is a barcode with a bad check digit; search still finds the product${RESET}"
request GET "/search?q=8901491101830" "" noauth

section 42 "PRODUCT LOOKUP — unknown malformed barcode  (GET /product/{barcode})  -> 404 + suggestion"
request GET "/product/9999999999998" "" noauth "" 4

# =============================================================================
#  TASK — USER ACTIVITY LOGGING
# =============================================================================
section 43 "LOG ACTIVITY  (POST /activity)  [auth]  ->  writes user_activity"
request POST "/activity" "{\"action_type\":\"scan\",\"barcode\":\"$BC_UNHEALTHY\",\"metadata\":{\"src\":\"test-suite\"}}" auth

section 44 "LOG ACTIVITY — invalid action_type  (POST /activity)  [auth]  ->  expect HTTP 400"
request POST "/activity" '{"action_type":"teleport"}' auth "" 4

section 45 "USER ACTIVITY HISTORY  (GET /activity/user/{user_id})  <- scans/compare/rate/favorite/share auto-logged above"
request GET "/activity/user/${USER_ID:-0}" "" noauth 5

section 46 "ACTIVITY TRENDS (overall)  (GET /activity/trends)"
request GET "/activity/trends" "" noauth

# =============================================================================
#  TASK — DAILY DIGEST / NOTIFICATION
# =============================================================================
section 47 "DAILY DIGEST  (GET /digest/{user_id})  <- summarises today's scans, notification/email ready"
request GET "/digest/${USER_ID:-0}" "" noauth

# =============================================================================
#  TASK 1 — WEEKLY CHALLENGES & LEADERBOARD
# =============================================================================
section 48 "LIST CHALLENGES  (GET /challenges)  [anonymous]  -> 4 active weekly challenges"
request GET "/challenges" "" noauth

section 49 "JOIN CHALLENGE — 'Scan 20 products this week'  (POST /challenges/1/join)  [auth]  ->  writes challenge_participants"
request POST "/challenges/1/join" "" auth

section 50 "JOIN CHALLENGE — 'Compare 10 products'  (POST /challenges/3/join)  [auth]"
request POST "/challenges/3/join" "" auth

section 51 "JOIN CHALLENGE — 'Rate 15 products'  (POST /challenges/4/join)  [auth]"
request POST "/challenges/4/join" "" auth

section 52 "RE-JOIN (idempotent)  (POST /challenges/1/join)  [auth]  ->  'Already joined'"
request POST "/challenges/1/join" "" auth

section 53 "CHALLENGE PROGRESS  (GET /challenges/1/progress)  [auth]  <- counts the scans from step 4"
request GET "/challenges/1/progress" "" auth

section 54 "LIST CHALLENGES with my progress  (GET /challenges)  [auth]  -> joined + progress per challenge"
request GET "/challenges" "" auth

section 55 "JOIN UNKNOWN CHALLENGE  (POST /challenges/999/join)  [auth]  ->  expect HTTP 404"
request POST "/challenges/999/join" "" auth "" 4

section 56 "LEADERBOARD — weekly  (GET /leaderboard?period=weekly)  -> rank, username, score, badges"
request GET "/leaderboard?period=weekly&limit=10" "" noauth 5

section 57 "LEADERBOARD — monthly  (GET /leaderboard?period=monthly)"
request GET "/leaderboard?period=monthly&limit=5" "" noauth 5

section 58 "LEADERBOARD — all-time  (GET /leaderboard?period=all-time)"
request GET "/leaderboard?period=all-time&limit=5" "" noauth 5

section 59 "LEADERBOARD — invalid period  (GET /leaderboard?period=daily)  ->  expect HTTP 400"
request GET "/leaderboard?period=daily" "" noauth "" 4

# =============================================================================
#  TASK 2 — SMART CART / SHOPPING LIST OPTIMIZATION
# =============================================================================
section 60 "CREATE SHOPPING LIST  (POST /shopping-list)  [auth]  ->  writes shopping_lists + items"
request POST "/shopping-list" "{\"name\":\"Weekly Groceries\",\"items\":[\"$BC_BAR\",\"$BC_BAR2\",\"$BC_UNHEALTHY\"]}" auth
LIST_ID="$(printf '%s' "$LAST_BODY" | json_get id)"
echo "${BLUE}shopping list id = ${LIST_ID:-?}${RESET}"

section 61 "GET SHOPPING LIST  (GET /shopping-list/{id})  <- each item scored"
request GET "/shopping-list/${LIST_ID:-0}" "" noauth

section 62 "OPTIMIZE SHOPPING LIST  (GET /shopping-list/{id}/optimize)  <- original + top 2 healthier alternatives"
request GET "/shopping-list/${LIST_ID:-0}/optimize" "" auth

section 63 "REPLACE AN ITEM  (POST /shopping-list/{id}/replace)  <- swap Chocobar for the healthy protein bar"
request POST "/shopping-list/${LIST_ID:-0}/replace" "{\"old_barcode\":\"$BC_BAR\",\"new_barcode\":\"$BC_HEALTHY\"}" auth

section 64 "GET UNKNOWN SHOPPING LIST  (GET /shopping-list/999999)  ->  expect HTTP 404"
request GET "/shopping-list/999999" "" noauth "" 4

section 65 "CREATE + DELETE a throwaway list  (POST then DELETE /shopping-list/{id})"
request POST "/shopping-list" "{\"items\":[\"$BC_HEALTHY\"]}" auth
TMP_LIST_ID="$(printf '%s' "$LAST_BODY" | json_get id)"
request DELETE "/shopping-list/${TMP_LIST_ID:-0}" "" auth

# =============================================================================
#  TASK 3 — COMMUNITY REVIEWS & DISCUSSIONS
# =============================================================================
section 66 "SUBMIT REVIEW  (POST /reviews)  [auth]  ->  writes reviews (text + 1-5 stars)"
request POST "/reviews" "{\"barcode\":\"$BC_UNHEALTHY\",\"rating\":4,\"review_text\":\"Great crunch but way too salty for daily snacking.\"}" auth
REVIEW_ID="$(printf '%s' "$LAST_BODY" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('review',{}).get('id',''))" 2>/dev/null)"
echo "${BLUE}review id = ${REVIEW_ID:-?}${RESET}"

section 67 "REVIEW VALIDATION — rating out of range  (POST /reviews)  [auth]  ->  expect HTTP 400"
request POST "/reviews" "{\"barcode\":\"$BC_UNHEALTHY\",\"rating\":9,\"review_text\":\"bad\"}" auth "" 4

section 68 "UPVOTE A REVIEW  (POST /reviews/{id}/vote)  [auth]  ->  writes review_votes"
request POST "/reviews/${REVIEW_ID:-0}/vote" '{"vote":"up"}' auth

section 69 "REPLY TO A REVIEW  (POST /reviews/{id}/replies)  [auth]  ->  writes review_replies"
request POST "/reviews/${REVIEW_ID:-0}/replies" '{"reply_text":"Agreed, the sodium is the main downside here."}' auth

section 70 "GET SINGLE REVIEW  (GET /reviews/{id})  <- with vote counts + replies"
request GET "/reviews/${REVIEW_ID:-0}" "" noauth

section 71 "GET ALL REVIEWS FOR A PRODUCT  (GET /product/{barcode}/reviews)  <- with average rating"
request GET "/product/$BC_UNHEALTHY/reviews" "" noauth

section 72 "DELETE SOMEONE ELSE'S REVIEW  (DELETE /reviews/{id})  <- 2nd user  ->  expect HTTP 403"
echo "${DIM}# register a 2nd user and try to delete user 1's review — the API must forbid it${RESET}"
STAMP2=$(date +%s)
request POST "/register" "{\"email\":\"tester2_${STAMP2}@example.com\",\"username\":\"tester2_${STAMP2}\",\"password\":\"$PASSWORD\"}" noauth
request POST "/login" "{\"email\":\"tester2_${STAMP2}@example.com\",\"password\":\"$PASSWORD\"}" noauth
TOKEN2="$(printf '%s' "$LAST_BODY" | json_get access_token)"
OLD_TOKEN="$TOKEN"; TOKEN="$TOKEN2"
request DELETE "/reviews/${REVIEW_ID:-0}" "" auth "" 4
TOKEN="$OLD_TOKEN"

section 73 "CREATE + DELETE own review  (POST then DELETE /reviews/{id})  [auth]  ->  'Review deleted'"
request POST "/reviews" "{\"barcode\":\"$BC_HEALTHY\",\"rating\":5,\"review_text\":\"Clean ingredients, will buy again.\"}" auth
TMP_REVIEW_ID="$(printf '%s' "$LAST_BODY" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('review',{}).get('id',''))" 2>/dev/null)"
request DELETE "/reviews/${TMP_REVIEW_ID:-0}" "" auth

# =============================================================================
#  TASK 1 — PERSONALIZED HOME FEED
# =============================================================================
section 74 "HOME FEED — personalized  (GET /home-feed)  [auth]  <- recently_scanned + recommendations + challenge_progress + badges_earned"
echo "${DIM}# Task 3 shape: recently_scanned[{...,score,grade,image_url}], recommendations[{...,score,reason,image_url}],${RESET}"
echo "${DIM}#             challenge_progress{challenge_name,progress,target}, badges_earned[{name,icon,earned_at}]${RESET}"
request GET "/home-feed" "" auth

section 75 "HOME FEED — via explicit user_id  (GET /home-feed?user_id=)  [public]"
request GET "/home-feed?user_id=${USER_ID:-0}" "" noauth

section 76 "HOME FEED — generic fallback  (GET /home-feed)  [anonymous]  -> popular recommendations, preview challenge (progress 0), no badges"
request GET "/home-feed" "" noauth

# =============================================================================
#  TASK 2 — SMART SEARCH WITH AUTOCOMPLETE
# =============================================================================
section 77 "AUTOCOMPLETE  (GET /search/autocomplete?q=pro)  -> name + brand + barcode suggestions"
request GET "/search/autocomplete?q=pro&limit=5" "" noauth

section 78 "AUTOCOMPLETE — blank query  (GET /search/autocomplete?q=)  -> empty suggestions (still 200)"
request GET "/search/autocomplete?q=" "" noauth

section 79 "SEARCH — enhanced filtering  (GET /search?q=protein&sort=score_desc&limit=5)"
request GET "/search?q=protein&sort=score_desc&limit=5" "" noauth 3

section 80 "SEARCH — filter by category  (GET /search?category=chips)"
request GET "/search?category=chips&limit=5" "" noauth 5

# =============================================================================
#  TASK 3 — "SWAPIFY RECOMMENDED" BADGE
# =============================================================================
section 81 "PRODUCT BADGE  (GET /product/{barcode}/badge)  <- criteria: score>7, no high-risk, no artificial colors"
request GET "/product/$BC_HEALTHY/badge" "" noauth

section 82 "PRODUCT BADGE — unhealthy product  (GET /product/{barcode}/badge)  -> is_recommended false + failing_criteria"
request GET "/product/$BC_UNHEALTHY/badge" "" noauth

section 83 "BADGE INTEGRATED IN /product  (GET /product/{barcode})  <- response now carries is_recommended + recommended_badge"
request GET "/product/$BC_HEALTHY" "" noauth

section 84 "PRODUCT BADGE — unknown barcode  (GET /product/{barcode}/badge)  ->  expect HTTP 404"
request GET "/product/0000000000000/badge" "" noauth "" 4

# =============================================================================
#  TASK 1 — API PERFORMANCE  (pagination, gzip compression)
# =============================================================================
section 85 "SEARCH PAGINATION — page 1  (GET /search?...&limit=3&offset=0)"
echo "${DIM}# limit + offset paginate the ranked results (Task 1B)${RESET}"
request GET "/search?q=&sort=name&limit=3&offset=0" "" noauth 3

section 86 "SEARCH PAGINATION — page 2  (GET /search?...&limit=3&offset=3)  <- different products than page 1"
request GET "/search?q=&sort=name&limit=3&offset=3" "" noauth 3

section 87 "GZIP COMPRESSION  (GET /search with Accept-Encoding: gzip)  -> Content-Encoding: gzip  (Task 1D)"
check_gzip "/search?q=&limit=50&sort=name"

# =============================================================================
#  TASK 2 — PRODUCT IMAGES  (image_url in responses + crowdsourced upload)
# =============================================================================
make_test_images

section 88 "IMAGE URL IN /search  (GET /search?q=protein)  <- every result carries image_url (placeholder when none)  (Task 2B)"
request GET "/search?q=protein&limit=5" "" noauth 5
check_image_url array

section 89 "IMAGE URL IN /similar  (GET /similar/{barcode})  <- every alternative carries image_url  (Task 2B)"
request GET "/similar/$BC_BAR" "" noauth 3
check_image_url array

section 90 "UPLOAD PRODUCT IMAGE — valid PNG  (POST /product/image)  [auth]  ->  stores reference, updates products.image_url  (Task 2C)"
request_upload "/product/image" "$BC_UNHEALTHY" "$IMG_DIR/valid.png" "image/png" auth

section 91 "PRODUCT NOW RETURNS THE UPLOADED image_url  (GET /product/{barcode})  <- cache invalidated on upload"
request GET "/product/$BC_UNHEALTHY" "" noauth
if printf '%s' "$LAST_BODY" | grep -q "/product-images/${BC_UNHEALTHY}\."; then
  echo "${GREEN}${BOLD}image_url now points at the uploaded file ✓  (cache was invalidated on upload)${RESET}"
  PASS=$((PASS+1))
else
  echo "${RED}${BOLD}image_url did not update to the uploaded file${RESET}"
  FAIL=$((FAIL+1))
fi

section 92 "UPLOAD — reject non-image  (POST /product/image with a text file)  ->  expect HTTP 400  (Task 2C validation)"
request_upload "/product/image" "$BC_UNHEALTHY" "$IMG_DIR/not_image.png" "image/png" auth 4

section 93 "UPLOAD — reject file > 2 MB  (POST /product/image with a 2.1 MB file)  ->  expect HTTP 413  (Task 2C validation)"
request_upload "/product/image" "$BC_UNHEALTHY" "$IMG_DIR/too_big.png" "image/png" auth 4

# =============================================================================
#  TASK 6 — OCR LABEL SCANNER (Proof of Concept)
# =============================================================================
section 94 "OCR AVAILABILITY  (GET /ocr/health)  -> reports whether Tesseract is installed"
request GET "/ocr/health" "" noauth
OCR_AVAILABLE="$(printf '%s' "$LAST_BODY" | json_get ocr_available)"
echo "${BLUE}OCR available = ${OCR_AVAILABLE:-False}  (True -> scan-label returns 200; else 503)${RESET}"

section 95 "OCR SCAN LABEL  (POST /ocr/scan-label)  <- extracts text/ingredients, scores via the engine"
# Expected status depends on whether the Tesseract engine is installed on this host.
if [ "$OCR_AVAILABLE" = "True" ]; then OCR_EXPECT=2; else OCR_EXPECT=5; fi
request_upload "/ocr/scan-label" "$BC_UNHEALTHY" "$IMG_DIR/valid.png" "image/png" noauth "$OCR_EXPECT"

# =============================================================================
banner "DATABASE VERIFICATION  (proving the writes actually persisted)"

echo
echo "${BOLD}users${RESET}  (baseline had ${BASE_USERS:-?} rows)"
db_query "SELECT id, username, email, created_at FROM users WHERE email='$EMAIL'"
NOW_USERS=$(db_query "SELECT COUNT(*) AS c FROM users" | grep -o 'c=[0-9]*' | cut -d= -f2)
echo "   ${GREEN}users: ${BASE_USERS:-?} -> ${NOW_USERS}${RESET}"

echo
echo "${BOLD}user_preferences${RESET}  (from steps 9 & 11)"
db_query "SELECT user_id, preferences FROM user_preferences WHERE user_id=${USER_ID:-0}"

echo
echo "${BOLD}scan_history${RESET}  (from step 4)"
db_query "SELECT id, barcode, scanned_at FROM scan_history WHERE user_id=${USER_ID:-0} ORDER BY id DESC LIMIT 5"
NOW_SCANS=$(db_query "SELECT COUNT(*) AS c FROM scan_history" | grep -o 'c=[0-9]*' | cut -d= -f2)
echo "   ${GREEN}scan_history total: ${BASE_SCANS:-?} -> ${NOW_SCANS}${RESET}"

echo
echo "${BOLD}favorites${RESET}  (added in 13, deleted in 15 -> expect none for this user)"
db_query "SELECT user_id, barcode, added_at FROM favorites WHERE user_id=${USER_ID:-0}"

echo
echo "${BOLD}missing_reports${RESET}  (from step 24)"
db_query "SELECT id, barcode, product_name, user_comment FROM missing_reports ORDER BY id DESC LIMIT 3"
NOW_REPORTS=$(db_query "SELECT COUNT(*) AS c FROM missing_reports" | grep -o 'c=[0-9]*' | cut -d= -f2)
echo "   ${GREEN}missing_reports total: ${BASE_REPORTS:-?} -> ${NOW_REPORTS}${RESET}"

echo
echo "${BOLD}product_ratings${RESET}  (from steps 28-30; note the re-rating in 30 UPDATED, didn't stack)"
db_query "SELECT id, barcode, taste_rating, quality_rating, value_rating, rated_at FROM product_ratings WHERE user_id=${USER_ID:-0} ORDER BY id DESC LIMIT 5"

echo
echo "${BOLD}comparison_history${RESET}  (from step 21; feeds the /recommendations engine)"
db_query "SELECT id, barcode, compared_at FROM comparison_history WHERE user_id=${USER_ID:-0} ORDER BY id DESC LIMIT 5"

echo
echo "${BOLD}user_activity${RESET}  (auto-logged scans/compare/rate/favorite/share + POST /activity from steps 43-47)"
db_query "SELECT id, action_type, barcode, created_at FROM user_activity WHERE user_id=${USER_ID:-0} ORDER BY id DESC LIMIT 8"

echo
echo "${BOLD}challenge_participants${RESET}  (from steps 49-52; the joins this user made)"
db_query "SELECT challenge_id, user_id, joined_at, completed_at FROM challenge_participants WHERE user_id=${USER_ID:-0} ORDER BY challenge_id"

echo
echo "${BOLD}shopping_lists + items${RESET}  (from steps 60-63; item swapped in step 63)"
db_query "SELECT id, name, user_id, created_at FROM shopping_lists WHERE user_id=${USER_ID:-0} ORDER BY id DESC LIMIT 3"
db_query "SELECT list_id, barcode FROM shopping_list_items WHERE list_id=${LIST_ID:-0} ORDER BY id"

echo
echo "${BOLD}reviews + votes + replies${RESET}  (from steps 66-70; review ${REVIEW_ID:-?} kept, votes/replies attached)"
db_query "SELECT id, barcode, rating, review_text, created_at FROM reviews WHERE user_id=${USER_ID:-0} ORDER BY id DESC LIMIT 3"
db_query "SELECT review_id, user_id, vote FROM review_votes WHERE review_id=${REVIEW_ID:-0}"
db_query "SELECT review_id, user_id, reply_text FROM review_replies WHERE review_id=${REVIEW_ID:-0}"

echo
echo "${BOLD}product_images${RESET}  (Task 2C; from the image upload in step 90 -> reference stored, products.image_url updated)"
db_query "SELECT id, barcode, image_url, content_type, file_size FROM product_images ORDER BY id DESC LIMIT 3"
db_query "SELECT barcode, image_url FROM products WHERE barcode='$BC_UNHEALTHY'"

echo
echo "${BOLD}product indexes${RESET}  (Task 1A; created at startup)"
db_query "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='products' AND name LIKE 'idx_%' ORDER BY name"

# =============================================================================
banner "SUMMARY"
TOTAL=$((PASS+FAIL))
echo "  ${GREEN}Passed: $PASS${RESET}   ${RED}Failed: $FAIL${RESET}   Total requests: $TOTAL   ${DIM}(pass = status matched the expected code)${RESET}"
echo "  AI /chat source: ${CHAT_SOURCE:-unknown}"
if [ "$CHAT_SOURCE" = "openrouter" ]; then
  echo "  ${GREEN}OpenRouter API key is working — real AI answers.${RESET}"
else
  echo "  ${YELLOW}/chat used the rule-based fallback (key missing, model offline, or rate-limited).${RESET}"
  echo "  ${YELLOW}Check '${LOG}' or the fallback_reason field above.${RESET}"
fi
echo
echo "${BOLD}Done.${RESET}"
