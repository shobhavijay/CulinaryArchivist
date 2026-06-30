"""
Recipe splitter — detects and splits multi-recipe images.

Inserted in the express path between express_transcribe and express_suggest.

Single recipe  → recipe_count=1, express_transcription unchanged.
Multiple recipes → structures each recipe separately, stores in
                   multi_recipe_transcriptions, sets express_transcription
                   to recipe[0].  After indexer the graph loops via
                   multi_recipe_advance_node until all recipes are processed.
"""
import json
import logging
import re

from culinary_archivist import llm_client
from culinary_archivist.state import ArchivistState
from culinary_archivist.agents.express_transcribe import (
    _normalise_fractions,
    _structure_with_qwen,
)

log = logging.getLogger(__name__)

_DETECT_PROMPT = """You are given raw OCR text from a recipe image. Determine how many distinct recipes are present.

A new recipe starts when there is a clear title or heading followed by its own ingredient list and steps.
Common separators: blank lines between sections, numbered recipe titles, bold headings.

Return ONLY a JSON object — no explanation, no markdown:

If the text contains exactly ONE recipe:
{"count": 1}

If the text contains MULTIPLE recipes:
{"count": <N>, "recipes": [
  {"title": "<recipe 1 title>", "text": "<complete raw text of recipe 1, including its title, ingredients and steps>"},
  {"title": "<recipe 2 title>", "text": "<complete raw text of recipe 2>"}
]}

--- RAW TEXT ---
"""


def _parse_detect_response(raw: str) -> dict:
    """Parse the LLM detection response into a dict."""
    text = raw.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            text = m.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("Failed to parse recipe-count response — defaulting to 1. Raw: %r", raw[:200])
        return {"count": 1}


def recipe_count_check_node(state: ArchivistState) -> dict:
    """
    LangGraph node — detects multiple recipes in an image.

    Reads express_transcription.source_text (the verbatim OCR dump from Step 1).
    If single recipe:   sets recipe_count=1, leaves express_transcription unchanged.
    If multiple:        structures each recipe, populates multi_recipe_transcriptions,
                        and sets express_transcription to the first recipe so the
                        rest of the express path runs normally for recipe[0].
    """
    transcription = state.get("express_transcription") or {}
    source_text = transcription.get("source_text") or ""

    if not source_text:
        log.info("recipe_count_check: no source_text — treating as single recipe")
        return {"recipe_count": 1, "current_recipe_index": 0}

    log.info("recipe_count_check: scanning %d chars for multiple recipes...", len(source_text))
    raw = llm_client.chat(
        messages=[{"role": "user", "content": _DETECT_PROMPT + source_text}],
        max_tokens=4096,
        temperature=0,
    )
    result = _parse_detect_response(raw)
    count = int(result.get("count", 1))
    llm_calls = state.get("llm_calls", 0) + 1

    if count <= 1 or "recipes" not in result:
        log.info("recipe_count_check: single recipe confirmed")
        return {"recipe_count": 1, "current_recipe_index": 0, "llm_calls": llm_calls}

    # Multiple recipes — structure each split individually
    recipes_raw = result.get("recipes") or []
    log.info("recipe_count_check: %d recipes detected — structuring each...", len(recipes_raw))

    structured = []
    extra_llm_calls = 0
    for i, r in enumerate(recipes_raw):
        text = _normalise_fractions(r.get("text") or "")
        if not text:
            continue
        log.info("  structuring recipe %d/%d: %r", i + 1, len(recipes_raw), r.get("title", "?"))
        parsed = _structure_with_qwen(text)
        if not parsed.get("source_text"):
            parsed["source_text"] = text
        structured.append(parsed)
        extra_llm_calls += 1

    if not structured:
        log.warning("recipe_count_check: structuring returned nothing — treating as single recipe")
        return {"recipe_count": 1, "current_recipe_index": 0, "llm_calls": llm_calls + extra_llm_calls}

    total_llm_calls = llm_calls + extra_llm_calls
    log.info("recipe_count_check: %d recipes ready (llm_calls +%d)", len(structured), total_llm_calls)
    return {
        "recipe_count":              len(structured),
        "current_recipe_index":      0,
        "multi_recipe_transcriptions": structured,
        "express_transcription":     structured[0],
        "llm_calls":                 total_llm_calls,
    }


def multi_recipe_advance_node(state: ArchivistState) -> dict:
    """
    LangGraph node — advances to the next recipe after one cycle completes.

    Called after indexer when current_recipe_index < recipe_count - 1.
    Loads the next recipe into express_transcription and resets per-recipe fields
    so the express path (suggest → hitl → pdf → indexer) runs cleanly again.
    """
    idx = (state.get("current_recipe_index") or 0) + 1
    recipes = state.get("multi_recipe_transcriptions") or []
    next_recipe = recipes[idx]
    log.info("multi_recipe_advance: recipe %d/%d — %r",
             idx + 1, len(recipes), next_recipe.get("title"))
    return {
        "current_recipe_index":    idx,
        "express_transcription":   next_recipe,
        # Reset fields that belong to a single recipe pass
        "hitl_suggestions":        None,
        "express_hitl_metadata":   None,
        "express_low_conf_flag":   False,
        "pdf_path":                None,
        "indexed":                 False,
        "duplicate_flag":          False,
        "duplicate_of":            None,
    }
