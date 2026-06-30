import logging
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from culinary_archivist import config
from culinary_archivist.state import ArchivistState

log = logging.getLogger(__name__)


class _RecipePDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, "Culinary Archivist", align="R")
        self.ln(6)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


_UNICODE_FRACTIONS_PDF = {
    "\u00bd": "1/2", "\u00bc": "1/4", "\u00be": "3/4",
    "\u2153": "1/3", "\u2154": "2/3",
    "\u215b": "1/8", "\u215c": "3/8", "\u215d": "5/8", "\u215e": "7/8",
    "\u2155": "1/5", "\u2156": "2/5", "\u2157": "3/5", "\u2158": "4/5",
    "\u2159": "1/6", "\u215a": "5/6",
}


def _safe(text) -> str:
    """Convert text to latin-1 safe string for fpdf2, normalising fraction glyphs first."""
    if not text:
        return ""
    s = str(text)
    for glyph, ascii_frac in _UNICODE_FRACTIONS_PDF.items():
        s = s.replace(glyph, ascii_frac)
    return s.encode("latin-1", errors="replace").decode("latin-1")


def _build_basic_pdf(transcription: dict, metadata: dict) -> FPDF:
    pdf = _RecipePDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    title = metadata.get("title") or transcription.get("title") or "Untitled Recipe"

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 10, _safe(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # Metadata line
    parts = []
    if metadata.get("origin"):
        parts.append(metadata["origin"])
    if metadata.get("era"):
        parts.append(metadata["era"])
    if metadata.get("tags"):
        tags = metadata["tags"] if isinstance(metadata["tags"], list) else [metadata["tags"]]
        parts.append(", ".join(tags))
    if parts:
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(0, 6, _safe("  -  ".join(parts)), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    else:
        pdf.ln(4)

    # Divider
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # Ingredients
    ingredients = transcription.get("ingredients") or []
    steps = transcription.get("steps") or []
    source_text = transcription.get("source_text") or ""

    if ingredients:
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, "Ingredients", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for item in ingredients:
            pdf.multi_cell(0, 6, _safe(f"  - {item}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # Steps
    if steps:
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, "Method", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for i, step in enumerate(steps, 1):
            pdf.multi_cell(0, 6, _safe(f"{i}.  {step}"), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    # Fallback: if structured parse failed, show raw transcription text
    if not ingredients and not steps and source_text:
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, "Transcribed Text", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(0, 5, _safe(source_text), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # Notes
    if metadata.get("notes"):
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(0, 6, _safe(f"Notes: {metadata['notes']}"), new_x="LMARGIN", new_y="NEXT")

    # Archived timestamp
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(180, 180, 180)
    pdf.multi_cell(0, 6, _safe(f"Archived: {datetime.now().strftime('%Y-%m-%d %H:%M')}"), new_x="LMARGIN", new_y="NEXT")

    return pdf


def _build_annotated_pdf(historian_output: dict, state: dict) -> FPDF:
    """
    Full-path PDF: recipe content (verbatim) + historian annotations block.
    Uses historian_output for everything — ingredients/steps are already
    preserved verbatim by the historian's normalise step.
    """
    pdf = _RecipePDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    title = historian_output.get("title") or "Untitled Recipe"

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 10, _safe(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # Origin / era / tags line
    parts = []
    origin = historian_output.get("origin") or state.get("origin_consensus")
    if origin:
        parts.append(origin)
    if historian_output.get("sub_region"):
        parts.append(historian_output["sub_region"])
    if historian_output.get("era"):
        parts.append(historian_output["era"])
    tags = historian_output.get("tags") or []
    if tags:
        parts.append(", ".join(tags))
    if parts:
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(0, 6, _safe("  |  ".join(parts)), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    else:
        pdf.ln(4)

    # Divider
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # Ingredients
    ingredients = historian_output.get("ingredients") or []
    steps       = historian_output.get("steps") or []
    source_text = historian_output.get("source_text") or ""

    if ingredients:
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, "Ingredients", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for item in ingredients:
            pdf.multi_cell(0, 6, _safe(f"  - {item}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # Steps
    if steps:
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, "Method", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for i, step in enumerate(steps, 1):
            pdf.multi_cell(0, 6, _safe(f"{i}.  {step}"), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    # Fallback raw text
    if not ingredients and not steps and source_text:
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, "Transcribed Text", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(0, 5, _safe(source_text), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # ── Historian Annotations ─────────────────────────────────────────────
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(60, 60, 100)
    pdf.multi_cell(0, 9, "Archivist Annotations", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(180, 180, 220)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(5)

    def _annotation_block(heading: str, body: str):
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(0, 7, _safe(heading), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(80, 80, 80)
        pdf.multi_cell(0, 6, _safe(body), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    if historian_output.get("provenance"):
        _annotation_block("Provenance", historian_output["provenance"])

    if historian_output.get("technique_notes"):
        _annotation_block("Technique Notes", historian_output["technique_notes"])

    archaic = historian_output.get("archaic_substitutions") or {}
    if archaic:
        lines = "\n".join(f"  {k}: {v}" for k, v in archaic.items())
        _annotation_block("Archaic Terms Found in This Recipe", lines)

    vibe = historian_output.get("vibe_keywords") or []
    if vibe:
        _annotation_block("Character Keywords", "  " + "  |  ".join(vibe))

    # Cross-verifier conflict note
    conflict = state.get("conflict_note")
    if conflict:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(140, 80, 80)
        pdf.multi_cell(0, 6, _safe(f"Origin Note: {conflict}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    if historian_output.get("notes"):
        _annotation_block("Notes", historian_output["notes"])

    # Archived timestamp
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(180, 180, 180)
    pdf.multi_cell(0, 6, _safe(f"Archived: {datetime.now().strftime('%Y-%m-%d %H:%M')}"), new_x="LMARGIN", new_y="NEXT")

    return pdf


def pdf_generator_node(state: ArchivistState) -> dict:
    mode = state.get("mode", "express")

    if mode == "express":
        transcription = state.get("express_transcription") or {}
        metadata      = state.get("express_hitl_metadata") or {}
        title         = metadata.get("title") or transcription.get("title") or "recipe"
        pdf           = _build_basic_pdf(transcription, metadata)
        variant       = "basic"
    else:
        # Full path — historian_output contains verbatim content + enrichment
        historian_output = state.get("historian_output") or {}
        title            = historian_output.get("title") or "recipe"
        pdf              = _build_annotated_pdf(historian_output, dict(state))
        variant          = "annotated"

    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in str(title))[:50]
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename   = f"{safe_title}_{timestamp}.pdf"
    out_path   = config.OUTPUT_DIR / filename

    pdf.output(str(out_path))

    log.info("PDF saved: %s (variant=%s)", out_path, variant)
    return {"pdf_path": str(out_path), "pdf_variant": variant}
