"""
Phase 5 — Search tools for the Historian ReAct loop.

search_wikipedia(query)  — Wikipedia article summary (+ first 2 sections)
search_web(query)        — DuckDuckGo text search filtered to cooking/food sites
"""
from __future__ import annotations

import logging
import textwrap

log = logging.getLogger(__name__)

# ── Wikipedia ────────────────────────────────────────────────────────────────

def search_wikipedia(query: str, sentences: int = 8) -> str:
    """
    Search Wikipedia for culinary history related to `query`.
    Returns a plain-text summary capped at `sentences` sentences.
    Falls back gracefully on disambiguation or missing articles.
    """
    try:
        import wikipedia as _wiki
        _wiki.set_lang("en")

        # Try direct page fetch first, fall back to search
        try:
            page = _wiki.page(query, auto_suggest=True)
            summary = _wiki.summary(query, sentences=sentences, auto_suggest=True)
            result = f"[Wikipedia: {page.title}]\n{summary}"
        except _wiki.exceptions.DisambiguationError as e:
            # Pick the first option that looks food-related
            options = e.options
            food_option = next(
                (o for o in options
                 if any(w in o.lower() for w in ("dish", "food", "cuisine", "recipe", "cooking"))),
                options[0] if options else None
            )
            if not food_option:
                return f"[Wikipedia] Disambiguation for '{query}' — no clear food article found."
            summary = _wiki.summary(food_option, sentences=sentences, auto_suggest=False)
            result = f"[Wikipedia: {food_option}]\n{summary}"
        except _wiki.exceptions.PageError:
            # Fall back to search results
            hits = _wiki.search(query, results=3)
            if not hits:
                return f"[Wikipedia] No results for '{query}'."
            summary = _wiki.summary(hits[0], sentences=sentences, auto_suggest=False)
            result = f"[Wikipedia: {hits[0]}]\n{summary}"

        # Cap output length so it doesn't overflow the context window
        return textwrap.shorten(result, width=2000, placeholder=" …[truncated]")

    except Exception as e:
        log.warning("Wikipedia search failed for %r: %s", query, e)
        return f"[Wikipedia] Search failed: {e}"


# ── DuckDuckGo ───────────────────────────────────────────────────────────────

# Food/cooking sites to prefer in results
_FOOD_SITES = [
    "seriouseats.com", "bonappetit.com", "food52.com", "thekitchn.com",
    "epicurious.com", "allrecipes.com", "bbc.co.uk/food", "hebbarskitchen.com",
    "archanaskitchen.com", "vegrecipesofindia.com", "indianhealthyrecipes.com",
    "recipetineats.com", "simplyrecipes.com", "101cookbooks.com",
    "smittenkitchen.com", "tasteofhome.com",
]


def search_web(query: str, max_results: int = 4) -> str:
    """
    DuckDuckGo text search for food/recipe/culinary history content.
    Returns top results as a short plain-text digest.
    """
    try:
        # Package was renamed from duckduckgo_search → ddgs in v9+
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]

        # Append culinary context only when the query has no food keywords at all
        food_keywords = ("recipe", "cuisine", "food", "dish", "cooking", "culinary",
                         "history", "curry", "spice", "ingredient", "traditional")
        q_lower = query.lower()
        if not any(kw in q_lower for kw in food_keywords):
            query = f"{query} cuisine history"

        log.info("DuckDuckGo search: %r", query)

        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results * 2))

        if not raw_results:
            return f"[Web] No results for '{query}'."

        # Prefer results from food sites
        preferred = [r for r in raw_results
                     if any(site in r.get("href", "") for site in _FOOD_SITES)]
        other     = [r for r in raw_results if r not in preferred]
        ordered   = (preferred + other)[:max_results]

        lines = []
        for r in ordered:
            title = r.get("title", "").strip()
            body  = r.get("body",  "").strip()
            href  = r.get("href",  "").strip()
            snippet = textwrap.shorten(body, width=300, placeholder="…")
            lines.append(f"• {title}\n  {snippet}\n  Source: {href}")

        return "[Web search results]\n" + "\n\n".join(lines)

    except Exception as e:
        log.warning("DuckDuckGo search failed for %r: %s", query, e)
        return f"[Web] Search failed: {e}"
