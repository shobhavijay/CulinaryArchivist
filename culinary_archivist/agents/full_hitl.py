"""
Full-path HITL node — runs after cross_verifier, before pdf_generator.

Shows the human what the Historian and Cross-Verifier determined:
  title, origin, era, tags, technique notes

Asks: confirm or correct any field.

On resume, merges human corrections into historian_output so the
PDF generator uses the final agreed values.
"""
import logging
from langgraph.types import interrupt
from culinary_archivist.state import ArchivistState

log = logging.getLogger(__name__)


def full_hitl_node(state: ArchivistState) -> dict:
    historian  = state.get("historian_output") or {}
    consensus  = state.get("origin_consensus") or historian.get("origin") or ""
    conflict   = state.get("conflict_note")

    # What we'll show the human
    suggestions = {
        "title":          historian.get("title") or "",
        "origin":         consensus,
        "era":            historian.get("era") or "",
        "tags":           historian.get("tags") or [],
        "technique_notes": historian.get("technique_notes") or "",
        "notes":          historian.get("notes") or "",
        "conflict_note":  conflict or "",
    }

    log.info(
        "Full HITL: pausing — title=%r  origin=%r  era=%r",
        suggestions["title"], suggestions["origin"], suggestions["era"],
    )

    form_data = interrupt({
        "type":        "full_hitl_form",
        "suggestions": suggestions,
    })

    if not isinstance(form_data, dict):
        form_data = {}

    accepted = form_data.get("accepted", False)

    if accepted:
        corrections = {}
    else:
        corrections = {k: v for k, v in form_data.items()
                       if k != "accepted" and v}

    # Merge corrections into historian_output
    updated = dict(historian)
    if corrections.get("title"):
        updated["title"]  = corrections["title"]
    if corrections.get("origin"):
        updated["origin"] = corrections["origin"]
    if corrections.get("era"):
        updated["era"]    = corrections["era"]
    if corrections.get("tags"):
        updated["tags"]   = corrections["tags"]
    if corrections.get("notes"):
        updated["notes"]  = corrections["notes"]

    log.info("Full HITL: final — title=%r  origin=%r", updated.get("title"), updated.get("origin"))

    return {
        "historian_output":  updated,
        "full_hitl_metadata": corrections,
        "hitl_escalations":  state.get("hitl_escalations", 0) + 1,
    }
