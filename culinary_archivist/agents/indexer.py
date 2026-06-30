"""
Indexer node — Phase 7: dual-store persistence + duplicate detection.

Flow:
  1. Build a flat record dict from ArchivistState (express or full path).
  2. Query ChromaDB for similar recipes already in the archive.
  3. If similarity distance < DUPLICATE_SIMILARITY_THRESHOLD:
       → interrupt() — human sees new vs existing, picks: index / replace / skip
  4. Act on decision, then write to SQLite + ChromaDB + JSON manifest.

interrupt() LangGraph semantics:
  • First execution  : interrupt(payload) raises → graph pauses, UI shows form
  • Resumed execution: interrupt(payload) returns the human's resume value
  The similarity check runs on both executions, which is fine — the second time
  interrupt() simply returns instead of raising.

State written:
  indexed       : bool
  duplicate_flag: bool
  duplicate_of  : str   (id of the matched recipe, when flagged)
"""
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from langgraph.types import interrupt

from culinary_archivist import config
from culinary_archivist.state import ArchivistState
from culinary_archivist.db.sqlite_store import (
    init_db, upsert_recipe as sqlite_upsert,
    get_recipe, delete_recipe as sqlite_delete,
)
from culinary_archivist.db.chroma_store import (
    upsert_recipe as chroma_upsert,
    delete_recipe as chroma_delete,
    query_similar, _build_document,
)

log = logging.getLogger(__name__)

_MANIFEST = config.OUTPUT_DIR / "recipe_index.json"

# Ensure SQLite schema exists (idempotent)
init_db()


# ── Manifest helpers (legacy) ─────────────────────────────────────────────────

def _load_manifest() -> list:
    if _MANIFEST.exists():
        return json.loads(_MANIFEST.read_text())
    return []


def _save_manifest(records: list) -> None:
    _MANIFEST.write_text(json.dumps(records, indent=2, ensure_ascii=False))


# ── Record builder ────────────────────────────────────────────────────────────

def _build_record(state: ArchivistState) -> dict:
    mode          = state.get("mode", "express")
    historian     = state.get("historian_output") or {}
    transcription = (
        state.get("express_transcription") or {}
        if mode == "express"
        else state.get("repaired_transcription") or {}
    )
    hitl = state.get("express_hitl_metadata") or {}

    record: dict = {
        "id":          str(uuid.uuid4()),
        "archived_at": datetime.now().isoformat(),
        "media_path":  state.get("media_path"),
        "pdf_path":    state.get("pdf_path"),
        "mode":        mode,
    }

    if mode == "express":
        record.update({
            "title":            hitl.get("title") or transcription.get("title"),
            "ingredients":      transcription.get("ingredients") or [],
            "steps":            transcription.get("steps") or [],
            "source_text":      transcription.get("source_text"),
            "region":           hitl.get("origin"),
            "sub_region":       None,
            "era":              hitl.get("era"),
            "tags":             hitl.get("tags") or [],
            "notes":            hitl.get("notes"),
            "origin_consensus": None,
            "final_origin":     None,
            "meal_type":        None,
            "is_hybrid":        None,
            "unknown_region":   None,
            "signal_density":   None,
            "orphan_ratio":     None,
            "score_margin":     None,
            "registry_scores":  None,
            "vibe_keywords":    None,
            "geo_tag":          None,
        })
    else:
        record.update({
            "title":       historian.get("title") or transcription.get("title"),
            "ingredients": (
                historian.get("ingredients") or transcription.get("ingredients") or []
            ),
            "steps":       (
                historian.get("steps") or transcription.get("steps") or []
            ),
            "source_text":      transcription.get("source_text"),
            "region":           state.get("final_origin") or historian.get("region_id"),
            "sub_region":       historian.get("sub_region"),
            "era":              historian.get("era"),
            "tags":             historian.get("tags") or [],
            "notes":            historian.get("notes"),
            "origin_consensus": state.get("origin_consensus"),
            "final_origin":     state.get("final_origin"),
            "meal_type":        state.get("meal_type"),
            "is_hybrid":        state.get("is_hybrid"),
            "unknown_region":   state.get("unknown_region"),
            "signal_density":   state.get("signal_density"),
            "orphan_ratio":     state.get("orphan_ratio"),
            "score_margin":     state.get("score_margin"),
            "registry_scores":  state.get("registry_scores"),
            "vibe_keywords":    historian.get("vibe_keywords"),
            "geo_tag":          historian.get("geo_tag"),
        })

    return record


# ── Duplicate detection ───────────────────────────────────────────────────────

# Generic pantry/measurement words filtered out of ingredient-overlap comparison —
# present in nearly every recipe regardless of cuisine, so they'd otherwise make
# unrelated dishes look like duplicates.
_GENERIC_INGREDIENT_WORDS = {
    "salt", "pepper", "oil", "water", "sugar", "fresh", "chopped", "sliced",
    "diced", "minced", "ground", "clove", "cloves", "taste", "needed", "and",
    "the", "for", "with", "tbsp", "tsp", "cup", "cups", "tablespoon",
    "tablespoons", "teaspoon", "teaspoons", "ounce", "ounces", "pound",
    "pounds", "gram", "grams", "small", "medium", "large", "to",
}


def _clean_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _GENERIC_INGREDIENT_WORDS}


def _ingredient_tokens(ingredients: list[str] | None) -> set[str]:
    tokens: set[str] = set()
    for line in ingredients or []:
        tokens |= _clean_tokens(line)
    return tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _find_duplicate(record: dict) -> dict | None:
    """
    Query ChromaDB for the closest existing recipe.
    Returns a result dict (id, title, distance, similarity %) or None if no
    match is close enough to show the human.

    Vector distance alone is not trusted as the final word: nomic-embed-text
    compresses short recipe documents into a narrow similarity band, so two
    genuinely different dishes (e.g. "Endive Boats with Smoked Salmon" vs.
    "Tomato Chutney Fry") can still score ~75% similar purely off shared
    generic cooking vocabulary. A lexical overlap check on title/ingredient
    tokens (see config.DUPLICATE_TITLE_OVERLAP_MIN / _INGREDIENT_JACCARD_MIN)
    has to also clear a bar before we bother the human with a false positive.
    """
    doc = _build_document(record)
    if not doc.strip():
        return None

    try:
        results = query_similar(doc, n_results=1)
    except Exception as exc:
        log.warning("Duplicate check: ChromaDB query failed — %s", exc)
        return None

    if not results:
        return None

    top        = results[0]
    distance   = top.get("distance", 1.0)
    similarity = round(max(0.0, 1.0 - distance) * 100, 1)  # as a percentage

    log.info(
        "Duplicate check: closest match id=%s title=%r distance=%.3f similarity=%.1f%%",
        top.get("id"), top.get("title"), distance, similarity,
    )

    if distance >= config.DUPLICATE_SIMILARITY_THRESHOLD:
        return None   # not similar enough to bother the human

    # Fetch full record from SQLite to give the human richer context
    existing = get_recipe(top["id"]) or {}

    # ── Lexical guard ──────────────────────────────────────────────────────
    title_overlap = _jaccard(
        _clean_tokens(record.get("title") or ""),
        _clean_tokens(existing.get("title") or top.get("title") or ""),
    )
    ingredient_overlap = _jaccard(
        _ingredient_tokens(record.get("ingredients")),
        _ingredient_tokens(existing.get("ingredients")),
    )

    log.info(
        "Duplicate check: lexical guard — title_overlap=%.2f  ingredient_overlap=%.2f "
        "(thresholds: title>=%.2f or ingredients>=%.2f)",
        title_overlap, ingredient_overlap,
        config.DUPLICATE_TITLE_OVERLAP_MIN, config.DUPLICATE_INGREDIENT_JACCARD_MIN,
    )

    if (title_overlap      < config.DUPLICATE_TITLE_OVERLAP_MIN
            and ingredient_overlap < config.DUPLICATE_INGREDIENT_JACCARD_MIN):
        log.info(
            "Duplicate check: vector distance was low (%.3f) but title/ingredient "
            "overlap too weak — treating as a false positive, not flagging '%s' vs '%s'",
            distance, record.get("title"), existing.get("title") or top.get("title"),
        )
        return None

    return {
        "id":         top["id"],
        "title":      existing.get("title") or top.get("title") or "(unknown)",
        "region":     existing.get("region") or top.get("region") or "—",
        "meal_type":  existing.get("meal_type") or top.get("meal_type") or "—",
        "era":        existing.get("era") or "—",
        "tags":       existing.get("tags") or [],
        "archived_at": existing.get("archived_at") or "—",
        "ingredients": (existing.get("ingredients") or [])[:6],   # preview only
        "distance":   round(distance, 4),
        "similarity": similarity,
    }


def _recipe_preview(record: dict) -> dict:
    """Compact view of the new recipe for the interrupt payload."""
    return {
        "title":       record.get("title") or "(unknown)",
        "region":      record.get("region") or "—",
        "meal_type":   record.get("meal_type") or "—",
        "era":         record.get("era") or "—",
        "tags":        record.get("tags") or [],
        "ingredients": (record.get("ingredients") or [])[:6],
    }


# ── Write helpers ─────────────────────────────────────────────────────────────

def _write_all(record: dict) -> None:
    """Write to SQLite, ChromaDB, and JSON manifest."""
    try:
        sqlite_upsert(record)
    except Exception as exc:
        log.error("Indexer: SQLite write failed — %s", exc)

    try:
        chroma_upsert(record)
    except Exception as exc:
        log.error(
            "Indexer: ChromaDB write failed — %s. Run: ollama pull %s",
            exc, config.EMBED_MODEL,
        )

    try:
        records = _load_manifest()
        records.append(record)
        _save_manifest(records)
    except Exception as exc:
        log.warning("Indexer: JSON manifest write failed — %s", exc)


def _delete_existing(existing_id: str) -> None:
    """Remove an existing recipe from both stores (replace flow)."""
    sqlite_delete(existing_id)
    chroma_delete(existing_id)


# ── Node ──────────────────────────────────────────────────────────────────────

def indexer_node(state: ArchivistState) -> dict:
    record = _build_record(state)

    # ── Duplicate check ───────────────────────────────────────────────────────
    match = _find_duplicate(record)

    if match:
        # interrupt() raises on first execution (pauses graph for human input).
        # On resumed execution it returns the human's decision dict.
        decision = interrupt({
            "type":            "duplicate_check",
            "new_recipe":      _recipe_preview(record),
            "existing_recipe": match,
            "similarity":      match["similarity"],
        })

        action = (decision or {}).get("action", "index")
        log.info("Indexer: duplicate decision=%r for existing id=%s", action, match["id"])

        if action == "skip":
            log.info("Indexer: human chose skip — not writing to archive")
            return {
                "indexed":        False,
                "duplicate_flag": True,
                "duplicate_of":   match["id"],
            }

        if action == "replace":
            log.info("Indexer: human chose replace — deleting existing id=%s", match["id"])
            _delete_existing(match["id"])

        # action == "index" (keep both) or action == "replace" (old removed above)
        _write_all(record)
        log.info(
            "Indexer: archived id=%s title=%r  [action=%s]",
            record["id"], record.get("title"), action,
        )
        return {
            "indexed":        True,
            "duplicate_flag": True,
            "duplicate_of":   match["id"],
        }

    # ── No duplicate — write normally ─────────────────────────────────────────
    _write_all(record)
    log.info(
        "Indexer: archived id=%s  title=%r  mode=%s  region=%s  meal_type=%s",
        record["id"], record.get("title"), record.get("mode"),
        record.get("region"), record.get("meal_type"),
    )
    return {
        "indexed":        True,
        "duplicate_flag": False,
        "duplicate_of":   "",
    }
