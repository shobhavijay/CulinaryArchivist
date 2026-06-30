"""
Restorer node — full path OCR entry point.

Reuses the same OCR logic as express_transcribe (PyMuPDF + Tesseract or Ollama vision)
but writes results into the full-path state fields:
  raw_transcription      : str  — verbatim OCR output
  repaired_transcription : dict — structured JSON {title, ingredients, steps, source_text}
  transcription_loop_count : int — incremented each pass for loop-guard in gate

This node is called once on first entry, then again if quality_evaluator signals
a low-confidence result (up to FULL_PATH_MAX_RESTORER_LOOPS times).
"""
import logging

from culinary_archivist import config
from culinary_archivist.state import ArchivistState

# Reuse all OCR/structuring helpers from express_transcribe — no duplication
from culinary_archivist.agents.express_transcribe import (
    _extract_text_pymupdf,
    _structure_with_qwen,
    _transcribe_ollama,
)

log = logging.getLogger(__name__)


def restorer_node(state: ArchivistState) -> dict:
    media_path  = state["media_path"]
    media_type  = state.get("media_type", "image")
    loop_count  = state.get("transcription_loop_count", 0)
    reflections = state.get("transcription_reflections") or []

    log.info(
        "Restorer [loop %d]: %s  reflexion_lessons=%d",
        loop_count + 1, media_path, len(reflections),
    )

    # ── OCR ───────────────────────────────────────────────────────────────────
    # Reflexion memory (if any) is forwarded into both OCR paths so verbal
    # lessons from prior failed attempts guide this attempt's prompts.
    use_pymupdf = config.USE_PYMUPDF and media_type == "pdf"

    if use_pymupdf:
        raw_text = _extract_text_pymupdf(media_path)
        log.info("Restorer PyMuPDF raw text (%d chars):\n%s", len(raw_text), raw_text[:500])
        if not raw_text:
            log.warning("Restorer: PyMuPDF extracted no text")
            structured = {"title": None, "ingredients": [], "steps": [], "source_text": None}
        else:
            structured = _structure_with_qwen(raw_text, reflections=reflections)
    else:
        log.info("Restorer: routing to vision model (%s) for media_type=%s", config.VISION_MODEL, media_type)
        structured = _transcribe_ollama(media_path, media_type, reflections=reflections)
        raw_text = structured.get("source_text") or ""

    log.info(
        "Restorer done: title=%r  ingredients=%d  steps=%d",
        structured.get("title"),
        len(structured.get("ingredients", [])),
        len(structured.get("steps", [])),
    )

    return {
        "raw_transcription":       raw_text,
        "repaired_transcription":  structured,
        "transcription_loop_count": loop_count + 1,
        "llm_calls": state.get("llm_calls", 0) + 1,
    }
