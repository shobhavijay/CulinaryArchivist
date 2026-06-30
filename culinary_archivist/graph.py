from langgraph.graph import StateGraph, END

from culinary_archivist.state import ArchivistState
from culinary_archivist.router import pre_flight_router, route_after_preflight
from culinary_archivist import config

# ── Express path ───────────────────────────────────────────────────────────
from culinary_archivist.agents.express_transcribe import express_transcribe_node
from culinary_archivist.agents.express_hitl       import express_suggest_node, express_hitl_node
from culinary_archivist.agents.recipe_splitter    import recipe_count_check_node, multi_recipe_advance_node

# ── Full path ──────────────────────────────────────────────────────────────
from culinary_archivist.agents.restorer          import restorer_node
from culinary_archivist.agents.quality_evaluator import quality_evaluator_node
from culinary_archivist.agents.reflexion_agent   import reflexion_agent_node
from culinary_archivist.agents.historian         import historian_node
from culinary_archivist.agents.cross_verifier    import cross_verifier_node
from culinary_archivist.agents.full_hitl         import full_hitl_node

# ── Discovery Subagent (Phase 6) ───────────────────────────────────────────
from culinary_archivist.agents.discovery import discovery_node

# ── Shared output (both paths) ─────────────────────────────────────────────
from culinary_archivist.agents.pdf_generator import pdf_generator_node
from culinary_archivist.agents.indexer       import indexer_node


# ─────────────────────────────────────────────────────────────────────────────
# Conditional edge functions — full path routing
# ─────────────────────────────────────────────────────────────────────────────

def route_after_quality_evaluator(state: ArchivistState) -> str:
    """
    After quality_evaluator:
      - low confidence + loops remaining → reflexion_agent (verbal reflection)
                                           then restorer (retry OCR with lessons)
      - quality OK                       → historian for enrichment
    """
    low_conf   = state.get("low_conf_flag", False)
    loop_count = state.get("transcription_loop_count", 0)

    if low_conf and loop_count < config.FULL_PATH_MAX_RESTORER_LOOPS:
        return "reflexion_agent"   # generate verbal lesson, then retry OCR
    return "historian"             # proceed to enrichment


def route_after_cross_verifier(state: ArchivistState) -> str:
    """
    After cross_verifier:
      - unknown region + discovery enabled → discovery node
      - otherwise                          → full_hitl
    """
    if state.get("unknown_region", False) and config.DISCOVERY_ENABLED:
        return "discovery"
    return "full_hitl"


def route_after_historian(state: ArchivistState) -> str:
    """
    After historian:
      - enrichment complete or partial → cross_verifier
      - not complete + loops remaining → historian (retry enrichment)
    """
    complete   = state.get("enrichment_complete", False)
    partial    = state.get("enrichment_partial", False)
    loop_count = state.get("historian_loop_count", 0)

    if (complete or partial) or loop_count >= config.FULL_PATH_MAX_HISTORIAN_LOOPS:
        return "cross_verifier"
    return "historian"   # retry enrichment


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(ArchivistState)

    # ── Entry ──────────────────────────────────────────────────────────────
    g.add_node("pre_flight_router",   pre_flight_router)

    # ── Express path ───────────────────────────────────────────────────────
    g.add_node("express_transcribe",    express_transcribe_node)
    g.add_node("recipe_count_check",    recipe_count_check_node)
    g.add_node("multi_recipe_advance",  multi_recipe_advance_node)
    g.add_node("express_suggest",       express_suggest_node)
    g.add_node("express_hitl",          express_hitl_node)

    # ── Full path ──────────────────────────────────────────────────────────
    g.add_node("restorer",            restorer_node)
    g.add_node("quality_evaluator",   quality_evaluator_node)
    g.add_node("reflexion_agent",     reflexion_agent_node)
    g.add_node("historian",           historian_node)
    g.add_node("cross_verifier",      cross_verifier_node)
    g.add_node("full_hitl",           full_hitl_node)

    # ── Discovery (stub — Phase 5+) ────────────────────────────────────────
    g.add_node("discovery",           discovery_node)

    # ── Shared output ──────────────────────────────────────────────────────
    g.add_node("pdf_generator",       pdf_generator_node)
    g.add_node("indexer",             indexer_node)

    # ── Edges — entry ──────────────────────────────────────────────────────
    g.set_entry_point("pre_flight_router")
    g.add_conditional_edges(
        "pre_flight_router",
        route_after_preflight,
        {
            "express": "express_transcribe",
            "full":    "restorer",
        },
    )

    # ── Edges — express path ───────────────────────────────────────────────
    g.add_edge("express_transcribe",   "recipe_count_check")
    g.add_edge("recipe_count_check",   "express_suggest")
    g.add_edge("multi_recipe_advance", "express_suggest")
    g.add_edge("express_suggest",      "express_hitl")
    g.add_edge("express_hitl",         "pdf_generator")

    # ── Edges — full path ──────────────────────────────────────────────────
    # restorer → quality_evaluator (always)
    g.add_edge("restorer", "quality_evaluator")

    # quality_evaluator → reflexion_agent (retry: verbal lesson first)
    #                  OR historian (quality OK: proceed to enrichment)
    g.add_conditional_edges(
        "quality_evaluator",
        route_after_quality_evaluator,
        {
            "reflexion_agent": "reflexion_agent",
            "historian":       "historian",
        },
    )
    # reflexion_agent always feeds into restorer for the next OCR attempt
    g.add_edge("reflexion_agent", "restorer")

    # historian → historian (retry) OR cross_verifier
    g.add_conditional_edges(
        "historian",
        route_after_historian,
        {
            "historian":      "historian",
            "cross_verifier": "cross_verifier",
        },
    )

    # cross_verifier → discovery (unknown region) OR full_hitl (known region)
    g.add_conditional_edges(
        "cross_verifier",
        route_after_cross_verifier,
        {
            "discovery": "discovery",
            "full_hitl": "full_hitl",
        },
    )
    # discovery always flows into full_hitl (for recipe metadata confirmation)
    g.add_edge("discovery", "full_hitl")
    g.add_edge("full_hitl", "pdf_generator")

    # ── Edges — convergence ────────────────────────────────────────────────
    g.add_edge("pdf_generator", "indexer")

    # After indexer: loop to next recipe if more remain, otherwise finish
    def route_after_indexer(state: ArchivistState) -> str:
        count = state.get("recipe_count", 1)
        idx   = state.get("current_recipe_index", 0)
        if count > 1 and idx < count - 1:
            return "multi_recipe_advance"
        return END

    g.add_conditional_edges(
        "indexer",
        route_after_indexer,
        {"multi_recipe_advance": "multi_recipe_advance", END: END},
    )

    return g


# Compiled without checkpointer for CLI smoke-tests.
# app.py compiles with MemorySaver to support interrupt()/HITL.
graph = build_graph().compile()
