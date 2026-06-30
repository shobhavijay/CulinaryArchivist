"""
cross_registry_score() — pure Python, no LLM, no graph wiring.

Scores a recipe's text signals against every loaded registry entry
and returns a ranked list of (region_id, score) plus an is_hybrid flag
when the top-two scores are within the margin threshold.

v2 — key changes over v1:
  • Stop-word filter removes generic English words from all signal sets
  • Landmark dish names extracted before " — " separator (not full sentences)
  • Archaic terms: keys only, not description values
  • Cooking technique names: before "(" only
  • Meal-type classifier added (breakfast / dessert / dinner / snack)
  • Minimum signal token length enforced (≥3 chars)
  • Signal density computed (weighted hits / recipe word count)
"""
from __future__ import annotations

import logging
import re
from typing import List

from culinary_archivist.registry.loader import load_registry

log = logging.getLogger(__name__)

# ── Tuning knobs ──────────────────────────────────────────────────────────────

HYBRID_MARGIN = 0.15

_W_SPICE      = 4.0   # signature spice hit (very distinctive)
_W_TECHNIQUE  = 2.5   # cooking technique hit
_W_INGREDIENT = 2.0   # base ingredient / pantry marker
_W_ARCHAIC    = 4.0   # archaic term hit (highly distinctive)
_W_VIBE       = 1.5   # vibe keyword
_W_LANDMARK   = 5.0   # landmark dish name (strongest signal)
_W_MEAL_TYPE  = 3.0   # meal-type category match

# ── Stop words ────────────────────────────────────────────────────────────────
# Common English words that appear in descriptions/sentences but carry no
# regional culinary signal. Filtered from ALL signal sets.

_STOP_WORDS: set[str] = {
    # Articles / prepositions / conjunctions
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "by", "from", "up", "as", "is", "it", "its", "be",
    "are", "was", "were", "this", "that", "these", "those", "into",
    "over", "under", "after", "before", "between", "through", "during",
    "about", "per", "via", "than", "then", "when", "where", "while",
    # Generic cooking verbs (appear in every cuisine's technique descriptions)
    "cook", "heat", "add", "stir", "mix", "combine", "place", "put",
    "make", "use", "let", "set", "cut", "remove", "bring", "serve",
    "allow", "cover", "turn", "keep", "season", "drain", "prepare",
    "transfer", "pour", "spread", "top", "fold", "beat", "whisk",
    "chop", "slice", "dice", "mince", "peel", "wash", "rinse",
    # Generic food words that appear in every cuisine
    "salt", "water", "oil", "sauce", "milk", "sugar", "rice",
    "and", "fresh", "cup", "cups", "tbsp", "tsp", "teaspoon",
    "tablespoon", "pound", "ounce", "gram", "kg", "ml", "liter",
    # Common English filler in descriptions
    "style", "based", "type", "made", "used", "using", "also", "very",
    "more", "most", "well", "even", "just", "only", "both", "each",
    "can", "may", "will", "has", "have", "had", "been", "being",
    "high", "low", "old", "new", "long", "large", "small", "hot",
    "cold", "dry", "wet", "rich", "light", "dark", "thick", "thin",
    "main", "side", "base", "layer", "form", "piece", "part", "cup",
}

# ── Meal-type signals ─────────────────────────────────────────────────────────
# Used to classify the recipe's meal category independently of region.
# Weighted hits steer the scorer toward sub-regions or context.

_MEAL_SIGNALS: dict[str, set[str]] = {
    "breakfast": {
        "pancake", "pancakes", "waffle", "waffles", "oatmeal", "oat", "oats",
        "granola", "muffin", "muffins", "toast", "smoothie", "yogurt",
        "scrambled", "poached egg", "french toast", "hash brown", "hash browns",
        "overnight oats", "acai", "cereal", "porridge", "crepe", "crepes",
        "breakfast", "brunch", "morning",
    },
    "dessert": {
        "cake", "cookie", "cookies", "brownie", "brownies", "pie", "tart",
        "pudding", "custard", "mousse", "frosting", "icing", "glaze",
        "cheesecake", "fudge", "candy", "chocolate", "caramel", "toffee",
        "dessert", "sweet", "biscuit", "scone", "donut", "doughnut",
        "macaron", "eclair", "profiterole", "meringue", "sorbet", "gelato",
        "halwa", "halwa", "ladoo", "barfi", "kheer", "payasam", "modak",
        "mithai", "jalebi", "gulab jamun",
    },
    "dinner": {
        "curry", "stew", "roast", "braise", "grill", "grilled", "baked",
        "soup", "biryani", "pulao", "dal", "dhal", "sabzi", "gravy",
        "masala", "kebab", "tikka", "korma", "vindaloo", "rogan",
        "stir fry", "noodle", "noodles", "pasta", "casserole", "chili",
        "burger", "sandwich", "taco", "burrito", "enchilada",
        "dinner", "lunch", "main course", "entree",
        # Italian / European pasta & main courses
        "fettuccine", "linguine", "spaghetti", "penne", "rigatoni", "tagliatelle",
        "alfredo", "carbonara", "bolognese", "risotto", "lasagna", "lasagne",
        "gnocchi", "ravioli", "tortellini", "osso buco", "piccata",
    },
    "snack": {
        "chips", "dip", "appetizer", "starter", "fritter", "pakora",
        "samosa", "chaat", "bhel", "popcorn", "cracker", "pretzel",
        "snack", "finger food",
    },
    "beverage": {
        "cocktail", "mocktail", "highball", "lowball", "old fashioned",
        "negroni", "mojito", "margarita", "mule", "spritz", "sangria",
        "muddled", "shaken", "stirred", "simple syrup", "bitters",
        "vermouth", "liqueur", "whiskey", "whisky", "bourbon", "rum",
        "gin", "vodka", "tequila", "mezcal", "brandy", "cognac",
        "wine", "champagne", "prosecco", "beer", "ale", "lager", "cider",
        "tonic water", "club soda", "iced tea", "lemonade", "espresso",
        "cappuccino", "latte", "mocha", "beverage", "drink", "smoothie",
    },
}


# ── Signal extraction helpers ─────────────────────────────────────────────────

def _clean_token(token: str) -> str | None:
    """Return lower-case token if it passes filters, else None."""
    t = token.lower().strip()
    if len(t) < 3:
        return None
    if t in _STOP_WORDS:
        return None
    return t


def _phrases_from_name(name: str, max_n: int = 4) -> set[str]:
    """
    Build n-gram phrase set from a clean ingredient/spice/dish name string.
    Keeps only tokens that pass the stop-word filter.
    """
    raw_words = re.findall(r"[a-z]+", name.lower())
    words = [w for w in raw_words if _clean_token(w) is not None]
    if not words:
        return set()
    phrases: set[str] = set()
    for n in range(1, min(max_n + 1, len(words) + 1)):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n])
            phrases.add(phrase)
    return phrases


def _extract_landmark_names(landmark_obj) -> List[str]:
    """
    Extract dish *names only* from landmark_dishes (before " — " or "(").
    Handles both list and dict (nested by sub-category) formats.
    """
    names: List[str] = []

    def _recurse(obj):
        if isinstance(obj, str):
            # Take only the part before " — " or "(" — that's the dish name
            name = re.split(r"\s+[—–-]\s+|\s+\(", obj)[0].strip()
            if name:
                names.append(name)
        elif isinstance(obj, list):
            for item in obj:
                _recurse(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                _recurse(v)

    _recurse(landmark_obj)
    return names


def _extract_technique_names(techniques: list) -> List[str]:
    """Extract technique name only (before '(') from technique strings."""
    names: List[str] = []
    for t in techniques:
        if isinstance(t, str):
            name = re.split(r"\s*\(", t)[0].strip()
            if name:
                names.append(name)
    return names


def _flatten_spices(obj, depth: int = 4) -> List[str]:
    """Recursively flatten signature_spices dict/list into ingredient name strings."""
    result: List[str] = []
    if isinstance(obj, str):
        result.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            result.extend(_flatten_spices(item, depth - 1))
    elif isinstance(obj, dict) and depth > 0:
        for v in obj.values():
            result.extend(_flatten_spices(v, depth - 1))
    return result


# ── Per-registry signal sets ──────────────────────────────────────────────────

def _extract_registry_signals(reg: dict) -> dict[str, set[str]]:
    """
    Extract categorised signal phrase sets from one registry entry.
    Each category is filtered for stop words and uses name-only extraction
    (not full description sentences).
    """
    # ── Spices: all leaf strings from signature_spices dict ──────────────────
    spice_names = _flatten_spices(reg.get("signature_spices", {}))
    spice_signals: set[str] = set()
    for name in spice_names:
        spice_signals |= _phrases_from_name(name)

    # ── Base ingredients ──────────────────────────────────────────────────────
    ingredient_signals: set[str] = set()
    base = reg.get("base_ingredients", {})
    for name in _flatten_spices(base):
        ingredient_signals |= _phrases_from_name(name)

    # ── Techniques: name only (before "(") ────────────────────────────────────
    technique_names = _extract_technique_names(reg.get("cooking_techniques", []))
    technique_signals: set[str] = set()
    for name in technique_names:
        technique_signals |= _phrases_from_name(name)

    # ── Archaic: KEYS only (not description values) ───────────────────────────
    archaic_keys = list(reg.get("archaic_terms", {}).keys())
    archaic_signals: set[str] = set()
    for key in archaic_keys:
        archaic_signals |= _phrases_from_name(key)

    # ── Vibe keywords ─────────────────────────────────────────────────────────
    vibe_signals: set[str] = set()
    for phrase in reg.get("vibe_keywords", []):
        if isinstance(phrase, str):
            vibe_signals |= _phrases_from_name(phrase)

    # ── Landmark dish names only ───────────────────────────────────────────────
    landmark_names = _extract_landmark_names(reg.get("landmark_dishes", []))
    landmark_signals: set[str] = set()
    for name in landmark_names:
        landmark_signals |= _phrases_from_name(name)

    return {
        "spice":      spice_signals,
        "ingredient": ingredient_signals,
        "technique":  technique_signals,
        "archaic":    archaic_signals,
        "vibe":       vibe_signals,
        "landmark":   landmark_signals,
    }


# ── Recipe phrase extraction ──────────────────────────────────────────────────

def _text_phrases(text: str, max_n: int = 4) -> set[str]:
    """All n-grams (1..max_n) from the recipe text, stop-word-filtered."""
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if _clean_token(w)]
    phrases: set[str] = set()
    for n in range(1, max_n + 1):
        for i in range(len(words) - n + 1):
            phrases.add(" ".join(words[i:i + n]))
    return phrases


# ── Meal-type classifier ──────────────────────────────────────────────────────

def classify_meal_type(recipe_text: str) -> str:
    """
    Classify the recipe as 'breakfast', 'dessert', 'dinner', 'snack', or 'unknown'.
    Uses whole-word keyword matching against _MEAL_SIGNALS — no LLM call.

    Uses regex word-boundary matching (\\b) so that e.g. 'sweet' does not match
    inside 'sweeten', 'icing' does not match inside 'slicing', 'tart' does not
    match inside 'mustard'.  Multi-word keywords (e.g. 'main course') are matched
    as complete phrases with boundaries on the outer edges only.

    Returns the category with the most hits, or 'unknown' if no clear signal.
    """
    text_lower = recipe_text.lower()
    counts: dict[str, int] = {}
    for meal_type, keywords in _MEAL_SIGNALS.items():
        hits = sum(
            1 for kw in keywords
            if re.search(r"\b" + re.escape(kw) + r"\b", text_lower)
        )
        if hits:
            counts[meal_type] = hits

    if not counts:
        return "unknown"
    return max(counts, key=counts.__getitem__)


# ── Cross-registry IDF helpers ────────────────────────────────────────────────

def _build_signal_frequency(
    all_signals: dict[str, dict[str, set[str]]]
) -> dict[str, int]:
    """
    Build a map of {phrase → number_of_registries_that_contain_it}.
    Used to apply an IDF penalty: phrases shared across many registries
    (e.g. 'chicken', 'garlic') score lower than region-exclusive phrases
    (e.g. 'curry leaves', 'buttermilk').
    """
    freq: dict[str, int] = {}
    for signals in all_signals.values():
        seen_in_this_registry: set[str] = set()
        for phrase_set in signals.values():
            seen_in_this_registry |= phrase_set
        for phrase in seen_in_this_registry:
            freq[phrase] = freq.get(phrase, 0) + 1
    return freq


def _build_global_signal_set(all_signals: dict[str, dict[str, set[str]]]) -> set[str]:
    """Union of every signal phrase across all registries."""
    global_set: set[str] = set()
    for signals in all_signals.values():
        for phrase_set in signals.values():
            global_set |= phrase_set
    return global_set


# ── Scoring (IDF-weighted) ────────────────────────────────────────────────────

def _score_recipe_against(
    recipe_phrases: set[str],
    signals: dict[str, set[str]],
    signal_frequency: dict[str, int],
) -> float:
    """
    Weighted hit count with cross-registry IDF penalty.
    A phrase found in N registries is worth (1/N) of its base weight,
    so region-exclusive signals dominate over generic shared ones.
    """
    weights = {
        "spice":      _W_SPICE,
        "ingredient": _W_INGREDIENT,
        "technique":  _W_TECHNIQUE,
        "archaic":    _W_ARCHAIC,
        "vibe":       _W_VIBE,
        "landmark":   _W_LANDMARK,
    }
    total = 0.0
    for category, weight in weights.items():
        hits = recipe_phrases & signals.get(category, set())
        if hits:
            log.debug("  [%s] hits: %s", category, sorted(hits))
        for hit in hits:
            freq      = signal_frequency.get(hit, 1)
            idf       = 1.0 / freq          # 1.0 if unique, 0.5 if in 2 registries, etc.
            total    += weight * idf
    return total


# ── Orphan signal detection ───────────────────────────────────────────────────

def _compute_orphan_signals(
    recipe_phrases: set[str],
    global_signals: set[str],
) -> tuple[set[str], float]:
    """
    Find SINGLE-WORD ingredient names in the recipe that appear in NO registry
    signal set — these are the clearest unknown-region evidence.

    Uses 1-gram words only (not arbitrary n-gram combinations) to avoid false
    positives from adjacent-word sequences like 'asafoetida hing dried red'
    which are not real signals but look like orphans.

    Examples for Thai curry:
      'lemongrass', 'galangal', 'kaffir', 'nam' → not in any registry → orphans
    Examples for South Indian sambar:
      'asafoetida', 'tamarind', 'mustard' → ARE in South Indian → not orphans

    Returns (orphan_word_set, orphan_ratio).
    orphan_ratio = orphan_words / total meaningful single-word recipe tokens (0-1).
    """
    # Only single meaningful words (stop-word-filtered, ≥3 chars already guaranteed
    # by _text_phrases → _clean_token)
    single_words = {p for p in recipe_phrases if len(p.split()) == 1}
    if not single_words:
        return set(), 0.0

    orphans = single_words - global_signals
    ratio   = round(len(orphans) / len(single_words), 4)
    return orphans, ratio


# ── Public API ────────────────────────────────────────────────────────────────

def cross_registry_score(recipe_text: str) -> dict:
    """
    Score recipe_text against every loaded registry entry.

    Returns
    -------
    dict with keys:
        ranked           : list[{region_id, score}] normalised 0-1, best-first
        top_region       : str
        is_hybrid        : bool
        raw_scores       : dict[region_id, float]   IDF-weighted, unnormalised
        top_raw_score    : float
        signal_density   : float  (top_raw / recipe_word_count)
        recipe_word_count: int
        meal_type        : str
        orphan_phrases   : list[str]  — meaningful recipe phrases in NO registry
        orphan_ratio     : float      — orphan_phrases / total multi-word phrases
    """
    registry = load_registry()
    if not registry:
        log.warning("cross_registry_score: registry is empty")
        return {
            "ranked": [], "top_region": "UNKNOWN", "is_hybrid": False,
            "raw_scores": {}, "top_raw_score": 0.0,
            "signal_density": 0.0, "recipe_word_count": 0,
            "meal_type": "unknown", "orphan_phrases": [], "orphan_ratio": 0.0,
        }

    recipe_phrases    = _text_phrases(recipe_text)
    recipe_words      = [w for w in re.findall(r"[a-z]+", recipe_text.lower()) if _clean_token(w)]
    recipe_word_count = max(len(recipe_words), 1)
    meal_type         = classify_meal_type(recipe_text)

    # ── Pre-compute cross-registry IDF + global signal set ───────────────────
    all_signals: dict[str, dict[str, set[str]]] = {
        region_id: _extract_registry_signals(reg)
        for region_id, reg in registry.items()
    }
    signal_frequency = _build_signal_frequency(all_signals)
    global_signals   = _build_global_signal_set(all_signals)

    # ── Orphan signals — in recipe but in no registry ─────────────────────────
    orphan_phrases, orphan_ratio = _compute_orphan_signals(recipe_phrases, global_signals)
    if orphan_phrases:
        log.info(
            "cross_registry_score: %d orphan phrase(s) (ratio=%.2f): %s",
            len(orphan_phrases), orphan_ratio,
            sorted(orphan_phrases)[:10],
        )

    # ── Score each registry with IDF weighting ────────────────────────────────
    raw_scores: dict[str, float] = {}
    for region_id, signals in all_signals.items():
        score = _score_recipe_against(recipe_phrases, signals, signal_frequency)
        raw_scores[region_id] = score
        log.info("cross_registry_score [%s] = %.2f", region_id, score)

    # Normalise to 0-1
    max_score = max(raw_scores.values()) if raw_scores else 1.0
    if max_score == 0:
        normalised = {k: 0.0 for k in raw_scores}
    else:
        normalised = {k: v / max_score for k, v in raw_scores.items()}

    ranked = sorted(
        [{"region_id": k, "score": round(v, 4)} for k, v in normalised.items()],
        key=lambda x: x["score"],
        reverse=True,
    )

    top_region     = ranked[0]["region_id"] if ranked else "UNKNOWN"
    top_raw_score  = max_score
    signal_density = round(top_raw_score / recipe_word_count, 4)

    is_hybrid = (
        len(ranked) >= 2
        and (ranked[0]["score"] - ranked[1]["score"]) <= HYBRID_MARGIN
    )

    log.info(
        "cross_registry_score: top=%s  raw=%.2f  density=%.3f  "
        "orphan_ratio=%.2f  meal=%s  hybrid=%s",
        top_region, top_raw_score, signal_density,
        orphan_ratio, meal_type, is_hybrid,
    )

    return {
        "ranked":            ranked,
        "top_region":        top_region,
        "is_hybrid":         is_hybrid,
        "raw_scores":        raw_scores,
        "top_raw_score":     top_raw_score,
        "signal_density":    signal_density,
        "recipe_word_count": recipe_word_count,
        "meal_type":         meal_type,
        "orphan_phrases":    sorted(orphan_phrases),
        "orphan_ratio":      orphan_ratio,
    }
