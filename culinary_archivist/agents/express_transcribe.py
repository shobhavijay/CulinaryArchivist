import base64
import json
import logging
import re
from pathlib import Path

from culinary_archivist import config, llm_client
from culinary_archivist.state import ArchivistState

log = logging.getLogger(__name__)

# ── Fraction normalisation ────────────────────────────────────────────────────

# Map Unicode fraction glyphs → ASCII equivalents
_UNICODE_FRACTIONS = {
    "\u00bd": "1/2",   # ½
    "\u00bc": "1/4",   # ¼
    "\u00be": "3/4",   # ¾
    "\u2153": "1/3",   # ⅓
    "\u2154": "2/3",   # ⅔
    "\u215b": "1/8",   # ⅛
    "\u215c": "3/8",   # ⅜
    "\u215d": "5/8",   # ⅝
    "\u215e": "7/8",   # ⅞
    "\u2155": "1/5",   # ⅕
    "\u2156": "2/5",   # ⅖
    "\u2157": "3/5",   # ⅗
    "\u2158": "4/5",   # ⅘
    "\u2159": "1/6",   # ⅙
    "\u215a": "5/6",   # ⅚
}


def _normalise_fractions(text: str) -> str:
    """
    Fix common OCR fraction misreads in raw OCR output before sending to qwen.

    Handles:
    1. Unicode fraction glyphs   → ASCII  (½ → 1/2)
    2. Stacked fraction artefacts → joined (11/2 → 1 1/2, 21/4 → 2 1/4)
    3. Digit glued to slash-frac  → spaced (2½ already mapped; 21/2 → 2 1/2)
    4. Phantom percent signs      → likely ½  (e.g. "1 %" in ingredient line)
    """
    # 1. Unicode fraction glyphs
    for glyph, ascii_frac in _UNICODE_FRACTIONS.items():
        text = text.replace(glyph, ascii_frac)

    # 2. Stacked fraction artefact: digit immediately followed by 1-digit/1-digit
    #    e.g. "11/2" → "1 1/2",  "21/4" → "2 1 /4", "31/3" → "3 1/3"
    #    Pattern: single digit, then single digit slash single digit (no space)
    #    Only trigger when the whole token looks like NM/D (len 3-4 chars)
    text = re.sub(
        r'(\d)(1/2|1/4|3/4|1/3|2/3|1/8|3/8|5/8|7/8|1/5|2/5|3/5|4/5|1/6|5/6)',
        r'\1 \2',
        text,
    )

    # 3. Glued whole+fraction with no space: e.g. "21/2" → "2 1/2"
    #    Match: digit(s) then digit/digit where numerator < denominator
    text = re.sub(
        r'(\d+)(\d)(/)(1?[0-9])\b',
        lambda m: f"{m.group(1)} {m.group(2)}{m.group(3)}{m.group(4)}"
        if int(m.group(2)) < int(m.group(4)) else m.group(0),
        text,
    )

    # 4. "1 %" pattern in ingredient context → "1 1/2"
    #    Only replace when it looks like a measurement (digit space %)
    text = re.sub(r'(\b\d+)\s+%', r'\1 1/2', text)

    return text

# Step 1: vision model reads the image verbatim, marks unreadable parts
_VISION_PROMPT = """Read every single word of text in this recipe image and copy it out exactly as written.
Include the recipe title, every ingredient with its quantity, and every instruction step in full.
If any word or number is blurry or impossible to read, write [unreadable] in its place.
Do not skip anything. Do not summarise. Copy the full text."""

# Prefix prepended to _VISION_PROMPT when Reflexion memory is available.
# The lessons come from reflexion_agent after prior failed attempts.
_VISION_REFLECTION_PREFIX = """\
[LESSONS FROM PRIOR TRANSCRIPTION ATTEMPTS — apply these carefully]
{lessons}

"""

# Step 2: text model structures the raw text into JSON
_STRUCTURE_PROMPT = """You will be given raw recipe text. Organise it into a JSON object.

Rules:
1. Copy every word exactly as written — do not paraphrase, summarise, or rewrite.
2. Where you see [unreadable], substitute the most likely culinary word and mark it with * (e.g. "2 tablespoons* butter").
3. Preserve all quantities, times, and temperatures exactly.
4. Only split into sections — do not change any words.
5. If no title is present in the text, infer a short descriptive title (5 words or fewer) from the ingredients. Set "title_suggested": true in that case.

Return ONLY a JSON object with exactly these four keys — no explanation, no markdown:
  "title"          : recipe name string (inferred if not found)
  "title_suggested": true if you inferred the title, false if it was in the text
  "ingredients"    : array of strings, one per ingredient line
  "steps"          : array of strings, one per instruction step

Do NOT include source_text — it is handled separately.

--- EXAMPLE INPUT (title present) ---
Dal Makhani
250g black lentils, 50g butter, 1 cup tomato puree, 1 tsp garam masala, salt to taste
Soak lentils overnight. Boil until soft. Fry tomato puree in butter. Add lentils and simmer 30 min. Season with garam masala and salt.

--- EXAMPLE OUTPUT ---
{"title":"Dal Makhani","title_suggested":false,"ingredients":["250g black lentils","50g butter","1 cup tomato puree","1 tsp garam masala","salt to taste"],"steps":["Soak lentils overnight.","Boil until soft.","Fry tomato puree in butter.","Add lentils and simmer 30 min.","Season with garam masala and salt."]}

--- NOW ORGANISE THIS RECIPE ---
"""


def _encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def _parse_json(raw: str) -> dict:
    import ast
    import re

    text = raw.strip()

    # 1. Strip markdown code fences (```json ... ``` or ``` ... ```)
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if match:
            text = match.group(1).strip()

    # 2. Extract the first { ... } block if model added preamble text
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        text = brace_match.group(0)

    # 3. Fix invalid JSON escape sequences from OCR artifacts (e.g. \$ \Y \¥)
    #    Valid JSON escapes: \" \\ \/ \b \f \n \r \t \uXXXX — everything else is invalid
    text = re.sub(r'\\([^"\\/bfnrtu])', lambda m: m.group(1), text)

    # 3. Standard JSON parse
    try:
        result = json.loads(text)
        if isinstance(result, list) and result:
            result = result[0]
        if isinstance(result, dict):
            log.info("Parsed transcription as standard JSON")
            return _normalise(result)
    except json.JSONDecodeError:
        pass

    # 4. Python dict syntax (single quotes) — common with smaller models
    try:
        result = ast.literal_eval(text)
        if isinstance(result, list) and result:
            result = result[0]
        if isinstance(result, dict):
            log.warning("Parsed transcription as Python dict (single quotes)")
            return _normalise(result)
    except Exception:
        pass

    # 5. Truncated response — try completing the JSON with common closings
    for closing in ["}", "}]", "\"]}", "\"]}}"]:
        try:
            result = json.loads(text + closing)
            if isinstance(result, list) and result:
                result = result[0]
            if isinstance(result, dict):
                log.warning("Parsed truncated JSON with closing: %r", closing)
                return _normalise(result)
        except Exception:
            pass
        try:
            result = ast.literal_eval(text + closing)
            if isinstance(result, list) and result:
                result = result[0]
            if isinstance(result, dict):
                log.warning("Parsed truncated Python dict with closing: %r", closing)
                return _normalise(result)
        except Exception:
            pass

    log.warning("All JSON parse attempts failed — storing raw text only")
    return {"title": None, "ingredients": [], "steps": [], "source_text": raw.strip()}


def _normalise(d: dict) -> dict:
    """Ensure all expected keys exist with correct types."""
    return {
        "title":           d.get("title") or None,
        "title_suggested": bool(d.get("title_suggested", False)),
        "ingredients":     d.get("ingredients") if isinstance(d.get("ingredients"), list) else [],
        "steps":           d.get("steps") if isinstance(d.get("steps"), list) else [],
        "source_text":     d.get("source_text") or None,
    }


# ── PyMuPDF path (default) ─────────────────────────────────────────────────────

def _extract_text_pymupdf(media_path: str) -> str:
    """
    Extract text from JPEG or PDF using PyMuPDF + Tesseract OCR.
    For text-based PDFs, text is extracted directly (no OCR needed).
    For images and scanned PDFs, image is converted to PDF first,
    then Tesseract OCR is applied (get_textpage_ocr requires a PDF page).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(media_path)
    page = doc[0]

    # Try direct text extraction first (works for text-based PDFs)
    direct_text = page.get_text().strip()
    if direct_text:
        log.info("PyMuPDF: direct text extraction succeeded (%d chars)", len(direct_text))
        doc.close()
        return direct_text

    # get_textpage_ocr requires a PDF page — convert image to PDF first
    log.info("PyMuPDF: converting image to PDF for Tesseract OCR...")
    pdf_bytes = doc.convert_to_pdf()
    doc.close()

    pdf_doc = fitz.open("pdf", pdf_bytes)
    pdf_page = pdf_doc[0]
    tp = pdf_page.get_textpage_ocr(flags=0, language="eng", tessdata=config.TESSDATA_PREFIX)
    ocr_text = pdf_page.get_text(textpage=tp).strip()
    pdf_doc.close()

    log.info("PyMuPDF OCR: extracted %d chars", len(ocr_text))
    return ocr_text


def _build_reflection_block(reflections: list[str] | None) -> str:
    """Format Reflexion memory entries into an inline lessons block."""
    if not reflections:
        return ""
    lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(reflections))
    return _VISION_REFLECTION_PREFIX.format(lessons=lines)


def _structure_with_qwen(raw_text: str, reflections: list[str] | None = None) -> dict:
    """Pass raw OCR text to text LLM for JSON structuring.

    If Reflexion memory entries are supplied they are prepended to the prompt
    as guidance for this retry pass (Shinn et al., 2023 — arXiv:2303.11366).
    """
    raw_text = _normalise_fractions(raw_text)
    reflection_block = _build_reflection_block(reflections)
    if reflection_block:
        log.info("Structuring with %d Reflexion lesson(s) injected", len(reflections))
    log.info("LLM structuring raw text into JSON...")
    structured = llm_client.chat(
        messages=[{
            "role": "user",
            "content": reflection_block + _STRUCTURE_PROMPT + raw_text,
        }],
        max_tokens=2048,
        temperature=0,
    )
    log.info("LLM structured output:\n%s", structured)
    parsed = _parse_json(structured)
    if not parsed.get("source_text"):
        parsed["source_text"] = raw_text
    return parsed


def _transcribe_pymupdf(media_path: str) -> dict:
    """Full PyMuPDF path: OCR → qwen structuring."""
    raw_text = _extract_text_pymupdf(media_path)
    log.info("PyMuPDF raw text:\n%s", raw_text)
    if not raw_text:
        log.warning("PyMuPDF extracted no text — returning empty transcription")
        return {"title": None, "ingredients": [], "steps": [], "source_text": None}
    return _structure_with_qwen(raw_text)


# ── Ollama vision path (USE_PYMUPDF=false) ─────────────────────────────────────

def _transcribe_ollama(
    media_path: str,
    media_type: str,
    reflections: list[str] | None = None,
) -> dict:
    """Two-step vision transcription with optional Reflexion memory injection.

    `reflections` — list of verbal lessons from prior failed attempts
    (Shinn et al., 2023 — arXiv:2303.11366).  When supplied they are prepended
    to both the vision prompt (Step 1) and the structuring prompt (Step 2).
    """
    reflection_block = _build_reflection_block(reflections)

    if media_type == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(media_path)
        page_text = reader.pages[0].extract_text() or ""
        if page_text.strip():
            structured = llm_client.chat(
                messages=[{"role": "user",
                           "content": reflection_block + _STRUCTURE_PROMPT + page_text}],
                max_tokens=2048,
                temperature=0,
            )
            return _parse_json(structured)
        log.warning("Image-based PDF: cannot render without poppler — transcription will be empty")
        return {"title": None, "ingredients": [], "steps": [], "source_text": None}

    # Image file — two-step: vision read → text structuring
    img_b64 = _encode_image(media_path)

    # Step 1: vision model reads the image text verbatim
    # Reflexion lessons are prepended so the vision model knows what to watch for
    vision_prompt = reflection_block + _VISION_PROMPT
    log.info(
        "Step 1: vision model (%s) reading image text%s...",
        config.VISION_MODEL if config.LLM_BACKEND == "ollama" else config.CLAUDE_VISION_MODEL,
        f" + {len(reflections)} Reflexion lesson(s)" if reflections else "",
    )
    raw_text = _normalise_fractions(
        llm_client.vision_chat(
            prompt=vision_prompt,
            image_b64=img_b64,
            max_tokens=1024,
            temperature=0,
        ).strip()
    )
    log.info("Step 1 raw text (fraction-normalised):\n%s", raw_text)

    # Step 2: text model structures the raw text into JSON
    log.info("Step 2: text model structuring into JSON...")
    parsed = _structure_with_qwen(raw_text, reflections=reflections)

    if not parsed.get("source_text"):
        parsed["source_text"] = raw_text

    return parsed


def express_transcribe_node(state: ArchivistState) -> dict:
    media_path = state["media_path"]
    media_type = state.get("media_type", "image")

    # PDFs → PyMuPDF (USE_PYMUPDF controls this)
    # Images → vision model always (USE_PYMUPDF has no effect on image files)
    use_pymupdf = config.USE_PYMUPDF and media_type == "pdf"

    if use_pymupdf:
        log.info("Express transcribe [PyMuPDF]: %s", media_path)
        transcription = _transcribe_pymupdf(media_path)
    else:
        log.info("Express transcribe [%s]: %s", config.VISION_MODEL, media_path)
        transcription = _transcribe_ollama(media_path, media_type)

    null_count = sum(
        1 for v in transcription.values()
        if v is None or v == [] or v == ""
    )
    low_conf = null_count >= 2

    log.info("Express transcribe done: title=%r low_conf=%s", transcription.get("title"), low_conf)

    return {
        "express_transcription": transcription,
        "express_low_conf_flag": low_conf,
        "llm_calls": state.get("llm_calls", 0) + 1,
    }
