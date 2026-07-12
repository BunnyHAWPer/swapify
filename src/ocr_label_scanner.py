"""OCR Label Scanner — Proof of Concept (Task 6).

Takes a photo of a packaged-food label and extracts the printed text using
Tesseract OCR (via ``pytesseract`` + ``Pillow``), then parses out:

  * the **ingredient list** (everything after the "Ingredients:" heading), and
  * any **nutrition facts** it can find (sugar, sodium/salt, saturated fat,
    protein, fiber, energy) as per-serving numbers,

so the result can be fed straight into Swapify's existing scoring engine
(``calculate_health_score_v2`` in app.py) — no separate scoring logic lives here.

Design notes
------------
* **Optional dependency.** Tesseract is not always installed (it's a native
  binary plus two Python packages). This module never imports them at module
  load; ``ocr_available()`` reports whether OCR can run and, if not, why. The
  FastAPI app imports this module unconditionally and simply returns a helpful
  503 when OCR isn't installed, so the rest of the backend keeps working.
* **Proof of concept.** Real-world label OCR needs image preprocessing
  (deskew, threshold, denoise) and a trained ingredient parser. This POC does a
  light grayscale + autocontrast pass and heuristic parsing — enough to
  demonstrate the end-to-end flow: image → text → ingredients → score.

Install (to actually run OCR):
    pip install pytesseract Pillow
    # plus the Tesseract engine itself:
    #   Windows : https://github.com/UB-Mannheim/tesseract/wiki  (then add to PATH
    #             or set TESSERACT_CMD to the tesseract.exe path)
    #   macOS   : brew install tesseract
    #   Linux   : sudo apt-get install tesseract-ocr
"""

from __future__ import annotations

import io
import os
import re
from typing import Dict, List, Optional


class OcrUnavailable(RuntimeError):
    """Raised when an OCR call is attempted but Tesseract/Pillow are missing."""


def ocr_available() -> tuple[bool, str]:
    """Return ``(available, reason)``.

    ``available`` is True only when ``pytesseract`` + ``Pillow`` import *and* the
    Tesseract binary is reachable. ``reason`` explains any failure so the caller
    (and the screen recording) can see exactly what to install.
    """
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on env
        return False, (
            "Python OCR packages not installed. Run: pip install pytesseract Pillow "
            f"({exc})"
        )

    # Allow an explicit path to the tesseract binary (common on Windows).
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd

    try:
        import pytesseract
        version = pytesseract.get_tesseract_version()
    except Exception as exc:  # pragma: no cover - depends on env
        return False, (
            "Tesseract engine not found. Install it and add to PATH (or set "
            "TESSERACT_CMD). See https://github.com/UB-Mannheim/tesseract/wiki. "
            f"({exc})"
        )
    return True, f"Tesseract {version} ready"


def extract_text_from_image(data: bytes, lang: str = "eng") -> str:
    """OCR the raw image ``data`` into a single text string.

    Applies a light preprocessing pass (grayscale + autocontrast) that noticeably
    improves recognition on photos of glossy packaging. Raises ``OcrUnavailable``
    when the OCR stack isn't installed.
    """
    available, reason = ocr_available()
    if not available:
        raise OcrUnavailable(reason)

    import pytesseract
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"Could not read image: {exc}")

    # Preprocess: grayscale + autocontrast for cleaner OCR on busy packaging.
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)

    return pytesseract.image_to_string(image, lang=lang)


# ------------------------------------------------------------------------------
# Parsing helpers — turn raw OCR text into structured, scorer-ready fields.
# ------------------------------------------------------------------------------

# Words that mark where the ingredient list ends (start of another panel).
_INGREDIENTS_END_MARKERS = (
    "nutrition", "nutritional", "allergen", "contains", "manufactured",
    "best before", "storage", "net weight", "mrp", "fssai", "marketed",
    "for allergen", "packed", "customer care",
)


def parse_ingredients(text: str) -> List[str]:
    """Extract the ingredient list from OCR text.

    Finds the "Ingredients" heading and returns everything up to the next label
    panel, split into individual ingredients. Nested parentheticals (e.g.
    "Chocolate (Sugar, Cocoa)") are kept with their parent item. Returns an empty
    list when no ingredient heading is found.
    """
    if not text:
        return []

    lower = text.lower()
    match = re.search(r"ingredients?\s*[:\-]?", lower)
    if not match:
        return []

    segment = text[match.end():]
    seg_lower = segment.lower()

    # Cut the segment at the first "next panel" marker so nutrition/allergen text
    # doesn't leak into the ingredient list.
    cut = len(segment)
    for marker in _INGREDIENTS_END_MARKERS:
        idx = seg_lower.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    segment = segment[:cut]

    # Normalise whitespace/newlines, then split on commas that are not inside
    # parentheses so "Milk Solids (Sugar, Cocoa)" stays a single item.
    segment = re.sub(r"\s+", " ", segment).strip(" .;:")
    ingredients, depth, current = [], 0, ""
    for ch in segment:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            ingredients.append(current)
            current = ""
        else:
            current += ch
    if current:
        ingredients.append(current)

    cleaned = []
    for item in ingredients:
        item = item.strip(" .;:*-•·")
        # Drop noise fragments the OCR sometimes leaves behind.
        if item and len(item) <= 60 and any(c.isalpha() for c in item):
            cleaned.append(item)
    return cleaned


# Regexes for the nutrients the scoring engine cares about. Each captures the
# first number following the nutrient name (units vary and are handled per-key).
_NUTRIENT_PATTERNS = {
    "sugar_g_per_serving": r"(?:added\s+)?sugars?\b[^\d]{0,20}?([\d.]+)\s*g",
    "saturated_fat_g_per_serving": r"saturated(?:\s+fat)?\b[^\d]{0,20}?([\d.]+)\s*g",
    "protein_g_per_serving": r"protein\b[^\d]{0,20}?([\d.]+)\s*g",
    "fiber_g_per_serving": r"(?:dietary\s+)?fib(?:re|er)\b[^\d]{0,20}?([\d.]+)\s*g",
    "calories_kcal_per_serving": r"(?:energy|calories)\b[^\d]{0,20}?([\d.]+)\s*(?:kcal|cal)",
}


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_nutrition(text: str) -> Dict[str, Optional[float]]:
    """Best-effort extraction of per-serving nutrition numbers from OCR text.

    Sodium is derived from a "sodium" figure when present (converted mg), else
    estimated from a "salt" figure (salt ≈ sodium × 2.5). Missing nutrients are
    omitted so the scorer treats them as unknown rather than zero.
    """
    if not text:
        return {}
    lower = text.lower()

    nutrition: Dict[str, Optional[float]] = {}
    for key, pattern in _NUTRIENT_PATTERNS.items():
        m = re.search(pattern, lower)
        if m:
            val = _to_float(m.group(1))
            if val is not None:
                nutrition[key] = val

    # Sodium (mg): prefer an explicit sodium figure, else convert from salt.
    m_sodium = re.search(r"sodium\b[^\d]{0,20}?([\d.]+)\s*(mg|g)?", lower)
    if m_sodium:
        val = _to_float(m_sodium.group(1))
        unit = (m_sodium.group(2) or "mg").lower()
        if val is not None:
            nutrition["sodium_mg_per_serving"] = val * 1000 if unit == "g" else val
    else:
        m_salt = re.search(r"salt\b[^\d]{0,20}?([\d.]+)\s*g", lower)
        if m_salt:
            val = _to_float(m_salt.group(1))
            if val is not None:
                # salt(g) → sodium(mg): /2.5 g then ×1000
                nutrition["sodium_mg_per_serving"] = round(val / 2.5 * 1000, 1)
    return nutrition


def scan_label(data: bytes, lang: str = "eng") -> Dict:
    """Full POC pipeline: image bytes → OCR text → ingredients + nutrition.

    Returns a dict with the raw text, the parsed ingredient list, a single
    ``ingredients_text`` string (comma-joined, ready for the scoring engine) and
    the parsed nutrition facts. Scoring itself is done by the caller (the app
    endpoint) so this module stays decoupled from the scoring code.
    """
    raw_text = extract_text_from_image(data, lang=lang)
    ingredients = parse_ingredients(raw_text)
    nutrition = parse_nutrition(raw_text)
    return {
        "raw_text": raw_text,
        "ingredients": ingredients,
        "ingredients_text": ", ".join(ingredients),
        "nutrition": nutrition,
    }


if __name__ == "__main__":
    # Tiny CLI so the POC can be demonstrated without the web server:
    #   python ocr_label_scanner.py path/to/label.jpg
    import json
    import sys

    ok, why = ocr_available()
    print(f"OCR available: {ok} — {why}")
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as fh:
            result = scan_label(fh.read())
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Usage: python ocr_label_scanner.py <image-path>")
