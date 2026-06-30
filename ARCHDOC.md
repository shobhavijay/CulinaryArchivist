# CulinaryArchivist — Architecture Reference

**Project:** CulinaryArchivist v2  
**Framework:** LangGraph (`StateGraph`)  
**Runtime:** Python 3.11+ · Ollama (local) **or** Anthropic API (cloud) · ChromaDB · SQLite · PyMuPDF + Tesseract

---

## Pipeline Overview

CulinaryArchivist accepts a recipe image or PDF and routes it through one of two paths based on media quality, producing a structured PDF archive entry and dual-store database record.

```
                          ┌─────────────────────┐
           INPUT ─────────► pre_flight_router    │
                          └──────────┬──────────┘
                     quality OK      │       quality poor / handwritten
                    ┌────────────────┴────────────────────┐
                    ▼                                     ▼
          [EXPRESS PATH]                          [FULL PATH]
        express_transcribe                         restorer
        recipe_count_check                           │
        multi_recipe_advance  ◄─────┐         quality_evaluator
        express_suggest              │               │
        express_hitl                 │        low ◄──┴── ok
                    │                │         │         │
                    │                │  reflexion_agent  │
                    │                │         │         │
                    │                └────── restorer    │
                    │                                    ▼
                    │                              historian ◄─┐
                    │                                  │       │ retry
                    │                             cross_verifier
                    │                                  │
                    │              unknown region ◄────┴──── known region
                    │                   │                        │
                    │               discovery                    │
                    │                   │                        │
                    │                   └──────► full_hitl ◄─────┘
                    │                                │
                    └──────────────────────┐         │
                                           ▼         ▼
                                       pdf_generator
                                           │
                                        indexer
                                           │
                              more recipes? ──► multi_recipe_advance
                              done?        ──► END
```

---

## Shared State Schema (`ArchivistState`)

All nodes share a `TypedDict` state object. Each node receives the full state and returns a partial update dict.

| Field | Type | Description |
|---|---|---|
| `media_path` | `str` | Path to input image or PDF |
| `media_type` | `str` | `"image"` or `"pdf"` |
| `express_transcription` | `dict` | Structured OCR output from express path |
| `express_low_conf_flag` | `bool` | Express path quality gate result |
| `raw_transcription` | `str` | Verbatim OCR output (full path) |
| `repaired_transcription` | `dict` | Structured JSON from full path OCR |
| `transcription_loop_count` | `int` | Number of restorer retry attempts |
| `transcription_quality_score` | `float` | Quality score from evaluator |
| `low_conf_flag` | `bool` | Whether quality threshold was not met |
| `transcription_reflections` | `list[str]` | Verbal lessons from reflexion_agent (cross-session) |
| `historian_output` | `dict` | Enriched metadata from historian |
| `historian_tool_calls` | `list` | Raw tool call log from historian ReAct loop |
| `historian_loop_count` | `int` | Number of historian retry iterations |
| `enrichment_complete` | `bool` | Historian declared full confidence |
| `enrichment_partial` | `bool` | Historian declared partial confidence |
| `origin_consensus` | `str` | Final agreed origin string from cross_verifier |
| `conflict_note` | `str \| None` | Conflict description when signals diverge |
| `unknown_region` | `bool` | Cross-verifier flagged unknown region |
| `provisional_region_id` | `str` | Draft region ID from discovery subagent |
| `pending_registry_entry` | `dict` | Draft YAML from discovery before approval |
| `discovery_approved` | `bool` | Human approved the draft registry entry |
| `pdf_path` | `str` | Output PDF path |
| `recipe_count` | `int` | Number of recipes on the card (multi-recipe support) |
| `current_recipe_index` | `int` | Index of recipe currently being processed |
| `llm_calls` | `int` | Running total of LLM calls in this run |
| `hitl_escalations` | `int` | Number of human-in-the-loop interrupts triggered |

---

## Agents

### `pre_flight_router`
**File:** `culinary_archivist/router.py`  
**Pattern:** Rule-based routing (no LLM)

Entry point for every run. Inspects `media_quality_score` and `detected_date_age` (computed from image metadata or EXIF) against configured thresholds to route the input to either the express path or the full path.

- `media_quality_score >= AUTO_EXPRESS_QUALITY_THRESHOLD` AND `date_age <= AUTO_EXPRESS_MAX_AGE_YEARS` → `express_transcribe`
- Otherwise → `restorer` (full path)

No LLM calls. Pure heuristic routing.

---

### `express_transcribe`
**File:** `culinary_archivist/agents/express_transcribe.py`  
**Pattern:** Two-step OCR pipeline

Handles recipe transcription on the express path. Supports two backends controlled by `USE_PYMUPDF`:

**PyMuPDF + Tesseract path (default, `USE_PYMUPDF=true`):**  
`_extract_text_pymupdf()` opens the file with PyMuPDF, attempts direct text extraction (works for digital PDFs), and falls back to Tesseract OCR via `get_textpage_ocr()` for scanned or image-based documents. This path is preferred for handwritten recipes: Tesseract transcribes faithfully and marks unreadable regions as blank rather than hallucinating.

**Vision-language model path (`USE_PYMUPDF=false`):**  
Step 1: a vision model reads the image and copies text verbatim, marking unreadable sections as `[unreadable]`. Step 2: a text model structures the raw output into a JSON object with `title`, `ingredients`, `steps`. The specific models depend on `LLM_BACKEND`: when `ollama`, uses `VISION_MODEL` (default `llama3.2-vision:11b`) and `TEXT_MODEL` (default `mistral:7b-instruct`) served locally via Ollama; when `anthropic`, uses `CLAUDE_VISION_MODEL` and `CLAUDE_TEXT_MODEL` (default `claude-opus-4-7`) via the Anthropic API.

**On OCR performance:** In testing, PyMuPDF + Tesseract consistently outperformed vision-language models on handwritten recipe cards. VLMs tend to hallucinate plausible-sounding text on dense cursive handwriting, while Tesseract fails loudly (blank output) rather than silently incorrectly. The express path uses the PyMuPDF backend by default for this reason.

A Unicode fraction normalisation pass (`_normalise_fractions()`) cleans common OCR fraction misreads (e.g. `11/2 → 1 1/2`, `½ → 1/2`) before structuring.

**Outputs:** `express_transcription`, `express_low_conf_flag`

---

### `recipe_count_check` and `multi_recipe_advance`
**File:** `culinary_archivist/agents/recipe_splitter.py`  
**Pattern:** State machine

`recipe_count_check` inspects the transcription for markers indicating multiple recipes on a single card and sets `recipe_count` in state. `multi_recipe_advance` increments `current_recipe_index` and resets per-recipe state fields so the same graph loop can process each recipe independently. After indexing, the graph checks `recipe_count > 1 and current_recipe_index < recipe_count - 1` to decide whether to route back to `multi_recipe_advance` or terminate.

---

### `express_suggest` and `express_hitl`
**File:** `culinary_archivist/agents/express_hitl.py`  
**Pattern:** Zero-LLM heuristic + LangGraph `interrupt()`

`express_suggest` generates tag suggestions from the transcription using heuristic rules (no LLM): ingredient keyword matching against a tag taxonomy, era inference from visible date, dietary flags. Runs in milliseconds.

`express_hitl` calls `interrupt()` with the suggested tags and transcription, pausing graph execution. The Chainlit UI surfaces these to the human. On `Command(resume=user_response)`, the node merges human corrections into state and continues. `MemorySaver` checkpoints state at the interrupt so the graph can survive process restarts between the interrupt and the resume.

---

### `restorer`
**File:** `culinary_archivist/agents/restorer.py`  
**Pattern:** Stateful retry node with Reflexion memory injection

Full-path OCR entry point. Reuses the same `_extract_text_pymupdf()`, `_structure_with_qwen()`, and `_transcribe_ollama()` helpers from `express_transcribe` (no code duplication). The key difference: reads `transcription_reflections` from state and forwards them as `reflections=` to both OCR helpers. These verbal lessons, generated by the reflexion agent after prior failed attempts, are prepended to OCR prompts as additional context.

Increments `transcription_loop_count` on each call. The quality evaluator uses this counter to enforce `FULL_PATH_MAX_RESTORER_LOOPS`.

**Outputs:** `raw_transcription`, `repaired_transcription`, `transcription_loop_count`

---

### `quality_evaluator`
**File:** `culinary_archivist/agents/quality_evaluator.py`  
**Pattern:** Rule-based scoring (no LLM)

Scores the structured transcription on two axes and combines them into `transcription_quality_score`:

1. **Completeness score** — presence and length of `title`, `ingredients`, `steps`, `source_text` against minimum-length thresholds
2. **Registry confidence score** — ingredient token overlap against the regional registry YAMLs

Sets `low_conf_flag = True` if the combined score falls below `FULL_PATH_QUALITY_THRESHOLD=0.5`. No LLM calls. Deterministic.

**Routing:** `low_conf_flag + loops_remaining → reflexion_agent` / `quality_ok → historian`

---

### `reflexion_agent`
**File:** `culinary_archivist/agents/reflexion_agent.py`  
**Academic basis:** Reflexion — Shinn et al., 2023 ([arXiv:2303.11366](https://arxiv.org/abs/2303.11366))

Implements the Reflexion pattern: after a failed transcription attempt, an LLM generates a verbal critique identifying the specific visual failure mode, and that critique is stored in a cross-session persistent memory store. On the next OCR attempt, the stored lessons are injected into the transcription prompts.

**The three Reflexion steps:**

**1. Reflect** — the LLM receives the quality score, raw OCR text, and partial structured output. It must produce a 2–4 sentence improvement note specifying what visual pattern caused the failure and what the next attempt should do differently. Notes are written to be general and transferable (no recipe-specific content) so they remain useful across future sessions.

**2. Persist** — the reflection is appended to `db/reflexion_memory.json` with a timestamp and quality score. This file is not cleared between sessions: lessons accumulate. The file is a simple JSON array bounded by `REFLEXION_MAX_MEMORY_ENTRIES=8` (older entries are retained in the file but not injected).

**3. Load and return** — the most recent N entries (newest-first) are returned in state as `transcription_reflections`. The next `restorer` invocation picks these up and prepends them to its OCR prompts.

**What this achieves:** OCR models running the same prompt on the same image will produce the same output. Reflexion breaks this cycle not by changing the model, but by changing its input — providing specific, accumulated context about what tends to go wrong with challenging handwritten documents. This is the "verbal reinforcement learning" framing from Shinn et al.: the verbal memory acts as a semantic gradient signal without any weight updates.

**Outputs:** `transcription_reflections`

---

### `historian`
**File:** `culinary_archivist/agents/historian.py`  
**Academic basis:** ReAct — Yao et al., 2022 ([arXiv:2210.03629](https://arxiv.org/abs/2210.03629))

The historian is the cultural enrichment agent. Given a structured recipe transcription, it determines geographic origin, historical era, regional cuisine classification, and produces a substitution map for archaic, dialect-specific, or OCR-garbled ingredient terms.

**ReAct implementation:**

The historian runs a loop of up to `HISTORIAN_MAX_REACT_STEPS=4` tool-calling steps before synthesising. At each step the LLM:

- **Reasons** about what evidence it has and what is still uncertain
- **Acts** by selecting a tool: `wikipedia_search(query)`, `web_search(query)`, or `registry_lookup(ingredient_term)`
- **Observes** the tool result and updates its working hypothesis

Available tools are injected into the synthesis prompt as a tool schema. The LLM outputs a structured action (tool name + arguments), the historian node executes it, appends the result to the observation history, and loops. After the step budget is exhausted (or the LLM signals synthesis is ready), a final synthesis call produces the `historian_output` dict.

**Archaic and OCR substitution:**

The historian's synthesis prompt instructs it to produce exactly ONE canonical substitution per garbled or archaic term — no alternatives, no bracketed annotations. The `_normalise_historian()` function applies these substitutions back into the actual `ingredients` and `steps` text arrays using `_apply_substitutions()`, a longest-match regex replacement. Original verbatim text is preserved in `ingredients_raw` and `steps_raw` for audit. This means the generated PDF shows corrected, readable text in the recipe card while the raw OCR output remains accessible.

**Historian output schema:**
```json
{
  "title":                  "string",
  "origin":                 "string  (e.g. 'Tamil Nadu, South India')",
  "region_id":              "string  (e.g. 'SOUTH_INDIAN')",
  "era":                    "string  (e.g. 'mid-20th century')",
  "tags":                   ["array", "of", "strings"],
  "technique_notes":        "string",
  "archaic_substitutions":  {"old_term": "canonical_term"},
  "ingredients":            ["corrected ingredient strings"],
  "ingredients_raw":        ["verbatim OCR ingredient strings"],
  "steps":                  ["corrected step strings"],
  "steps_raw":              ["verbatim OCR step strings"]
}
```

**Routing:** After synthesis, `route_after_historian` checks `enrichment_complete` and `enrichment_partial` flags. If neither is set and loops remain, the historian is called again. Otherwise routes to `cross_verifier`.

---

### `cross_verifier`
**File:** `culinary_archivist/agents/cross_verifier.py`  
**Backup:** `culinary_archivist/agents/cross_verifier_original.py` (rule-based fallback)  
**Academic basis:** Self-Refine — Madaan et al., 2023 ([arXiv:2303.17651](https://arxiv.org/abs/2303.17651))

The cross-verifier arbitrates between two region-attribution signals: the historian's inferred origin and a registry confidence score computed by matching recipe ingredients against regional cuisine YAML databases. When these signals agree, the result is straightforward. When they diverge within the hybrid threshold margin, the cross-verifier runs a Self-Refine loop to arrive at a defensible attribution.

**Self-Refine implementation:**

The loop iterates between two LLM calls:

1. **FEEDBACK call** (`_FEEDBACK_PROMPT`) — given both signals, the score margin, ingredient density evidence, and full recipe context, the LLM critiques the current attribution. It identifies which evidence is strongest, where the signals are misleading, and whether the current guess is defensible.

2. **REFINE call** (`_REFINE_PROMPT`) — the LLM produces an updated structured attribution:
   ```json
   {
     "final_region_id": "SOUTH_INDIAN",
     "final_origin": "Tamil Nadu, South India",
     "confidence": 0.82,
     "reasoning": "string",
     "continue_refining": false
   }
   ```
   If `continue_refining=true` and the step budget allows (`CROSS_VERIFIER_MAX_REFINE_STEPS=3`), the loop repeats with the new attribution as the starting point for the next feedback call.

**Fallback:** If all LLM calls fail (model unavailable, JSON parse errors, etc.), `_rule_based_fallback()` applies the original threshold-based arbitration logic from `cross_verifier_original.py`, ensuring the pipeline never blocks.

**Outputs:** `origin_consensus`, `conflict_note`, `unknown_region`

---

### `discovery`
**File:** `culinary_archivist/agents/discovery.py`  
**Pattern:** LLM synthesis + `interrupt()` for registry expansion

Triggered when `unknown_region=True` AND `DISCOVERY_ENABLED=true`. Reuses evidence already collected by the historian (no repeat searches) to draft a new regional cuisine registry entry in YAML format.

Steps:
1. Derive a candidate `region_id` slug from the historian's origin string
2. One LLM call fills in the gaps: `signature_spices`, `landmark_dishes`, `flavor_profile`
3. `interrupt()` surfaces the draft YAML entry to the human for approval
4. On approve: write the YAML file to `REGISTRY_DIR/` and invalidate the registry cache
5. On skip/reject: discard silently, continue to `full_hitl`

This enables the pipeline to self-extend its regional knowledge base with human oversight.

---

### `full_hitl`
**File:** `culinary_archivist/agents/full_hitl.py`  
**Pattern:** LangGraph `interrupt()`

Surfaces the historian's full enrichment output (title, origin, era, tags, technique notes) to the human for review and optional correction. Uses `interrupt()` / `Command(resume=...)` with `MemorySaver` checkpointing, identical in mechanism to `express_hitl`.

On resume, merges human corrections into `historian_output` so the PDF generator receives the final agreed values.

---

### `pdf_generator`
**File:** `culinary_archivist/agents/pdf_generator.py`  
**Pattern:** Deterministic rendering (no LLM)

Generates the archive PDF. Two modes:

**Express PDF** — clean layout with title, ingredients, and steps from the transcription. Tags and suggested metadata in a footer panel.

**Full/Annotated PDF** — full recipe card layout plus an annotations section: cultural origin, era, technique notes, archaic substitution map (showing original → canonical mappings), and historian tool call summary. The `ingredients` and `steps` fields used for the main recipe card are the corrected values from `_normalise_historian()` — substitutions are applied into the visible recipe content. The raw OCR text is preserved in the annotations section.

Uses PyMuPDF (`fitz`) for PDF generation.

**Outputs:** `pdf_path`

---

### `indexer`
**File:** `culinary_archivist/agents/indexer.py`  
**Pattern:** Dual-store persistence with lexical-guarded duplicate detection

Persists the archived recipe to two stores:

**SQLite** (`db/recipes.db`) — structured metadata row: title, origin, era, tags, pdf path, quality score, llm call count, timestamp.

**ChromaDB** (`chroma/`) — vector embedding (via `nomic-embed-text` through Ollama) for semantic duplicate detection.

**Duplicate detection flow:**

Before inserting, `_find_duplicate()` queries ChromaDB for the nearest existing recipe by cosine distance. If the distance falls below `DUPLICATE_SIMILARITY_THRESHOLD=0.40`, a lexical guard is applied:

- Title token overlap (Jaccard on cleaned, stopword-filtered tokens) must exceed `DUPLICATE_TITLE_OVERLAP_MIN=0.30`, OR
- Ingredient token overlap must exceed `DUPLICATE_INGREDIENT_JACCARD_MIN=0.25`

If the vector distance passes but both lexical guards fail, the match is logged as a false positive and discarded. This two-stage approach was introduced after observing that `nomic-embed-text` produces a narrow similarity band for short culinary documents — unrelated dishes like "Tomato Chutney Fry" and "Endive Boats with Smoked Salmon" scored 73–77% similar purely due to generic cooking vocabulary dominating the embedding.

---

## LangGraph Patterns Reference

| Pattern | Where used | LangGraph mechanism |
|---|---|---|
| Conditional routing | pre_flight_router, quality_evaluator, historian, cross_verifier, indexer | `add_conditional_edges()` |
| Human-in-the-loop | express_hitl, full_hitl, discovery | `interrupt()` + `Command(resume=...)` + `MemorySaver` |
| Retry loop | restorer/reflexion_agent, historian | Graph cycle via conditional edge returning same node name |
| Multi-recipe cycle | indexer → multi_recipe_advance | Conditional edge looping back into express path |
| Shared typed state | All nodes | `StateGraph(ArchivistState)` with `TypedDict` |

---

## Configuration Reference (key knobs)

| Variable | Default | Description |
|---|---|---|
| `LLM_BACKEND` | `ollama` | `ollama` = local Ollama server; `anthropic` = Anthropic cloud API |
| `VISION_MODEL` | `llama3.2-vision:11b` | Ollama vision model (used when `LLM_BACKEND=ollama`) |
| `TEXT_MODEL` | `mistral:7b-instruct` | Ollama text model (used when `LLM_BACKEND=ollama`) |
| `CLAUDE_VISION_MODEL` | `claude-opus-4-7` | Anthropic vision model (used when `LLM_BACKEND=anthropic`) |
| `CLAUDE_TEXT_MODEL` | `claude-opus-4-7` | Anthropic text model (used when `LLM_BACKEND=anthropic`) |
| `USE_PYMUPDF` | `true` | Route PDFs/images through PyMuPDF + Tesseract instead of vision LLM |
| `FULL_PATH_QUALITY_THRESHOLD` | `0.5` | Below this → Reflexion loop |
| `FULL_PATH_MAX_RESTORER_LOOPS` | `3` | Max OCR retry attempts |
| `HISTORIAN_MAX_REACT_STEPS` | `4` | Max tool calls per historian invocation |
| `FULL_PATH_MAX_HISTORIAN_LOOPS` | `2` | Max historian retry iterations |
| `CROSS_VERIFIER_MAX_REFINE_STEPS` | `3` | Max Self-Refine iterations in cross_verifier |
| `REFLEXION_MAX_MEMORY_ENTRIES` | `8` | Number of past reflections injected per prompt |
| `DUPLICATE_SIMILARITY_THRESHOLD` | `0.40` | ChromaDB cosine distance threshold for duplicate check |
| `DUPLICATE_TITLE_OVERLAP_MIN` | `0.30` | Minimum title Jaccard similarity for confirmed duplicate |
| `DUPLICATE_INGREDIENT_JACCARD_MIN` | `0.25` | Minimum ingredient Jaccard similarity for confirmed duplicate |
| `DISCOVERY_ENABLED` | `false` | Enable auto-drafting of new regional registry entries |

---

## Academic References

| Paper | Authors | ArXiv | Implementation |
|---|---|---|---|
| ReAct: Synergizing Reasoning and Acting in Language Models | Yao et al. | [2210.03629](https://arxiv.org/abs/2210.03629) | `historian.py` — tool-calling loop |
| Self-Refine: Iterative Refinement with Self-Feedback | Madaan et al. | [2303.17651](https://arxiv.org/abs/2303.17651) | `cross_verifier.py` — FEEDBACK→REFINE loop |
| Reflexion: Language Agents with Verbal Reinforcement Learning | Shinn et al. | [2303.11366](https://arxiv.org/abs/2303.11366) | `reflexion_agent.py` — verbal memory + injection |
