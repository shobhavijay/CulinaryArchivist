"""
Cross-Verifier node — full path, Loop 3.

Compares the region claimed by the Historian against the region
identified by the cross_registry_score() signal matching.

If they agree    → origin_consensus = agreed region, high confidence
If they conflict → examine score_margin:
                     margin > 0.30 → trust registry (strong signal)
                     margin ≤ 0.30 → trust historian (weaker signal, model knowledge wins)
                   Sets conflict_note explaining the decision.

State written:
  origin_consensus      : str
  final_origin          : str
  conflict_note         : str | None
  cross_verify_loop_count : int
"""
import logging

from culinary_archivist import config
from culinary_archivist.state import ArchivistState
from culinary_archivist.registry.loader import load_registry

log = logging.getLogger(__name__)

# When score_margin > this, the registry scorer is confident enough to override
_REGISTRY_TRUST_MARGIN = 0.30


def cross_verifier_node(state: ArchivistState) -> dict:
    loop_count  = state.get("cross_verify_loop_count", 0)
    historian   = state.get("historian_output") or {}
    best_region = state.get("best_match_region", "UNKNOWN")
    score_margin = state.get("score_margin", 0.0)
    is_hybrid   = state.get("is_hybrid", False)

    historian_region = historian.get("region_id") or "UNKNOWN"
    historian_origin = historian.get("origin") or "Unknown"

    log.info(
        "Cross-verifier [loop %d]: historian_region=%s  registry_region=%s  margin=%.3f  is_hybrid=%s",
        loop_count + 1, historian_region, best_region, score_margin, is_hybrid,
    )

    # ── Load registry for display names ──────────────────────────────────────
    registry = load_registry()
    best_display = registry.get(best_region, {}).get("display_name", best_region)

    # ── Agreement check ───────────────────────────────────────────────────────
    conflict_note = None

    # ── Highest-priority case: historian identified a region NOT in our registry
    # The scorer can only match known regions; if the LLM (with Wikipedia/web tools)
    # found a genuinely different cuisine, trust it and flag for Discovery.
    if (historian_region
            and historian_region not in ("UNKNOWN", "")
            and historian_region not in registry):
        origin_consensus = historian_origin
        final_origin     = historian_region
        conflict_note = (
            f"Historian identified '{historian_origin}' ({historian_region}) — "
            f"not in current registry. Registry scored '{best_display}' as closest match "
            f"but signal density was low. Flagging as unknown region for Discovery."
        )
        log.info(
            "Cross-verifier: historian found non-registry region %r — "
            "trusting historian, flagging unknown_region=True",
            historian_region,
        )
        # unknown_region set below in the detection block

    elif historian_region == best_region or historian_region == "UNKNOWN":
        # Agree — or historian couldn't determine — trust registry
        origin_consensus = best_display
        final_origin     = best_region
        log.info("Cross-verifier: consensus on %s", final_origin)

    elif is_hybrid:
        # Hybrid recipe — both sources partially right
        hist_display = registry.get(historian_region, {}).get("display_name", historian_region)
        origin_consensus = f"{hist_display} / {best_display} (hybrid)"
        final_origin     = best_region  # primary registry match
        conflict_note = (
            f"Hybrid recipe: historian identified {hist_display}, "
            f"registry scorer identified {best_display} as primary. "
            f"Score margin was {score_margin:.2f} — both influences present."
        )
        log.info("Cross-verifier: hybrid — %s", origin_consensus)

    elif score_margin > _REGISTRY_TRUST_MARGIN:
        # Registry has a clear winner — override historian
        origin_consensus = best_display
        final_origin     = best_region
        conflict_note = (
            f"Conflict: historian suggested {historian_origin} ({historian_region}), "
            f"but registry scorer strongly favours {best_display} "
            f"(margin={score_margin:.2f} > threshold {_REGISTRY_TRUST_MARGIN}). "
            f"Registry decision applied."
        )
        log.info("Cross-verifier: registry overrides historian — %s", final_origin)

    else:
        # Low registry margin — trust historian's richer contextual knowledge
        hist_display = registry.get(historian_region, {}).get("display_name", historian_region)
        origin_consensus = historian_origin
        final_origin     = historian_region
        conflict_note = (
            f"Conflict: historian suggested {historian_origin} ({historian_region}), "
            f"registry scorer favoured {best_display} "
            f"(margin={score_margin:.2f} ≤ threshold {_REGISTRY_TRUST_MARGIN}). "
            f"Historian decision applied — low registry signal."
        )
        log.info("Cross-verifier: historian overrides weak registry — %s", final_origin)

    if conflict_note:
        log.info("Cross-verifier conflict note: %s", conflict_note)

    # ── Unknown-region detection ──────────────────────────────────────────────
    # Three conditions can each independently flag unknown_region:
    #   1. Historian found a region not in our registry (handled above in conflict block)
    #   2. signal_density is below threshold (weak match against all known registries)
    known_regions   = set(registry.keys())
    historian_known = historian_region in known_regions
    signal_density = state.get("signal_density", 1.0)
    orphan_ratio   = state.get("orphan_ratio",   0.0)

    historian_found_unknown = (
        historian_region
        and historian_region not in ("UNKNOWN", "")
        and not historian_known
    )
    weak_registry_match = (
        signal_density < config.DISCOVERY_UNKNOWN_SCORE_THRESHOLD
        or orphan_ratio > config.DISCOVERY_ORPHAN_RATIO_THRESHOLD
    )

    unknown_region = historian_found_unknown or weak_registry_match

    if unknown_region:
        log.info(
            "Cross-verifier: UNKNOWN REGION — "
            "historian_found_unknown=%s  weak_registry_match=%s  "
            "signal_density=%.3f  threshold=%.3f  historian_region=%r",
            historian_found_unknown, weak_registry_match,
            signal_density, config.DISCOVERY_UNKNOWN_SCORE_THRESHOLD, historian_region,
        )

    # Propagate final_origin back into historian_output for PDF / indexer use
    updated_historian = dict(historian)
    updated_historian["origin"]    = origin_consensus
    updated_historian["region_id"] = final_origin

    return {
        "origin_consensus":        origin_consensus,
        "final_origin":            final_origin,
        "conflict_note":           conflict_note,
        "cross_verify_loop_count": loop_count + 1,
        "historian_output":        updated_historian,
        "unknown_region":          unknown_region,
    }
