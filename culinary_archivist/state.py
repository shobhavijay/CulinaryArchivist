from typing import TypedDict


class ArchivistState(TypedDict, total=False):
    # ── Input ──────────────────────────────────────────────────────────
    media_path: str
    media_type: str               # "image" | "pdf"
    user_region_hint: str

    # ── Pre-Flight Router ──────────────────────────────────────────────
    mode: str                     # "express" | "full"
    media_quality_score: float    # set by ingest; used by auto-detect
    media_date_detected: int      # year from EXIF or visible text; 0 if absent

    # ── Express Path ───────────────────────────────────────────────────
    express_transcription: dict   # {title, ingredients, steps, source_text}
    express_hitl_metadata: dict   # {origin, era, tags, notes} — human-filled
    express_low_conf_flag: bool   # True if transcription had many nulls
    hitl_suggestions: dict        # {title, origin, tags, title_is_suggested} pre-computed
    full_hitl_metadata: dict      # human corrections after full-path historian run

    # ── Multi-recipe support ───────────────────────────────────────────
    recipe_count: int             # number of recipes detected in image (default 1)
    multi_recipe_transcriptions: list  # [dict, ...] all structured recipes when count > 1
    current_recipe_index: int     # 0-based index of recipe currently being processed

    # ── Full Path — Loop 1: Transcription ─────────────────────────────
    raw_transcription: str
    repaired_transcription: dict
    transcription_quality_score: float
    transcription_confidence: str
    transcription_loop_count: int
    working_memory: dict
    # Reflexion memory (Shinn et al., 2023 — arXiv:2303.11366).
    # Verbal lessons from prior failed transcription attempts, carried into
    # the next restorer pass as additional context.  Populated by
    # reflexion_agent; also loaded from db/reflexion_memory.json at the
    # start of each retry so lessons persist across archiving sessions.
    transcription_reflections: list

    # ── Full Path — Cross-Registry Scoring ────────────────────────────
    registry_scores: dict         # {region_id: score}
    best_match_region: str
    score_margin: float
    signal_density: float         # top_raw_score / recipe_word_count
    orphan_ratio: float           # multi-word recipe phrases matching NO registry / total
    meal_type: str                # 'breakfast' | 'dessert' | 'dinner' | 'snack' | 'unknown'
    claimed_coherence: float
    is_hybrid: bool

    # ── Full Path — Routing ────────────────────────────────────────────
    route: str
    low_conf_flag: bool
    draft_flag: bool

    # ── Full Path — Loop 2: Historian ─────────────────────────────────
    historian_output: dict
    historian_tool_calls: list
    historian_loop_count: int
    enrichment_complete: bool
    enrichment_partial: bool

    # ── Full Path — Loop 3: Cross-Verification ────────────────────────
    origin_consensus: str
    conflict_note: str
    cross_verify_loop_count: int
    final_origin: str

    # ── Full Path — Discovery Subagent ────────────────────────────────
    unknown_region: bool           # True when recipe doesn't match any registry region well
    provisional_region_id: str
    pending_registry_entry: dict   # draft YAML dict before human approval
    discovery_approved: bool       # True if human approved writing the new registry entry

    # ── Indexer — Duplicate Detection ─────────────────────────────────
    duplicate_flag: bool           # True when a similar recipe was found at index time
    duplicate_of: str              # recipe_id of the similar existing recipe

    # ── Output (both paths) ───────────────────────────────────────────
    pdf_path: str
    pdf_variant: str              # "basic" | "annotated"
    indexed: bool

    # ── Observability ──────────────────────────────────────────────────
    llm_calls: int
    tool_calls: int
    total_loop_iterations: int
    hitl_escalations: int
    errors: list
