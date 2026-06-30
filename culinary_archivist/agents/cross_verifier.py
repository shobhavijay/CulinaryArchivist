"""
Cross-Verifier node — full path, Loop 3.

Implements the Self-Refine loop (Madaan et al., 2023 — arXiv:2303.17651)
to converge on a well-reasoned, confident region attribution:

  For each iteration (up to CROSS_VERIFIER_MAX_REFINE_STEPS):
    FEEDBACK — LLM critiques the current attribution given all available signals
    REFINE   — LLM produces a revised determination based on that critique
    STOP if  — LLM signals it is satisfied (continue_refining=false)
              OR max steps reached

Two independent signals are fed into the loop:
  • Historian  — LLM + Wikipedia/web research  (rich but potentially hallucinatory)
  • Registry   — keyword / ingredient matching  (deterministic but registry-limited)

When they agree, a single pass typically confirms quickly.
When they conflict, the loop works through the ambiguity with explicit reasoning
rather than a hard-coded margin threshold — producing a conflict note that
explains the final decision in plain language.

A rule-based fallback (original margin-threshold logic) is retained and used
automatically if all LLM calls fail (network down, model unavailable, etc.).

State written:
  origin_consensus        : str
  final_origin            : str
  conflict_note           : str | None
  cross_verify_loop_count : int
  unknown_region          : bool
"""
import json
import logging
import re

from culinary_archivist import config, llm_client
from culinary_archivist.state import ArchivistState
from culinary_archivist.registry.loader import load_registry

log = logging.getLogger(__name__)

# Kept for the rule-based fallback path only
_REGISTRY_TRUST_MARGIN = 0.30


# ── Self-Refine prompts ───────────────────────────────────────────────────────

_FEEDBACK_PROMPT = """\
You are auditing a region attribution for a culinary archival system.

Two independent methods produced these conclusions about recipe origin:

  Historian (LLM + Wikipedia / web research):
    Region : {historian_origin}
    ID     : {historian_region}

  Registry scorer (keyword / ingredient matching against curated region profiles):
    Region : {registry_display}
    ID     : {best_region}
    Score margin over next candidate : {score_margin:.2f}
    Signal density                   : {signal_density:.3f}
    Is hybrid cuisine                : {is_hybrid}

Recipe context:
  Title            : {title}
  Key ingredients  : {ingredients_short}

Critique this attribution. Address:
- Which signal is more reliable for this specific recipe and why?
- Is the registry margin strong evidence or weak noise?
- Are there known cross-regional or diaspora dishes that could confuse either signal?
- What concrete uncertainty remains?

Be specific — two to four sentences maximum.
"""

_REFINE_PROMPT = """\
You are finalising the region attribution for a culinary archive record.

Current critique of the attribution:
{feedback}

Available signals for reference:
  Historian claim : {historian_origin}  (id: {historian_region})
  Registry claim  : {registry_display}  (id: {best_region})
  Score margin    : {score_margin:.2f}
  Signal density  : {signal_density:.3f}

Choose the single best answer:
  A) AGREE with historian — {historian_origin}  (id: {historian_region})
  B) AGREE with registry  — {registry_display}  (id: {best_region})
  C) HYBRID               — list primary and secondary regions
  D) UNKNOWN              — evidence too weak to attribute confidently

Output ONLY a valid JSON object (no markdown, no extra text):
{{
  "final_region_id"  : "<registry id, or UNKNOWN>",
  "final_origin"     : "<human-readable origin string>",
  "confidence"       : "<high | medium | low>",
  "reasoning"        : "<one concise sentence explaining the choice>",
  "continue_refining": <true if you want one more critique pass, false if satisfied>
}}
"""


# ── JSON helper ───────────────────────────────────────────────────────────────

def _parse_refine_json(raw: str) -> dict | None:
    text = raw.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    try:
        result = json.loads(text)
        if isinstance(result, dict) and "final_region_id" in result:
            return result
    except json.JSONDecodeError:
        pass
    return None


# ── Self-Refine loop ──────────────────────────────────────────────────────────

def _run_self_refine(
    *,
    historian_origin: str,
    historian_region: str,
    best_region: str,
    registry_display: str,
    score_margin: float,
    signal_density: float,
    is_hybrid: bool,
    title: str,
    ingredients_short: str,
    max_steps: int,
) -> dict:
    """
    Self-Refine (Madaan et al., 2023):
      Each pass: FEEDBACK (critique current state) → REFINE (revised determination)
      Stops when the LLM signals confidence or max_steps is reached.

    Returns a dict with keys:
      final_region_id, final_origin, confidence, reasoning, feedback_trail
    """
    prompt_kwargs = dict(
        historian_origin=historian_origin,
        historian_region=historian_region,
        best_region=best_region,
        registry_display=registry_display,
        score_margin=score_margin,
        signal_density=signal_density,
        is_hybrid=is_hybrid,
        title=title,
        ingredients_short=ingredients_short,
    )

    # Sensible starting state — overwritten on first REFINE call
    current: dict = {
        "final_region_id":   historian_region if historian_region not in ("UNKNOWN", "") else best_region,
        "final_origin":      historian_origin,
        "confidence":        "low",
        "reasoning":         "(pending self-refine)",
        "continue_refining": True,
    }
    feedback_trail: list[str] = []

    for step in range(max_steps):
        log.info("Cross-verifier Self-Refine step %d/%d", step + 1, max_steps)

        # ── FEEDBACK ──────────────────────────────────────────────────────────
        try:
            feedback = llm_client.chat(
                messages=[{"role": "user", "content": _FEEDBACK_PROMPT.format(**prompt_kwargs)}],
                max_tokens=300,
                temperature=0,
            )
        except Exception as exc:
            log.warning("Self-Refine: feedback call failed (step %d): %s", step + 1, exc)
            break

        feedback = (feedback or "").strip()
        log.info("  Feedback: %s", feedback[:200])
        feedback_trail.append(feedback)

        # ── REFINE ────────────────────────────────────────────────────────────
        try:
            raw = llm_client.chat(
                messages=[{"role": "user", "content": _REFINE_PROMPT.format(
                    feedback=feedback, **prompt_kwargs
                )}],
                max_tokens=256,
                temperature=0,
            )
        except Exception as exc:
            log.warning("Self-Refine: refine call failed (step %d): %s", step + 1, exc)
            break

        parsed = _parse_refine_json(raw)
        if parsed:
            current = parsed
            log.info(
                "  Refined → region=%r  confidence=%s  continue=%s",
                current.get("final_region_id"),
                current.get("confidence"),
                current.get("continue_refining"),
            )
        else:
            log.warning("  Self-Refine: could not parse refine JSON (step %d)", step + 1)

        if not current.get("continue_refining", True):
            log.info("Cross-verifier Self-Refine: LLM satisfied after %d step(s)", step + 1)
            break

    current["feedback_trail"] = feedback_trail
    return current


# ── Rule-based fallback (original logic, used if LLM calls all fail) ─────────

def _rule_based_fallback(
    *,
    historian_origin: str,
    historian_region: str,
    best_region: str,
    best_display: str,
    score_margin: float,
    is_hybrid: bool,
    registry: dict,
) -> dict:
    """
    Original margin-threshold arbitration from cross_verifier_original.py.
    Invoked automatically when the Self-Refine LLM calls fail entirely so the
    node never silently returns empty results.
    """
    if historian_region == best_region or historian_region == "UNKNOWN":
        return dict(final_region_id=best_region, final_origin=best_display, conflict_note=None)

    if is_hybrid:
        hist_display = registry.get(historian_region, {}).get("display_name", historian_region)
        return dict(
            final_region_id=best_region,
            final_origin=f"{hist_display} / {best_display} (hybrid)",
            conflict_note=(
                f"Hybrid recipe: historian identified {hist_display}, "
                f"registry scorer identified {best_display} as primary. "
                f"Score margin was {score_margin:.2f} — both influences present."
            ),
        )

    if score_margin > _REGISTRY_TRUST_MARGIN:
        return dict(
            final_region_id=best_region,
            final_origin=best_display,
            conflict_note=(
                f"Conflict: historian suggested {historian_origin} ({historian_region}), "
                f"registry scorer strongly favours {best_display} "
                f"(margin={score_margin:.2f} > threshold {_REGISTRY_TRUST_MARGIN}). "
                f"Registry decision applied. [fallback — Self-Refine LLM unavailable]"
            ),
        )

    hist_display = registry.get(historian_region, {}).get("display_name", historian_region)
    return dict(
        final_region_id=historian_region,
        final_origin=historian_origin,
        conflict_note=(
            f"Conflict: historian suggested {historian_origin} ({historian_region}), "
            f"registry scorer favoured {best_display} "
            f"(margin={score_margin:.2f} <= threshold {_REGISTRY_TRUST_MARGIN}). "
            f"Historian decision applied. [fallback — Self-Refine LLM unavailable]"
        ),
    )


# ── Node ──────────────────────────────────────────────────────────────────────

def cross_verifier_node(state: ArchivistState) -> dict:
    loop_count     = state.get("cross_verify_loop_count", 0)
    historian      = state.get("historian_output") or {}
    best_region    = state.get("best_match_region", "UNKNOWN")
    score_margin   = state.get("score_margin", 0.0)
    is_hybrid      = state.get("is_hybrid", False)
    signal_density = state.get("signal_density", 1.0)
    orphan_ratio   = state.get("orphan_ratio", 0.0)

    historian_region = historian.get("region_id") or "UNKNOWN"
    historian_origin = historian.get("origin") or "Unknown"

    # Recipe context fed into Self-Refine prompts
    transcription     = state.get("repaired_transcription") or {}
    title             = historian.get("title") or transcription.get("title") or "(unknown)"
    ingredients_list  = historian.get("ingredients") or transcription.get("ingredients") or []
    ingredients_short = ", ".join(str(i) for i in ingredients_list[:8])

    registry     = load_registry()
    best_display = registry.get(best_region, {}).get("display_name", best_region)

    log.info(
        "Cross-verifier [loop %d]: historian=%s  registry=%s  margin=%.3f  is_hybrid=%s",
        loop_count + 1, historian_region, best_region, score_margin, is_hybrid,
    )

    conflict_note    = None
    origin_consensus = None
    final_origin     = None

    # ── Highest-priority case: historian found a region not in our registry ───
    # Self-Refine can't help when the registry is simply incomplete; trust the
    # historian directly and flag for the Discovery node to handle.
    if (historian_region
            and historian_region not in ("UNKNOWN", "")
            and historian_region not in registry):
        origin_consensus = historian_origin
        final_origin     = historian_region
        conflict_note    = (
            f"Historian identified '{historian_origin}' ({historian_region}) — "
            f"not in current registry. Registry scored '{best_display}' as closest match "
            f"but signal density was low. Flagging as unknown region for Discovery."
        )
        log.info(
            "Cross-verifier: non-registry region %r — trusting historian, "
            "flagging unknown_region=True",
            historian_region,
        )

    else:
        # ── Self-Refine: FEEDBACK → REFINE loop ──────────────────────────────
        refined = _run_self_refine(
            historian_origin=historian_origin,
            historian_region=historian_region,
            best_region=best_region,
            registry_display=best_display,
            score_margin=score_margin,
            signal_density=signal_density,
            is_hybrid=is_hybrid,
            title=title,
            ingredients_short=ingredients_short,
            max_steps=config.CROSS_VERIFIER_MAX_REFINE_STEPS,
        )

        final_region_id  = refined.get("final_region_id") or ""
        feedback_trail   = refined.get("feedback_trail") or []

        # If Self-Refine produced no useful output (all LLM calls failed),
        # fall back to the original rule-based arbitration.
        if not final_region_id or refined.get("reasoning") == "(pending self-refine)":
            log.warning(
                "Cross-verifier: Self-Refine produced no result — using rule-based fallback"
            )
            fb = _rule_based_fallback(
                historian_origin=historian_origin,
                historian_region=historian_region,
                best_region=best_region,
                best_display=best_display,
                score_margin=score_margin,
                is_hybrid=is_hybrid,
                registry=registry,
            )
            final_origin     = fb["final_region_id"]
            origin_consensus = fb["final_origin"]
            conflict_note    = fb.get("conflict_note")

        else:
            final_origin     = final_region_id if final_region_id != "UNKNOWN" else "UNKNOWN"
            origin_consensus = refined.get("final_origin") or best_display
            confidence       = refined.get("confidence", "")
            reasoning        = refined.get("reasoning", "")

            if final_origin == "UNKNOWN":
                origin_consensus = "Unknown"

            # Build a conflict note only when the two sources disagreed
            if historian_region not in ("UNKNOWN", "") and historian_region != best_region:
                conflict_note = (
                    f"Conflict resolved via Self-Refine "
                    f"({len(feedback_trail)} feedback pass(es)): "
                    f"historian suggested '{historian_origin}' ({historian_region}), "
                    f"registry suggested '{best_display}' ({best_region}). "
                    f"Final: '{origin_consensus}' — confidence={confidence}. "
                    f"Reasoning: {reasoning}"
                )

            log.info(
                "Cross-verifier Self-Refine done: final=%s  confidence=%s",
                final_origin, confidence,
            )

    if conflict_note:
        log.info("Cross-verifier conflict note: %s", conflict_note)

    # ── Unknown-region detection (unchanged from original) ────────────────────
    known_regions           = set(registry.keys())
    historian_known         = historian_region in known_regions
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
            "signal_density=%.3f  historian_region=%r",
            historian_found_unknown, weak_registry_match,
            signal_density, historian_region,
        )

    # Propagate final_origin back into historian_output for PDF / indexer use
    updated_historian            = dict(historian)
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
