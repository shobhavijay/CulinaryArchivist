"""
Reflexion agent node — full path, between quality_evaluator and restorer.

Implements Reflexion (Shinn et al., 2023 — arXiv:2303.11366):

  After each failed transcription attempt (low quality score), this node:
    1. REFLECT  — the LLM generates a verbal critique of why the current
                  transcription fell short and what patterns caused the failure
                  (dense handwriting, mixed scripts, columnar layout, etc.).
    2. PERSIST  — the reflection is appended to a cross-session JSON store
                  (config.REFLEXION_MEMORY_PATH, default: db/reflexion_memory.json)
                  so lessons accumulate and carry forward to future archiving runs.
    3. LOAD     — the most recent N reflections (config.REFLEXION_MAX_MEMORY_ENTRIES)
                  are returned in state as `transcription_reflections`, which the
                  restorer node then injects into the vision and structuring prompts
                  as additional context for the next attempt.

The verbal memory acts as a "semantic gradient signal" (Shinn et al.) guiding
the next transcription pass without any weight updates or additional training.
Reflections are deliberately written as general, transferable lessons rather than
recipe-specific corrections, so they remain useful across different recipe images
in future sessions.

State consumed:
  repaired_transcription       : dict   (the failed structured attempt)
  raw_transcription            : str    (verbatim vision-model output)
  transcription_quality_score  : float
  transcription_loop_count     : int

State written:
  transcription_reflections    : list[str]  (recent lessons, newest-first)
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from culinary_archivist import config, llm_client
from culinary_archivist.state import ArchivistState

log = logging.getLogger(__name__)


# ── Reflection prompt ─────────────────────────────────────────────────────────

_REFLECT_PROMPT = """\
You are reviewing a failed recipe transcription to produce an improvement note
for the next OCR attempt.

Quality score achieved : {quality_score:.2f}  (minimum required: {threshold:.2f})
Loop attempt number    : {loop_count}

What the structured extraction produced:
  title        : {title}
  ingredients  : {num_ingredients} line(s) extracted
  steps        : {num_steps} step(s) extracted
  source_text  : {has_source}

Raw text the vision model read from the image (first 800 chars):
---
{raw_text}
---

Incomplete structured output:
  ingredients (first 5): {ingredients_preview}
  steps       (first 3): {steps_preview}

Write a concise improvement note (2–4 sentences) for the NEXT transcription attempt.
Your note must:
  1. Identify the specific visual pattern that caused the failure
     (e.g. "dense cursive handwriting with overlapping ascenders",
      "two-column layout misread as continuous prose",
      "quantities written as fractions above ingredient names").
  2. State what the transcription model should do differently next time
     (e.g. "treat each line break as a new ingredient boundary",
      "look for repeated dash or bullet characters as list markers").
  3. Be general enough to apply to similar recipe images in future sessions —
     do NOT repeat ingredient names, dish names, or any recipe-specific content.
"""


# ── Persistent memory helpers ─────────────────────────────────────────────────

def _load_memory() -> list[dict]:
    """Load all persisted reflections from disk. Returns [] if file absent."""
    path = config.REFLEXION_MEMORY_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("reflections", [])
    except Exception as exc:
        log.warning("Reflexion: could not read memory file %s — %s", path, exc)
        return []


def _save_memory(reflections: list[dict]) -> None:
    """Write the full reflections list back to disk (creates dirs if needed)."""
    path = config.REFLEXION_MEMORY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reflections": reflections}, indent=2, ensure_ascii=False))
    except Exception as exc:
        log.warning("Reflexion: could not write memory file %s — %s", path, exc)


def _append_reflection(reflection_text: str, quality_score: float) -> list[dict]:
    """Append a new entry to the persistent store and return the updated list."""
    entries = _load_memory()
    entries.append({
        "timestamp":     datetime.now().isoformat(),
        "quality_score": round(quality_score, 4),
        "reflection":    reflection_text,
    })
    _save_memory(entries)
    return entries


def _recent_texts(entries: list[dict]) -> list[str]:
    """
    Return the text of the most recent N entries (newest-first) for injection
    into the next transcription prompt.
    """
    n = config.REFLEXION_MAX_MEMORY_ENTRIES
    recent = entries[-n:][::-1]          # last N, reversed so newest is first
    return [e["reflection"] for e in recent]


# ── Node ──────────────────────────────────────────────────────────────────────

def reflexion_agent_node(state: ArchivistState) -> dict:
    transcription = state.get("repaired_transcription") or {}
    raw_text      = (state.get("raw_transcription") or "").strip()
    quality_score = state.get("transcription_quality_score", 0.0)
    loop_count    = state.get("transcription_loop_count", 1)

    ingredients = transcription.get("ingredients") or []
    steps       = transcription.get("steps")       or []

    log.info(
        "Reflexion agent [after loop %d]: quality_score=%.2f  "
        "ingredients=%d  steps=%d",
        loop_count, quality_score, len(ingredients), len(steps),
    )

    # ── Generate verbal reflection ─────────────────────────────────────────────
    prompt = _REFLECT_PROMPT.format(
        quality_score     = quality_score,
        threshold         = config.FULL_PATH_QUALITY_THRESHOLD,
        loop_count        = loop_count,
        title             = transcription.get("title") or "(not found)",
        num_ingredients   = len(ingredients),
        num_steps         = len(steps),
        has_source        = "yes" if transcription.get("source_text") else "no",
        raw_text          = raw_text[:800] or "(none captured)",
        ingredients_preview = str(ingredients[:5]) if ingredients else "[]",
        steps_preview     = str(steps[:3]) if steps else "[]",
    )

    reflection_text: str | None = None
    try:
        reflection_text = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        reflection_text = (reflection_text or "").strip()
        log.info("Reflexion: generated reflection (%d chars): %s",
                 len(reflection_text), reflection_text[:150])
    except Exception as exc:
        log.warning("Reflexion: LLM call failed — %s. Skipping new reflection.", exc)

    # ── Persist and load back ──────────────────────────────────────────────────
    if reflection_text:
        all_entries = _append_reflection(reflection_text, quality_score)
        log.info(
            "Reflexion: memory now has %d total entries (file: %s)",
            len(all_entries), config.REFLEXION_MEMORY_PATH,
        )
    else:
        all_entries = _load_memory()

    recent = _recent_texts(all_entries)
    log.info(
        "Reflexion: injecting %d recent reflection(s) into next transcription pass",
        len(recent),
    )

    return {
        "transcription_reflections": recent,
        "llm_calls": state.get("llm_calls", 0) + 1,
    }
