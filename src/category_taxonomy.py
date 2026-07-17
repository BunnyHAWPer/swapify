"""Single source of truth for Swapify's product-category taxonomy.

Why this module exists
----------------------
"Better alternatives" (``/similar``) and the recommendation engine only ever
compare products *within the same category*. That is only as good as the category
label each product carries. The original taxonomy was a flat 13-keyword list,
duplicated in three files, that dropped ~two-thirds of the catalogue into a single
``other`` bucket and mis-ordered its keywords ("bar" matched "chocolate **bar**"
before "chocolate" ever got a look-in). The result was the reported bug: scanning
*Ching's Schezwan Chutney* surfaced *Maggi noodles* as a "better alternative",
because both had collapsed into the same meaningless ``other`` pile.

This module replaces that with one ordered, specific-before-generic ruleset used
everywhere a category is derived (the API seed in ``app.py`` and the ops scripts
``sync_db.py`` / ``import_data.py``). Ordering is deliberate:

* product *form* wins over brand — "chocolate **milkshake**" is a milkshake,
  "brownie **ice cream**" is ice cream, not a cake;
* unambiguous confectionery brands (Snickers, Twix, Kinder …) are pinned to
  ``chocolate`` before the generic "cake"/"biscuit" rules can steal them;
* "chocolate" is matched as a whole word class, so "chocolate **bar**" is a
  chocolate, never a generic "bar".

``guess_category`` takes the product name and (optionally) the brand, because
several products name their type only in the brand ("Snickers", "Bounty",
"Maggi"). Returns ``"other"`` when nothing matches — and callers treat ``other``
as *"no known peers"*, never as a bucket to pull alternatives from.
"""

from __future__ import annotations

# Ordered (category, keywords) rules. The FIRST rule with a keyword found in the
# lowercased "<name> <brand>" text wins, so the most specific / least ambiguous
# rules are listed first. Keep this the only place the taxonomy is defined.
CATEGORY_RULES = [
    # --- Health / functional drinks & powders (before "chocolate": a chocolate
    #     whey powder is a supplement, not a chocolate bar) ---------------------
    ("health_drink", [
        "whey", "muscleblaze", "protein powder", "horlic", "horlick", "boost",
        "bournvita", "complan", "pediasure", "ensure", "malt drink", "malted",
    ]),
    # --- Ready-to-eat meals & tonics ----------------------------------------
    ("ready_to_eat", [
        "ready to eat", "ready-to-eat", "dal makhini", "dal makhani",
        "paneer makhini", "paneer tikka", "punjabi choley", "punjabi tadka",
        "pav bhaji", "tomato rice", "pongal", "rajama", "rajma", "upma",
        "choley", "chyawanprash",
    ]),
    # --- Instant noodles / ramen --------------------------------------------
    ("noodles", ["noodle", "noodoles", "ramen", "maggi", "indomine", "vermicelli"]),
    # --- Frozen desserts (before cake/chocolate: "brownie ice cream" is ice cream) ---
    ("ice_cream", [
        "ice cream", "icecream", "kulfi", "ice bar", "chocobar", "frozen dessert",
        "ice pop", "kwality walls", "magnum", "cornetto", "corneto", "gourmet ice",
    ]),
    # --- Milkshakes & drinkable dairy (before "milk"/"chocolate") ------------
    ("milkshake", ["milkshake", "milk shake"]),
    ("yogurt", ["yogurt", "yoghurt", "greek yog"]),
    # --- Breakfast: muesli/granola before cereal; cereal before chocolate ----
    ("muesli", ["muesli", "museli", "granola"]),
    ("cereal", ["corn flakes", "cornflakes", "chocos cereal", "cerelac",
                "cereal", "flakes"]),
    ("oats", ["oats", "overnight oats", "rolled oat", "steel cut oat"]),
    # --- Confectionery brands pinned to chocolate BEFORE cake/biscuit can grab
    #     them ("Twix cookie...", "Hershey's kisses cookies..." are chocolates) --
    ("chocolate", [
        "snickers", "bounty", "twix", "ferrero", "rocher", "kinder", "galaxy",
        "milkybar", "munch", "five star", "raffaello", "nutties", "dairy milk",
        "schoko", "bueno", "kisses", "chocopie", "choco pie",
    ]),
    # --- Pancakes before cake ("pancake" contains "cake") -------------------
    ("pancake", ["pancake"]),
    # --- Bakery: cakes, pastries, pies, croissants --------------------------
    ("cake", [
        "cake", "muffin", "swiss roll", "spyroll", "croissant", "crossiant",
        "brownie", "chocobakes", "gobbles", "layered cake", "moonfills",
    ]),
    ("biscuit", [
        "biscuit", "biscoff", "cookie", "cookies", "marie", "jim jam", "bourbon",
        "hide and seek", "good day", "digestive", "toast", "parle g", "parle-g",
        "monaco", "cheeselings", "dark fantasy", "rusk",
    ]),
    ("protein_bar", [
        "protein bar", "energy bar", "whole truth", "yoga bar", "yogabar",
        "datebites", "date bites", "nuts protein",
    ]),
    # --- Salty snacks: chips, namkeen, makhana ------------------------------
    ("chips", [
        "chips", "wafer chip", "crisps", "nachos", "doritos", "kurkure", "bhujia",
        "bhujiya", "namkeen", "chiwda", "chivda", "mixture", "murukku", "sev",
        "makhana", "makana", "veg stix", "mong dal", "moong dal", "dal moth",
        "aaloo", "banana chips", "roasted channa", "takatak", "lacche",
        "deli chips",
    ]),
    # --- General chocolate (whole "chocolate" class, so "chocolate bar" -> here) ---
    ("chocolate", ["chocolate", "chocoate", "kitkat", "hershey", "cadbury choco",
                   "ragabites"]),
    # --- Juices ------------------------------------------------------------
    ("juice", [
        "juice", "aamras", "aam panna", "kala khatta", "anar", "santra", "kokam",
        "jal jeera", "thandai", "guvava", "guava", "pulp", "aamchi",
    ]),
    ("energy_drink", ["red bull", "monster", "gatorade", "getorade", "energy drink"]),
    # --- Soft drinks / sodas / drink mixes ----------------------------------
    ("soft_drink", [
        "cola", "coke", "sprite", "fanta", "limca", "thumbs up", "mountain dew",
        "pepsi", "maaza", "mazza", "frooti", "slice", "appy", "rooh afza",
        "rasna", "tang", "glucon", "mogu mogu", "lahori", "fizz", "soda",
        "cold drink", "sparkling",
    ]),
    # --- Drinkable dairy (chaas, lassi, flavoured milk) ---------------------
    ("dairy_drink", [
        "chaas", "lassi", "buttermilk", "kool", "badam", "milk packet",
        "tetra milk", "vanilla milk", "flavoured milk", "flavored milk",
    ]),
    ("coffee", ["coffee", "latte", "cafe", "nescafe", "cappuccino", "espresso"]),
    ("makhana", ["makhana", "makana"]),
    ("sauce", [
        "chutney", "schezwan", "ketchup", "sauce", "mayonnaise", "dip",
        "pickle", "spread",
    ]),
    ("nut_mix", ["nut mix", "nut mixture", "trail mix", "antioxidant mix",
                 "roasted nut"]),
    ("supplement", ["eno", "electrolyte", "ors", "sachet"]),
    # --- Generic fallbacks (last, so specific rules always win) -------------
    ("bar", ["bar"]),
    ("drink", ["drink", "beverage"]),
]


def guess_category(name, brand=None) -> str:
    """Return the product category for a name (and optional brand).

    Matching is case-insensitive over ``"<name> <brand>"`` so products that name
    their type only in the brand ("Snickers", "Maggi") still classify. Returns
    ``"other"`` when no rule matches; callers must treat ``other`` as "no known
    peers", never as a group to draw alternatives from.
    """
    text = ((name or "") + " " + (brand or "")).lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return "other"
