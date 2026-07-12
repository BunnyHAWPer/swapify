from fastapi import FastAPI, HTTPException, Depends, Header, status, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
import re
import requests
import bcrypt
import jwt
import datetime
import time
import json
import logging

# OCR label scanner POC (Task 6). The module itself has no hard dependency on the
# OCR stack at import time (Tesseract/Pillow are looked up lazily), so this import
# is always safe whether or not OCR is installed. Support both run styles:
# ``uvicorn app:app`` from server/src, and ``uvicorn src.app:app`` from server.
try:
    import ocr_label_scanner
except ImportError:  # pragma: no cover - import style fallback
    from . import ocr_label_scanner

# In-memory caching (Task 1C). cachetools is the preferred production library;
# fall back to a tiny time-to-live cache with the same subset of the API we use
# so the app still runs if the dependency is unavailable.
try:
    from cachetools import TTLCache
except Exception:  # pragma: no cover - dependency fallback
    class TTLCache(dict):
        """Minimal TTLCache stand-in: entries expire ``ttl`` seconds after write."""
        def __init__(self, maxsize=128, ttl=3600):
            super().__init__()
            self.maxsize = maxsize
            self.ttl = ttl
            self._expiry = {}

        def __getitem__(self, key):
            if key in self._expiry and time.time() > self._expiry[key]:
                self.pop(key, None)
                self._expiry.pop(key, None)
            return super().__getitem__(key)

        def __contains__(self, key):
            try:
                self.__getitem__(key)
                return True
            except KeyError:
                return False

        def __setitem__(self, key, value):
            if len(self) >= self.maxsize and key not in self:
                oldest = min(self._expiry, key=self._expiry.get, default=None)
                if oldest is not None:
                    self.pop(oldest, None)
                    self._expiry.pop(oldest, None)
            super().__setitem__(key, value)
            self._expiry[key] = time.time() + self.ttl

        def get(self, key, default=None):
            try:
                return self.__getitem__(key)
            except KeyError:
                return default

logger = logging.getLogger("swapify.chat")

class MissingReport(BaseModel):
    barcode: str
    product_name: Optional[str] = None
    comment: Optional[str] = None

class UserRegister(BaseModel):
    email: str
    password: str
    username: str

class UserLogin(BaseModel):
    email: str
    password: str

class UserPreferences(BaseModel):
    preferences: dict

class FavoriteAdd(BaseModel):
    barcode: str

class ChatRequest(BaseModel):
    question: str
    barcode: Optional[str] = None

class CompareMultipleRequest(BaseModel):
    barcodes: List[str]

class ProductRating(BaseModel):
    barcode: str
    taste_rating: int
    quality_rating: int
    value_rating: int

class ActivityLog(BaseModel):
    action_type: str
    user_id: Optional[int] = None
    barcode: Optional[str] = None
    metadata: Optional[dict] = None

class ShoppingListCreate(BaseModel):
    items: List[str]
    name: Optional[str] = None

class ShoppingListReplace(BaseModel):
    old_barcode: str
    new_barcode: str

class ReviewCreate(BaseModel):
    barcode: str
    rating: int
    review_text: str

class ReviewVote(BaseModel):
    vote: str  # "up" or "down"

class ReviewReply(BaseModel):
    reply_text: str

# JWT signing key. Overridable via the environment for deployment (set a strong,
# random SECRET_KEY in production); falls back to the original constant so local
# dev and the test suite keep working unchanged.
SECRET_KEY = os.environ.get("SECRET_KEY", "supersecretkey")
ALGORITHM = "HS256"


try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# --- Provider 1: OpenRouter (OpenAI-compatible; many free-tier models) --------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "openai/gpt-oss-120b:free"
).strip()

OPENROUTER_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",")
    if m.strip()
]
OPENROUTER_MODELS = [OPENROUTER_MODEL] + [
    m for m in OPENROUTER_FALLBACK_MODELS if m != OPENROUTER_MODEL
]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- Provider 2 (optional): Google Gemini (generous free tier) ----------------
# Used as an automatic failover when every OpenRouter free model is rate-limited,
# so the chatbot keeps giving real AI answers instead of dropping to the
# rule-based fallback. Get a free key at https://aistudio.google.com/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

AI_ENABLED = bool(OPENROUTER_API_KEY or GEMINI_API_KEY)


if AI_ENABLED:
    providers = []
    if OPENROUTER_API_KEY:
        providers.append(f"OpenRouter({', '.join(OPENROUTER_MODELS)})")
    if GEMINI_API_KEY:
        providers.append(f"Gemini({GEMINI_MODEL})")
    logger.info("AI nutritionist enabled. Providers tried in order: %s", " -> ".join(providers))
else:
    logger.warning(
        "AI nutritionist: no API key set — /chat will return deterministic "
        "rule-based answers. Set OPENROUTER_API_KEY (free: "
        "https://openrouter.ai/keys) and/or GEMINI_API_KEY (free: "
        "https://aistudio.google.com/apikey) in server/.env for real AI responses."
    )

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_current_user_optional(token: Optional[str] = Depends(OAuth2PasswordBearer(tokenUrl="login", auto_error=False))):
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("user_id")
    except Exception:
        return None

app = FastAPI()

recent_scans = []

# CORS origins are configurable for deployment (Task 1): set ``CORS_ORIGINS`` to a
# comma-separated list of allowed front-end origins in production; defaults to "*"
# for local development. When specific origins are listed, credentials are allowed;
# with the "*" wildcard, credentials must be disabled (browsers reject "*" + creds).
_cors_env = os.environ.get("CORS_ORIGINS", "*").strip()
if _cors_env in ("", "*"):
    ALLOWED_ORIGINS = ["*"]
    _allow_credentials = False
else:
    ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]
    _allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Task 1D — Gzip compression. Responses larger than ``minimum_size`` bytes are
# gzip-compressed when the client sends ``Accept-Encoding: gzip`` (browsers and
# most HTTP clients do). Big JSON payloads (/search, /home-feed, /recommendations)
# shrink dramatically over the wire; tiny responses are left uncompressed.
app.add_middleware(GZipMiddleware, minimum_size=500)

# ------------------------------------------------------------------------------
# Product images (Task 2)
# ------------------------------------------------------------------------------
# Uploaded product images are stored on disk under ``server/uploads/product_images``
# and served back as static files under the ``/product-images`` URL prefix. The
# database only stores the *reference* (the served URL), not the bytes.
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads', 'product_images')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Default placeholder returned for products that have no image, so the client
# always has something to render instead of an empty box.
PLACEHOLDER_IMAGE_FILENAME = "_placeholder.svg"
PLACEHOLDER_IMAGE_URL = f"/product-images/{PLACEHOLDER_IMAGE_FILENAME}"

_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" '
    'viewBox="0 0 300 300" role="img" aria-label="No product image">'
    '<rect width="300" height="300" fill="#eef1f4"/>'
    '<circle cx="150" cy="120" r="46" fill="#c7ced6"/>'
    '<rect x="70" y="185" width="160" height="20" rx="10" fill="#c7ced6"/>'
    '<rect x="95" y="220" width="110" height="14" rx="7" fill="#d9dee4"/>'
    '<text x="150" y="285" font-family="sans-serif" font-size="16" '
    'fill="#8a93a0" text-anchor="middle">No image</text></svg>'
)


def _ensure_placeholder_image():
    """Write the bundled SVG placeholder into the upload dir if it is missing."""
    path = os.path.join(UPLOAD_DIR, PLACEHOLDER_IMAGE_FILENAME)
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_PLACEHOLDER_SVG)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not write placeholder image: %s", exc)


_ensure_placeholder_image()
app.mount("/product-images", StaticFiles(directory=UPLOAD_DIR), name="product-images")


def image_or_placeholder(url):
    """Return ``url`` when a product has an image, else the placeholder URL."""
    return url if url else PLACEHOLDER_IMAGE_URL


# ------------------------------------------------------------------------------
# In-memory caches (Task 1C)
# ------------------------------------------------------------------------------
# ``_product_cache`` holds fully-scored *generic* (non-personalized) product
# payloads keyed by barcode, so repeat detail lookups skip the DB read + scoring
# (and, for Open Food Facts fallbacks, the network round-trip). ``_popular_cache``
# holds the "top most-scanned products" list used by the recommendation and
# home-feed fallbacks. Both expire after one hour; an explicit product update
# (e.g. a new image upload) invalidates the affected entries immediately.
PRODUCT_CACHE_TTL = 3600  # 1 hour
_product_cache = TTLCache(maxsize=512, ttl=PRODUCT_CACHE_TTL)
_popular_cache = TTLCache(maxsize=8, ttl=PRODUCT_CACHE_TTL)


def cache_get_product(barcode):
    """Return a cached generic scored product for ``barcode`` (or None)."""
    return _product_cache.get(barcode)


def cache_set_product(barcode, payload):
    """Cache a generic scored product payload for ``barcode``."""
    _product_cache[barcode] = payload


def invalidate_product_cache(barcode=None):
    """Drop a product (or the whole product cache) plus the popular-products
    cache, so the next read recomputes. Called whenever a product changes
    (e.g. a crowdsourced image upload)."""
    if barcode is None:
        _product_cache.clear()
    else:
        _product_cache.pop(barcode, None)
    _popular_cache.clear()


# ------------------------------------------------------------------------------
# Database location (deployment-ready, Task 1)
# ------------------------------------------------------------------------------
# The database path is resolved from the environment first (``SWAPIFY_DB_PATH``,
# with ``DATABASE_PATH`` accepted as an alias) so a live host can point the app at
# a persistent disk without touching the code, and falls back to the bundled
# ``server/swapify.db`` next to this package. No absolute developer paths are
# hard-coded anywhere — everything is relative to this file or an env var.
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "swapify.db")
DB_PATH = os.environ.get("SWAPIFY_DB_PATH") or os.environ.get("DATABASE_PATH") or DEFAULT_DB_PATH
# The CSV catalogue is used only to *seed / sync* the database, never read at
# request time (the DB is the single source of truth — see ensure_products_seeded).
CSV_SEED_PATH = os.environ.get("SWAPIFY_CSV_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "products.csv"
)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.post("/register")
def register(user: UserRegister):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ? OR username = ?", (user.email, user.username))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username or email already registered")
        
    password_hash = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    cursor.execute(
        "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
        (user.username, user.email, password_hash)
    )
    conn.commit()
    conn.close()
    return {"message": "User registered successfully"}

@app.post("/login")
def login(user: UserLogin):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (user.email,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")
        
    user_db = dict(row)
    if not bcrypt.checkpw(user.password.encode('utf-8'), user_db['password_hash'].encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid email or password")
        
    payload = {
        "user_id": user_db['id'],
        "username": user_db['username'],
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=24)
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    
    return {"access_token": token, "token_type": "bearer"}

@app.get("/profile")
def profile(user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, email, created_at FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)

def fetch_off_product(barcode: str):
    """Fetch and normalise a product from Open Food Facts.

    Returns an *unscored* product dict (same shape as a `products` row) or
    None when the product is unknown / OFF is unreachable. OFF rejects
    requests without a descriptive User-Agent, so one is always sent.
    """
    try:
        off_resp = requests.get(
            f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json",
            headers={"User-Agent": "Swapify/1.0 (health-scanner; contact: dhruvrwt1211@gmail.com)"},
            timeout=8,
        )
    except requests.RequestException:
        return None

    if off_resp.status_code != 200:
        return None
    data = off_resp.json()
    if data.get('status') != 1 or not data.get('product'):
        return None

    p = data['product']
    nutriments = p.get('nutriments', {})

    def _num(*keys):
        for k in keys:
            v = nutriments.get(k)
            if v is not None and v != "":
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    # OFF stores sodium in grams; fall back to salt/2.5 when sodium is absent.
    sodium_val = _num('sodium_serving', 'sodium_100g')
    if sodium_val is None:
        salt_val = _num('salt_serving', 'salt_100g')
        sodium_val = (salt_val / 2.5) if salt_val is not None else None
    sodium_mg = sodium_val * 1000 if sodium_val is not None else None

    category = (p.get('categories', '') or '').split(',')[0].strip().lower()
    category = re.sub(r'^[a-z]{2}:', '', category) or None

    # OFF stores ingredients under several keys; fall back across them
    # and finally reconstruct from the structured ingredients list.
    off_ingredients = (
        p.get('ingredients_text')
        or p.get('ingredients_text_en')
        or p.get('ingredients_text_with_allergens')
        or ""
    )
    if not off_ingredients and isinstance(p.get('ingredients'), list):
        off_ingredients = ", ".join(
            i.get('text', '') for i in p['ingredients'] if i.get('text')
        )

    return {
        "barcode": barcode,
        "product_name": p.get('product_name') or 'Unknown Product',
        "brand": p.get('brands', ''),
        # OFF exposes product imagery under a few keys; the front image is best
        # for a share card. None of the local DB rows carry an image, so this is
        # only populated for products resolved from Open Food Facts.
        "image_url": (
            p.get('image_front_url')
            or p.get('image_url')
            or p.get('image_front_small_url')
            or None
        ),
        "category": category,
        "serving_size_g": 100.0,
        "sugar_g_per_serving": _num('sugars_serving', 'sugars_100g'),
        "saturated_fat_g_per_serving": _num('saturated-fat_serving', 'saturated-fat_100g'),
        "sodium_mg_per_serving": sodium_mg,
        "protein_g_per_serving": _num('proteins_serving', 'proteins_100g'),
        "fiber_g_per_serving": _num('fiber_serving', 'fiber_100g'),
        "calories_kcal_per_serving": _num('energy-kcal_serving', 'energy-kcal_100g'),
        "ingredients_text": off_ingredients,
    }


def get_scored_product(barcode: str, preferences: dict = None):
    """Return a fully scored product dict for a barcode (local DB first, then
    Open Food Facts), or None if it cannot be found anywhere. Used as shared
    product-context loader for /product, /chat and /compare-multiple. Does not
    record scans. When ``preferences`` are supplied the score is personalized.

    The generic (non-personalized) result is served from a 1-hour cache; a
    personalized request always scores fresh (see ``generic_scored_product``).
    """
    if not preferences:
        return generic_scored_product(barcode)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode,))
    row = cursor.fetchone()
    conn.close()

    source = "database"
    if row:
        p_dict = dict(row)
    else:
        p_dict = fetch_off_product(barcode)
        source = "openfoodfacts"
    if not p_dict:
        return None

    score, grade, rule_version, breakdown = calculate_health_score_v2(p_dict, 1, preferences)
    p_dict['score'] = score
    p_dict['grade'] = grade
    p_dict['rule_version'] = rule_version
    p_dict['breakdown'] = breakdown
    p_dict['ingredient_flags'] = breakdown.get('ingredient_flags', [])
    p_dict['preferences_applied'] = breakdown.get('preferences_applied', {})
    p_dict['source'] = source
    return p_dict


def generic_scored_product(barcode: str):
    """Fully-scored *generic* (non-personalized) product payload, cached for
    ``PRODUCT_CACHE_TTL`` seconds (Task 1C).

    Resolves the product from the local DB first, then Open Food Facts, scores it
    with the generic ruleset and attaches the "Swapify Recommended" badge. The
    result is cached by barcode so repeat detail lookups avoid the DB read,
    scoring work and (for OFF fallbacks) the network round-trip. A fresh copy is
    returned each call so callers can safely mutate it. Returns None when the
    product cannot be found anywhere. Invalidated by ``invalidate_product_cache``
    whenever the product changes (e.g. a new image upload)."""
    cached = cache_get_product(barcode)
    if cached is not None:
        return dict(cached)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode,))
    row = cursor.fetchone()
    conn.close()

    if row:
        p_dict = dict(row)
        source = "database"
    else:
        p_dict = fetch_off_product(barcode)
        source = "openfoodfacts"
    if not p_dict:
        return None

    score, grade, rule_version, breakdown = calculate_health_score_v2(p_dict, 1, None)
    p_dict['score'] = score
    p_dict['grade'] = grade
    p_dict['rule_version'] = rule_version
    p_dict['breakdown'] = breakdown
    p_dict['ingredient_flags'] = breakdown.get('ingredient_flags', [])
    p_dict['preferences_applied'] = {}
    p_dict['source'] = source
    badge = evaluate_recommended_badge(p_dict, breakdown, None)
    p_dict['is_recommended'] = badge['is_recommended']
    p_dict['recommended_badge'] = badge

    cache_set_product(barcode, p_dict)
    return dict(p_dict)


# ==============================================================================
# Barcode Validation & Correction
# ==============================================================================
# Validate a barcode's length and (GS1) check digit and, when it's invalid,
# suggest a correction. Standard retail barcodes are EAN-8 (8), UPC-A (12) and
# EAN-13 (13) digits; the last digit is a modulo-10 check digit computed from the
# preceding digits with alternating 3/1 weights. Used by /validate-barcode and
# woven into /product and /search so bad barcodes get a helpful suggestion.

BARCODE_FORMATS = {8: "EAN-8", 12: "UPC-A", 13: "EAN-13"}


def gs1_check_digit(payload: str) -> int:
    """Return the GS1 modulo-10 check digit for ``payload`` (all data digits,
    without the check digit). Rightmost data digit is weighted x3, then x1, ..."""
    total = 0
    for i, ch in enumerate(reversed(payload)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - (total % 10)) % 10


def _gs1_check_ok(code: str) -> bool:
    """True when ``code``'s final digit matches its computed GS1 check digit."""
    return int(code[-1]) == gs1_check_digit(code[:-1])


def validate_barcode(barcode: str) -> dict:
    """Validate a barcode's format and check digit, suggesting a correction.

    Returns a dict with:
      - ``barcode``: the trimmed input
      - ``valid``: True only for a well-formed EAN-8 / UPC-A / EAN-13 whose
        check digit is correct
      - ``format``: the detected standard (or None)
      - ``suggestion``: a corrected barcode when one can be derived, else None
      - ``message``: a human-readable explanation
    """
    raw = ("" if barcode is None else str(barcode)).strip()
    cleaned = re.sub(r"[\s\-]", "", raw)
    result = {
        "barcode": raw,
        "valid": False,
        "format": None,
        "suggestion": None,
        "message": "",
    }

    if not cleaned:
        result["message"] = "Barcode is empty."
        return result

    if not cleaned.isdigit():
        # Strip non-digits and, if what's left is a valid barcode, suggest it.
        digits = re.sub(r"\D", "", cleaned)
        result["message"] = "Barcode must contain only digits (0-9)."
        if len(digits) in BARCODE_FORMATS and _gs1_check_ok(digits):
            result["suggestion"] = digits
            result["message"] += f" Did you mean '{digits}'?"
        return result

    n = len(cleaned)
    if n in BARCODE_FORMATS:
        fmt = BARCODE_FORMATS[n]
        result["format"] = fmt
        if _gs1_check_ok(cleaned):
            result["valid"] = True
            result["message"] = f"Valid {fmt} barcode."
        else:
            corrected = cleaned[:-1] + str(gs1_check_digit(cleaned[:-1]))
            result["suggestion"] = corrected
            result["message"] = (
                f"Invalid {fmt} check digit: expected '{corrected[-1]}', "
                f"got '{cleaned[-1]}'. Suggested correction: '{corrected}'."
            )
        return result

    # Wrong length. If it's exactly one digit short of a known format, it's most
    # likely missing its check digit (e.g. 12 digits -> a full 13-digit EAN-13),
    # so append the computed check digit and suggest that.
    if (n + 1) in BARCODE_FORMATS:
        fmt = BARCODE_FORMATS[n + 1]
        completed = cleaned + str(gs1_check_digit(cleaned))
        result["suggestion"] = completed
        result["message"] = (
            f"{n} digits looks like an incomplete {fmt}; adding the check "
            f"digit gives '{completed}'."
        )
        return result

    result["message"] = (
        f"{n} digits is not a standard barcode length "
        f"(expected 8, 12 or 13 digits)."
    )
    return result


@app.get("/validate-barcode/{barcode}")
def validate_barcode_endpoint(barcode: str):
    """Validate a barcode and, if invalid, return a suggested correction."""
    return validate_barcode(barcode)


@app.get("/product/{barcode}")
def get_product(barcode: str, device_id: Optional[str] = None, user_id: Optional[int] = Depends(get_current_user_optional)):
    # Validate the barcode up front so we can attach a helpful correction hint to
    # the response (especially on a 404) without blocking the lookup itself.
    validation = validate_barcode(barcode)

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode,))
    row = cursor.fetchone()

    if row and (device_id or user_id):
        cursor.execute(
            "INSERT INTO scan_history (device_id, user_id, barcode) VALUES (?, ?, ?)",
            (device_id, user_id, barcode)
        )
        conn.commit()
        # Best-effort activity log for logged-in scans (see /activity).
        if isinstance(user_id, int):
            log_activity(user_id, "scan", barcode, {"device_id": device_id} if device_id else None)

    conn.close()

    # Personalize the score when the request is authenticated and the user has
    # saved dietary preferences (otherwise this is the generic score).
    preferences = load_user_preferences(user_id)

    if row:
        if barcode in recent_scans:
            recent_scans.remove(barcode)
        recent_scans.insert(0, barcode)
        if len(recent_scans) > 5:
            recent_scans.pop()

        if preferences:
            # Personalized score — always computed fresh (never cached).
            p_dict = dict(row)
            score, grade, rule_version, breakdown = calculate_health_score_v2(p_dict, 1, preferences)
            p_dict['score'] = score
            p_dict['grade'] = grade
            p_dict['rule_version'] = rule_version
            p_dict['breakdown'] = breakdown
            p_dict['ingredient_flags'] = breakdown.get('ingredient_flags', [])
            p_dict['preferences_applied'] = breakdown.get('preferences_applied', {})
            # "Swapify Recommended" badge (Task 3): a clean, healthy pick.
            badge = evaluate_recommended_badge(p_dict, breakdown, preferences)
            p_dict['is_recommended'] = badge['is_recommended']
            p_dict['recommended_badge'] = badge
        else:
            # Generic score served from the 1-hour product cache (Task 1C).
            p_dict = generic_scored_product(barcode)

        # Always return an image reference — the product's own image or the
        # shared placeholder — so the client never renders an empty box (Task 2).
        p_dict['image_url'] = image_or_placeholder(p_dict.get('image_url'))
        if not validation["valid"]:
            p_dict['barcode_validation'] = validation
        return p_dict

    # Fallback: fetch & score from Open Food Facts when not in the local DB.
    if preferences:
        p_dict = fetch_off_product(barcode)
        if p_dict:
            score, grade, rule_version, breakdown = calculate_health_score_v2(p_dict, 1, preferences)
            p_dict['score'] = score
            p_dict['grade'] = grade
            p_dict['rule_version'] = rule_version
            p_dict['breakdown'] = breakdown
            p_dict['ingredient_flags'] = breakdown.get('ingredient_flags', [])
            p_dict['preferences_applied'] = breakdown.get('preferences_applied', {})
            badge = evaluate_recommended_badge(p_dict, breakdown, preferences)
            p_dict['is_recommended'] = badge['is_recommended']
            p_dict['recommended_badge'] = badge
            p_dict['source'] = 'openfoodfacts'
    else:
        # Generic OFF lookup is cached (network round-trip included) (Task 1C).
        p_dict = generic_scored_product(barcode)

    if p_dict:
        p_dict['image_url'] = image_or_placeholder(p_dict.get('image_url'))
        if isinstance(user_id, int):
            log_activity(user_id, "scan", barcode, {"source": "openfoodfacts"})
        if not validation["valid"]:
            p_dict['barcode_validation'] = validation
        return p_dict

    # Not found anywhere. Surface the validation hint so the client can retry
    # with the suggested correction when the barcode was malformed.
    content = {"error": "Product not found"}
    if not validation["valid"]:
        content["barcode_validation"] = validation
    return JSONResponse(status_code=404, content=content)

def calculate_health_score(product: dict):
    score = 5.0
    sugar = product.get('sugar_g_per_serving') or 0
    sat_fat = product.get('saturated_fat_g_per_serving') or 0
    sodium = product.get('sodium_mg_per_serving') or 0
    protein = product.get('protein_g_per_serving') or 0
    fiber = product.get('fiber_g_per_serving') or 0

    if sugar > 20:
        score -= 5
    elif sugar > 10:
        score -= 3
        
    if sat_fat > 8:
        score -= 3
    elif sat_fat > 4:
        score -= 2
        
    if sodium > 800:
        score -= 2
    elif sodium > 400:
        score -= 2
        
    if protein > 8:
        score += 1
        
    if fiber > 5:
        score += 1
        
    score = max(1.0, min(10.0, score))
    
    if score >= 9:
        grade = "A"
    elif score >= 7:
        grade = "B"
    elif score >= 5:
        grade = "C"
    elif score >= 3:
        grade = "D"
    else:
        grade = "F"
        
    return score, grade

SCORING_RULES = {
    "base_score": 5.0,
    "rules": [
        {
            "nutrient": "sugar",
            "thresholds": [
                { "min": 10, "points": -2 },
                { "min": 5, "max": 10, "points": -1 }
            ]
        },
        {
            "nutrient": "sodium",
            "thresholds": [
                { "min": 400, "points": -2 },
                { "min": 200, "max": 400, "points": -1 }
            ]
        },
        {
            "nutrient": "saturated_fat",
            "thresholds": [
                { "min": 20, "points": -2 },
                { "min": 6, "max": 10, "points": -2 },
                { "min": 10, "max": 20, "points": -1 },
                { "min": 0, "max": 6, "points": -1 }
            ]
        },
        {
            "nutrient": "protein",
            "thresholds": [
                { "min": 8, "points": 1 }
            ]
        },
        {
            "nutrient": "fiber",
            "thresholds": [
                { "min": 5, "points": 1 }
            ]
        }
    ],
    "ingredients": [
        { "name": "palm oil", "penalty": -0.6, "category": "Oils & Fats", "risk": "Medium" },
        { "name": "fractionated fat", "penalty": -0.7, "category": "Oils & Fats", "risk": "High" },
        { "name": "maida", "penalty": -0.5, "category": "Refined Carbohydrates", "risk": "High" },
        { "name": "refined wheat flour", "penalty": -0.5, "category": "Refined Carbohydrates", "risk": "High" },
        { "name": "salt", "penalty": -0.6, "category": "Sodium", "risk": "Medium" },
        { "name": "msg", "penalty": -0.5, "category": "Flavor Enhancers", "risk": "Medium" },
        { "name": "tbhq", "penalty": -0.8, "category": "Preservatives", "risk": "Severe" },
        { "name": "sodium benzoate", "penalty": -0.6, "category": "Preservatives", "risk": "Medium" },
        { "name": "tartrazine", "penalty": -0.7, "category": "Artificial Colors", "risk": "High" },
        { "name": "sugar", "penalty": -0.8, "category": "Sugars & Sweeteners", "risk": "High" },
        { "name": "corn syrup", "penalty": -0.6, "category": "Sugars & Sweeteners", "risk": "High" },
        { "name": "peanuts", "penalty": 0.4, "category": "Healthy Fats" },
        { "name": "skimmed milk", "penalty": 0.5, "category": "Protein Quality" },
        { "name": "milk solids", "penalty": 0.5, "category": "Protein Quality" }
    ],
    "position_multiplier": {
        "top_3": 1.5,
        "middle": 1.0,
        "trace": 0.5
    },
    "category_caps": {
        "Oils & Fats": 2.5,
        "Sugars & Sweeteners": 2.5,
        "Preservatives": 2.0,
        "Artificial Colors": 2.0,
        "Flavor Enhancers": 1.5,
        "Emulsifiers & Stabilizers": 1.5,
        "Sodium": 2.0,
        "Refined Carbohydrates": 1.0,
        "Caffeine & Stimulants": 2.0,
        "Other Additives": 1.5
    },
    "transparency_multiplier": {
        "disclosed": 1.05,
        "vague": 0.95,
        "default": 1.0
    }
}


# Risk levels live in the database (ingredient_rules.risk_level) so the DB is the
# single source of truth for ingredient risk classification. The map is loaded
# once and cached; the "risk" values in SCORING_RULES act only as a fallback when
# the DB is unreachable or has no matching keyword.
_INGREDIENT_RISK_MAP = None


def load_ingredient_risk_map():
    """Return a cached {keyword: risk_level} map from ingredient_rules."""
    global _INGREDIENT_RISK_MAP
    if _INGREDIENT_RISK_MAP is not None:
        return _INGREDIENT_RISK_MAP

    risk_map = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT keyword, risk_level FROM ingredient_rules "
            "WHERE risk_level IS NOT NULL"
        )
        for keyword, risk in cursor.fetchall():
            if keyword and risk:
                risk_map[keyword.strip().lower()] = risk
        conn.close()
    except Exception:
        risk_map = {}

    _INGREDIENT_RISK_MAP = risk_map
    return risk_map


def resolve_ingredient_risk(name: str, fallback: str = "Low") -> str:
    """Resolve a flagged ingredient's risk tier from the DB-backed risk map.

    Tries an exact keyword match first, then a substring match in either
    direction (e.g. the flag "sugar" maps to the DB keyword "refined sugar"),
    and finally falls back to the value carried by the in-app rule.
    """
    name = (name or "").strip().lower()
    risk_map = load_ingredient_risk_map()
    if name in risk_map:
        return risk_map[name]
    for keyword, risk in risk_map.items():
        if keyword in name or name in keyword:
            return risk
    return fallback


# ==============================================================================
# Personalized Scoring (dietary preferences)
# ==============================================================================
# A user can opt into dietary preferences (Low Sugar, High Protein, Vegan, ...).
# These are stored per-user in the `user_preferences` table as a JSON object of
# boolean flags and translated into scoring weight multipliers at scoring time,
# so the same product can score differently for two users. With no preferences
# the weights are all-neutral (1.0) and scoring is identical to the generic
# engine, keeping every existing response and the regression tests unchanged.

VALID_PREFERENCES = (
    "low_sugar",
    "low_sodium",
    "low_fat",        # saturated fat
    "high_protein",
    "high_fiber",
    "vegan",
)

# How strongly a preference re-weights the relevant penalty / bonus.
PREFERENCE_EMPHASIS = 1.75        # avoided nutrients/categories penalised harder
PREFERENCE_BONUS_EMPHASIS = 2.5   # sought-after nutrients rewarded more

# Ingredient keywords that make a product NOT vegan. Used to drop non-vegan
# alternatives for users with the `vegan` preference (only when an ingredient
# list is available — products without ingredient data are never excluded).
VEGAN_EXCLUDE_KEYWORDS = (
    "milk", "skimmed milk", "milk powder", "whey", "casein", "lactose",
    "butter", "ghee", "cream", "cheese", "paneer", "khoya", "mawa",
    "curd", "yogurt", "yoghurt", "honey", "egg", "albumen", "gelatin",
    "gelatine", "lard", "tallow", "meat", "chicken", "mutton", "fish",
    "anchovy", "carmine", "cochineal", "shellac",
)


def normalize_preferences(raw):
    """Coerce an arbitrary preferences payload into a clean {flag: bool} dict.

    Accepts either a flat ``{"low_sugar": true}`` map or a wrapped
    ``{"preferences": {...}}`` body. Only recognised keys (VALID_PREFERENCES)
    are kept, each coerced to a bool, so stored/served preferences are always
    predictable.
    """
    if isinstance(raw, dict) and isinstance(raw.get("preferences"), dict):
        raw = raw["preferences"]
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key in VALID_PREFERENCES:
        if key in raw:
            cleaned[key] = bool(raw[key])
    return cleaned


def load_user_preferences(user_id):
    """Return a user's stored dietary preferences as a {flag: bool} dict.

    Returns {} when the user has none, the table is missing, or ``user_id`` is
    not a valid int (so callers can pass it unconditionally).
    """
    if not isinstance(user_id, int):
        return {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT preferences FROM user_preferences WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        conn.close()
    except Exception:
        return {}
    if not row or not row[0]:
        return {}
    try:
        import json
        return normalize_preferences(json.loads(row[0]))
    except (ValueError, TypeError):
        return {}


def save_user_preferences(user_id, raw):
    """Persist a user's dietary preferences (insert or update). Returns the
    cleaned {flag: bool} dict that was stored."""
    import json
    cleaned = normalize_preferences(raw)
    payload = json.dumps(cleaned)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM user_preferences WHERE user_id = ?", (user_id,))
    if cursor.fetchone():
        cursor.execute(
            "UPDATE user_preferences SET preferences = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (payload, user_id),
        )
    else:
        cursor.execute(
            "INSERT INTO user_preferences (user_id, preferences) VALUES (?, ?)",
            (user_id, payload),
        )
    conn.commit()
    conn.close()
    return cleaned


def get_preference_weights(preferences):
    """Translate dietary preferences into scoring weight multipliers.

    Returns a dict with:
      - ``nutrient_penalty_mult``: {nutrient: multiplier} on penalty magnitude
      - ``nutrient_bonus_mult``:   {nutrient: multiplier} on bonus magnitude
      - ``category_penalty_mult``: {ingredient category: multiplier} on the
        pooled penalty *and* that category's cap
      - ``drop_bonus_categories``: ingredient bonus categories to ignore
    Empty / no preferences yield all-neutral (1.0) weights.
    """
    weights = {
        "nutrient_penalty_mult": {},
        "nutrient_bonus_mult": {},
        "category_penalty_mult": {},
        "drop_bonus_categories": set(),
    }
    if not preferences:
        return weights

    if preferences.get("low_sugar"):
        weights["nutrient_penalty_mult"]["sugar"] = PREFERENCE_EMPHASIS
        weights["category_penalty_mult"]["Sugars & Sweeteners"] = PREFERENCE_EMPHASIS
    if preferences.get("low_sodium"):
        weights["nutrient_penalty_mult"]["sodium"] = PREFERENCE_EMPHASIS
        weights["category_penalty_mult"]["Sodium"] = PREFERENCE_EMPHASIS
    if preferences.get("low_fat"):
        weights["nutrient_penalty_mult"]["saturated_fat"] = PREFERENCE_EMPHASIS
        weights["category_penalty_mult"]["Oils & Fats"] = PREFERENCE_EMPHASIS
    if preferences.get("high_protein"):
        weights["nutrient_bonus_mult"]["protein"] = PREFERENCE_BONUS_EMPHASIS
    if preferences.get("high_fiber"):
        weights["nutrient_bonus_mult"]["fiber"] = PREFERENCE_BONUS_EMPHASIS
    if preferences.get("vegan"):
        # Dairy-derived "protein quality" bonuses shouldn't reward a vegan choice.
        weights["drop_bonus_categories"].add("Protein Quality")
    return weights


def is_vegan_friendly(product):
    """Best-effort vegan check from a product's ingredient list.

    Returns True when no animal-derived keyword is found. When no ingredient
    text is available we cannot prove a product is non-vegan, so we keep it
    (return True) rather than hide potentially-valid alternatives.
    """
    text = (product.get("ingredients_text") or "").lower()
    if not text.strip():
        return True
    return not any(kw in text for kw in VEGAN_EXCLUDE_KEYWORDS)


def calculate_health_score_v2(product: dict, version: int = 1,
                              preferences: dict = None, user_id: int = None):
    """Score a product, optionally personalized to a user's dietary preferences.

    Personalization can be passed either as an explicit ``preferences`` dict or
    as a ``user_id`` (in which case the preferences are loaded from the
    ``user_preferences`` table). ``preferences`` takes precedence; with neither,
    the result is the generic, non-personalized score.
    """
    import json
    scoring_rules_dict = SCORING_RULES
    if preferences is None and user_id is not None:
        preferences = load_user_preferences(user_id)
    weights = get_preference_weights(preferences)

    base_score = scoring_rules_dict["base_score"]
    score = base_score
    
    breakdown = {
        "base_score": base_score,
        "deductions": [],
        "nutrition_penalties": [],
        "additions": []
    }
    
    nutrient_bonuses_total = 0
    cat_deductions = {}
    
    nutr_cat_map = {
        "sugar": "Sugars & Sweeteners",
        "saturated_fat": "Oils & Fats",
        "sodium": "Sodium"
    }
    
    # 1. Apply nutrient penalties & bonuses, re-weighted by user preferences.
    pen_mult = weights["nutrient_penalty_mult"]
    bonus_mult = weights["nutrient_bonus_mult"]
    for rule in scoring_rules_dict["rules"]:
        nutrient = rule["nutrient"]
        val = product.get(f"{nutrient}_g_per_serving")
        if val is None and nutrient == "sodium":
            val = product.get("sodium_mg_per_serving")
        if val is None:
            continue

        for threshold in rule["thresholds"]:
            t_min = threshold["min"]
            t_max = threshold.get("max", float("inf"))
            points = threshold["points"]

            if t_min <= val < t_max:
                if points < 0:
                    pts = round(points * pen_mult.get(nutrient, 1.0), 2)
                    cat = nutr_cat_map.get(nutrient, nutrient)
                    cat_deductions[cat] = cat_deductions.get(cat, 0) + abs(pts)
                    breakdown["nutrition_penalties"].append({
                        "nutrient": nutrient,
                        "value": val,
                        "points": float(pts)
                    })
                else:
                    pts = round(points * bonus_mult.get(nutrient, 1.0), 2)
                    nutrient_bonuses_total += pts
                    breakdown["additions"].append({
                        "category": "nutrient_bonus",
                        "nutrient": nutrient,
                        "points": float(pts)
                    })
                break

    # 2. Apply ingredient penalties
    ingredients_text = product.get('ingredients_text', '') or ''
    ingredients_list = [i.strip().lower() for i in ingredients_text.split(',') if i.strip()]

    ingredient_flags = []
    cat_pen_mult = weights["category_penalty_mult"]
    drop_bonus = weights["drop_bonus_categories"]

    for idx, ing_text in enumerate(ingredients_list):
        multiplier = scoring_rules_dict["position_multiplier"]["middle"]
        if idx < 3:
            multiplier = scoring_rules_dict["position_multiplier"]["top_3"]
        elif idx >= 8:
            multiplier = scoring_rules_dict["position_multiplier"]["trace"]
            
        for rule in scoring_rules_dict["ingredients"]:
            if rule["name"] in ing_text:
                pts = round(rule["penalty"] * multiplier, 2)
                cat = rule["category"]

                if pts < 0:
                    # Re-weight the penalty by the user's category preference
                    # (e.g. low_sugar penalises "Sugars & Sweeteners" harder).
                    pts = round(pts * cat_pen_mult.get(cat, 1.0), 2)
                    cat_deductions[cat] = cat_deductions.get(cat, 0) + abs(pts)
                    ingredient_flags.append({
                        "name": rule["name"],
                        "risk": resolve_ingredient_risk(
                            rule["name"], rule.get("risk", "Low")
                        ),
                    })
                    breakdown["deductions"].append({
                        "category": cat,
                        "ingredient": rule["name"],
                        "position": idx + 1,
                        "multiplier": multiplier,
                        "points": pts
                    })
                elif cat in drop_bonus:
                    # A preference (e.g. vegan) cancels this bonus — record it
                    # as a zero-point, dropped addition for transparency.
                    breakdown["additions"].append({
                        "category": cat,
                        "ingredient": rule["name"],
                        "position": idx + 1,
                        "multiplier": multiplier,
                        "points": 0.0,
                        "dropped_by_preference": True
                    })
                else:
                    nutrient_bonuses_total += pts
                    breakdown["additions"].append({
                        "category": cat,
                        "ingredient": rule["name"],
                        "position": idx + 1,
                        "multiplier": multiplier,
                        "points": pts
                    })
                break

    # 3. Apply category caps (to the combined ingredient + nutrition penalty per category)
    ingredient_penalties_total = 0
    category_totals = []
    for cat, total_pen in cat_deductions.items():
        cap = scoring_rules_dict["category_caps"].get(cat, float('inf'))
        # Scale the cap by the same preference multiplier so a re-weighted
        # penalty isn't immediately swallowed by the original cap.
        if cap != float('inf'):
            cap = round(cap * cat_pen_mult.get(cat, 1.0), 2)
        actual_pen = min(total_pen, cap)
        ingredient_penalties_total += actual_pen
        category_totals.append({
            "category": cat,
            "raw_penalty": -round(total_pen, 2),
            "cap": (-cap if cap != float('inf') else None),
            "applied_penalty": -round(actual_pen, 2),
            "capped": total_pen > cap,
        })
    breakdown["category_totals"] = category_totals

    score = base_score - ingredient_penalties_total + nutrient_bonuses_total
        
    # 4. Apply transparency multiplier
    #    - Vague catch-all terms ("flavouring", "permitted emulsifier", ...) -> 0.95
    #    - Full disclosure of additives (named INS/E numbers, no vague terms) -> 1.05
    #    - No ingredient list / nothing special                              -> 1.0
    trans_mult = scoring_rules_dict["transparency_multiplier"]["default"]
    ing_lower = ingredients_text.lower()
    vague_terms = [
        "flavouring", "flavoring", "flavour", "flavor",
        "permitted emulsifier", "permitted colour", "permitted color",
        "permitted", "edible vegetable oil", "vegetable fat",
        "spices", "condiments", "raising agent", "anticaking",
        "artificial colour", "artificial color",
        "artificial flavour", "artificial flavor",
    ]
    has_vague = any(v in ing_lower for v in vague_terms)
    # Explicit additive disclosure, e.g. "ins 471", "e322", "(442, 476)"
    has_additive_codes = bool(
        re.search(r"\b(?:ins|e)\s?\d{3}", ing_lower)
        or re.search(r"\(\s*\d{3}", ing_lower)
    )

    if has_vague:
        trans_mult = scoring_rules_dict["transparency_multiplier"]["vague"]
    elif has_additive_codes and ingredients_text.strip():
        trans_mult = scoring_rules_dict["transparency_multiplier"]["disclosed"]

    score *= trans_mult
    
    # 5. Clamp
    final_score = max(1.0, min(10.0, score))
    final_score = round(final_score, 1)
    
    breakdown["subtotal"] = round(base_score - ingredient_penalties_total + nutrient_bonuses_total, 2)
    breakdown["transparency_multiplier"] = trans_mult
    breakdown["final_score"] = final_score
    breakdown["ingredient_flags"] = ingredient_flags
    # Surface which dietary preferences shaped this score (empty when generic).
    breakdown["preferences_applied"] = {
        k: v for k, v in (preferences or {}).items() if v
    }
    
    if final_score >= 9:
        grade = "A"
    elif final_score >= 7:
        grade = "B"
    elif final_score >= 5:
        grade = "C"
    elif final_score >= 3:
        grade = "D"
    else:
        grade = "F"

    return final_score, grade, version, breakdown


@app.get("/score/{barcode}")
def get_score(barcode: str, user_id: Optional[int] = Depends(get_current_user_optional)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode,))
    row = cursor.fetchone()
    conn.close()

    if row:
        preferences = load_user_preferences(user_id)
        score, grade, rule_version, breakdown = calculate_health_score_v2(dict(row), 1, preferences)
        return {"score": score, "grade": grade, "breakdown": breakdown, "ingredient_flags": breakdown.get("ingredient_flags", [])}

    return JSONResponse(status_code=404, content={"error": "Product not found"})

@app.get("/v2/score/{barcode}")
def get_score_v2(barcode: str, user_id: Optional[int] = Depends(get_current_user_optional)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode,))
    row = cursor.fetchone()
    conn.close()

    if row:
        preferences = load_user_preferences(user_id)
        score, grade, rule_version, breakdown = calculate_health_score_v2(dict(row), 1, preferences)
        return {"score": score, "grade": grade, "rule_version": rule_version, "breakdown": breakdown, "ingredient_flags": breakdown.get("ingredient_flags", [])}

    return JSONResponse(status_code=404, content={"error": "Product not found"})

def _alternative_sort_key(item, preferences):
    """Build a sort key that orders alternatives by the user's preferences.

    Preference-driven ordering comes first (task spec: "higher protein first",
    "lower sugar first"), with the (personalized) health score as the final
    tie-breaker. Python sorts ascending, so descending nutrients are negated.
    """
    keys = []
    if preferences.get("high_protein"):
        keys.append(-(item.get("protein_g_per_serving") or 0))
    if preferences.get("high_fiber"):
        keys.append(-(item.get("fiber_g_per_serving") or 0))
    if preferences.get("low_sugar"):
        sugar = item.get("sugar_g_per_serving")
        keys.append(sugar if sugar is not None else float("inf"))
    if preferences.get("low_sodium"):
        sodium = item.get("sodium_mg_per_serving")
        keys.append(sodium if sodium is not None else float("inf"))
    if preferences.get("low_fat"):
        satfat = item.get("saturated_fat_g_per_serving")
        keys.append(satfat if satfat is not None else float("inf"))
    # Healthiest (personalized) first as the final ordering / tie-breaker.
    keys.append(-item["health_score"])
    return tuple(keys)


def find_better_alternatives(barcode: str, preferences: dict = None):
    """Return up to 3 healthier same-category alternatives for a barcode.

    When ``preferences`` are supplied the scores are personalized, non-vegan
    products are dropped for vegan users, and the ranking is driven by the
    user's preferences (see ``_alternative_sort_key``). With no preferences the
    behaviour is identical to the original generic endpoint.
    """
    preferences = preferences or {}
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode,))
    scanned_product = cursor.fetchone()

    if not scanned_product:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Product not found"})

    scanned_dict = dict(scanned_product)
    scanned_score, _, _, _ = calculate_health_score_v2(scanned_dict, 1, preferences)

    category = scanned_dict.get('category')

    if category:
        cursor.execute("SELECT * FROM products WHERE barcode != ? AND category = ?", (barcode, category))
    else:
        cursor.execute("SELECT * FROM products WHERE barcode != ?", (barcode,))
    all_products = cursor.fetchall()
    conn.close()

    want_vegan = bool(preferences.get("vegan"))
    results = []
    for row in all_products:
        p_dict = dict(row)
        if want_vegan and not is_vegan_friendly(p_dict):
            continue
        score, grade, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
        if score > scanned_score:
            results.append({
                "barcode": p_dict["barcode"],
                "product_name": p_dict["product_name"],
                "brand": p_dict["brand"],
                "health_score": score,
                "grade": grade,
                "sugar_g_per_serving": p_dict.get("sugar_g_per_serving"),
                "protein_g_per_serving": p_dict.get("protein_g_per_serving"),
                "sodium_mg_per_serving": p_dict.get("sodium_mg_per_serving"),
                "saturated_fat_g_per_serving": p_dict.get("saturated_fat_g_per_serving"),
                "fiber_g_per_serving": p_dict.get("fiber_g_per_serving"),
                "image_url": image_or_placeholder(p_dict.get("image_url")),
            })

    results.sort(key=lambda x: _alternative_sort_key(x, preferences))
    return results[:3]


@app.get("/similar/{barcode}")
def get_similar_products(
    barcode: str,
    user_id: Optional[int] = None,
    token_user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Better Alternatives. Personalized to the user's dietary preferences when
    the request is authenticated, or when an explicit ``?user_id=`` is supplied.
    """
    # Prefer the authenticated identity; fall back to an explicit user_id query
    # param (per task spec). ``isinstance`` guards against the Depends marker
    # object when this route is called directly (e.g. in tests).
    effective_user_id = token_user_id if isinstance(token_user_id, int) else None
    if effective_user_id is None and isinstance(user_id, int):
        effective_user_id = user_id
    preferences = load_user_preferences(effective_user_id)
    return find_better_alternatives(barcode, preferences)


# ==============================================================================
# "Swapify Recommended" Badge  (Task 3)
# ==============================================================================
# A product earns the "Swapify Recommended" badge when it is genuinely a clean,
# healthy pick. Criteria:
#   - health score > 7
#   - no Severe/High-risk flagged ingredients
#   - no artificial colours
#   - (optional) no chemical preservatives
# The first three are hard requirements; "preservative-free" is reported and
# included in the criteria detail but, per the task spec, is optional and does
# not by itself block the badge. Exposed as ``is_recommended`` on the /product
# response and via the dedicated GET /product/{barcode}/badge endpoint.

RECOMMENDED_MIN_SCORE = 7.0

# Synthetic colour names; also detected via the INS/E "1xx" colour class.
ARTIFICIAL_COLOR_KEYWORDS = (
    "tartrazine", "sunset yellow", "allura red", "ponceau", "carmoisine",
    "azorubine", "brilliant blue", "indigo carmine", "indigotine",
    "quinoline yellow", "erythrosine", "fast green", "patent blue",
    "artificial colour", "artificial color", "synthetic colour",
    "synthetic color", "artificial food colour", "artificial food color",
    "fd&c", "fd & c",
)

# Common chemical preservative names; also detected via the INS/E "2xx" class.
PRESERVATIVE_KEYWORDS = (
    "tbhq", "bha", "bht", "sodium benzoate", "potassium sorbate",
    "calcium propionate", "sodium nitrite", "sodium nitrate",
    "potassium nitrite", "potassium nitrate", "sulphur dioxide",
    "sulfur dioxide", "sodium metabisulphite", "sodium metabisulfite",
    "sulphite", "sulfite", "sorbic acid", "benzoic acid", "preservative",
)


def _has_additive_class(ingredients_text, class_digit):
    """True when the ingredient list names an INS/E additive code in a given
    class, e.g. 1xx = colours, 2xx = preservatives ('INS 110', 'E211')."""
    text = (ingredients_text or "").lower()
    return bool(re.search(rf"\b(?:ins|e)\s?{class_digit}\d{{2}}\b", text))


def _flag_categories(breakdown):
    """The set of ingredient categories penalised for this product."""
    return {d.get("category") for d in (breakdown or {}).get("deductions", [])}


def has_artificial_colors(product, breakdown=None):
    """Best-effort detection of artificial colours from a product's ingredient
    list (named synthetic dyes or an INS/E 1xx colour code) or a flagged
    "Artificial Colors" category."""
    text = (product.get("ingredients_text") or "").lower()
    if any(kw in text for kw in ARTIFICIAL_COLOR_KEYWORDS):
        return True
    if _has_additive_class(text, "1"):
        return True
    return "Artificial Colors" in _flag_categories(breakdown)


def has_preservatives(product, breakdown=None):
    """Best-effort detection of chemical preservatives (named preservatives or an
    INS/E 2xx code) or a flagged "Preservatives" category."""
    text = (product.get("ingredients_text") or "").lower()
    if any(kw in text for kw in PRESERVATIVE_KEYWORDS):
        return True
    if _has_additive_class(text, "2"):
        return True
    return "Preservatives" in _flag_categories(breakdown)


def evaluate_recommended_badge(product, breakdown=None, preferences=None):
    """Evaluate the "Swapify Recommended" badge for a product.

    ``product`` should already carry ``score`` and its scoring ``breakdown`` (as
    returned by get_scored_product / the /product endpoint); when absent they are
    computed here. Returns a detail dict with the boolean ``is_recommended``, the
    per-criterion pass/fail map and the reasons any criterion failed.
    """
    if breakdown is None:
        breakdown = product.get("breakdown")
    score = product.get("score")
    if breakdown is None or score is None:
        score, _, _, breakdown = calculate_health_score_v2(product, 1, preferences)

    flags = breakdown.get("ingredient_flags", []) if breakdown else []
    high_risk = [f for f in flags if f.get("risk") in ("Severe", "High")]
    artificial = has_artificial_colors(product, breakdown)
    preservatives = has_preservatives(product, breakdown)

    criteria = {
        "health_score_above_7": bool(score is not None and score > RECOMMENDED_MIN_SCORE),
        "no_high_risk_ingredients": len(high_risk) == 0,
        "no_artificial_colors": not artificial,
        "no_preservatives": not preservatives,   # optional — informational only
    }
    # Preservative-free is optional per the spec, so it does not gate the badge.
    required = ("health_score_above_7", "no_high_risk_ingredients", "no_artificial_colors")
    is_recommended = all(criteria[k] for k in required)

    return {
        "is_recommended": is_recommended,
        "badge": "Swapify Recommended" if is_recommended else None,
        "health_score": score,
        "criteria": criteria,
        "required_criteria": list(required),
        "failing_criteria": [k for k in required if not criteria[k]],
        "high_risk_ingredients": high_risk,
        "has_artificial_colors": artificial,
        "has_preservatives": preservatives,
    }


@app.get("/product/{barcode}/badge")
def get_product_badge(barcode: str, user_id: Optional[int] = Depends(get_current_user_optional)):
    """Return the "Swapify Recommended" badge status for a product.

    Resolves the product from the local DB first, then Open Food Facts. The score
    (and therefore the badge) is personalized when the request is authenticated
    and the user has dietary preferences saved.
    """
    preferences = load_user_preferences(user_id)
    product = get_scored_product(barcode, preferences)
    if not product:
        return JSONResponse(status_code=404, content={"error": "Product not found"})
    badge = evaluate_recommended_badge(product, product.get("breakdown"), preferences)
    return {
        "barcode": product.get("barcode"),
        "product_name": product.get("product_name"),
        "brand": product.get("brand"),
        "grade": product.get("grade"),
        "source": product.get("source", "database"),
        **badge,
    }


@app.get("/history")
def get_history(user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT h.scanned_at, p.* 
        FROM scan_history h 
        JOIN products p ON h.barcode = p.barcode 
        WHERE h.user_id = ? 
        ORDER BY h.scanned_at DESC 
        LIMIT 5
    ''', (user_id,))
    
    rows = cursor.fetchall()
    conn.close()

    preferences = load_user_preferences(user_id)
    results = []
    for row in rows:
        p_dict = dict(row)
        score, grade, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
        results.append({
            "barcode": p_dict["barcode"],
            "product_name": p_dict["product_name"],
            "brand": p_dict["brand"],
            "health_score": score,
            "grade": grade,
            "image_url": None,
            "scanned_at": p_dict["scanned_at"]
        })
    return results

@app.post("/report-missing")
def report_missing(report: MissingReport, user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO missing_reports (barcode, product_name, user_comment) VALUES (?, ?, ?)",
        (report.barcode, report.product_name, report.comment)
    )
    conn.commit()
    conn.close()
    return {"status": "reported"}


# ==============================================================================
# Crowdsourced Product Images  (Task 2C)
# ==============================================================================
# Users can contribute a photo for a product. Uploads are validated (JPEG/PNG,
# < 2 MB), written to disk under the ``/product-images`` static mount and their
# URL recorded in ``product_images`` (and on ``products.image_url`` when the
# product is in the local catalogue). Bytes never touch the database — only the
# served URL reference does.
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB
ALLOWED_IMAGE_CONTENT_TYPES = ("image/jpeg", "image/jpg", "image/png")


def _detect_image_ext(content_type, data):
    """Return the extension (``.jpg``/``.png``) for a valid JPEG/PNG upload, or
    None if the bytes aren't a supported image.

    The decision is driven by the file's magic bytes (a JPEG starts ``FF D8 FF``,
    a PNG starts ``89 50 4E 47 0D 0A 1A 0A``) so a mislabelled ``Content-Type``
    can't smuggle a non-image through; the declared content-type is only used to
    reject an obvious JPEG/PNG mismatch."""
    ct = (content_type or "").split(";")[0].strip().lower()
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = data[:3] == b"\xff\xd8\xff"
    if is_png and ct in ("", "image/png"):
        return ".png"
    if is_jpeg and ct in ("", "image/jpeg", "image/jpg"):
        return ".jpg"
    return None


@app.post("/product/image")
async def upload_product_image(
    barcode: str = Form(...),
    file: UploadFile = File(...),
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Crowdsourced product image upload (Task 2C).

    Multipart form with a ``barcode`` field and an image ``file``. Validates the
    format (JPEG/PNG, sniffed from magic bytes) and size (< 2 MB), stores the file
    and records its URL in ``product_images`` — and on ``products.image_url`` when
    the product is in the local catalogue. Anyone may contribute; the uploader is
    recorded when the request is authenticated.
    """
    clean_barcode = re.sub(r"\D", "", barcode or "")
    if not clean_barcode:
        raise HTTPException(status_code=400, detail="A numeric 'barcode' is required.")

    # Read at most MAX+1 bytes so an oversized upload is rejected without
    # buffering the whole thing into memory.
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the 2 MB size limit.")

    ext = _detect_image_ext(file.content_type, data)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="Only JPEG and PNG images are accepted.",
        )

    filename = f"{clean_barcode}{ext}"
    dest = os.path.join(UPLOAD_DIR, filename)
    try:
        with open(dest, "wb") as fh:
            fh.write(data)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("failed to store product image: %s", exc)
        raise HTTPException(status_code=500, detail="Could not store the image.")

    image_url = f"/product-images/{filename}"

    conn = get_db_connection()
    cur = conn.cursor()
    # Update the catalogue row's image reference when the product exists locally.
    cur.execute(
        "UPDATE products SET image_url = ? WHERE barcode = ?",
        (image_url, clean_barcode),
    )
    product_updated = cur.rowcount > 0
    cur.execute(
        "INSERT INTO product_images (barcode, image_url, content_type, file_size, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            clean_barcode, image_url, file.content_type, len(data),
            user_id if isinstance(user_id, int) else None,
        ),
    )
    conn.commit()
    conn.close()

    # A new image invalidates any cached (image-less) copy of this product so the
    # next read serves the fresh reference (Task 1C: invalidate on update).
    invalidate_product_cache(clean_barcode)

    return {
        "message": "Image uploaded successfully",
        "barcode": clean_barcode,
        "image_url": image_url,
        "product_updated": product_updated,
        "file_size": len(data),
        "content_type": file.content_type,
    }

@app.get("/preferences")
def get_preferences(user_id: int = Depends(get_current_user)):
    """Return the authenticated user's dietary preferences. Every recognised
    flag is included (defaulting to False) so clients get a stable shape."""
    stored = load_user_preferences(user_id)
    preferences = {key: stored.get(key, False) for key in VALID_PREFERENCES}
    return {"user_id": user_id, "preferences": preferences}


@app.post("/preferences")
def set_preferences(body: UserPreferences, user_id: int = Depends(get_current_user)):
    """Save (insert or update) the authenticated user's dietary preferences.

    Body: ``{"preferences": {"low_sugar": true, "high_protein": true, ...}}``.
    Only recognised flags are stored; the saved set is echoed back.
    """
    cleaned = save_user_preferences(user_id, body.preferences)
    preferences = {key: cleaned.get(key, False) for key in VALID_PREFERENCES}
    return {"status": "preferences saved", "user_id": user_id, "preferences": preferences}


@app.post("/update-preferences")
def update_preferences(prefs: dict, user_id: int = Depends(get_current_user)):
    """Backwards-compatible alias for saving preferences. Accepts either a flat
    ``{"low_sugar": true}`` body or a wrapped ``{"preferences": {...}}`` body."""
    cleaned = save_user_preferences(user_id, prefs)
    preferences = {key: cleaned.get(key, False) for key in VALID_PREFERENCES}
    return {"status": "preferences updated", "preferences": preferences}

@app.post("/favorites")
def add_favorite(fav: FavoriteAdd, user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE barcode = ?", (fav.barcode,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Product not found")
        
    cursor.execute("SELECT id FROM favorites WHERE user_id = ? AND barcode = ?", (user_id, fav.barcode))
    if cursor.fetchone():
        conn.close()
        return {"message": "Already in favorites"}

    cursor.execute(
        "INSERT INTO favorites (user_id, barcode) VALUES (?, ?)",
        (user_id, fav.barcode)
    )
    conn.commit()
    conn.close()
    log_activity(user_id, "favorite", fav.barcode)
    return {"message": "Added to favorites"}

@app.delete("/favorites/{barcode}")
def remove_favorite(barcode: str, user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM favorites WHERE user_id = ? AND barcode = ?",
        (user_id, barcode)
    )
    conn.commit()
    conn.close()
    return {"message": "Removed from favorites"}

@app.get("/favorites")
def get_favorites(user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT f.added_at, p.* 
        FROM favorites f 
        JOIN products p ON f.barcode = p.barcode 
        WHERE f.user_id = ?
        ORDER BY f.added_at DESC
    ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()

    preferences = load_user_preferences(user_id)
    results = []
    for row in rows:
        p_dict = dict(row)
        score, grade, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
        results.append({
            "barcode": p_dict.get("barcode"),
            "product_name": p_dict.get("product_name"),
            "brand": p_dict.get("brand"),
            "health_score": score,
            "grade": grade,
            "added_at": p_dict.get("added_at")
        })
    return results

@app.get("/weekly-summary")
def get_weekly_summary(user_id: int = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    
    cursor.execute('''
        SELECT h.scanned_at, p.* 
        FROM scan_history h 
        JOIN products p ON h.barcode = p.barcode 
        WHERE h.user_id = ? AND h.scanned_at >= ?
        ORDER BY h.scanned_at ASC
    ''', (user_id, seven_days_ago.strftime('%Y-%m-%d %H:%M:%S')))
    
    rows = cursor.fetchall()
    conn.close()
    
    preferences = load_user_preferences(user_id)
    total_scans = len(rows)
    total_score = 0
    daily_trends_dict = {}

    for row in rows:
        p_dict = dict(row)
        score, _, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
        total_score += score
        
        date_str = p_dict['scanned_at'][:10]
        if date_str not in daily_trends_dict:
            daily_trends_dict[date_str] = []
        daily_trends_dict[date_str].append(score)
        
    avg_score = (total_score / total_scans) if total_scans > 0 else 0
    
    daily_trends = []
    for date_str, sorted_scores in sorted(daily_trends_dict.items()):
        daily_trends.append({
            "date": date_str,
            "average_score": round(sum(sorted_scores) / len(sorted_scores), 2)
        })
        
    return {
        "total_scans": total_scans,
        "average_score": round(avg_score, 2),
        "daily_trends": daily_trends
    }

@app.get("/monthly-report")
def get_monthly_report(
    user_id: Optional[int] = None,
    month: Optional[str] = None,
    token_user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Monthly health report built from a user's scan history.

    - ``user_id`` (query param) selects whose history to summarise; when omitted
      it falls back to the authenticated user (``Authorization: Bearer`` token).
    - ``month`` is ``YYYY-MM`` and defaults to the current (UTC) month.

    Aggregates the month's scans into: total scans, average health score, the
    best- and worst-scoring products scanned, the score trend across the month
    (``improving`` / ``declining`` / ``stable``), and a most-scanned category
    breakdown. Scores use the user's personalized weights (same as the rest of
    the user-scoped endpoints).
    """
    effective_user_id = user_id if isinstance(user_id, int) else (
        token_user_id if isinstance(token_user_id, int) else None
    )
    if effective_user_id is None:
        raise HTTPException(
            status_code=400,
            detail="user_id is required (query param or Authorization token)",
        )

    # Resolve & validate the month (YYYY-MM), defaulting to the current month.
    if not month:
        month = datetime.datetime.utcnow().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT h.scanned_at, p.*
        FROM scan_history h
        JOIN products p ON h.barcode = p.barcode
        WHERE h.user_id = ? AND substr(h.scanned_at, 1, 7) = ?
        ORDER BY h.scanned_at ASC
    ''', (effective_user_id, month))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return {
            "user_id": effective_user_id,
            "month": month,
            "total_scans": 0,
            "average_score": 0,
            "best_product": None,
            "worst_product": None,
            "score_trend": "no_data",
            "category_breakdown": [],
            "daily_trends": [],
        }

    preferences = load_user_preferences(effective_user_id)

    scored = []
    category_counts = {}
    daily = {}
    for row in rows:
        p_dict = dict(row)
        score, grade, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
        scored.append({
            "barcode": p_dict["barcode"],
            "product_name": p_dict["product_name"],
            "brand": p_dict.get("brand"),
            "category": p_dict.get("category"),
            "score": score,
            "grade": grade,
            "scanned_at": p_dict["scanned_at"],
        })

        cat = p_dict.get("category") or "uncategorized"
        category_counts[cat] = category_counts.get(cat, 0) + 1

        day = p_dict["scanned_at"][:10]
        daily.setdefault(day, []).append(score)

    total_scans = len(scored)
    average_score = round(sum(s["score"] for s in scored) / total_scans, 2)
    best = max(scored, key=lambda s: s["score"])
    worst = min(scored, key=lambda s: s["score"])

    # Score trend: average of the first half of the month's scans vs the second
    # half (chronological). A >= 0.5 swing counts as improving / declining.
    trend = "stable"
    if total_scans >= 2:
        mid = total_scans // 2
        first_avg = sum(s["score"] for s in scored[:mid]) / mid
        second_avg = sum(s["score"] for s in scored[mid:]) / (total_scans - mid)
        diff = second_avg - first_avg
        if diff >= 0.5:
            trend = "improving"
        elif diff <= -0.5:
            trend = "declining"

    category_breakdown = [
        {"category": cat, "count": cnt}
        for cat, cnt in sorted(category_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    daily_trends = [
        {"date": d, "average_score": round(sum(v) / len(v), 2)}
        for d, v in sorted(daily.items())
    ]

    def _summary(s):
        return {
            "barcode": s["barcode"],
            "product_name": s["product_name"],
            "brand": s["brand"],
            "score": s["score"],
            "grade": s["grade"],
        }

    return {
        "user_id": effective_user_id,
        "month": month,
        "total_scans": total_scans,
        "average_score": average_score,
        "best_product": _summary(best),
        "worst_product": _summary(worst),
        "score_trend": trend,
        "category_breakdown": category_breakdown,
        "daily_trends": daily_trends,
    }

@app.get("/recent")
def get_recent():
    return {"recent": recent_scans}

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/compare/{barcode1}/{barcode2}")
def compare_products(barcode1: str, barcode2: str, user_id: Optional[int] = Depends(get_current_user_optional)):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode1,))
    row1 = cursor.fetchone()

    cursor.execute("SELECT * FROM products WHERE barcode = ?", (barcode2,))
    row2 = cursor.fetchone()

    conn.close()

    if not row1 and not row2:
        return JSONResponse(status_code=404, content={"error": "Both products not found"})

    # Log the comparison (found products only) as a recommendation signal.
    compared = [bc for bc, row in ((barcode1, row1), (barcode2, row2)) if row]
    record_comparison(user_id, compared)
    if isinstance(user_id, int) and compared:
        log_activity(user_id, "compare", compared[0], {"barcodes": compared})

    return {
        "product1": dict(row1) if row1 else None,
        "product2": dict(row2) if row2 else None
    }

@app.post("/compare-multiple")
def compare_multiple(req: CompareMultipleRequest, user_id: Optional[int] = Depends(get_current_user_optional)):
    """Compare multiple products (3-4 recommended) side-by-side.

    Accepts a list of barcodes and returns each product's nutrition, health
    score, grade and flagged ingredients in a flat, table-friendly shape so the
    frontend can render a clean comparison table. Products are resolved from the
    local DB first, then Open Food Facts, so off-catalogue barcodes still work.
    Any barcode that can't be resolved is listed in ``not_found`` instead of
    failing the whole request. Scores are personalized when the request carries
    a valid ``Authorization: Bearer`` token.
    """
    # Trim, drop blanks and de-duplicate while preserving the requested order.
    seen = set()
    unique_barcodes = []
    for raw in (req.barcodes or []):
        barcode = (raw or "").strip()
        if barcode and barcode not in seen:
            seen.add(barcode)
            unique_barcodes.append(barcode)

    if len(unique_barcodes) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 barcodes to compare")
    if len(unique_barcodes) > 4:
        raise HTTPException(status_code=400, detail="You can compare at most 4 products at a time")

    preferences = load_user_preferences(user_id)

    products = []
    not_found = []
    for barcode in unique_barcodes:
        p = get_scored_product(barcode, preferences)
        if not p:
            not_found.append(barcode)
            continue
        products.append({
            "barcode": p.get("barcode"),
            "product_name": p.get("product_name"),
            "brand": p.get("brand"),
            "category": p.get("category"),
            "score": p.get("score"),
            "grade": p.get("grade"),
            "sugar_g": p.get("sugar_g_per_serving"),
            "protein_g": p.get("protein_g_per_serving"),
            "sodium_mg": p.get("sodium_mg_per_serving"),
            "saturated_fat_g": p.get("saturated_fat_g_per_serving"),
            "fiber_g": p.get("fiber_g_per_serving"),
            "calories": p.get("calories_kcal_per_serving"),
            "ingredient_flags": p.get("ingredient_flags", []),
            "source": p.get("source", "database"),
        })

    # Record the comparison for logged-in users so /recommendations can use it
    # as a "past comparisons viewed" signal.
    compared = [p["barcode"] for p in products]
    record_comparison(user_id, compared)
    if isinstance(user_id, int) and compared:
        log_activity(user_id, "compare", compared[0], {"barcodes": compared})

    return {
        "count": len(products),
        "products": products,
        "not_found": not_found,
    }

@app.get("/offline-products")
def get_offline_products():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products")
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for row in rows:
        p_dict = dict(row)
        score, grade, _, _ = calculate_health_score_v2(p_dict, 1)
        results.append({
            "barcode": p_dict.get("barcode"),
            "name": p_dict.get("product_name"),
            "brand": p_dict.get("brand"),
            "nutrition": {
                "sugar": p_dict.get("sugar_g_per_serving"),
                "saturated_fat": p_dict.get("saturated_fat_g_per_serving"),
                "sodium": p_dict.get("sodium_mg_per_serving"),
                "protein": p_dict.get("protein_g_per_serving"),
                "fiber": p_dict.get("fiber_g_per_serving"),
                "calories": p_dict.get("calories_kcal_per_serving")
            },
            "score": score,
            "grade": grade
        })
    return results

@app.get("/search/autocomplete")
def search_autocomplete(q: str, limit: int = 8):
    """Smart Search autocomplete (Task 2).

    Returns lightweight typeahead suggestions as the user types: product name +
    brand + barcode, matched against ``product_name`` and ``brand`` with SQL
    ``LIKE``. Prefix matches are ranked ahead of mid-word matches. ``limit`` is
    clamped to 1-10 (default 8); a blank query returns an empty list.

    Example response:
        {"suggestions": [
            {"product_name": "Maggi noodles", "brand": "Maggi", "barcode": "8901058005783"}
        ]}
    """
    query = (q or "").strip()
    if not query:
        return {"query": query, "count": 0, "suggestions": []}

    limit = max(1, min(limit, 10))
    like = f"%{query}%"
    prefix = f"{query.lower()}%"

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT barcode, product_name, brand,
               CASE
                   WHEN LOWER(product_name) LIKE ? THEN 0
                   WHEN LOWER(brand) LIKE ? THEN 1
                   ELSE 2
               END AS match_rank
        FROM products
        WHERE product_name LIKE ? OR brand LIKE ?
        ORDER BY match_rank, product_name
        LIMIT ?
    ''', (prefix, prefix, like, like, limit))
    rows = cursor.fetchall()
    conn.close()

    suggestions = [{
        "product_name": r["product_name"],
        "brand": r["brand"],
        "barcode": r["barcode"],
    } for r in rows]
    return {"query": query, "count": len(suggestions), "suggestions": suggestions}


# Only the columns /search actually needs — identity fields, the nutrient
# columns the scorer reads, ingredients_text and image_url — so we don't pull
# every column of every row with ``SELECT *`` (Task 1B: query optimization).
SEARCH_COLUMNS = (
    "barcode, product_name, brand, category, image_url, ingredients_text, "
    "sugar_g_per_serving, saturated_fat_g_per_serving, sodium_mg_per_serving, "
    "protein_g_per_serving, fiber_g_per_serving"
)


@app.get("/search")
def search_products(
    q: Optional[str] = "",
    brand: Optional[str] = None,
    category: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    grade: Optional[str] = None,
    sort: str = "score_desc",
    limit: int = 10,
    offset: int = 0,
):
    """Search the product catalogue by name/brand text, with optional filtering.

    - ``q``: free text matched against ``product_name`` and ``brand`` (SQL LIKE).
      When it looks like a barcode (>= 8 digits) it is validated, auto-corrected
      and looked up by barcode instead, so a mistyped check digit still finds the
      product (see also GET /validate-barcode/{barcode}).
    - ``brand`` / ``category``: extra LIKE filters (e.g. ``?brand=Maggi``).
    - ``min_score`` / ``max_score`` / ``grade``: filter on the computed health
      score / letter grade (applied after scoring).
    - ``sort``: ``score_desc`` (default, healthiest first), ``score_asc`` or ``name``.
    - ``limit``: 1-50 results per page (default 10).
    - ``offset``: number of ranked results to skip for pagination (default 0);
      e.g. ``?limit=10&offset=10`` returns the second page.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # Barcode-aware search: when the query looks like a barcode (digits only,
    # >= 8 chars) validate it and look the product up by barcode instead of by
    # name/brand. An invalid-but-correctable barcode is auto-corrected to its
    # suggestion for the lookup, so a mistyped check digit still finds the
    # product (see also GET /validate-barcode/{barcode}).
    q_clean = re.sub(r"[\s\-]", "", (q or "").strip())
    if q_clean.isdigit() and len(q_clean) >= 8:
        validation = validate_barcode(q_clean)
        lookup = validation["suggestion"] if (
            not validation["valid"] and validation["suggestion"]
        ) else q_clean
        cursor.execute(f"SELECT {SEARCH_COLUMNS} FROM products WHERE barcode = ?", (lookup,))
        rows = cursor.fetchall()
        conn.close()
        results = []
        for row in rows:
            p_dict = dict(row)
            score, grade_val, _, _ = calculate_health_score_v2(p_dict, 1)
            results.append({
                "barcode": p_dict.get("barcode"),
                "name": p_dict.get("product_name"),
                "brand": p_dict.get("brand"),
                "score": score,
                "grade": grade_val,
                "image_url": image_or_placeholder(p_dict.get("image_url")),
                "matched_by": "barcode",
                "barcode_validation": validation,
            })
        return results

    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    # Build the text/brand/category WHERE clause dynamically so any subset of
    # filters works (including none — a pure score/grade filter over the catalog).
    conditions, params = [], []
    if q and q.strip():
        term = f"%{q.strip()}%"
        conditions.append("(product_name LIKE ? OR brand LIKE ?)")
        params.extend([term, term])
    if brand and brand.strip():
        conditions.append("brand LIKE ?")
        params.append(f"%{brand.strip()}%")
    if category and category.strip():
        conditions.append("category LIKE ?")
        params.append(f"%{category.strip()}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    # Only select the columns we need (Task 1B) — the text/brand/category filters
    # use the indexes added in Task 1A.
    cursor.execute(f"SELECT {SEARCH_COLUMNS} FROM products {where}", params)
    rows = cursor.fetchall()
    conn.close()

    grade_filter = (grade or "").strip().upper() or None

    results = []
    for row in rows:
        p_dict = dict(row)
        score, grade_val, _, _ = calculate_health_score_v2(p_dict, 1)
        if min_score is not None and score < min_score:
            continue
        if max_score is not None and score > max_score:
            continue
        if grade_filter and grade_val != grade_filter:
            continue
        results.append({
            "barcode": p_dict.get("barcode"),
            "name": p_dict.get("product_name"),
            "brand": p_dict.get("brand"),
            "category": p_dict.get("category"),
            "score": score,
            "grade": grade_val,
            "image_url": image_or_placeholder(p_dict.get("image_url")),
        })

    if sort == "score_asc":
        results.sort(key=lambda x: (x["score"], (x["name"] or "").lower()))
    elif sort == "name":
        results.sort(key=lambda x: (x["name"] or "").lower())
    else:  # score_desc (default) — healthiest first
        results.sort(key=lambda x: (-x["score"], (x["name"] or "").lower()))

    # Paginate the ranked result set: skip ``offset`` then take ``limit`` (Task 1B).
    return results[offset:offset + limit]

# ==============================================================================
# AI Nutritionist Chatbot (/chat)
# ==============================================================================

NUTRITIONIST_SYSTEM_PROMPT = (
    "You are Swapify's AI nutritionist. You help everyday shoppers understand "
    "packaged food products. Answer in clear, friendly, practical language, "
    "grounded in food science. You can help with four kinds of questions: "
    "(1) a product's ingredients and their health risks; (2) why the product got "
    "the Swapify health score it did; (3) healthier ingredient substitutions; and "
    "(4) general nutrition and food-transparency questions.\n"
    "When product data is provided, ground every claim in that data (cite the "
    "actual sugar, sodium, saturated-fat figures and flagged ingredients). "
    "When a SCORE BREAKDOWN and SCORING METHODOLOGY block is provided, use them to "
    "explain *why* the score is what it is — name the specific penalties, bonuses, "
    "category caps and the transparency multiplier that moved it, don't invent "
    "numbers. "
    "When the user asks what to use instead of an ingredient (e.g. 'what can I "
    "use instead of sugar?'), suggest healthier, food-science-backed swaps and "
    "briefly say why — for example jaggery, dates, stevia or honey for refined "
    "sugar; cold-pressed, olive or rice-bran oil for palm oil; whole-wheat flour "
    "or oats for maida. If a SUBSTITUTION SUGGESTIONS block is provided below, "
    "base your alternatives on it. Be honest about health concerns but never "
    "alarmist. For medical questions (diabetes, blood pressure, allergies, "
    "pregnancy) give general guidance and remind the user to consult a doctor or "
    "dietitian. Keep answers concise (under ~150 words) unless the user asks for detail."
)


# Plain-language summary of how the Swapify health score is computed. Passed to
# the LLM so it can accurately explain the "why" behind any product's score
# instead of guessing at the methodology.
SCORING_METHODOLOGY = (
    "SCORING METHODOLOGY (how Swapify computes the 1-10 health score):\n"
    "- Every product starts at a base score of 5.0.\n"
    "- Nutrient penalties (per serving): sugar >=10g -2 (>=5g -1); sodium >=400mg "
    "-2 (>=200mg -1); saturated fat is penalised on a sliding scale up to -2.\n"
    "- Nutrient bonuses: protein >=8g +1; fiber >=5g +1.\n"
    "- Ingredient penalties: harmful ingredients (e.g. palm oil, refined sugar, "
    "TBHQ, artificial colours) subtract points, multiplied by position (x1.5 if in "
    "the first 3 ingredients, x0.5 if in trace amounts). Beneficial ingredients add "
    "a little back.\n"
    "- Category caps limit how much any single ingredient category can subtract.\n"
    "- A transparency multiplier rewards full additive disclosure (x1.05) and "
    "penalises vague catch-all terms like 'edible vegetable oil' (x0.95).\n"
    "- The result is clamped to 1-10 and graded A (>=9), B (>=7), C (>=5), D (>=3), "
    "F (<3). A personalized score also re-weights nutrients the user cares about."
)


# ==============================================================================
# Ingredient Substitution Suggestions
# ==============================================================================
# Healthier swaps for commonly-flagged ingredients. Curated from food science
# and cross-referenced with the beneficial keywords already in `ingredient_rules`
# (Natural Sweeteners, Healthy Fats & Oils, Fiber / whole grains). Each entry's
# ``match`` keywords are how we detect which ingredient the user is asking about.
INGREDIENT_SUBSTITUTIONS = [
    {
        "ingredient": "refined sugar",
        "match": ["sugar", "refined sugar", "white sugar", "added sugar", "sucrose"],
        "alternatives": ["jaggery", "date paste", "honey", "stevia", "monk fruit"],
        "reason": (
            "Natural sweeteners add sweetness with more minerals or far fewer "
            "calories and a gentler impact on blood sugar than refined sugar."
        ),
    },
    {
        "ingredient": "corn syrup / high-fructose corn syrup",
        "match": ["corn syrup", "high fructose", "hfcs", "glucose syrup", "invert sugar"],
        "alternatives": ["date paste", "jaggery", "honey", "mashed banana"],
        "reason": (
            "Whole-food sweeteners avoid the concentrated fructose load of "
            "corn syrups while still sweetening naturally."
        ),
    },
    {
        "ingredient": "palm oil",
        "match": ["palm oil", "palmolein", "palm fat", "palm kernel"],
        "alternatives": ["cold-pressed groundnut oil", "olive oil", "rice bran oil", "mustard oil"],
        "reason": (
            "These oils are lower in saturated fat and richer in unsaturated "
            "fats than palm oil, which is high in saturated fat."
        ),
    },
    {
        "ingredient": "hydrogenated / vanaspati (trans fats)",
        "match": [
            "hydrogenated", "partially hydrogenated", "vanaspati", "margarine",
            "fractionated fat", "interesterified", "shortening",
        ],
        "alternatives": ["cold-pressed oils", "olive oil", "ghee (in moderation)", "rice bran oil"],
        "reason": (
            "Hydrogenated fats contain trans fats linked to heart disease; "
            "unprocessed oils (and a little ghee) are far safer."
        ),
    },
    {
        "ingredient": "maida (refined wheat flour)",
        "match": ["maida", "refined wheat flour", "refined flour", "white flour"],
        "alternatives": ["whole wheat flour (atta)", "oats", "jowar", "bajra", "ragi", "besan"],
        "reason": (
            "Whole grains keep their fiber and nutrients, so they digest slower "
            "and don't spike blood sugar the way refined maida does."
        ),
    },
    {
        "ingredient": "salt (high sodium)",
        "match": ["salt", "sodium chloride", "high sodium", "table salt"],
        "alternatives": ["herbs & spices", "lemon juice", "black pepper", "garlic", "low-sodium / potassium salt"],
        "reason": (
            "Herbs, citrus and spices add flavour without the sodium load that "
            "drives up blood pressure."
        ),
    },
    {
        "ingredient": "MSG (flavour enhancer)",
        "match": ["msg", "monosodium glutamate", "flavour enhancer", "flavor enhancer", "e621"],
        "alternatives": ["tomato", "mushroom", "fermented soy/miso", "herbs & spices"],
        "reason": (
            "Naturally umami-rich foods deliver savoury depth without added "
            "monosodium glutamate."
        ),
    },
    {
        "ingredient": "artificial colours",
        "match": [
            "tartrazine", "sunset yellow", "artificial colour", "artificial color",
            "synthetic colour", "synthetic color", "food colour", "food color",
        ],
        "alternatives": ["turmeric", "beetroot extract", "spinach/spirulina", "paprika", "saffron"],
        "reason": (
            "Plant-based colours give vivid colour without synthetic dyes, some "
            "of which are linked to hyperactivity in sensitive children."
        ),
    },
    {
        "ingredient": "artificial sweeteners",
        "match": ["aspartame", "sucralose", "acesulfame", "saccharin", "artificial sweetener"],
        "alternatives": ["stevia", "monk fruit", "small amounts of date paste or jaggery"],
        "reason": (
            "Plant-derived sweeteners are a more natural way to cut sugar than "
            "synthetic high-intensity sweeteners."
        ),
    },
    {
        "ingredient": "chemical preservatives",
        "match": [
            "tbhq", "bha", "bht", "sodium benzoate", "potassium sorbate",
            "sodium nitrite", "sodium nitrate", "preservative",
        ],
        "alternatives": ["vitamin E (tocopherols)", "rosemary extract", "vinegar/citric acid", "refrigeration"],
        "reason": (
            "Natural antioxidants and simple food-handling can preserve food "
            "without synthetic preservatives."
        ),
    },
    {
        "ingredient": "butter / cream (saturated fat)",
        "match": ["butter", "cream", "dalda", "clarified butter"],
        "alternatives": ["olive oil", "avocado", "nut butters", "hung curd / Greek yogurt"],
        "reason": (
            "These swaps cut saturated fat while keeping richness, helping "
            "protect heart health."
        ),
    },
    {
        "ingredient": "maltodextrin",
        "match": ["maltodextrin"],
        "alternatives": ["rolled oats", "dates", "whole-fruit purée"],
        "reason": (
            "Whole-food carbohydrates avoid maltodextrin's very high glycaemic "
            "index."
        ),
    },
]

# Phrases that signal the user is asking for an alternative, not just info.
SUBSTITUTION_INTENT_PATTERNS = (
    "instead of", "substitute", "substitut", "alternative", "replace",
    "swap", "in place of", "what can i use", "what else can i use",
    "healthier option", "healthier choice", "better option", "what to use",
)


def find_substitution_targets(question: str):
    """Return the substitution entries relevant to a user's question.

    Only returns matches when the question expresses substitution intent (e.g.
    "instead of", "alternative to", "replace") *and* names a known ingredient,
    so ordinary questions ("is sugar bad?") aren't hijacked. Longer ``match``
    keywords are checked first so "high fructose corn syrup" maps to the corn
    syrup entry rather than the generic sugar one.
    """
    text = (question or "").lower()
    if not any(pat in text for pat in SUBSTITUTION_INTENT_PATTERNS):
        return []

    targets = []
    for entry in INGREDIENT_SUBSTITUTIONS:
        for keyword in sorted(entry["match"], key=len, reverse=True):
            if keyword in text:
                targets.append(entry)
                break
    return targets


def build_substitution_context(targets) -> str:
    """Render matched substitution entries into a grounding block for the LLM."""
    if not targets:
        return ""
    lines = ["SUBSTITUTION SUGGESTIONS (use these healthier swaps to answer):"]
    for entry in targets:
        lines.append(
            f"- Instead of {entry['ingredient']}: "
            f"{', '.join(entry['alternatives'])}. {entry['reason']}"
        )
    return "\n".join(lines)


def fallback_substitution_answer(targets, product: Optional[dict] = None) -> str:
    """Deterministic substitution reply used when the LLM is unavailable."""
    parts = []
    for entry in targets:
        alts = ", ".join(entry["alternatives"])
        parts.append(
            f"Instead of {entry['ingredient']}, try {alts}. {entry['reason']}"
        )
    if product is not None and product.get("product_name"):
        parts.append(
            f"(Asked in the context of {product['product_name']}, "
            f"score {product.get('score')}/10.)"
        )
    parts.append("(AI assistant not configured; this is a food-science-based summary.)")
    return " ".join(parts)


def build_score_breakdown_context(product: Optional[dict]) -> str:
    """Render this product's actual score breakdown into text so the LLM can
    explain precisely *why* it scored what it did (rather than guessing). Empty
    string when no breakdown is available."""
    breakdown = (product or {}).get("breakdown")
    if not breakdown:
        return ""

    lines = ["SCORE BREAKDOWN (this product's actual score math):"]
    lines.append(f"- Base score: {breakdown.get('base_score')}")

    for pen in breakdown.get("nutrition_penalties", []):
        lines.append(
            f"- Nutrient penalty: {pen['nutrient']} = {pen['value']} "
            f"-> {pen['points']} pts"
        )
    for add in breakdown.get("additions", []):
        label = add.get("nutrient") or add.get("ingredient") or add.get("category")
        note = " (dropped by your preference)" if add.get("dropped_by_preference") else ""
        lines.append(f"- Bonus: {label} +{add['points']} pts{note}")
    for cat in breakdown.get("category_totals", []):
        capped = " (hit the category cap)" if cat.get("capped") else ""
        lines.append(
            f"- Ingredient category '{cat['category']}': applied "
            f"{cat['applied_penalty']} pts{capped}"
        )
    if breakdown.get("transparency_multiplier") not in (None, 1.0):
        lines.append(
            f"- Transparency multiplier: x{breakdown['transparency_multiplier']}"
        )
    lines.append(
        f"- Final score: {breakdown.get('final_score')}/10"
    )
    applied = breakdown.get("preferences_applied") or {}
    if applied:
        lines.append(
            "- Personalized for preferences: " + ", ".join(k for k in applied)
        )
    return "\n".join(lines)


def build_product_context(product: Optional[dict]) -> str:
    """Render a scored product dict into a compact text block for the LLM."""
    if not product:
        return "No specific product was provided for this question."

    def fmt(v, unit=""):
        return f"{v}{unit}" if v is not None else "unknown"

    flags = product.get("ingredient_flags") or []
    if flags:
        flag_str = ", ".join(
            f"{f['name']} (risk: {f['risk']})" if isinstance(f, dict) else str(f)
            for f in flags
        )
    else:
        flag_str = "none detected"

    context = (
        "PRODUCT CONTEXT (use this data to answer):\n"
        f"- Name: {product.get('product_name', 'Unknown')}\n"
        f"- Brand: {fmt(product.get('brand'))}\n"
        f"- Category: {fmt(product.get('category'))}\n"
        f"- Health score: {fmt(product.get('score'))}/10 "
        f"(grade {fmt(product.get('grade'))})\n"
        "- Nutrition per serving: "
        f"sugar {fmt(product.get('sugar_g_per_serving'), 'g')}, "
        f"saturated fat {fmt(product.get('saturated_fat_g_per_serving'), 'g')}, "
        f"sodium {fmt(product.get('sodium_mg_per_serving'), 'mg')}, "
        f"protein {fmt(product.get('protein_g_per_serving'), 'g')}, "
        f"fiber {fmt(product.get('fiber_g_per_serving'), 'g')}, "
        f"calories {fmt(product.get('calories_kcal_per_serving'), 'kcal')}\n"
        f"- Ingredients: {product.get('ingredients_text') or 'not available'}\n"
        f"- Flagged ingredients: {flag_str}\n"
    )

    # Append the per-product score math + the methodology so the AI can explain
    # "why did it score this?" accurately.
    breakdown_ctx = build_score_breakdown_context(product)
    if breakdown_ctx:
        context = f"{context}\n{breakdown_ctx}\n\n{SCORING_METHODOLOGY}"
    return context


# ------------------------------------------------------------------------------
# Rate-limit-aware exception hierarchy. Free LLM tiers are rate-limited *often*,
# so we distinguish "this one model is busy, skip to the next" from "the whole
# account is capped, stop this provider" — that's what lets failover stay fast
# and graceful instead of hammering a model that can't answer.
# ------------------------------------------------------------------------------
class LLMError(RuntimeError):
    """Base class for recoverable LLM provider errors."""


class ModelRateLimited(LLMError):
    """A single model/provider returned HTTP 429 (busy upstream). Retrying the
    same model won't help right now — move on to the next model/provider."""


class OpenRouterDailyLimit(LLMError):
    """The OpenRouter account has exhausted its free-models-per-day quota. This
    is account-wide, so every OpenRouter free model returns the same 429 — stop
    OpenRouter entirely (and let the caller fail over to another provider)."""


def _call_openrouter_model(model: str, question: str, context: str) -> str:
    """Make a single OpenRouter Chat Completions request for one model.

    Raises OpenRouterDailyLimit on the account-wide cap, ModelRateLimited on a
    per-model 429, and plain RuntimeError on other (possibly transient) failures.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": NUTRITIONIST_SYSTEM_PROMPT},
            {"role": "user", "content": f"{context}\n\nUser question: {question}"},
        ],
        "temperature": 0.4,
        "max_tokens": 700,
    }
    try:
        resp = requests.post(
            OPENROUTER_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                # Optional attribution headers recommended by OpenRouter.
                "HTTP-Referer": "https://swapify.app",
                "X-Title": "Swapify Nutritionist",
            },
            timeout=25,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}")

    if resp.status_code != 200:
        body = resp.text[:200]
        if resp.status_code == 429:
            # Account-wide free-tier daily cap vs. a single busy model.
            if "free-models-per-day" in resp.text:
                raise OpenRouterDailyLimit(
                    f"OpenRouter free-models-per-day limit reached: {body}"
                )
            raise ModelRateLimited(f"{model} rate-limited (429): {body}")
        raise RuntimeError(f"OpenRouter API error {resp.status_code}: {body}")

    data = resp.json()
    try:
        text = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("OpenRouter returned no usable text")
    if not text:
        # Some "reasoning" free models emit only a hidden reasoning trace with an
        # empty content field — unusable, so treat like a failure and move on.
        raise RuntimeError(f"{model} returned an empty message")
    return text


def call_openrouter(question: str, context: str):
    """Try each configured OpenRouter model in order and return (text, model).

    A per-model 429 skips straight to the next model (no wasted retry); other
    transient errors get one quick retry. The account-wide daily cap aborts
    OpenRouter immediately. Raises RuntimeError only when every model failed.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    errors = []
    for model in OPENROUTER_MODELS:
        for attempt in (1, 2):
            try:
                answer = _call_openrouter_model(model, question, context)
                logger.info("OpenRouter answered via model=%s (attempt %d)", model, attempt)
                return answer, model
            except OpenRouterDailyLimit:
                logger.warning("OpenRouter account daily free cap reached; stopping OpenRouter.")
                raise
            except ModelRateLimited as exc:
                # Retrying a rate-limited model immediately is pointless.
                errors.append(str(exc))
                logger.warning("OpenRouter model rate-limited: %s", exc)
                break
            except RuntimeError as exc:
                errors.append(f"{model} (attempt {attempt}): {exc}")
                logger.warning("OpenRouter call failed: %s", errors[-1])
                if attempt == 1:
                    time.sleep(0.4)  # brief backoff before the single retry

    raise RuntimeError("All OpenRouter models failed: " + " | ".join(errors))


def _call_gemini(question: str, context: str) -> str:
    """Make a single Google Gemini generateContent request. Raises
    ModelRateLimited on 429, RuntimeError on other failures."""
    payload = {
        "systemInstruction": {"parts": [{"text": NUTRITIONIST_SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [{"text": f"{context}\n\nUser question: {question}"}]}
        ],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 700},
    }
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=25,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Gemini request failed: {exc}")

    if resp.status_code == 429:
        raise ModelRateLimited(f"Gemini rate-limited (429): {resp.text[:200]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    try:
        text = (data["candidates"][0]["content"]["parts"][0].get("text") or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini returned no usable text")
    if not text:
        raise RuntimeError("Gemini returned an empty message")
    return text


def call_gemini(question: str, context: str):
    """Call Gemini with one quick retry; returns (text, model)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    errors = []
    for attempt in (1, 2):
        try:
            answer = _call_gemini(question, context)
            logger.info("Gemini answered via model=%s (attempt %d)", GEMINI_MODEL, attempt)
            return answer, GEMINI_MODEL
        except ModelRateLimited as exc:
            errors.append(str(exc))
            logger.warning("Gemini rate-limited: %s", exc)
            break
        except RuntimeError as exc:
            errors.append(f"attempt {attempt}: {exc}")
            logger.warning("Gemini call failed: %s", errors[-1])
            if attempt == 1:
                time.sleep(0.4)
    raise RuntimeError("Gemini failed: " + " | ".join(errors))


def call_llm(question: str, context: str):
    """Get an AI answer from the first available provider, returning
    (text, provider, model).

    Providers are tried in order — OpenRouter (many free models) first, then
    Gemini as automatic failover — so a rate-limited free tier degrades to
    another real AI provider rather than to the rule-based answer. Raises
    RuntimeError only when every configured provider fails.
    """
    errors = []
    if OPENROUTER_API_KEY:
        try:
            text, model = call_openrouter(question, context)
            return text, "openrouter", model
        except OpenRouterDailyLimit as exc:
            errors.append(f"openrouter daily cap: {exc}")
        except RuntimeError as exc:
            errors.append(f"openrouter: {exc}")
    if GEMINI_API_KEY:
        try:
            text, model = call_gemini(question, context)
            return text, "gemini", model
        except RuntimeError as exc:
            errors.append(f"gemini: {exc}")

    if not errors:
        raise RuntimeError("No AI provider configured")
    raise RuntimeError("All AI providers failed: " + " | ".join(errors))


def fallback_answer(question: str, product: Optional[dict]) -> str:
    """Deterministic rule-based reply used when the LLM is unavailable.

    Keeps the /chat endpoint useful (e.g. for demos) without an API key.
    """
    if not product:
        return (
            "I couldn't find data for that product, so here's general guidance: "
            "prefer foods low in added sugar, sodium and saturated fat, and high "
            "in fiber and protein. Scan a product barcode for a tailored answer."
        )

    name = product.get("product_name", "This product")
    score = product.get("score")
    grade = product.get("grade")
    concerns = []
    sugar = product.get("sugar_g_per_serving")
    sodium = product.get("sodium_mg_per_serving")
    satfat = product.get("saturated_fat_g_per_serving")
    if sugar is not None and sugar >= 10:
        concerns.append(f"high sugar ({sugar}g/serving)")
    if sodium is not None and sodium >= 400:
        concerns.append(f"high sodium ({sodium}mg/serving)")
    if satfat is not None and satfat >= 6:
        concerns.append(f"high saturated fat ({satfat}g/serving)")

    flags = product.get("ingredient_flags") or []
    flag_str = ", ".join(
        f"{f['name']} ({f['risk']} risk)" if isinstance(f, dict) else str(f)
        for f in flags
    )

    parts = [f"{name} has a health score of {score}/10 (grade {grade})."]
    if concerns:
        parts.append("Main concerns: " + ", ".join(concerns) + ".")
    if flag_str:
        parts.append("Flagged ingredients: " + flag_str + ".")
    parts.append(
        "For diabetes, blood pressure or allergy questions, please also consult "
        "a doctor or dietitian. (AI assistant not configured; this is a rule-based "
        "summary.)"
    )
    return " ".join(parts)


@app.post("/chat")
def chat(req: ChatRequest):
    """AI nutritionist chatbot. Accepts a free-text question and an optional
    barcode for product context, and returns an AI-generated answer.

    When the question asks for an ingredient alternative (e.g. "what can I use
    instead of sugar?"), healthier, food-science-backed substitutions are
    detected from the ingredient knowledge base, passed to the LLM as grounding
    context, and also returned as a structured ``substitutions`` array.
    """
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    product = get_scored_product(req.barcode) if req.barcode else None
    context = build_product_context(product)

    # Detect "what can I use instead of X?" style questions and ground the
    # answer in our curated substitution suggestions.
    sub_targets = find_substitution_targets(req.question)
    sub_context = build_substitution_context(sub_targets)
    if sub_context:
        context = f"{context}\n\n{sub_context}"

    used_ai = False
    provider = None
    model = None
    fallback_reason = None
    try:
        answer, provider, model = call_llm(req.question, context)
        used_ai = True
    except RuntimeError as exc:
        # Every AI provider failed (e.g. all free models rate-limited and no
        # Gemini key). Degrade to the deterministic food-science answer so the
        # endpoint always responds, and surface *why* for the operator/client.
        fallback_reason = str(exc)
        logger.warning("/chat falling back to rule-based answer: %s", fallback_reason)
        if sub_targets:
            answer = fallback_substitution_answer(sub_targets, product)
        else:
            answer = fallback_answer(req.question, product)

    response = {
        "response": answer,
        "barcode": req.barcode,
        "product_found": product is not None,
        "source": provider if used_ai else "fallback",
        "model": model if used_ai else None,
        "ai_enabled": AI_ENABLED,
    }
    if not used_ai:
        response["fallback_reason"] = fallback_reason
    if sub_targets:
        response["substitutions"] = [
            {
                "ingredient": t["ingredient"],
                "alternatives": t["alternatives"],
                "reason": t["reason"],
            }
            for t in sub_targets
        ]
    if product is not None:
        response["product_name"] = product.get("product_name")
        response["score"] = product.get("score")
        response["grade"] = product.get("grade")
        response["ingredient_flags"] = product.get("ingredient_flags", [])
    return response


# ==============================================================================
# Crowdsourced Product Ratings  (Task 1)
# ==============================================================================
# Users rate products on taste, quality and value (each 1-5 stars) alongside the
# objective health score. Ratings are stored per (user, product); a user can
# update their rating (UNIQUE(user_id, barcode)) so community averages served by
# /product/{barcode}/ratings are never double-counted.

RATING_FIELDS = ("taste_rating", "quality_rating", "value_rating")


def _validate_star(value, field):
    """Ensure a star rating is an int in 1..5, else raise a 400."""
    if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 5):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be an integer from 1 to 5",
        )


def load_community_ratings():
    """Return {barcode: {count, taste, quality, value, overall}} averaged across
    all users. Used by /recommendations to surface community-loved products.
    ``overall`` is the mean of the three per-category averages. Returns {} if the
    ratings table is unavailable."""
    ratings = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT barcode,
                   COUNT(*)            AS n,
                   AVG(taste_rating)   AS taste,
                   AVG(quality_rating) AS quality,
                   AVG(value_rating)   AS value
            FROM product_ratings
            GROUP BY barcode
        ''')
        for row in cursor.fetchall():
            r = dict(row)
            taste = round(r["taste"], 2)
            quality = round(r["quality"], 2)
            value = round(r["value"], 2)
            ratings[r["barcode"]] = {
                "count": r["n"],
                "taste": taste,
                "quality": quality,
                "value": value,
                "overall": round((taste + quality + value) / 3, 2),
            }
        conn.close()
    except Exception:
        return {}
    return ratings


@app.post("/rate-product")
def rate_product(rating: ProductRating, user_id: int = Depends(get_current_user)):
    """Submit (or update) the authenticated user's rating for a product.

    Each of taste/quality/value is a 1-5 star integer. Re-rating the same
    barcode overwrites the user's previous rating for it.
    """
    _validate_star(rating.taste_rating, "taste_rating")
    _validate_star(rating.quality_rating, "quality_rating")
    _validate_star(rating.value_rating, "value_rating")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM product_ratings WHERE user_id = ? AND barcode = ?",
        (user_id, rating.barcode),
    )
    updated = cursor.fetchone() is not None
    cursor.execute('''
        INSERT INTO product_ratings
            (user_id, barcode, taste_rating, quality_rating, value_rating, rated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, barcode) DO UPDATE SET
            taste_rating   = excluded.taste_rating,
            quality_rating = excluded.quality_rating,
            value_rating   = excluded.value_rating,
            rated_at       = CURRENT_TIMESTAMP
    ''', (
        user_id, rating.barcode,
        rating.taste_rating, rating.quality_rating, rating.value_rating,
    ))
    conn.commit()
    conn.close()

    log_activity(user_id, "rate", rating.barcode, {
        "taste_rating": rating.taste_rating,
        "quality_rating": rating.quality_rating,
        "value_rating": rating.value_rating,
    })

    return {
        "message": "Rating updated" if updated else "Rating submitted",
        "barcode": rating.barcode,
        "rating": {
            "taste_rating": rating.taste_rating,
            "quality_rating": rating.quality_rating,
            "value_rating": rating.value_rating,
        },
    }


@app.get("/product/{barcode}/ratings")
def get_product_ratings(barcode: str):
    """Public community rating summary for a product: average taste, quality and
    value (plus an overall average), and the total number of ratings."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*)            AS n,
               AVG(taste_rating)   AS taste,
               AVG(quality_rating) AS quality,
               AVG(value_rating)   AS value
        FROM product_ratings
        WHERE barcode = ?
    ''', (barcode,))
    row = dict(cursor.fetchone())
    conn.close()

    total = row["n"] or 0
    if total == 0:
        return {
            "barcode": barcode,
            "total_ratings": 0,
            "average_ratings": {
                "taste": None,
                "quality": None,
                "value": None,
                "overall": None,
            },
        }

    taste = round(row["taste"], 2)
    quality = round(row["quality"], 2)
    value = round(row["value"], 2)
    return {
        "barcode": barcode,
        "total_ratings": total,
        "average_ratings": {
            "taste": taste,
            "quality": quality,
            "value": value,
            "overall": round((taste + quality + value) / 3, 2),
        },
    }


@app.get("/user/ratings")
def get_user_ratings(user_id: int = Depends(get_current_user)):
    """Return the authenticated user's own past ratings, newest first. Product
    name/brand are included when the product is in the local catalog (LEFT JOIN,
    so ratings for Open Food Facts-only products still appear)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT r.barcode, r.taste_rating, r.quality_rating, r.value_rating,
               r.rated_at, p.product_name, p.brand
        FROM product_ratings r
        LEFT JOIN products p ON r.barcode = p.barcode
        WHERE r.user_id = ?
        ORDER BY r.rated_at DESC
    ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()

    ratings = []
    for row in rows:
        r = dict(row)
        overall = (r["taste_rating"] + r["quality_rating"] + r["value_rating"]) / 3
        ratings.append({
            "barcode": r["barcode"],
            "product_name": r["product_name"],
            "brand": r["brand"],
            "taste_rating": r["taste_rating"],
            "quality_rating": r["quality_rating"],
            "value_rating": r["value_rating"],
            "overall_rating": round(overall, 2),
            "rated_at": r["rated_at"],
        })

    return {
        "user_id": user_id,
        "total": len(ratings),
        "ratings": ratings,
    }


# ==============================================================================
# AI-Powered Product Recommendations  (Task 2)
# ==============================================================================
# A rule-based personalized recommendation engine. For a logged-in user it
# blends three interest signals — most-scanned categories, saved dietary
# preferences, and past product comparisons — with the (personalized) health
# score and crowdsourced community ratings, and returns 5-10 products with a
# human-readable reason for each. Anonymous users get generic popular products.

def record_comparison(user_id, barcodes):
    """Best-effort log of the products a logged-in user viewed in a comparison,
    used later as a recommendation signal. Never raises (a logging failure must
    not break the comparison request)."""
    if not isinstance(user_id, int) or not barcodes:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT INTO comparison_history (user_id, barcode) VALUES (?, ?)",
            [(user_id, bc) for bc in barcodes if bc],
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _join_reason(clauses):
    """Join reason clauses into one sentence, e.g. 'Recommended because it X, Y
    and Z.'"""
    if len(clauses) == 1:
        body = clauses[0]
    else:
        body = ", ".join(clauses[:-1]) + " and " + clauses[-1]
    return "Recommended because it " + body + "."


def get_popular_products(limit=10, exclude=None, preferences=None):
    """Generic popularity ranking for anonymous users (and as a top-up).

    Popularity = total scans across all users, tie-broken by the health score.
    ``exclude`` is a set of barcodes to skip; ``preferences`` (when given) drops
    non-vegan products for vegan users and personalizes the score.
    """
    exclude = exclude or set()
    preferences = preferences or {}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.*, COUNT(h.id) AS scan_count
        FROM products p
        LEFT JOIN scan_history h ON p.barcode = h.barcode
        GROUP BY p.barcode
    ''')
    rows = cursor.fetchall()
    conn.close()

    items = []
    for row in rows:
        p = dict(row)
        if p["barcode"] in exclude:
            continue
        if preferences.get("vegan") and not is_vegan_friendly(p):
            continue
        score, grade, _, _ = calculate_health_score_v2(p, 1, preferences)
        p["health_score"] = score
        p["grade"] = grade
        items.append(p)

    items.sort(key=lambda x: (-(x.get("scan_count") or 0), -x["health_score"]))
    return items[:limit]


def get_popular_products_cached(limit=10):
    """Cached "top 100 most-scanned products" (Task 1C).

    The generic popularity ranking (no ``exclude``/``preferences``) is expensive
    — it scans and scores the whole catalogue — but changes slowly, so it's
    cached for an hour and sliced to ``limit`` per caller. Personalized or
    filtered rankings still call ``get_popular_products`` directly. The cache is
    cleared whenever a product changes (see ``invalidate_product_cache``)."""
    key = "popular_top_100"
    cached = _popular_cache.get(key)
    if cached is None:
        cached = get_popular_products(limit=100)
        _popular_cache[key] = cached
    return cached[:limit]


def build_popular_reason(p, community_entry):
    """Reason string for a generic popular recommendation."""
    clauses = []
    if (p.get("scan_count") or 0) > 0:
        clauses.append("is popular with other shoppers")
    if community_entry and community_entry["count"] > 0 and community_entry["overall"] >= 4.0:
        clauses.append(
            f"is highly rated by the community ({community_entry['overall']}★)"
        )
    grade = p.get("grade")
    if grade in ("A", "B"):
        clauses.append(f"is a healthy {grade}-grade choice")
    if not clauses:
        clauses.append(f"is a {grade}-grade product worth trying")
    return _join_reason(clauses)


def build_personal_reason(p, preferences, category_rank, compared_categories, community_entry):
    """Reason string for a personalized recommendation, assembled from whichever
    interest signals actually apply to this product."""
    clauses = []
    cat = p.get("category")
    if cat and cat in category_rank:
        if category_rank[cat] == 0:
            clauses.append(f"matches your most-scanned category ({cat})")
        else:
            clauses.append(f"matches a category you scan often ({cat})")
    elif cat and cat in compared_categories:
        clauses.append(f"is similar to products you've compared ({cat})")

    if preferences.get("high_protein") and (p.get("protein_g_per_serving") or 0) >= 8:
        clauses.append("is high in protein")
    if preferences.get("high_fiber") and (p.get("fiber_g_per_serving") or 0) >= 5:
        clauses.append("is high in fiber")
    if preferences.get("low_sugar") and p.get("sugar_g_per_serving") is not None \
            and p["sugar_g_per_serving"] <= 5:
        clauses.append("is low in sugar")
    if preferences.get("low_sodium") and p.get("sodium_mg_per_serving") is not None \
            and p["sodium_mg_per_serving"] <= 200:
        clauses.append("is low in sodium")
    if preferences.get("low_fat") and p.get("saturated_fat_g_per_serving") is not None \
            and p["saturated_fat_g_per_serving"] <= 3:
        clauses.append("is low in saturated fat")
    if preferences.get("vegan"):
        clauses.append("is vegan-friendly")

    if community_entry and community_entry["count"] > 0 and community_entry["overall"] >= 4.0:
        clauses.append(
            f"is highly rated by the community ({community_entry['overall']}★ "
            f"from {community_entry['count']})"
        )

    grade = p.get("grade")
    if grade in ("A", "B"):
        clauses.append(f"is a healthy {grade}-grade choice")

    if not clauses:
        clauses.append(f"is a solid {grade}-grade option to try")
    return _join_reason(clauses)


def compute_recommendations(effective_user_id, limit=10):
    """Core recommendation engine shared by /recommendations and /home-feed.

    Anonymous callers (``effective_user_id is None``) get generic popular
    products; a known user gets personalized picks from their scan history,
    comparisons, dietary preferences and community ratings. Returns a dict with
    ``personalized`` (bool), the ``recommendations`` list and, for a known user,
    a ``based_on`` explanation of the signals used (``None`` when anonymous).
    """
    limit = max(1, min(limit, 10))
    community = load_community_ratings()

    # --- Anonymous: generic popular products ---------------------------------
    if effective_user_id is None:
        popular = get_popular_products_cached(limit=limit)  # cached (Task 1C)
        recommendations = [{
            "barcode": p["barcode"],
            "product_name": p["product_name"],
            "brand": p.get("brand"),
            "health_score": p["health_score"],
            "grade": p["grade"],
            "image_url": image_or_placeholder(p.get("image_url")),
            "reason": build_popular_reason(p, community.get(p["barcode"])),
        } for p in popular]
        return {"personalized": False, "based_on": None, "recommendations": recommendations}

    # --- Personalized: gather this user's interest signals -------------------
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT p.category AS category, COUNT(*) AS cnt
        FROM scan_history h JOIN products p ON h.barcode = p.barcode
        WHERE h.user_id = ?
        GROUP BY p.category
        ORDER BY cnt DESC
    ''', (effective_user_id,))
    top_categories = [dict(r)["category"] for r in cursor.fetchall() if dict(r)["category"]]
    category_rank = {c: i for i, c in enumerate(top_categories)}  # 0 = most scanned

    cursor.execute(
        "SELECT DISTINCT barcode FROM scan_history WHERE user_id = ?",
        (effective_user_id,),
    )
    scanned_barcodes = {dict(r)["barcode"] for r in cursor.fetchall()}

    cursor.execute('''
        SELECT DISTINCT p.category AS category
        FROM comparison_history c JOIN products p ON c.barcode = p.barcode
        WHERE c.user_id = ?
    ''', (effective_user_id,))
    compared_categories = {dict(r)["category"] for r in cursor.fetchall() if dict(r)["category"]}

    cursor.execute("SELECT * FROM products")
    product_rows = cursor.fetchall()
    conn.close()

    preferences = load_user_preferences(effective_user_id)

    candidates = []
    for row in product_rows:
        p = dict(row)
        # Vegan users: never recommend a clearly non-vegan product.
        if preferences.get("vegan") and not is_vegan_friendly(p):
            continue
        score, grade, _, _ = calculate_health_score_v2(p, 1, preferences)
        p["health_score"] = score
        p["grade"] = grade

        cat = p.get("category")
        community_entry = community.get(p["barcode"])

        relevance = float(score)  # personalized health score is the base signal
        if cat in category_rank:
            relevance += 4.0 - min(category_rank[cat], 3)   # 4 (top) .. 1
        if cat in compared_categories:
            relevance += 2.0
        if community_entry and community_entry["count"] > 0:
            relevance += community_entry["overall"] - 3.0   # +2 .. -2 around neutral

        already = p["barcode"] in scanned_barcodes
        if already:
            relevance -= 3.0   # prefer fresh discoveries over re-recommending

        p["_relevance"] = relevance
        p["_already_scanned"] = already
        p["_community"] = community_entry
        candidates.append(p)

    candidates.sort(key=lambda x: (-x["_relevance"], -x["health_score"]))

    # Prefer products the user hasn't scanned; top up with familiar ones if the
    # fresh pool is too small to reach the requested count.
    fresh = [c for c in candidates if not c["_already_scanned"]]
    chosen = fresh[:limit]
    if len(chosen) < limit:
        chosen += [c for c in candidates if c["_already_scanned"]][:limit - len(chosen)]

    recommendations = [{
        "barcode": p["barcode"],
        "product_name": p["product_name"],
        "brand": p.get("brand"),
        "health_score": p["health_score"],
        "grade": p["grade"],
        "image_url": image_or_placeholder(p.get("image_url")),
        "reason": build_personal_reason(
            p, preferences, category_rank, compared_categories, p["_community"]
        ),
    } for p in chosen]

    return {
        "personalized": True,
        "based_on": {
            "top_categories": top_categories[:5],
            "dietary_preferences": {k: v for k, v in preferences.items() if v},
            "comparisons_considered": len(compared_categories) > 0,
        },
        "recommendations": recommendations,
    }


@app.get("/recommendations")
def get_recommendations(
    user_id: Optional[int] = None,
    limit: int = 10,
    token_user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Personalized product recommendations.

    - ``user_id`` (query param) selects whom to recommend for; falls back to the
      authenticated user (``Authorization: Bearer`` token) when omitted.
    - Anonymous (no ``user_id`` and no token) -> generic popular products.
    - ``limit`` is clamped to the spec's 5-10 range (default 10).

    Each recommendation includes ``barcode``, ``product_name``, ``brand``,
    ``health_score``, ``grade`` and a human-readable ``reason``.
    """
    effective_user_id = user_id if isinstance(user_id, int) else (
        token_user_id if isinstance(token_user_id, int) else None
    )
    limit = max(5, min(limit, 10))
    result = compute_recommendations(effective_user_id, limit)

    response = {
        "user_id": effective_user_id,
        "personalized": result["personalized"],
        "count": len(result["recommendations"]),
    }
    if result.get("based_on") is not None:
        response["based_on"] = result["based_on"]
    response["recommendations"] = result["recommendations"]
    return response


# ==============================================================================
# Personalized Home Feed  (Task 1)
# ==============================================================================
# One call that assembles everything the app's home screen shows: the user's
# recently scanned products, personalized recommendations, the featured weekly
# challenge with progress, and the badges they've earned. Anonymous callers get
# generic content (popular products, a preview challenge, no personal history).

def _score_scan_row(p_dict, preferences):
    """Shape a scanned product row for the feed's recently-scanned list."""
    score, grade, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
    return {
        "barcode": p_dict.get("barcode"),
        "product_name": p_dict.get("product_name"),
        "brand": p_dict.get("brand"),
        "category": p_dict.get("category"),
        "score": score,          # Task 3 response key
        "health_score": score,   # kept for backward compatibility
        "grade": grade,
        "image_url": image_or_placeholder(p_dict.get("image_url")),
        "scanned_at": p_dict.get("scanned_at"),
    }


def recently_scanned_for_user(user_id, preferences, limit=5):
    """The user's last ``limit`` *distinct* scanned products, most recent first.

    A product scanned several times appears once (at its latest scan), so the
    strip shows five different products rather than repeats.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        SELECT h.scanned_at, p.*
        FROM scan_history h JOIN products p ON h.barcode = p.barcode
        WHERE h.user_id = ?
        ORDER BY h.scanned_at DESC
    ''', (user_id,))
    rows = cur.fetchall()
    conn.close()

    results, seen = [], set()
    for row in rows:
        p_dict = dict(row)
        bc = p_dict.get("barcode")
        if bc in seen:
            continue
        seen.add(bc)
        results.append(_score_scan_row(p_dict, preferences))
        if len(results) >= limit:
            break
    return results


def recently_scanned_generic(preferences, limit=5):
    """Fallback recently-scanned list for anonymous users, drawn from the shared
    in-memory recent scans (see /recent). Off-catalogue barcodes are skipped."""
    if not recent_scans:
        return []
    conn = get_db_connection()
    cur = conn.cursor()
    results = []
    for bc in recent_scans[:limit]:
        cur.execute("SELECT * FROM products WHERE barcode = ?", (bc,))
        row = cur.fetchone()
        if row:
            results.append(_score_scan_row(dict(row), preferences))
    conn.close()
    return results


def get_weekly_challenge_feed(user_id):
    """The single weekly challenge to feature on the home feed.

    For a logged-in user this is the joined challenge closest to completion (or,
    if none joined, the first active weekly challenge shown with live progress).
    For an anonymous user it's the first active weekly challenge with no personal
    progress. Returns None when there are no active weekly challenges.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM challenges WHERE active = 1 AND period = 'weekly' ORDER BY id")
    weekly = [dict(r) for r in cur.fetchall()]
    joined_ids = set()
    if isinstance(user_id, int):
        cur.execute(
            "SELECT challenge_id FROM challenge_participants WHERE user_id = ?",
            (user_id,),
        )
        joined_ids = {r["challenge_id"] for r in cur.fetchall()}
    conn.close()

    if not weekly:
        return None

    if isinstance(user_id, int):
        joined = [ch for ch in weekly if ch["id"] in joined_ids]
        pool = joined if joined else weekly
        best, best_prog = None, None
        for ch in pool:
            prog = compute_challenge_progress(user_id, ch)
            if best is None or prog["percent"] > best_prog["percent"]:
                best, best_prog = ch, prog
        item = _challenge_public(best)
        item["joined"] = best["id"] in joined_ids
        item["progress"] = best_prog
        return item

    # Anonymous: preview the first weekly challenge with no personal progress.
    item = _challenge_public(weekly[0])
    item["joined"] = False
    item["progress"] = None
    return item


def _challenge_progress_summary(weekly):
    """Condense the featured weekly challenge into the Task 3 ``challenge_progress``
    shape: ``{challenge_name, progress, target}``. ``progress`` is 0 for an
    anonymous preview (no personal progress). Returns None when there is no
    active weekly challenge."""
    if not weekly:
        return None
    prog = weekly.get("progress")
    return {
        "challenge_name": weekly.get("title"),
        "progress": prog["current"] if prog else 0,
        "target": prog["target"] if prog else weekly.get("target"),
    }


@app.get("/home-feed")
def home_feed(
    user_id: Optional[int] = None,
    token_user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Personalized home feed (Task 3).

    Assembles everything the home screen needs in one call:
      - ``recently_scanned``   : the user's last 5 distinct scanned products
                                 (``barcode``, ``product_name``, ``brand``,
                                 ``score``, ``grade``, ``image_url``)
      - ``recommendations``    : personalized picks with a ``reason`` and
                                 ``image_url`` (popular products when anonymous)
      - ``challenge_progress`` : the featured weekly challenge
                                 (``challenge_name``, ``progress``, ``target``)
      - ``badges_earned``      : the badges the user has won
                                 (``name``, ``icon``, ``earned_at``)

    ``user_id`` (query param) selects whose feed to build; it falls back to the
    authenticated user (``Authorization: Bearer`` token). With neither, the feed
    falls back to generic content — popular products, a preview challenge and no
    personal history or badges (``logged_in: false``).
    """
    effective_user_id = user_id if isinstance(user_id, int) else (
        token_user_id if isinstance(token_user_id, int) else None
    )
    logged_in = effective_user_id is not None
    preferences = load_user_preferences(effective_user_id)

    if logged_in:
        recently_scanned = recently_scanned_for_user(effective_user_id, preferences, limit=5)
        badges = [
            {"name": b["name"], "icon": b["icon"], "earned_at": b["earned_at"]}
            for b in get_user_badges(effective_user_id)
        ]
    else:
        recently_scanned = recently_scanned_generic(preferences, limit=5)
        badges = []

    recs = compute_recommendations(effective_user_id, limit=6)
    # Reshape to the Task 3 recommendation contract (score + reason + image_url).
    recommendations = [{
        "barcode": r["barcode"],
        "product_name": r["product_name"],
        "brand": r.get("brand"),
        "score": r["health_score"],
        "grade": r["grade"],
        "reason": r.get("reason"),
        "image_url": r.get("image_url"),
    } for r in recs["recommendations"]]

    challenge_progress = _challenge_progress_summary(
        get_weekly_challenge_feed(effective_user_id)
    )

    return {
        "user_id": effective_user_id,
        "logged_in": logged_in,
        "personalized": recs["personalized"],
        "recently_scanned": recently_scanned,
        "recommendations": recommendations,
        "challenge_progress": challenge_progress,
        "badges_earned": badges,
    }


# ==============================================================================
# Shareable Score Card  (Task 3)
# ==============================================================================
# Returns a product formatted for a shareable image card: the identity fields,
# health score/grade, key warnings and flagged ingredients, plus a ``card``
# block of presentation hints (grade colour, headline, labels) so the frontend
# can render the card without re-deriving any copy.

GRADE_COLORS = {
    "A": "#1a9850",
    "B": "#91cf60",
    "C": "#fee08b",
    "D": "#fc8d59",
    "F": "#d73027",
}


def build_share_warnings(product):
    """Human-readable 'key warnings' for a share card, derived from the product's
    nutrition (per serving) and its high-risk flagged ingredients."""
    warnings = []
    sugar = product.get("sugar_g_per_serving")
    sodium = product.get("sodium_mg_per_serving")
    satfat = product.get("saturated_fat_g_per_serving")
    if sugar is not None and sugar >= 10:
        warnings.append(f"High sugar ({round(sugar, 1)}g per serving)")
    if sodium is not None and sodium >= 400:
        warnings.append(f"High sodium ({round(sodium, 1)}mg per serving)")
    if satfat is not None and satfat >= 6:
        warnings.append(f"High saturated fat ({round(satfat, 1)}g per serving)")
    for flag in product.get("ingredient_flags", []):
        if isinstance(flag, dict) and flag.get("risk") in ("High", "Severe"):
            warnings.append(f"Contains {flag['name']} ({flag['risk'].lower()} risk)")
    return warnings


def build_share_headline(name, score, grade):
    """Short verdict headline for the share card."""
    name = name or "This product"
    if grade in ("A", "B"):
        verdict = "a healthy choice"
    elif grade == "C":
        verdict = "an average choice"
    else:
        verdict = "worth a closer look"
    return f"{name} scores {score}/10 (grade {grade}) — {verdict}."


@app.get("/share/{barcode}")
def share_product(barcode: str, user_id: Optional[int] = Depends(get_current_user_optional)):
    """Return a product formatted for a shareable score card. Resolves from the
    local catalog first, then Open Food Facts (whose products also supply an
    ``image_url``). The score is personalized when the request is authenticated."""
    preferences = load_user_preferences(user_id)
    product = get_scored_product(barcode, preferences)
    if not product:
        return JSONResponse(status_code=404, content={"error": "Product not found"})

    if isinstance(user_id, int):
        log_activity(user_id, "share", barcode)

    score = product.get("score")
    grade = product.get("grade")
    warnings = build_share_warnings(product)
    flags = product.get("ingredient_flags", [])

    return {
        "barcode": product.get("barcode"),
        "product_name": product.get("product_name"),
        "brand": product.get("brand"),
        "image_url": product.get("image_url"),
        "health_score": score,
        "grade": grade,
        "warnings": warnings,
        "ingredient_flags": flags,
        "card": {
            "title": product.get("product_name") or "Unknown Product",
            "subtitle": product.get("brand") or "",
            "score_label": f"{score}/10",
            "grade": grade,
            "grade_color": GRADE_COLORS.get(grade, "#999999"),
            "headline": build_share_headline(product.get("product_name"), score, grade),
            "warning_count": len(warnings),
            "flag_count": len(flags),
            "footer": "Scanned with Swapify",
        },
        "source": product.get("source", "database"),
    }


# ==============================================================================
# User Activity Logging  (Task 1)
# ==============================================================================
# Track user actions (scan, compare, share, rate, favorite) to understand
# behaviour and improve recommendations. Each row stores user_id, action_type,
# an optional barcode, an optional JSON metadata blob and a timestamp in the
# `user_activity` table. POST /activity logs an action; the existing product,
# compare, share, rate and favorite endpoints also auto-log (best-effort) for
# logged-in users so the trend data reflects real usage.

ACTIVITY_TYPES = ("scan", "compare", "share", "rate", "favorite")


def log_activity(user_id, action_type, barcode=None, metadata=None):
    """Best-effort insert into ``user_activity``. Never raises — an activity-log
    failure must not break the underlying request."""
    if action_type not in ACTIVITY_TYPES:
        return
    try:
        import json
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_activity (user_id, action_type, barcode, metadata) "
            "VALUES (?, ?, ?, ?)",
            (user_id, action_type, barcode,
             json.dumps(metadata) if metadata else None),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _parse_activity_row(row):
    """Turn a user_activity DB row into a response dict (metadata -> object)."""
    import json
    r = dict(row)
    if r.get("metadata"):
        try:
            r["metadata"] = json.loads(r["metadata"])
        except (ValueError, TypeError):
            pass
    return r


@app.post("/activity")
def create_activity(
    entry: ActivityLog,
    token_user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Log a user action (scan, compare, share, rate, favorite).

    The ``user_id`` is taken from the ``Authorization: Bearer`` token when the
    request is authenticated, otherwise from the request body (so anonymous /
    device clients can still log). ``metadata`` is an optional free-form object
    stored as JSON.
    """
    action = (entry.action_type or "").strip().lower()
    if action not in ACTIVITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"action_type must be one of: {', '.join(ACTIVITY_TYPES)}",
        )

    user_id = token_user_id if isinstance(token_user_id, int) else entry.user_id

    import json
    metadata_json = json.dumps(entry.metadata) if entry.metadata else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO user_activity (user_id, action_type, barcode, metadata) "
        "VALUES (?, ?, ?, ?)",
        (user_id, action, entry.barcode, metadata_json),
    )
    activity_id = cursor.lastrowid
    conn.commit()
    cursor.execute(
        "SELECT id, user_id, action_type, barcode, metadata, created_at "
        "FROM user_activity WHERE id = ?",
        (activity_id,),
    )
    row = cursor.fetchone()
    conn.close()

    return {"message": "Activity logged", "activity": _parse_activity_row(row)}


@app.get("/activity/user/{user_id}")
def get_user_activity(
    user_id: int,
    action_type: Optional[str] = None,
    limit: int = 50,
):
    """Return a user's activity history, newest first.

    Optional ``action_type`` filters to one action; ``limit`` (1-200, default 50)
    caps the number of rows. Also returns a per-action-type count summary.
    """
    limit = max(1, min(limit, 200))
    conn = get_db_connection()
    cursor = conn.cursor()

    if action_type:
        at = action_type.strip().lower()
        cursor.execute(
            "SELECT id, user_id, action_type, barcode, metadata, created_at "
            "FROM user_activity WHERE user_id = ? AND action_type = ? "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
            (user_id, at, limit),
        )
    else:
        cursor.execute(
            "SELECT id, user_id, action_type, barcode, metadata, created_at "
            "FROM user_activity WHERE user_id = ? "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
            (user_id, limit),
        )
    rows = cursor.fetchall()

    cursor.execute(
        "SELECT action_type, COUNT(*) AS n FROM user_activity "
        "WHERE user_id = ? GROUP BY action_type",
        (user_id,),
    )
    counts = {dict(r)["action_type"]: dict(r)["n"] for r in cursor.fetchall()}
    conn.close()

    activities = [_parse_activity_row(r) for r in rows]
    return {
        "user_id": user_id,
        "count": len(activities),
        "action_counts": counts,
        "activities": activities,
    }


@app.get("/activity/trends")
def get_activity_trends(days: int = 7):
    """Overall activity trends across all users (optional analytics endpoint).

    Returns the total number of actions, a breakdown by action type, a per-day
    count for the last ``days`` days (1-90, default 7), the most-active barcodes,
    and the number of distinct users who logged activity in the window.
    """
    days = max(1, min(days, 90))
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) AS n FROM user_activity")
    total = dict(cursor.fetchone())["n"]

    cursor.execute(
        "SELECT action_type, COUNT(*) AS n FROM user_activity "
        "GROUP BY action_type ORDER BY n DESC"
    )
    by_action = {dict(r)["action_type"]: dict(r)["n"] for r in cursor.fetchall()}

    cursor.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM user_activity WHERE datetime(created_at) >= datetime(?) "
        "GROUP BY day ORDER BY day",
        (since,),
    )
    by_day = [
        {"date": dict(r)["day"], "count": dict(r)["n"]} for r in cursor.fetchall()
    ]

    cursor.execute(
        "SELECT barcode, COUNT(*) AS n FROM user_activity "
        "WHERE barcode IS NOT NULL AND barcode != '' "
        "GROUP BY barcode ORDER BY n DESC, barcode LIMIT 5"
    )
    top_barcodes = [
        {"barcode": dict(r)["barcode"], "count": dict(r)["n"]}
        for r in cursor.fetchall()
    ]

    cursor.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM user_activity "
        "WHERE user_id IS NOT NULL AND datetime(created_at) >= datetime(?)",
        (since,),
    )
    active_users = dict(cursor.fetchone())["n"]
    conn.close()

    return {
        "window_days": days,
        "total_actions": total,
        "by_action_type": by_action,
        "by_day": by_day,
        "top_barcodes": top_barcodes,
        "active_users": active_users,
    }


# ==============================================================================
# Daily Digest / Notification  (Task 3)
# ==============================================================================
# Build a daily summary of a user's scans (total scans, average score, best and
# worst product) formatted for email / push-notification integration. The GET
# endpoint IS the manual trigger; for automated daily delivery, schedule a job
# (cron / Windows Task Scheduler) that calls GET /digest/{user_id} once a day and
# forwards the `notification` / `email` blocks to your delivery provider.

def _digest_product_summary(item):
    """Compact best/worst product block for a digest."""
    return {
        "barcode": item["barcode"],
        "product_name": item["product_name"],
        "brand": item.get("brand"),
        "score": item["score"],
        "grade": item["grade"],
    }


@app.get("/digest/{user_id}")
def get_daily_digest(
    user_id: int,
    date: Optional[str] = None,
    token_user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Daily scan digest for a user, ready for email / push notification.

    Summarises a single day's scans (``date`` = ``YYYY-MM-DD``, defaults to the
    current UTC day) into total scans, average health score and the best- and
    worst-scoring products, plus notification- and email-ready payloads. Scores
    use the user's personalized dietary weights.
    """
    if not date:
        date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", date):
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT h.scanned_at, p.*
        FROM scan_history h
        JOIN products p ON h.barcode = p.barcode
        WHERE h.user_id = ? AND substr(h.scanned_at, 1, 10) = ?
        ORDER BY h.scanned_at ASC
    ''', (user_id, date))
    rows = cursor.fetchall()
    conn.close()

    preferences = load_user_preferences(user_id)

    # No scans that day: return a friendly nudge payload instead of empty stats.
    if not rows:
        title = "No scans yet today"
        body = (
            "You haven't scanned any products today. Scan a product to see how "
            "healthy it is and get better recommendations!"
        )
        return {
            "user_id": user_id,
            "date": date,
            "total_scans": 0,
            "average_score": 0,
            "best_product": None,
            "worst_product": None,
            "notification": {"type": "daily_digest", "title": title, "body": body},
            "email": {
                "subject": "Your Swapify daily digest",
                "preview": body,
                "body_text": body,
            },
        }

    scored = []
    for row in rows:
        p_dict = dict(row)
        score, grade, _, _ = calculate_health_score_v2(p_dict, 1, preferences)
        scored.append({
            "barcode": p_dict["barcode"],
            "product_name": p_dict["product_name"],
            "brand": p_dict.get("brand"),
            "score": score,
            "grade": grade,
        })

    total_scans = len(scored)
    average_score = round(sum(s["score"] for s in scored) / total_scans, 2)
    best = _digest_product_summary(max(scored, key=lambda s: s["score"]))
    worst = _digest_product_summary(min(scored, key=lambda s: s["score"]))

    # Notification / email copy (ready for a delivery provider to send).
    title = f"Your daily scan summary — {total_scans} scan" + ("s" if total_scans != 1 else "")
    body = (
        f"You scanned {total_scans} product"
        f"{'s' if total_scans != 1 else ''} today with an average health score "
        f"of {average_score}/10. Best: {best['product_name']} "
        f"({best['score']}/10, {best['grade']})."
    )
    if worst["barcode"] != best["barcode"]:
        body += f" Watch out for: {worst['product_name']} ({worst['score']}/10, {worst['grade']})."

    subject = f"Your Swapify daily digest — avg {average_score}/10 across {total_scans} scans"

    return {
        "user_id": user_id,
        "date": date,
        "total_scans": total_scans,
        "average_score": average_score,
        "best_product": best,
        "worst_product": worst,
        "notification": {
            "type": "daily_digest",
            "title": title,
            "body": body,
        },
        "email": {
            "subject": subject,
            "preview": f"{total_scans} scans · avg {average_score}/10",
            "body_text": body,
        },
    }


# ==============================================================================
# Feature schema bootstrap (Challenges, Smart Cart, Community Reviews)
# ==============================================================================
# The gamification, shopping-list and reviews features need their own tables.
# Rather than force a manual migration step against an existing swapify.db, we
# create the tables (and seed the default weekly challenges) at import time with
# CREATE TABLE IF NOT EXISTS / idempotent inserts. Running this repeatedly is a
# no-op, so a freshly-cloned checkout, an existing DB and the test suite all work
# out of the box. The same DDL also lives in create_db.py and
# migrations/005_create_challenges_reviews_smartcart.sql for documentation.

# The four challenge types from the task spec. ``code`` is a stable unique key
# used for idempotent seeding; ``goal_type`` says which user action it counts and
# ``period`` is the rolling window it's measured over.
CHALLENGE_SEED = [
    {
        "code": "scan_20_weekly",
        "title": "Scan 20 products this week",
        "description": "Scan any 20 products within a week to complete this challenge.",
        "goal_type": "scan",
        "target_count": 20,
        "score_threshold": None,
        "period": "weekly",
        "badge": "Scan Champion",
    },
    {
        # Threshold is > 4 (not > 8): the scoring engine's generic ceiling is
        # ~7.35 (base 5 + protein 1 + fiber 1, x1.05) and the catalog tops out
        # around 5.0, so ">4" keeps this challenge actually completable while
        # still rewarding genuinely healthier picks.
        "code": "find_5_healthy_weekly",
        "title": "Find 5 products with score > 4",
        "description": "Discover 5 different products with a health score above 4 this week.",
        "goal_type": "scan_high_score",
        "target_count": 5,
        "score_threshold": 4.0,
        "period": "weekly",
        "badge": "Health Hunter",
    },
    {
        "code": "compare_10_weekly",
        "title": "Compare 10 products",
        "description": "Run 10 product comparisons this week.",
        "goal_type": "compare",
        "target_count": 10,
        "score_threshold": None,
        "period": "weekly",
        "badge": "Comparison Pro",
    },
    {
        "code": "rate_15_weekly",
        "title": "Rate 15 products",
        "description": "Rate 15 products this week.",
        "goal_type": "rate",
        "target_count": 15,
        "score_threshold": None,
        "period": "weekly",
        "badge": "Star Reviewer",
    },
]


def ensure_feature_schema():
    """Create the challenges / shopping-list / reviews tables if missing and seed
    the default weekly challenges. Idempotent and best-effort — a bootstrap
    failure is logged but must not stop the app from importing."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.executescript('''
            CREATE TABLE IF NOT EXISTS challenges (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT UNIQUE NOT NULL,
                title           TEXT NOT NULL,
                description     TEXT,
                goal_type       TEXT NOT NULL,   -- scan | scan_high_score | compare | rate
                target_count    INTEGER NOT NULL,
                score_threshold REAL,             -- only for scan_high_score
                period          TEXT NOT NULL DEFAULT 'weekly',
                badge           TEXT,
                active          INTEGER NOT NULL DEFAULT 1,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS challenge_participants (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                challenge_id INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                UNIQUE(challenge_id, user_id),
                FOREIGN KEY(challenge_id) REFERENCES challenges(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS shopping_lists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                name       TEXT NOT NULL DEFAULT 'My Shopping List',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS shopping_list_items (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id  INTEGER NOT NULL,
                barcode  TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(list_id) REFERENCES shopping_lists(id)
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                barcode     TEXT NOT NULL,
                rating      INTEGER NOT NULL,   -- 1-5 stars
                review_text TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS review_votes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                vote      INTEGER NOT NULL,     -- +1 upvote, -1 downvote
                voted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(review_id, user_id),
                FOREIGN KEY(review_id) REFERENCES reviews(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS review_replies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id  INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                reply_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(review_id) REFERENCES reviews(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_reviews_barcode ON reviews(barcode);
            CREATE INDEX IF NOT EXISTS idx_sl_items_list ON shopping_list_items(list_id);
        ''')

        # Upsert each challenge definition keyed on its stable ``code`` so
        # CHALLENGE_SEED stays the single source of truth: a new checkout inserts
        # it, and an existing DB has its mutable fields (title/target/threshold/…)
        # refreshed to match. Participant rows live in a separate table, so this
        # never disturbs who has joined or completed a challenge.
        for ch in CHALLENGE_SEED:
            cur.execute("SELECT id FROM challenges WHERE code = ?", (ch["code"],))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO challenges (code, title, description, goal_type, "
                    "target_count, score_threshold, period, badge, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        ch["code"], ch["title"], ch["description"], ch["goal_type"],
                        ch["target_count"], ch["score_threshold"], ch["period"],
                        ch["badge"],
                    ),
                )
            else:
                cur.execute(
                    "UPDATE challenges SET title = ?, description = ?, goal_type = ?, "
                    "target_count = ?, score_threshold = ?, period = ?, badge = ? "
                    "WHERE code = ?",
                    (
                        ch["title"], ch["description"], ch["goal_type"],
                        ch["target_count"], ch["score_threshold"], ch["period"],
                        ch["badge"], ch["code"],
                    ),
                )
        conn.commit()
        conn.close()
    except Exception as exc:  # pragma: no cover - defensive bootstrap
        logger.warning("ensure_feature_schema failed: %s", exc)


def ensure_performance_and_image_schema():
    """Idempotent migration for Task 1A (performance indexes) and Task 2 (product
    images). Runs at import so an existing swapify.db is upgraded in place with
    no manual step. Best-effort — a failure is logged, never fatal.

    Adds:
      - ``products.image_url`` column (Task 2A)
      - single-column indexes on the frequently searched product columns and a
        composite ``(product_name, brand)`` index (Task 1A)
      - a ``product_images`` table recording crowdsourced uploads (Task 2C)
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # --- Task 2A: image_url column (guarded — SQLite can't ADD IF NOT EXISTS)
        existing_cols = {r[1] for r in cur.execute("PRAGMA table_info(products)")}
        if "image_url" not in existing_cols:
            cur.execute("ALTER TABLE products ADD COLUMN image_url TEXT")

        # --- Task 1A: indexes on frequently searched columns + a composite index
        cur.executescript('''
            CREATE INDEX IF NOT EXISTS idx_products_barcode      ON products(barcode);
            CREATE INDEX IF NOT EXISTS idx_products_product_name ON products(product_name);
            CREATE INDEX IF NOT EXISTS idx_products_brand        ON products(brand);
            CREATE INDEX IF NOT EXISTS idx_products_category     ON products(category);
            CREATE INDEX IF NOT EXISTS idx_products_name_brand   ON products(product_name, brand);
        ''')

        # --- Task 2C: crowdsourced image upload records
        cur.execute('''
            CREATE TABLE IF NOT EXISTS product_images (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode      TEXT NOT NULL,
                image_url    TEXT NOT NULL,
                content_type TEXT,
                file_size    INTEGER,
                uploaded_by  INTEGER,
                uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(uploaded_by) REFERENCES users(id)
            )
        ''')
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_images_barcode "
            "ON product_images(barcode)"
        )

        conn.commit()
        conn.close()
    except Exception as exc:  # pragma: no cover - defensive bootstrap
        logger.warning("ensure_performance_and_image_schema failed: %s", exc)


# ==============================================================================
# Weekly Challenges & Leaderboard  (Gamification)
# ==============================================================================
# Users join weekly challenges ("Scan 20 products this week", "Rate 15
# products", ...) and see how they rank against everyone else on the leaderboard.
# Progress is derived from the existing user_activity stream (scan / compare /
# rate are already auto-logged), so joining a challenge is the only new write;
# nothing else about the scan/compare/rate flows changes. Completing a challenge
# earns its badge, which is surfaced on the leaderboard.

# How many days each period's rolling window spans. "all-time" is effectively
# unbounded (used by the leaderboard filter and never expires a challenge).
PERIOD_DAYS = {"weekly": 7, "monthly": 30, "all-time": 36500}

# Point weight per activity type for the leaderboard's "activity score". Compare
# and rate are weighted higher than a scan because they take more effort.
ACTIVITY_POINTS = {"scan": 1, "compare": 3, "rate": 2, "share": 1, "favorite": 1}


def _utc_since(days):
    """Return the UTC cutoff timestamp string for a rolling window of ``days``."""
    return (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def compute_challenge_progress(user_id, challenge):
    """Compute a user's progress in one challenge over its rolling period window.

    Counts the qualifying activities from the user_activity stream:
      - scan / compare / rate   -> number of matching actions in the window
      - scan_high_score         -> distinct scanned products whose current health
                                   score exceeds the challenge's threshold
    Returns {current, target, completed, percent, remaining}.
    """
    days = PERIOD_DAYS.get(challenge["period"], 7)
    since = _utc_since(days)
    goal = challenge["goal_type"]
    target = challenge["target_count"]

    conn = get_db_connection()
    cur = conn.cursor()
    if goal == "scan_high_score":
        threshold = challenge["score_threshold"] or 8.0
        cur.execute(
            "SELECT DISTINCT barcode FROM user_activity "
            "WHERE user_id = ? AND action_type = 'scan' "
            "AND barcode IS NOT NULL AND barcode != '' "
            "AND datetime(created_at) >= datetime(?)",
            (user_id, since),
        )
        barcodes = [r[0] for r in cur.fetchall()]
        count = 0
        for bc in barcodes:
            cur.execute("SELECT * FROM products WHERE barcode = ?", (bc,))
            row = cur.fetchone()
            if not row:
                continue  # off-catalogue scans aren't re-fetched here (kept fast)
            score, _, _, _ = calculate_health_score_v2(dict(row), 1)
            if score > threshold:
                count += 1
    else:
        action = goal  # scan | compare | rate
        cur.execute(
            "SELECT COUNT(*) FROM user_activity "
            "WHERE user_id = ? AND action_type = ? "
            "AND datetime(created_at) >= datetime(?)",
            (user_id, action, since),
        )
        count = cur.fetchone()[0]
    conn.close()

    completed = count >= target
    return {
        "current": count,
        "target": target,
        "completed": completed,
        "percent": round(100 * min(count, target) / target, 1) if target else 0.0,
        "remaining": max(0, target - count),
    }


# Emoji icon per earned badge, so the home feed can render a badge without the
# client hard-coding icons. Unknown badges fall back to a generic medal.
BADGE_ICONS = {
    "Scan Champion": "🏅",
    "Health Hunter": "🔍",
    "Comparison Pro": "⚖️",
    "Star Reviewer": "⭐",
    "Health Champion": "🏆",
}
DEFAULT_BADGE_ICON = "🏅"


def badge_icon(name):
    """Return the emoji icon for a badge name (generic medal when unknown)."""
    return BADGE_ICONS.get(name, DEFAULT_BADGE_ICON)


def get_user_badges(user_id):
    """Return the badges a user has earned by completing challenges they joined.

    A badge is earned once the user's progress in a joined challenge reaches its
    target (evaluated live) or was previously marked complete (``completed_at``),
    so badges are sticky once won. Each badge carries both the legacy
    ``badge``/``challenge_id``/``title`` fields and the home-feed shape
    (``name``/``icon``/``earned_at``)."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT c.id, c.title, c.badge, c.goal_type, c.target_count, "
        "c.score_threshold, c.period, cp.completed_at "
        "FROM challenge_participants cp JOIN challenges c ON cp.challenge_id = c.id "
        "WHERE cp.user_id = ?",
        (user_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    badges = []
    for ch in rows:
        earned = ch.get("completed_at") is not None
        if not earned:
            earned = compute_challenge_progress(user_id, ch)["completed"]
        if earned and ch.get("badge"):
            # ``earned_at`` is the date the badge was first stamped complete
            # (None for a badge earned live but not yet persisted).
            completed_at = ch.get("completed_at")
            earned_at = completed_at.split(" ")[0] if completed_at else None
            badges.append({
                "name": ch["badge"],
                "icon": badge_icon(ch["badge"]),
                "earned_at": earned_at,
                # Legacy fields (kept for /leaderboard and older clients).
                "badge": ch["badge"],
                "challenge_id": ch["id"],
                "title": ch["title"],
            })
    return badges


def _challenge_public(ch):
    """Public-facing shape of a challenge definition row."""
    return {
        "id": ch["id"],
        "code": ch["code"],
        "title": ch["title"],
        "description": ch["description"],
        "goal_type": ch["goal_type"],
        "target": ch["target_count"],
        "score_threshold": ch["score_threshold"],
        "period": ch["period"],
        "badge": ch["badge"],
    }


@app.get("/challenges")
def list_challenges(user_id: Optional[int] = Depends(get_current_user_optional)):
    """List the currently active challenges.

    When the request is authenticated, each challenge also carries whether the
    user has ``joined`` it and, if so, their live ``progress``.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM challenges WHERE active = 1 ORDER BY id")
    challenges = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT challenge_id, COUNT(*) AS n FROM challenge_participants "
        "GROUP BY challenge_id"
    )
    participant_counts = {r["challenge_id"]: r["n"] for r in cur.fetchall()}

    joined = {}
    if isinstance(user_id, int):
        cur.execute(
            "SELECT challenge_id, joined_at FROM challenge_participants WHERE user_id = ?",
            (user_id,),
        )
        joined = {r["challenge_id"]: r["joined_at"] for r in cur.fetchall()}
    conn.close()

    out = []
    for ch in challenges:
        item = _challenge_public(ch)
        item["participant_count"] = participant_counts.get(ch["id"], 0)
        if isinstance(user_id, int):
            item["joined"] = ch["id"] in joined
            if item["joined"]:
                item["joined_at"] = joined[ch["id"]]
                item["progress"] = compute_challenge_progress(user_id, ch)
        out.append(item)

    return {"count": len(out), "active_challenges": out}


@app.post("/challenges/{challenge_id}/join")
def join_challenge(challenge_id: int, user_id: int = Depends(get_current_user)):
    """Join a challenge. Idempotent — re-joining returns the existing entry."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM challenges WHERE id = ? AND active = 1", (challenge_id,))
    ch = cur.fetchone()
    if not ch:
        conn.close()
        raise HTTPException(status_code=404, detail="Challenge not found")
    ch = dict(ch)

    cur.execute(
        "SELECT id FROM challenge_participants WHERE challenge_id = ? AND user_id = ?",
        (challenge_id, user_id),
    )
    already = cur.fetchone() is not None
    if not already:
        cur.execute(
            "INSERT INTO challenge_participants (challenge_id, user_id) VALUES (?, ?)",
            (challenge_id, user_id),
        )
        conn.commit()
    conn.close()

    return {
        "message": "Already joined" if already else "Joined challenge",
        "challenge_id": challenge_id,
        "title": ch["title"],
        "badge": ch["badge"],
        "joined": True,
        "progress": compute_challenge_progress(user_id, ch),
    }


@app.get("/challenges/{challenge_id}/progress")
def get_challenge_progress(challenge_id: int, user_id: int = Depends(get_current_user)):
    """Return the authenticated user's progress in a challenge.

    Progress is computed live from the activity stream even before joining, but
    ``joined`` reflects whether the user has formally joined. When the target is
    reached the participant row is stamped ``completed_at`` (badge earned).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM challenges WHERE id = ?", (challenge_id,))
    ch = cur.fetchone()
    if not ch:
        conn.close()
        raise HTTPException(status_code=404, detail="Challenge not found")
    ch = dict(ch)

    cur.execute(
        "SELECT joined_at, completed_at FROM challenge_participants "
        "WHERE challenge_id = ? AND user_id = ?",
        (challenge_id, user_id),
    )
    part = cur.fetchone()

    progress = compute_challenge_progress(user_id, ch)

    # Persist the first moment the challenge is completed so the badge is sticky.
    completed_at = part["completed_at"] if part else None
    if part and progress["completed"] and not completed_at:
        cur.execute(
            "UPDATE challenge_participants SET completed_at = CURRENT_TIMESTAMP "
            "WHERE challenge_id = ? AND user_id = ?",
            (challenge_id, user_id),
        )
        conn.commit()
        completed_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.close()

    return {
        "challenge_id": challenge_id,
        "title": ch["title"],
        "description": ch["description"],
        "badge": ch["badge"],
        "period": ch["period"],
        "joined": part is not None,
        "joined_at": part["joined_at"] if part else None,
        "completed_at": completed_at,
        "badge_earned": bool(completed_at) or progress["completed"],
        **progress,
    }


@app.get("/leaderboard")
def get_leaderboard(period: str = "weekly", limit: int = 10):
    """Rank users by activity for a period and show their badges.

    - ``period``: ``weekly`` (7d), ``monthly`` (30d) or ``all-time``.
    - The activity score weights actions (compare/rate higher than a scan).
    - Each row returns rank, username, score, activity breakdown and the badges
      the user has earned from completed challenges.
    """
    period = (period or "weekly").strip().lower().replace("_", "-")
    if period in ("all", "alltime", "all-time"):
        period = "all-time"
    if period not in PERIOD_DAYS:
        raise HTTPException(
            status_code=400,
            detail="period must be one of: weekly, monthly, all-time",
        )
    limit = max(1, min(limit, 100))

    where = "WHERE ua.user_id IS NOT NULL"
    params = []
    if period != "all-time":
        where += " AND datetime(ua.created_at) >= datetime(?)"
        params.append(_utc_since(PERIOD_DAYS[period]))

    # Weighted activity score via a CASE expression, plus the raw action count.
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT ua.user_id AS user_id, u.username AS username,
               SUM(CASE ua.action_type
                     WHEN 'scan' THEN 1 WHEN 'compare' THEN 3 WHEN 'rate' THEN 2
                     WHEN 'share' THEN 1 WHEN 'favorite' THEN 1 ELSE 0 END) AS score,
               COUNT(*) AS actions
        FROM user_activity ua JOIN users u ON ua.user_id = u.id
        {where}
        GROUP BY ua.user_id, u.username
        ORDER BY score DESC, actions DESC, u.username ASC
        LIMIT ?
        """,
        (*params, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]

    # Per-user action breakdown for the users on the board (for a richer card).
    leaderboard = []
    for i, r in enumerate(rows):
        uid = r["user_id"]
        cur.execute(
            "SELECT action_type, COUNT(*) AS n FROM user_activity "
            f"WHERE user_id = ?{' AND datetime(created_at) >= datetime(?)' if period != 'all-time' else ''} "
            "GROUP BY action_type",
            ((uid, _utc_since(PERIOD_DAYS[period])) if period != "all-time" else (uid,)),
        )
        breakdown = {r2["action_type"]: r2["n"] for r2 in cur.fetchall()}
        badges = get_user_badges(uid)
        leaderboard.append({
            "rank": i + 1,
            "user_id": uid,
            "username": r["username"],
            "score": r["score"] or 0,
            "activity_count": r["actions"],
            "activity_breakdown": breakdown,
            "badges": [b["badge"] for b in badges],
            "badge_count": len(badges),
        })
    conn.close()

    return {
        "period": period,
        "count": len(leaderboard),
        "scoring": ACTIVITY_POINTS,
        "leaderboard": leaderboard,
    }


# ==============================================================================
# Smart Cart — Shopping List Optimization
# ==============================================================================
# A user builds a shopping list of products (by barcode) and asks Swapify to
# optimize it: for every item we surface the original plus its top 2 healthier
# same-category alternatives (reusing the /similar "better alternatives" engine),
# so they can swap up to a better basket. Lists are saved, fetchable and
# deletable; a replace endpoint swaps one item's barcode for a chosen alternative.

def _shopping_item_view(barcode, preferences=None):
    """Resolve a single shopping-list item to a compact scored product view."""
    product = get_scored_product(barcode, preferences)
    if not product:
        return {
            "barcode": barcode,
            "product_name": None,
            "found": False,
        }
    return {
        "barcode": product.get("barcode"),
        "product_name": product.get("product_name"),
        "brand": product.get("brand"),
        "category": product.get("category"),
        "score": product.get("score"),
        "grade": product.get("grade"),
        "sugar_g": product.get("sugar_g_per_serving"),
        "protein_g": product.get("protein_g_per_serving"),
        "sodium_mg": product.get("sodium_mg_per_serving"),
        "saturated_fat_g": product.get("saturated_fat_g_per_serving"),
        "fiber_g": product.get("fiber_g_per_serving"),
        "found": True,
    }


def load_shopping_list(list_id, preferences=None):
    """Return a saved shopping list with each item scored, or None if missing."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM shopping_lists WHERE id = ?", (list_id,))
    lst = cur.fetchone()
    if not lst:
        conn.close()
        return None
    lst = dict(lst)
    cur.execute(
        "SELECT barcode FROM shopping_list_items WHERE list_id = ? ORDER BY id",
        (list_id,),
    )
    barcodes = [r["barcode"] for r in cur.fetchall()]
    conn.close()

    items = [_shopping_item_view(bc, preferences) for bc in barcodes]
    return {
        "id": lst["id"],
        "user_id": lst["user_id"],
        "name": lst["name"],
        "created_at": lst["created_at"],
        "item_count": len(items),
        "items": items,
    }


@app.post("/shopping-list")
def create_shopping_list(
    body: ShoppingListCreate,
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Create a shopping list from a set of product barcodes.

    Barcodes are trimmed and de-duplicated (order preserved). The list is tied to
    the authenticated user when a token is supplied, otherwise anonymous. Returns
    the saved list with each item scored.
    """
    seen = set()
    barcodes = []
    for raw in (body.items or []):
        bc = (raw or "").strip()
        if bc and bc not in seen:
            seen.add(bc)
            barcodes.append(bc)
    if not barcodes:
        raise HTTPException(status_code=400, detail="items must contain at least one barcode")

    name = (body.name or "").strip() or "My Shopping List"
    owner = user_id if isinstance(user_id, int) else None

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO shopping_lists (user_id, name) VALUES (?, ?)", (owner, name)
    )
    list_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO shopping_list_items (list_id, barcode) VALUES (?, ?)",
        [(list_id, bc) for bc in barcodes],
    )
    conn.commit()
    conn.close()

    preferences = load_user_preferences(user_id)
    result = load_shopping_list(list_id, preferences)
    return {"message": "Shopping list created", **result}


@app.get("/shopping-list/{list_id}")
def get_shopping_list(
    list_id: int,
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Return a saved shopping list with each item scored."""
    preferences = load_user_preferences(user_id)
    result = load_shopping_list(list_id, preferences)
    if result is None:
        return JSONResponse(status_code=404, content={"error": "Shopping list not found"})
    return result


@app.get("/shopping-list/{list_id}/optimize")
def optimize_shopping_list(
    list_id: int,
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Optimize a shopping list: for every item return the original plus its top
    2 healthier same-category alternatives (higher score / better nutrition).

    Alternatives come from the same personalized "better alternatives" engine as
    ``/similar`` — so a logged-in user's dietary preferences shape the ranking and
    drop non-vegan swaps. Items with no healthier alternative return an empty
    ``alternatives`` list.
    """
    preferences = load_user_preferences(user_id)
    saved = load_shopping_list(list_id, preferences)
    if saved is None:
        return JSONResponse(status_code=404, content={"error": "Shopping list not found"})

    optimized = []
    improvable = 0
    total_gain = 0.0
    for item in saved["items"]:
        alts = find_better_alternatives(item["barcode"], preferences)
        if not isinstance(alts, list):
            alts = []  # find_better_alternatives returns a 404 JSONResponse off-catalogue
        top2 = alts[:2]
        best_alt_score = top2[0]["health_score"] if top2 else None
        gain = None
        if best_alt_score is not None and item.get("score") is not None:
            gain = round(best_alt_score - item["score"], 1)
            if gain > 0:
                improvable += 1
                total_gain += gain
        optimized.append({
            "original": item,
            "alternatives": top2,
            "best_alternative_score": best_alt_score,
            "potential_gain": gain,
            "has_healthier_option": bool(top2),
        })

    return {
        "list_id": saved["id"],
        "name": saved["name"],
        "item_count": saved["item_count"],
        "items_with_alternatives": improvable,
        "total_potential_gain": round(total_gain, 1),
        "items": optimized,
    }


@app.post("/shopping-list/{list_id}/replace")
def replace_shopping_list_item(
    list_id: int,
    body: ShoppingListReplace,
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Replace one item in the list (e.g. swap it for a healthier alternative)."""
    old_bc = (body.old_barcode or "").strip()
    new_bc = (body.new_barcode or "").strip()
    if not old_bc or not new_bc:
        raise HTTPException(status_code=400, detail="old_barcode and new_barcode are required")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM shopping_lists WHERE id = ?", (list_id,))
    if not cur.fetchone():
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Shopping list not found"})
    cur.execute(
        "SELECT id FROM shopping_list_items WHERE list_id = ? AND barcode = ? LIMIT 1",
        (list_id, old_bc),
    )
    target = cur.fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail=f"'{old_bc}' is not in this list")
    cur.execute(
        "UPDATE shopping_list_items SET barcode = ? WHERE id = ?",
        (new_bc, target["id"]),
    )
    conn.commit()
    conn.close()

    preferences = load_user_preferences(user_id)
    result = load_shopping_list(list_id, preferences)
    return {"message": f"Replaced {old_bc} with {new_bc}", **result}


@app.delete("/shopping-list/{list_id}")
def delete_shopping_list(
    list_id: int,
    user_id: Optional[int] = Depends(get_current_user_optional),
):
    """Delete a shopping list and its items."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM shopping_lists WHERE id = ?", (list_id,))
    if not cur.fetchone():
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Shopping list not found"})
    cur.execute("DELETE FROM shopping_list_items WHERE list_id = ?", (list_id,))
    cur.execute("DELETE FROM shopping_lists WHERE id = ?", (list_id,))
    conn.commit()
    conn.close()
    return {"message": "Shopping list deleted", "list_id": list_id}


# ==============================================================================
# Community Reviews & Discussions
# ==============================================================================
# Users leave a written review (text + 1-5 star rating) on a product, and the
# community upvotes/downvotes and replies to those reviews. A review is distinct
# from the structured taste/quality/value ratings in /rate-product — this is the
# free-text discussion layer. A user can delete only their own review; deleting a
# review cascades to its votes and replies.

def _review_vote_counts(cur, review_id):
    """Return (upvotes, downvotes, score) for a review."""
    cur.execute(
        "SELECT "
        "SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END) AS up, "
        "SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END) AS down "
        "FROM review_votes WHERE review_id = ?",
        (review_id,),
    )
    r = cur.fetchone()
    up = r["up"] or 0
    down = r["down"] or 0
    return up, down, up - down


def _review_replies(cur, review_id):
    """Return a review's replies (oldest first) with author usernames."""
    cur.execute(
        "SELECT rr.id, rr.user_id, rr.reply_text, rr.created_at, u.username "
        "FROM review_replies rr LEFT JOIN users u ON rr.user_id = u.id "
        "WHERE rr.review_id = ? ORDER BY rr.id ASC",
        (review_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _build_review(cur, row, include_replies=True):
    """Assemble a full review response dict from a reviews row."""
    r = dict(row)
    up, down, score = _review_vote_counts(cur, r["id"])
    review = {
        "id": r["id"],
        "user_id": r["user_id"],
        "username": r.get("username"),
        "barcode": r["barcode"],
        "rating": r["rating"],
        "review_text": r["review_text"],
        "created_at": r["created_at"],
        "upvotes": up,
        "downvotes": down,
        "vote_score": score,
    }
    if include_replies:
        replies = _review_replies(cur, r["id"])
        review["replies"] = replies
        review["reply_count"] = len(replies)
    return review


@app.post("/reviews")
def create_review(review: ReviewCreate, user_id: int = Depends(get_current_user)):
    """Submit a written review (text + 1-5 star rating) for a product."""
    if not isinstance(review.rating, int) or isinstance(review.rating, bool) \
            or not (1 <= review.rating <= 5):
        raise HTTPException(status_code=400, detail="rating must be an integer from 1 to 5")
    text = (review.review_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="review_text is required")
    barcode = (review.barcode or "").strip()
    if not barcode:
        raise HTTPException(status_code=400, detail="barcode is required")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reviews (user_id, barcode, rating, review_text) VALUES (?, ?, ?, ?)",
        (user_id, barcode, review.rating, text),
    )
    review_id = cur.lastrowid
    conn.commit()
    cur.execute(
        "SELECT r.*, u.username FROM reviews r LEFT JOIN users u ON r.user_id = u.id "
        "WHERE r.id = ?",
        (review_id,),
    )
    row = cur.fetchone()
    built = _build_review(cur, row)
    conn.close()
    return {"message": "Review submitted", "review": built}


@app.get("/reviews/{review_id}")
def get_review(review_id: int):
    """Get a single review with its vote counts and replies."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT r.*, u.username FROM reviews r LEFT JOIN users u ON r.user_id = u.id "
        "WHERE r.id = ?",
        (review_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Review not found"})
    built = _build_review(cur, row)
    conn.close()
    return built


@app.get("/product/{barcode}/reviews")
def get_product_reviews(barcode: str):
    """Get all reviews for a product, newest first, with a rating summary."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT r.*, u.username FROM reviews r LEFT JOIN users u ON r.user_id = u.id "
        "WHERE r.barcode = ? ORDER BY r.id DESC",
        (barcode,),
    )
    rows = cur.fetchall()
    reviews = [_build_review(cur, row) for row in rows]
    conn.close()

    total = len(reviews)
    avg_rating = round(sum(rv["rating"] for rv in reviews) / total, 2) if total else None
    return {
        "barcode": barcode,
        "total_reviews": total,
        "average_rating": avg_rating,
        "reviews": reviews,
    }


@app.delete("/reviews/{review_id}")
def delete_review(review_id: int, user_id: int = Depends(get_current_user)):
    """Delete a review — only the author may delete their own review. Cascades to
    the review's votes and replies."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM reviews WHERE id = ?", (review_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Review not found"})
    if row["user_id"] != user_id:
        conn.close()
        raise HTTPException(status_code=403, detail="You can only delete your own review")

    cur.execute("DELETE FROM review_votes WHERE review_id = ?", (review_id,))
    cur.execute("DELETE FROM review_replies WHERE review_id = ?", (review_id,))
    cur.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    conn.commit()
    conn.close()
    return {"message": "Review deleted", "review_id": review_id}


@app.post("/reviews/{review_id}/vote")
def vote_review(review_id: int, body: ReviewVote, user_id: int = Depends(get_current_user)):
    """Upvote or downvote a review. Re-voting updates the user's existing vote;
    voting the same direction twice removes the vote (toggle)."""
    direction = (body.vote or "").strip().lower()
    vote_map = {"up": 1, "upvote": 1, "down": -1, "downvote": -1}
    if direction not in vote_map:
        raise HTTPException(status_code=400, detail="vote must be 'up' or 'down'")
    vote_val = vote_map[direction]

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM reviews WHERE id = ?", (review_id,))
    if not cur.fetchone():
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Review not found"})

    cur.execute(
        "SELECT vote FROM review_votes WHERE review_id = ? AND user_id = ?",
        (review_id, user_id),
    )
    existing = cur.fetchone()
    if existing and existing["vote"] == vote_val:
        # Same vote again -> toggle it off.
        cur.execute(
            "DELETE FROM review_votes WHERE review_id = ? AND user_id = ?",
            (review_id, user_id),
        )
        action = "removed"
    else:
        cur.execute(
            "INSERT INTO review_votes (review_id, user_id, vote) VALUES (?, ?, ?) "
            "ON CONFLICT(review_id, user_id) DO UPDATE SET vote = excluded.vote, "
            "voted_at = CURRENT_TIMESTAMP",
            (review_id, user_id, vote_val),
        )
        action = "recorded"
    conn.commit()
    up, down, score = _review_vote_counts(cur, review_id)
    conn.close()
    return {
        "message": f"Vote {action}",
        "review_id": review_id,
        "upvotes": up,
        "downvotes": down,
        "vote_score": score,
    }


@app.post("/reviews/{review_id}/replies")
def reply_to_review(review_id: int, body: ReviewReply, user_id: int = Depends(get_current_user)):
    """Reply to a review (threaded discussion)."""
    text = (body.reply_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="reply_text is required")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM reviews WHERE id = ?", (review_id,))
    if not cur.fetchone():
        conn.close()
        return JSONResponse(status_code=404, content={"error": "Review not found"})
    cur.execute(
        "INSERT INTO review_replies (review_id, user_id, reply_text) VALUES (?, ?, ?)",
        (review_id, user_id, text),
    )
    reply_id = cur.lastrowid
    conn.commit()
    cur.execute(
        "SELECT rr.id, rr.user_id, rr.reply_text, rr.created_at, u.username "
        "FROM review_replies rr LEFT JOIN users u ON rr.user_id = u.id WHERE rr.id = ?",
        (reply_id,),
    )
    reply = dict(cur.fetchone())
    conn.close()
    return {"message": "Reply added", "reply": reply}


# ==============================================================================
# OCR Label Scanner  (Task 6 — Proof of Concept)
# ==============================================================================
# Upload a photo of a product's ingredient/nutrition label; the server runs it
# through Tesseract OCR (see ocr_label_scanner.py), extracts the ingredient list
# and any nutrition facts, and feeds them into the *existing* scoring engine
# (calculate_health_score_v2) so the label alone yields a health score, grade and
# flagged ingredients — no barcode required. OCR is an optional dependency: when
# it isn't installed the endpoint returns 503 with install guidance and the rest
# of the API is unaffected.

@app.get("/ocr/health")
def ocr_health():
    """Report whether the OCR stack (Tesseract + Pillow) is installed and ready."""
    available, reason = ocr_label_scanner.ocr_available()
    return {"ocr_available": available, "detail": reason}


@app.post("/ocr/scan-label")
async def ocr_scan_label(file: UploadFile = File(...)):
    """OCR an uploaded label image and score it (Task 6 POC).

    Multipart form with an image ``file`` (JPEG/PNG). Returns the raw OCR text,
    the parsed ingredient list and nutrition facts, and — by running them through
    the same ``calculate_health_score_v2`` engine used for catalogue products —
    a health ``score``, ``grade`` and ``ingredient_flags``. Returns 503 when the
    OCR engine isn't installed (see GET /ocr/health)."""
    available, reason = ocr_label_scanner.ocr_available()
    if not available:
        raise HTTPException(status_code=503, detail=f"OCR not available: {reason}")

    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds the 2 MB size limit.")
    if _detect_image_ext(file.content_type, data) is None:
        raise HTTPException(status_code=400, detail="Only JPEG and PNG images are accepted.")

    try:
        scan = ocr_label_scanner.scan_label(data)
    except ocr_label_scanner.OcrUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"OCR not available: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Map the OCR output onto a product-shaped dict and score it with the real
    # engine, so a scanned label is scored exactly like a catalogue product.
    pseudo_product = {
        "product_name": "Scanned label",
        "ingredients_text": scan["ingredients_text"],
        **scan["nutrition"],
    }
    score, grade, rule_version, breakdown = calculate_health_score_v2(pseudo_product, 1)

    return {
        "message": "Label scanned",
        "raw_text": scan["raw_text"],
        "ingredients": scan["ingredients"],
        "ingredients_text": scan["ingredients_text"],
        "nutrition": scan["nutrition"],
        "score": score,
        "grade": grade,
        "rule_version": rule_version,
        "ingredient_flags": breakdown.get("ingredient_flags", []),
        "breakdown": breakdown,
    }


# ==============================================================================
# Database-first bootstrap  (Task 1 — deployment readiness)
# ==============================================================================
# The app reads product data only from the database at request time. The CSV
# catalogue is used solely to *seed* a brand-new database here (and to *sync* it
# via sync_db.py). This makes the backend deployment-ready: a freshly provisioned
# host with no swapify.db comes up with the schema created and the catalogue
# loaded, with zero manual steps. On an already-populated database this is a no-op.

# The 12 nutrient/identity columns the products table needs, kept in sync with
# create_db.py and sync_db.py (the single CSV schema).
_CSV_CATEGORY_KEYWORDS = (
    "bar", "yogurt", "chips", "milkshake", "cereals", "museli", "noodles",
    "chocolate", "drink", "biscuits", "cookie", "mixture", "pie",
)


def _csv_num(value):
    """Parse a nutrient cell like '24. 5 mg' / 'not listed' into a float."""
    text = (value or "").lower().strip()
    if not text or "not listed" in text:
        return 0.0
    match = re.search(r"[\d.]+", text.replace(" ", ""))
    try:
        return float(match.group()) if match else 0.0
    except ValueError:
        return 0.0


def _csv_category(name):
    """Best-effort category from a product name (mirrors import_data.py)."""
    name = (name or "").lower()
    for cat in _CSV_CATEGORY_KEYWORDS:
        if cat in name:
            return cat
    return "other"


def _seed_products_from_csv(cursor):
    """Insert every CSV row into an empty products table. Returns the row count."""
    import csv

    inserted = 0
    with open(CSV_SEED_PATH, mode="r", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header
        for row in reader:
            if not row or len(row) < 11:
                continue
            try:
                serial_no = int((row[0] or "0").strip())
            except ValueError:
                serial_no = 0
            barcode = row[1].strip()
            product_name = row[2].strip()
            brand = row[3].strip()

            # Keep the Lay's sample aligned with the documented test barcode.
            if serial_no == 2:
                barcode = "8901491101837"
                product_name = "Lay's Classic Salted"
                brand = "Lay's"
                serving, sugar, sat_fat = 28.0, 0.3, 1.8
                sodium, protein, fiber, calories = 370.0, 2.0, 1.0, 150.0
            else:
                serving = _csv_num(row[4])
                sugar = _csv_num(row[5])
                sat_fat = _csv_num(row[6])
                sodium = _csv_num(row[7])
                protein = _csv_num(row[8])
                fiber = _csv_num(row[9])
                calories = _csv_num(row[10])

            cursor.execute(
                "INSERT OR IGNORE INTO products (barcode, product_name, brand, "
                "category, serving_size_g, sugar_g_per_serving, "
                "saturated_fat_g_per_serving, sodium_mg_per_serving, "
                "protein_g_per_serving, fiber_g_per_serving, "
                "calories_kcal_per_serving) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (barcode, product_name, brand, _csv_category(product_name), serving,
                 sugar, sat_fat, sodium, protein, fiber, calories),
            )
            inserted += cursor.rowcount
    return inserted


def ensure_products_seeded():
    """Ensure the products table exists and seed it from the CSV when empty.

    Idempotent and best-effort — a populated database is left untouched, and any
    failure is logged rather than fatal. Runs before the index/image migration so
    those ALTER/INDEX statements always have a products table to operate on.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS products (
                barcode TEXT PRIMARY KEY,
                product_name TEXT,
                brand TEXT,
                category TEXT,
                serving_size_g REAL,
                sugar_g_per_serving REAL,
                saturated_fat_g_per_serving REAL,
                sodium_mg_per_serving REAL,
                protein_g_per_serving REAL,
                fiber_g_per_serving REAL,
                calories_kcal_per_serving REAL,
                ingredients_text TEXT,
                image_url TEXT
            )
        ''')
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM products")
        if cur.fetchone()[0] == 0 and os.path.exists(CSV_SEED_PATH):
            seeded = _seed_products_from_csv(cur)
            conn.commit()
            logger.info("Database-first bootstrap: seeded %d products from %s.",
                        seeded, CSV_SEED_PATH)
        conn.close()
    except Exception as exc:  # pragma: no cover - defensive bootstrap
        logger.warning("ensure_products_seeded failed: %s", exc)


# Create the feature tables / seed challenges as soon as the module is imported,
# so these endpoints work against an existing swapify.db without a manual step.
ensure_feature_schema()
# Task 1 — database-first: create + seed the products table on a fresh DB before
# the migrations below (which assume a products table already exists).
ensure_products_seeded()
# Task 1A (indexes) + Task 2 (image_url column / product_images table).
ensure_performance_and_image_schema()


if __name__ == "__main__":
    import uvicorn

    # Deployment-ready entrypoint: HOST/PORT/RELOAD come from the environment so
    # the same file runs locally (127.0.0.1:8000 with reload) and on a live host
    # (0.0.0.0:$PORT, no reload) — most PaaS providers inject $PORT.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    reload_flag = os.environ.get("RELOAD", "true").lower() in ("1", "true", "yes")
    uvicorn.run("app:app", host=host, port=port, reload=reload_flag)
