"""
CLI entry point — runs the Culinary Archivist pipeline without Chainlit.

Usage:
    python -m culinary_archivist.main --media /path/to/recipe.jpg
    python -m culinary_archivist.main --media /path/to/recipe.pdf --mode full

    # via pyproject.toml script entry point:
    archivist --media /path/to/recipe.jpg

HITL nodes (express_hitl, full_hitl, indexer duplicate check) prompt the user
interactively at the terminal.  Multi-recipe images loop automatically through
each recipe.
"""
import argparse
import json
import logging
import uuid

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from culinary_archivist.graph import build_graph

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s — %(message)s")
log = logging.getLogger(__name__)


def _prompt_choice(prompt: str, options: list[str]) -> str:
    options_lower = [o.lower() for o in options]
    while True:
        try:
            raw = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n→ Interrupted — using default:", options[0])
            return options[0]
        if raw in options_lower:
            return raw
        print(f"  Please enter one of: {', '.join(options)}")


def _auto_resume(interrupt_payload: dict) -> dict:
    itype = interrupt_payload.get("type", "unknown")
    print("\n" + "─" * 60)
    print(f"[HITL — {itype}]")

    # ── Duplicate detection ───────────────────────────────────────────────────
    if itype == "duplicate_check":
        new   = interrupt_payload.get("new_recipe", {})
        exist = interrupt_payload.get("existing_recipe", {})
        sim   = interrupt_payload.get("similarity", 0)
        print(f"\n  ⚠ Possible duplicate ({sim:.1f}% similar)")
        print(f"  NEW:      {new.get('title')}  |  {new.get('region')}  |  {new.get('era')}")
        print(f"  EXISTING: {exist.get('title')}  |  archived {(exist.get('archived_at') or '')[:10]}")
        choice = _prompt_choice(
            "  → Action [index / replace / skip]: ", ["index", "replace", "skip"]
        )
        print("─" * 60)
        return {"action": choice}

    # ── Express HITL ──────────────────────────────────────────────────────────
    if itype == "express_hitl_form":
        suggestions = interrupt_payload.get("suggestions", {})
        print("\n  Agent suggestions:")
        print(json.dumps(suggestions, indent=4, default=str))
        choice = _prompt_choice(
            "  → Accept suggestions? [yes / no]: ", ["yes", "no"]
        )
        print("─" * 60)
        return {"accepted": choice == "yes"}

    # ── Full HITL ─────────────────────────────────────────────────────────────
    if itype == "full_hitl_form":
        output = interrupt_payload.get("historian_output", {})
        print("\n  Historian output:")
        for k, v in output.items():
            if v:
                print(f"    {k}: {v}")
        choice = _prompt_choice(
            "  → Accept historian output? [yes / no]: ", ["yes", "no"]
        )
        print("─" * 60)
        return {"accepted": choice == "yes"}

    # ── Unknown ───────────────────────────────────────────────────────────────
    print(json.dumps(interrupt_payload, indent=2, default=str))
    print("→ Unknown interrupt — auto-accepting")
    print("─" * 60)
    return {"accepted": True}


def _print_progress(event: dict) -> None:
    indicators = [
        ("express_transcription",  "✓ express_transcribe"),
        ("recipe_count",           "✓ recipe_count_check"),
        ("hitl_suggestions",       "✓ express_suggest"),
        ("express_hitl_metadata",  "✓ express_hitl"),
        ("repaired_transcription", "✓ restorer"),
        ("quality_score",          "✓ quality_evaluator"),
        ("historian_output",       "✓ historian"),
        ("origin_consensus",       "✓ cross_verifier"),
        ("full_hitl_metadata",     "✓ full_hitl"),
        ("pdf_path",               "✓ pdf_generator"),
        ("indexed",                "✓ indexer"),
    ]
    for key, label in indicators:
        if key in event and event[key] is not None:
            print(f"  {label}")
            break


def main():
    parser = argparse.ArgumentParser(description="Culinary Archivist — CLI runner")
    parser.add_argument("--media",  required=True, help="Path to recipe image or PDF")
    parser.add_argument("--mode",   choices=["express", "full"], default="",
                        help="Force express or full path (default: auto-detect)")
    parser.add_argument("--region", default="", help="Optional region hint")
    args = parser.parse_args()

    media_path = args.media
    media_type = "image" if media_path.lower().endswith((".jpg", ".jpeg", ".png")) else "pdf"

    initial_state = {
        "media_path":            media_path,
        "media_type":            media_type,
        "mode":                  args.mode,
        "user_region_hint":      args.region,
        "media_quality_score":   0.0,
        "media_date_detected":   0,
        "llm_calls":             0,
        "tool_calls":            0,
        "total_loop_iterations": 0,
        "hitl_escalations":      0,
        "errors":                [],
    }

    checkpointer = MemorySaver()
    graph = build_graph().compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    print(f"\nRunning archivist on: {media_path}  (mode={args.mode or 'auto'})")
    print("=" * 60)

    # ── First invocation ──────────────────────────────────────────────────────
    for event in graph.stream(initial_state, config=config, stream_mode="values"):
        _print_progress(event)

    # ── Interrupt loop ────────────────────────────────────────────────────────
    while True:
        state = graph.get_state(config)
        if not state.next:
            break

        interrupts = []
        for task in state.tasks:
            interrupts.extend(task.interrupts)
        if not interrupts:
            break

        resume_value = _auto_resume(interrupts[0].value)

        for event in graph.stream(
            Command(resume=resume_value), config=config, stream_mode="values"
        ):
            _print_progress(event)

    # ── Final result ──────────────────────────────────────────────────────────
    final = graph.get_state(config).values
    print("\n" + "=" * 60)
    print("DONE — final state:")
    for key in ("mode", "pdf_path", "indexed", "duplicate_flag",
                "recipe_count", "llm_calls", "errors"):
        print(f"  {key}: {final.get(key)}")


if __name__ == "__main__":
    main()
