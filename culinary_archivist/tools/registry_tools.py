"""
Phase 5 — Registry tools for the Historian ReAct loop.

lookup_registry(region_id)          — full profile text for a region
search_landmark_dishes(dish_name)   — which regions list this dish as landmark
list_regions()                      — enumerate available region_ids
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def lookup_registry(region_id: str) -> str:
    """
    Return a detailed text profile for the given region_id from the internal registry.
    Use list_regions() to discover valid region_ids.
    """
    try:
        from culinary_archivist.registry.loader import get_region, load_registry

        # Normalise — accept lower-case or mixed-case input
        registry = load_registry()
        norm_id  = region_id.upper().replace(" ", "_").replace("-", "_")

        # Try exact match first, then prefix search
        reg = registry.get(norm_id)
        if not reg:
            candidates = [k for k in registry if k.startswith(norm_id)]
            if candidates:
                reg = registry[candidates[0]]
                norm_id = candidates[0]

        if not reg:
            available = ", ".join(sorted(registry.keys()))
            return f"[Registry] Region '{region_id}' not found. Available: {available}"

        lines = [
            f"[Registry: {reg.get('display_name', norm_id)}]",
            f"Region ID: {norm_id}",
        ]

        if reg.get("sub_regions"):
            lines.append(f"Sub-regions: {', '.join(reg['sub_regions'][:8])}")

        if reg.get("description"):
            lines.append(f"Description: {reg['description']}")

        fp = reg.get("flavor_profile", {})
        if fp:
            lines.append(
                f"Flavor: dominant={fp.get('dominant', '?')}, "
                f"heat={fp.get('heat_level', '?')}, "
                f"sourness={fp.get('sourness', '?')}"
            )

        sp = reg.get("signature_spices", {})
        for key in ("tempering_base", "whole", "ground"):
            val = sp.get(key, [])
            if val:
                lines.append(f"Spices ({key}): {', '.join(str(v) for v in val[:8])}")
        fat = sp.get("fat_marker", [])
        if fat:
            lines.append(f"Fat: {', '.join(str(f) for f in fat[:4])}")

        techniques = reg.get("cooking_techniques", [])[:6]
        if techniques:
            lines.append(f"Techniques: {'; '.join(str(t) for t in techniques)}")

        landmark = reg.get("landmark_dishes", [])
        if isinstance(landmark, list):
            lines.append(f"Landmark dishes: {', '.join(str(d) for d in landmark[:10])}")

        archaic = reg.get("archaic_terms", {})
        if archaic:
            sample = list(archaic.items())[:8]
            lines.append("Archaic terms: " + "; ".join(f"{k} → {v}" for k, v in sample))

        era = reg.get("era_markers", {})
        if era:
            lines.append(f"Era markers: {era}")

        vibe = reg.get("vibe_keywords", [])[:8]
        if vibe:
            lines.append(f"Vibe: {', '.join(vibe)}")

        return "\n".join(lines)

    except Exception as e:
        log.warning("lookup_registry failed for %r: %s", region_id, e)
        return f"[Registry] Lookup failed: {e}"


def search_landmark_dishes(dish_name: str) -> str:
    """
    Search all registry entries for `dish_name` in their landmark_dishes lists.
    Returns matching regions and context. Useful for provenance cross-checking.
    """
    try:
        from culinary_archivist.registry.loader import load_registry

        registry = load_registry()
        dish_lower = dish_name.lower().strip()
        matches: list[str] = []

        for region_id, reg in registry.items():
            landmark = reg.get("landmark_dishes", [])
            if not isinstance(landmark, list):
                continue
            for dish in landmark:
                if dish_lower in str(dish).lower():
                    region_name = reg.get("display_name", region_id)
                    matches.append(f"  • {region_name} ({region_id}): listed as landmark dish")
                    break

        if not matches:
            return (
                f"[Registry] '{dish_name}' not found in any registry landmark_dishes list. "
                "It may be a local or household name, or the registry may not cover its region."
            )

        return f"[Registry] Landmark dish search for '{dish_name}':\n" + "\n".join(matches)

    except Exception as e:
        log.warning("search_landmark_dishes failed for %r: %s", dish_name, e)
        return f"[Registry] Search failed: {e}"


def list_regions() -> str:
    """Return a list of all available registry region_ids and their display names."""
    try:
        from culinary_archivist.registry.loader import load_registry

        registry = load_registry()
        lines = ["[Registry] Available regions:"]
        for region_id, reg in sorted(registry.items()):
            display = reg.get("display_name", region_id)
            lines.append(f"  {region_id}: {display}")
        return "\n".join(lines)

    except Exception as e:
        return f"[Registry] List failed: {e}"
