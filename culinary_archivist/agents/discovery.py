"""
Discovery Subagent — Phase 6.

Triggers when cross_verifier flags an unknown region (top registry score
below DISCOVERY_UNKNOWN_SCORE_THRESHOLD) AND DISCOVERY_ENABLED=true.

Strategy — reuse what the Historian already found, no repeat searches:
  1. Read historian_output + historian_tool_calls from state  (free)
  2. Derive a candidate region_id from the origin string      (pure Python)
  3. One small qwen call fills the gaps historian didn't produce:
       signature_spices, landmark_dishes, flavor_profile      (1 LLM call)
  4. Assemble a full registry YAML dict
  5. interrupt() — human sees the draft and either approves or skips
  6. On approve  → write YAML to registry dir + invalidate cache
     On skip/reject → discard silently

State read:
  historian_output      : dict  — enrichment findings
  historian_tool_calls  : list  — raw Wikipedia/web observations

State written:
  provisional_region_id : str
  pending_registry_entry: dict   — draft YAML before approval
  discovery_approved    : bool
  hitl_escalations      : int    (incremented)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml
from langgraph.types import interrupt

from culinary_archivist import config, llm_client
from culinary_archivist.state import ArchivistState

log = logging.getLogger(__name__)


# ── Region-ID derivation ──────────────────────────────────────────────────────

def _derive_region_id(origin: str) -> str:
    """
    "Tamil Nadu, South India"  → "TAMIL_NADU"
    "Rajasthani"               → "RAJASTHANI"
    "Sichuan, China"           → "SICHUAN"
    Falls back to "UNKNOWN_REGION" if origin is blank.
    """
    if not origin:
        return "UNKNOWN_REGION"
    # Take only the first comma-separated part
    first = origin.split(",")[0].strip()
    # Drop parenthetical qualifiers
    first = re.sub(r"\s*\(.*?\)", "", first).strip()
    # Drop common trailing qualifiers like "cuisine", "region", "style"
    first = re.sub(r"\s+(cuisine|region|style|food)$", "", first, flags=re.IGNORECASE).strip()
    # Slug-ify
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", first).upper().strip("_")
    return slug or "UNKNOWN_REGION"


# ── Gap-filler prompt ─────────────────────────────────────────────────────────

_GAP_FILLER_PROMPT = """\
You are a culinary archivist creating a new registry entry for a cuisine not yet in our database.
Based on the historian findings and research observations below, fill in ONLY these 4 fields.
Return ONLY a valid JSON object — no explanation, no markdown.

{{
  "signature_spices": {{
    "tempering_base": [<list of whole spices used for tempering — e.g. mustard seeds, curry leaves>],
    "ground":         [<list of ground spice blends — e.g. garam masala, five-spice>],
    "fat_marker":     [<list of dominant fats — e.g. ghee, coconut oil, lard>]
  }},
  "landmark_dishes": [<list of 6-10 famous dishes from this cuisine, as strings>],
  "flavor_profile": {{
    "dominant":   <short phrase — e.g. "tangy, earthy, coconut-forward">,
    "heat_level": <"mild" | "medium" | "high" | "very high">,
    "sourness":   <"low" | "medium" | "high">
  }},
  "vibe_keywords": [<5-8 evocative single words or short phrases — e.g. "slow-cooked", "festive">]
}}

--- HISTORIAN FINDINGS ---
Origin       : {origin}
Sub-region   : {sub_region}
Era          : {era}
Technique    : {technique_notes}
Provenance   : {provenance}
Tags         : {tags}
Archaic terms: {archaic}

--- RESEARCH OBSERVATIONS (Wikipedia + Web) ---
{observations}

--- OUTPUT ONLY THE JSON OBJECT ---
"""


def _call_gap_filler(historian: dict, observations: list[str]) -> dict:
    """One small qwen call to fill signature_spices, landmark_dishes, flavor_profile."""
    obs_text = "\n\n".join(observations[:6]) if observations else "(none)"
    # Trim to avoid context overflow
    if len(obs_text) > 2500:
        obs_text = obs_text[:2500] + " …[truncated]"

    prompt = _GAP_FILLER_PROMPT.format(
        origin         = historian.get("origin") or "Unknown",
        sub_region     = historian.get("sub_region") or "—",
        era            = historian.get("era") or "—",
        technique_notes= historian.get("technique_notes") or "—",
        provenance     = historian.get("provenance") or "—",
        tags           = ", ".join(historian.get("tags") or []) or "—",
        archaic        = str(historian.get("archaic_substitutions") or {})[:300],
        observations   = obs_text,
    )

    log.info("Discovery: calling LLM for gap-filler fields...")
    try:
        raw = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
            temperature=0,
        ).strip()
        log.debug("Discovery gap-filler raw:\n%s", raw[:800])
    except Exception as e:
        log.error("Discovery: gap-filler LLM call failed: %s", e)
        return {}

    # Parse JSON
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        if m:
            raw = m.group(1).strip()
    brace = re.search(r"\{[\s\S]*\}", raw)
    if brace:
        raw = brace.group(0)
    raw = re.sub(r'\\([^"\\/bfnrtu])', lambda m: m.group(1), raw)

    try:
        return json.loads(raw)
    except Exception:
        log.warning("Discovery: gap-filler JSON parse failed")
        return {}


# ── Registry YAML assembly ────────────────────────────────────────────────────

def _build_registry_entry(region_id: str, historian: dict, gaps: dict) -> dict:
    """
    Assemble a full registry YAML dict from historian findings + gap-filler output.
    All fields have safe defaults so the YAML is always valid even if partial.
    """
    origin = historian.get("origin") or ""
    # Display name: use origin string, strip " [inferred]" suffixes
    display_name = re.sub(r"\s*\[.*?\]", "", origin).strip() or region_id.replace("_", " ").title()

    # Sub-regions: historian sub_region → single-item list
    sub_reg = historian.get("sub_region")
    sub_regions = [sub_reg] if sub_reg else []

    # Description from provenance
    description = historian.get("provenance") or ""

    # Cooking techniques — split technique_notes into a list
    techniques_raw = historian.get("technique_notes") or ""
    techniques = [t.strip() for t in re.split(r"[;,\n]", techniques_raw) if t.strip()][:8]

    # Archaic terms from historian
    archaic = historian.get("archaic_substitutions") or {}

    # Era markers — wrap era string in a simple dict
    era = historian.get("era") or ""
    era_markers = {"general": era} if era else {}

    # Vibe keywords: merge historian's + gap-filler's, deduplicate
    hist_vibe = historian.get("vibe_keywords") or []
    gap_vibe  = gaps.get("vibe_keywords") or []
    vibe_keywords = list(dict.fromkeys(hist_vibe + gap_vibe))[:10]

    # Tags → partial signal (stored separately, not a standard registry field)
    # Use them to supplement landmark_dishes if gap-filler returned none
    gap_landmark = gaps.get("landmark_dishes") or []

    # Signature spices
    sig_spices = gaps.get("signature_spices") or {}

    # Flavor profile
    flavor_profile = gaps.get("flavor_profile") or {
        "dominant":   "unknown",
        "heat_level": "medium",
        "sourness":   "medium",
    }

    return {
        "region_id":         region_id,
        "display_name":      display_name,
        "sub_regions":       sub_regions,
        "description":       description,
        "flavor_profile":    flavor_profile,
        "signature_spices":  sig_spices,
        "cooking_techniques": techniques,
        "landmark_dishes":   gap_landmark,
        "archaic_terms":     archaic,
        "era_markers":       era_markers,
        "vibe_keywords":     vibe_keywords,
        "_discovery_note":   "Auto-drafted by Discovery Subagent — review before using in production.",
    }


# ── YAML writing + cache invalidation ────────────────────────────────────────

def _write_registry_yaml(entry: dict) -> Path:
    """Write the draft entry to the registry directory. Returns the file path."""
    region_id = entry["region_id"].lower()
    registry_dir = Path(config.REGISTRY_DIR)
    registry_dir.mkdir(parents=True, exist_ok=True)
    out_path = registry_dir / f"{region_id}.yaml"

    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(entry, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    log.info("Discovery: wrote new registry entry → %s", out_path)

    # Invalidate the in-memory cache so next registry load picks up the new file
    from culinary_archivist.registry import loader as _loader
    _loader._CACHE = None
    log.info("Discovery: registry cache invalidated")

    return out_path


# ── Node ─────────────────────────────────────────────────────────────────────

def discovery_node(state: ArchivistState) -> dict:
    unknown_region = state.get("unknown_region", False)

    # ── Guard: skip if disabled or not needed ────────────────────────────────
    if not config.DISCOVERY_ENABLED:
        log.info("Discovery: DISCOVERY_ENABLED=false — skipping")
        return {"discovery_approved": False}

    if not unknown_region:
        log.info("Discovery: unknown_region=False — skipping")
        return {"discovery_approved": False}

    # ── Read historian output (already in state — no repeat searches) ─────────
    historian     = state.get("historian_output") or {}
    observations  = state.get("historian_tool_calls") or []   # raw Wikipedia/web text

    origin = historian.get("origin") or ""
    log.info("Discovery: drafting registry entry for origin=%r", origin)

    # ── Derive candidate region_id ────────────────────────────────────────────
    region_id = _derive_region_id(origin)

    # Avoid overwriting an existing registry entry
    from culinary_archivist.registry.loader import load_registry
    existing = load_registry()
    if region_id in existing:
        log.info("Discovery: region_id %r already in registry — skipping draft", region_id)
        return {
            "provisional_region_id": region_id,
            "discovery_approved":    False,
        }

    log.info("Discovery: candidate region_id = %r", region_id)

    # ── Gap-filler LLM call ───────────────────────────────────────────────────
    gaps = _call_gap_filler(historian, observations)

    # ── Assemble full registry entry ──────────────────────────────────────────
    entry = _build_registry_entry(region_id, historian, gaps)
    log.info(
        "Discovery: draft ready — region_id=%r  landmark_dishes=%d  techniques=%d",
        region_id, len(entry.get("landmark_dishes", [])), len(entry.get("cooking_techniques", [])),
    )

    # ── HITL: show draft to human ─────────────────────────────────────────────
    draft_yaml_str = yaml.dump(entry, allow_unicode=True, sort_keys=False, default_flow_style=False)

    form_data = interrupt({
        "type":       "discovery_form",
        "region_id":  region_id,
        "draft_yaml": draft_yaml_str,
        "entry":      entry,
    })

    # ── Process human response ────────────────────────────────────────────────
    if not isinstance(form_data, dict):
        form_data = {}

    approved = form_data.get("approved", False)

    if approved:
        out_path = _write_registry_yaml(entry)
        log.info("Discovery: approved — saved to %s", out_path)
    else:
        log.info("Discovery: skipped by human — draft discarded")

    return {
        "provisional_region_id":  region_id,
        "pending_registry_entry": entry,
        "discovery_approved":     approved,
        "hitl_escalations":       state.get("hitl_escalations", 0) + 1,
        "llm_calls":              state.get("llm_calls", 0) + 1,
    }
