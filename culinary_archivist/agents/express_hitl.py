"""
Express HITL node.

Before pausing for the human, the agent:
  1. Uses cross_registry_score() to suggest the most likely region.
  2. Calls qwen to suggest 2–3 tags based on the recipe content.
  3. Uses the OCR title (or qwen-suggested title) as the pre-filled title.

The interrupt payload sent to Chainlit includes a `suggestions` dict so the
UI can present them to the human for confirmation or correction.

Resume value from Chainlit:
  {"title": str, "origin": str, "era": str, "tags": list[str], "notes": str,
   "accepted": bool}   ← True if human said "yes" to all suggestions
"""
import logging

from langgraph.types import interrupt

from culinary_archivist import config
from culinary_archivist.state import ArchivistState
from culinary_archivist.registry.scorer import cross_registry_score

log = logging.getLogger(__name__)

# ── Keyword sets for instant tag inference (no LLM call) ─────────────────────

_MEAT_KEYWORDS = {
    "chicken", "mutton", "lamb", "beef", "pork", "fish", "prawn", "shrimp",
    "crab", "lobster", "anchovy", "sardine", "tuna", "salmon", "bacon",
    "sausage", "mince", "keema", "turkey",
}
_SPICE_KEYWORDS = {
    "chilli", "chili", "cayenne", "jalapeño", "serrano", "guntur",
    "pepper", "paprika", "gochujang", "sriracha", "habanero",
}
_SWEET_KEYWORDS = {
    "sugar", "honey", "maple", "chocolate", "cake", "cookie", "brownie",
    "pie", "tart", "pudding", "custard", "mousse", "cream cheese", "frosting",
    "icing", "dessert", "sweet",
}
_BREAKFAST_KEYWORDS = {
    "pancake", "waffle", "oat", "granola", "egg", "omelette", "muffin",
    "smoothie", "yogurt", "toast", "cereal", "porridge",
}
_SLOW_KEYWORDS = {
    "overnight", "slow", "braise", "simmer", "marinate", "dum", "nihari",
    "stew", "pot roast", "confit",
}

# Beverages — checked FIRST, before any food-only heuristic. Without this,
# a cocktail like an Old Fashioned (sugar cube + no meat) falls through to
# "vegetarian, dessert", which is technically true but meaningless for a drink.
_BEVERAGE_KEYWORDS = {
    "cocktail", "mocktail", "highball", "lowball", "rocks glass", "coupe",
    "martini glass", "old fashioned", "negroni", "mojito", "margarita",
    "mule", "spritz", "sangria", "punch bowl", "muddle", "muddled",
    "shaken", "stirred", "garnish with a twist", "simple syrup",
    "bitters", "vermouth", "liqueur",
    "whiskey", "whisky", "bourbon", "rye whiskey", "rum", "gin", "vodka",
    "tequila", "mezcal", "brandy", "cognac",
    "wine", "champagne", "prosecco", "beer", "ale", "lager", "cider",
    "tonic water", "club soda", "soda water",
    "iced tea", "lemonade", "espresso", "cappuccino", "latte", "mocha",
}
_ALCOHOL_KEYWORDS = {
    "whiskey", "whisky", "bourbon", "rye whiskey", "rum", "gin", "vodka",
    "tequila", "mezcal", "brandy", "cognac", "vermouth", "liqueur",
    "bitters", "wine", "champagne", "prosecco", "beer", "ale", "lager",
    "cider", "cocktail",
}

# Human-readable category labels, keyed by the internal category id
_CATEGORY_LABELS = {
    "beverage":  "Beverage / Cocktail",
    "dessert":   "Dessert",
    "breakfast": "Breakfast",
    "main":      "Main Course",
}


def _classify_category(all_text: str) -> str:
    """
    Coarse recipe category — checked once, before any tag-specific heuristic,
    so downstream tagging logic doesn't misfire on ingredient overlap
    (e.g. sugar in a cocktail being read as a dessert signal).
    Returns one of: "beverage", "dessert", "breakfast", "main".
    """
    if any(k in all_text for k in _BEVERAGE_KEYWORDS):
        return "beverage"
    if any(k in all_text for k in _SWEET_KEYWORDS):
        return "dessert"
    if any(k in all_text for k in _BREAKFAST_KEYWORDS):
        return "breakfast"
    return "main"


def _suggest_tags_from_ingredients(
    transcription: dict, region_id: str | None
) -> tuple[list[str], str]:
    """
    Derive 2–3 tags instantly from ingredient keywords + registry region,
    plus a coarse category label. Zero LLM calls — pure string matching.

    Returns (tags, category_label).
    """
    all_text = " ".join([
        transcription.get("title") or "",
        " ".join(transcription.get("ingredients") or []),
        " ".join(transcription.get("steps") or []),
    ]).lower()

    category = _classify_category(all_text)
    tags: list[str] = []

    if category == "beverage":
        # Vegetarian/non-vegetarian doesn't apply to drinks — skip it.
        tags.append("cocktail" if any(k in all_text for k in _ALCOHOL_KEYWORDS) else "non-alcoholic")
        tags.append("beverage")
        if any(k in all_text for k in _SPICE_KEYWORDS):
            tags.append("spicy")   # e.g. spicy margarita, bloody mary
        return tags[:3], _CATEGORY_LABELS["beverage"]

    # ── Food path (unchanged behaviour below) ─────────────────────────────
    # Vegetarian / non-vegetarian
    if any(k in all_text for k in _MEAT_KEYWORDS):
        tags.append("non-vegetarian")
    else:
        tags.append("vegetarian")

    # Meal type
    if category == "dessert":
        tags.append("dessert")
    elif category == "breakfast":
        tags.append("breakfast")

    # Heat
    if any(k in all_text for k in _SPICE_KEYWORDS):
        tags.append("spicy")

    # Technique
    if any(k in all_text for k in _SLOW_KEYWORDS):
        tags.append("slow-cooked")

    # Region vibe (from registry scorer result — already computed for free)
    if region_id == "SOUTH_INDIAN":
        tags.append("traditional")
    elif region_id == "AMERICAN":
        if "bbq" in all_text or "smoke" in all_text:
            tags.append("bbq")

    return tags[:3], _CATEGORY_LABELS.get(category, _CATEGORY_LABELS["main"])   # cap tags at 3


def _suggest_region(transcription: dict) -> tuple[str | None, str | None]:
    """
    Run cross_registry_score on the transcription.
    Returns (region_display_name, region_id) or (None, None) if no clear match.
    """
    parts = []
    if transcription.get("title"):
        parts.append(transcription["title"])
    parts.extend(transcription.get("ingredients", []))
    parts.extend(transcription.get("steps", []))
    recipe_text = " ".join(parts)

    if not recipe_text.strip():
        return None, None

    try:
        result = cross_registry_score(recipe_text)
        ranked = result.get("ranked", [])
        if not ranked or ranked[0]["score"] == 0:
            return None, None

        region_id = result["top_region"]

        # Load display name from registry
        from culinary_archivist.registry.loader import get_region
        reg = get_region(region_id)
        display = reg.get("display_name", region_id) if reg else region_id

        log.info("Region suggestion: %s (score=%.3f)", display, ranked[0]["score"])
        return display, region_id
    except Exception as e:
        log.warning("Region suggestion failed: %s", e)
        return None, None




def express_suggest_node(state: ArchivistState) -> dict:
    """
    Runs BEFORE express_hitl_node.
    Computes suggestions and writes them into state.values so that
    app.py can read them reliably from state.values["hitl_suggestions"]
    after the interrupt — no fragile state.tasks extraction needed.
    """
    transcription = state.get("express_transcription") or {}

    region_display, region_id = _suggest_region(transcription)
    suggested_tags, category = _suggest_tags_from_ingredients(transcription, region_id)

    suggestions = {
        "title":             transcription.get("title") or "",
        "origin":            region_display or "",
        "tags":              suggested_tags,
        "category":          category,
        "title_is_suggested": bool(transcription.get("title_suggested", False)),
    }

    log.info(
        "Express suggest: title=%r  origin=%r  tags=%s  category=%r",
        suggestions["title"], suggestions["origin"], suggestions["tags"], category,
    )
    return {"hitl_suggestions": suggestions}


def express_hitl_node(state: ArchivistState) -> dict:
    # Suggestions were pre-computed by express_suggest_node and are in state
    suggestions = state.get("hitl_suggestions") or {}
    low_conf    = state.get("express_low_conf_flag", False)

    log.info(
        "Express HITL: pausing for human — title=%r  origin=%r  tags=%s  low_conf=%s",
        suggestions.get("title"), suggestions.get("origin"),
        suggestions.get("tags"), low_conf,
    )

    # ── Pause graph, hand suggestions to Chainlit ────────────────────────────
    form_data = interrupt({
        "type":        "express_hitl_form",
        "low_conf":    low_conf,
        "suggestions": suggestions,
    })

    # ── Merge human response with suggestions ────────────────────────────────
    if not isinstance(form_data, dict):
        form_data = {}

    accepted = form_data.get("accepted", False)

    if accepted:
        # Human said yes — use suggestions verbatim
        metadata = {
            "title":    suggestions.get("title")    or None,
            "origin":   suggestions.get("origin")   or None,
            "era":      form_data.get("era")        or None,
            "tags":     suggestions.get("tags")     or [],
            "category": suggestions.get("category") or None,
            "notes":    form_data.get("notes")      or None,
        }
    else:
        # Human provided corrections — their values override suggestions
        # (category isn't a correctable text field in the HITL form — it's
        # always taken from the heuristic suggestion)
        metadata = {
            "title":    form_data.get("title")  or suggestions.get("title")  or None,
            "origin":   form_data.get("origin") or suggestions.get("origin") or None,
            "era":      form_data.get("era")    or None,
            "tags":     form_data.get("tags")   or suggestions.get("tags")   or [],
            "category": suggestions.get("category") or None,
            "notes":    form_data.get("notes")  or None,
        }

    log.info("Express HITL: final metadata — %s", metadata)
    return {
        "express_hitl_metadata": metadata,
        "hitl_escalations": state.get("hitl_escalations", 0) + 1,
    }
