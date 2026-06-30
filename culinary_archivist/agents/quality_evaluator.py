"""
Quality Evaluator node — full path.

Responsibilities:
1. Compute a transcription completeness score (0–1) from repaired_transcription.
2. Run cross_registry_score() against all registry entries.
3. Write cross-registry results + quality fields into state.
4. Set low_conf_flag so the router can decide whether to loop or continue.

State written:
  transcription_quality_score : float  0–1
  transcription_confidence    : str    "high" | "medium" | "low"
  registry_scores             : dict   {region_id: raw_score}
  best_match_region           : str
  score_margin                : float  top1 - top2 (normalised)
  is_hybrid                   : bool
  low_conf_flag               : bool
"""
import logging

from culinary_archivist import config
from culinary_archivist.state import ArchivistState
from culinary_archivist.registry.scorer import cross_registry_score, classify_meal_type

log = logging.getLogger(__name__)


def _completeness(transcription: dict) -> float:
    """
    0.25 per populated field: title, ingredients (non-empty list),
    steps (non-empty list), source_text.
    """
    score = 0.0
    if transcription.get("title"):
        score += 0.25
    if transcription.get("ingredients"):
        score += 0.25
    if transcription.get("steps"):
        score += 0.25
    if transcription.get("source_text"):
        score += 0.25
    return score


def quality_evaluator_node(state: ArchivistState) -> dict:
    transcription = state.get("repaired_transcription") or {}

    # ── Completeness ─────────────────────────────────────────────────────────
    completeness = _completeness(transcription)
    log.info("Quality evaluator: completeness=%.2f", completeness)

    # ── Build recipe text for scorer ─────────────────────────────────────────
    parts = []
    if transcription.get("title"):
        parts.append(transcription["title"])
    parts.extend(transcription.get("ingredients", []))
    parts.extend(transcription.get("steps", []))
    if transcription.get("source_text"):
        parts.append(transcription["source_text"])
    recipe_text = " ".join(parts)

    # ── Cross-registry score ──────────────────────────────────────────────────
    if recipe_text.strip():
        scoring = cross_registry_score(recipe_text)
    else:
        log.warning("Quality evaluator: no recipe text — skipping registry scoring")
        scoring = {
            "ranked":    [],
            "top_region": "UNKNOWN",
            "is_hybrid": False,
            "raw_scores": {},
        }

    ranked         = scoring["ranked"]
    top_region     = scoring["top_region"]
    is_hybrid      = scoring["is_hybrid"]
    raw_scores     = scoring["raw_scores"]
    signal_density = scoring.get("signal_density", 0.0)
    meal_type      = scoring.get("meal_type", "unknown")
    orphan_ratio   = scoring.get("orphan_ratio", 0.0)
    orphan_phrases = scoring.get("orphan_phrases", [])

    # Score margin between #1 and #2 (normalised scores)
    if len(ranked) >= 2:
        margin = ranked[0]["score"] - ranked[1]["score"]
    else:
        margin = 1.0

    log.info(
        "Quality evaluator: top_region=%s  is_hybrid=%s  margin=%.3f  "
        "signal_density=%.3f  orphan_ratio=%.2f  meal_type=%s",
        top_region, is_hybrid, margin, signal_density, orphan_ratio, meal_type,
    )
    if orphan_phrases:
        log.info("Quality evaluator: orphan phrases (not in any registry): %s", orphan_phrases[:8])

    # ── Unknown-region detection ──────────────────────────────────────────────
    # Two independent signals that a recipe doesn't belong to any known region:
    #   1. orphan_ratio > threshold — many recipe phrases match NO registry at all
    #      (e.g. 'fish sauce', 'curry paste', 'kaffir lime' for Thai curry)
    #   2. signal_density < threshold — overall weak match across all registries
    # Either alone is enough to flag the region as unknown.
    is_unknown = (
        orphan_ratio   > config.DISCOVERY_ORPHAN_RATIO_THRESHOLD
        or signal_density < config.DISCOVERY_UNKNOWN_SCORE_THRESHOLD
    )
    if is_unknown:
        log.info(
            "Quality evaluator: unknown region detected "
            "(orphan_ratio=%.2f threshold=%.2f  OR  density=%.3f threshold=%.3f) "
            "— setting best_match_region=UNKNOWN",
            orphan_ratio, config.DISCOVERY_ORPHAN_RATIO_THRESHOLD,
            signal_density, config.DISCOVERY_UNKNOWN_SCORE_THRESHOLD,
        )
        effective_region = "UNKNOWN"
    else:
        effective_region = top_region

    # ── Registry confidence boost ─────────────────────────────────────────────
    registry_confidence = ranked[0]["score"] if ranked else 0.0

    # Combined quality: 60% completeness + 40% registry identification confidence
    quality_score = round(0.6 * completeness + 0.4 * registry_confidence, 4)

    if quality_score >= 0.8:
        confidence = "high"
    elif quality_score >= config.FULL_PATH_QUALITY_THRESHOLD:
        confidence = "medium"
    else:
        confidence = "low"

    low_conf = quality_score < config.FULL_PATH_QUALITY_THRESHOLD

    log.info(
        "Quality evaluator: quality_score=%.3f  confidence=%s  low_conf=%s",
        quality_score, confidence, low_conf,
    )

    return {
        "transcription_quality_score": quality_score,
        "transcription_confidence":    confidence,
        "registry_scores":             raw_scores,
        "best_match_region":           effective_region,
        "score_margin":                round(margin, 4),
        "signal_density":              round(signal_density, 4),
        "orphan_ratio":                round(orphan_ratio, 4),
        "meal_type":                   meal_type,
        "is_hybrid":                   is_hybrid,
        "low_conf_flag":               low_conf,
    }
