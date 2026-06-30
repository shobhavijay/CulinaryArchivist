"""
Phase 5 — FastMCP tool server for the Culinary Archivist Historian.

Exposes four tools via the Model Context Protocol:
  • search_wikipedia      — Wikipedia article summaries
  • search_web            — DuckDuckGo search (food/cooking sites preferred)
  • lookup_registry       — Internal culinary registry lookup by region_id
  • search_landmark_dishes — Cross-registry dish provenance search

The server can be run standalone for external MCP clients:
    python -m culinary_archivist.tools.mcp_server

The Historian ReAct loop in historian.py also imports and calls these
functions directly (in-process) — no HTTP round-trip needed for local use.
"""
import logging

from fastmcp import FastMCP

from culinary_archivist.tools.search_tools   import search_wikipedia, search_web
from culinary_archivist.tools.registry_tools import (
    lookup_registry,
    search_landmark_dishes,
    list_regions,
)

log = logging.getLogger(__name__)

# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="CulinaryArchivistTools",
    instructions=(
        "Tools for culinary historical research. "
        "Use search_wikipedia for culinary history. "
        "Use search_web for food site recipes and context. "
        "Use lookup_registry for internal regional profiles. "
        "Use search_landmark_dishes to find which regions claim a dish."
    ),
)


@mcp.tool()
def wikipedia(query: str) -> str:
    """
    Search Wikipedia for culinary history, dish origins, or cultural food context.
    Best for: regional cuisine overviews, specific dish history, ingredient origins.
    Example: query='Sambar South Indian lentil soup history'
    """
    return search_wikipedia(query)


@mcp.tool()
def web_search(query: str) -> str:
    """
    Search cooking and food websites (DuckDuckGo) for recipes, traditions, and context.
    Best for: regional dish details, ingredient usage, technique explanations.
    Example: query='Kerala sadya traditional feast dishes'
    """
    return search_web(query)


@mcp.tool()
def registry(region_id: str) -> str:
    """
    Look up the internal culinary registry for a region.
    Returns flavor profile, signature spices, techniques, landmark dishes, and archaic terms.
    Use list_known_regions first if unsure of the region_id.
    Example: region_id='SOUTH_INDIAN'
    """
    return lookup_registry(region_id)


@mcp.tool()
def landmark_dishes(dish_name: str) -> str:
    """
    Search all registry regions for a dish by name in their landmark_dishes list.
    Returns which regions claim the dish as a landmark.
    Example: dish_name='Sambar'
    """
    return search_landmark_dishes(dish_name)


@mcp.tool()
def list_known_regions() -> str:
    """
    List all available region_ids in the internal registry.
    Call this before lookup_registry if you are unsure which region_id to use.
    """
    return list_regions()


# ── Tool registry for the ReAct dispatcher ───────────────────────────────────
# historian.py imports TOOL_MAP to dispatch tool calls without running MCP server

TOOL_MAP: dict[str, callable] = {
    "search_wikipedia":       search_wikipedia,
    "search_web":             search_web,
    "lookup_registry":        lookup_registry,
    "search_landmark_dishes": search_landmark_dishes,
    "list_regions":           list_regions,
}

# Compact tool descriptions injected into the historian prompt
TOOL_DESCRIPTIONS = """\
Available tools (call at most one per step):
- search_wikipedia(query)        : Wikipedia summary for culinary/historical context
- search_web(query)              : DuckDuckGo search on food/cooking sites
- lookup_registry(region_id)     : Internal registry profile (spices, techniques, landmark dishes)
- search_landmark_dishes(name)   : Find which regions claim a dish as a landmark
- list_regions()                 : List all available registry region IDs\
"""


if __name__ == "__main__":
    log.basicConfig(level=logging.INFO)
    mcp.run()
