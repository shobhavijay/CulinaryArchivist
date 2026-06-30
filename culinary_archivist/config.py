import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
REGISTRY_DIR = Path(os.getenv("ARCHIVIST_REGISTRY_DIR", BASE_DIR / "registry"))
DATA_DIR = Path(os.getenv("ARCHIVIST_DATA_DIR", Path.cwd() / "data"))
OUTPUT_DIR = Path(os.getenv("ARCHIVIST_OUTPUT_DIR", Path.cwd() / "output"))

# Phase 7 — Dual-store persistence
DB_DIR    = Path(os.getenv("ARCHIVIST_DB_DIR",    Path.cwd() / "db"))     # SQLite lives here
CHROMA_DIR = Path(os.getenv("ARCHIVIST_CHROMA_DIR", Path.cwd() / "chroma")) # ChromaDB persist dir

# Embedding model for ChromaDB (Ollama).  Pull with: ollama pull nomic-embed-text
# nomic-embed-text: 274 MB, 768-dim, strong multilingual recipe embeddings
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# Duplicate detection — cosine distance below this triggers a human review prompt.
# 0.0 = identical · 1.0 = completely different
# 0.40 is generous: catches same recipe from different scan/source; human decides.
DUPLICATE_SIMILARITY_THRESHOLD = float(os.getenv("DUPLICATE_SIMILARITY_THRESHOLD", "0.40"))

# Lexical guard on top of vector distance (see indexer.py: _find_duplicate).
# nomic-embed-text compresses short recipe documents into a narrow similarity
# band (observed: distinct dishes like "Endive Boats with Smoked Salmon" and
# "Tomato Chutney Fry" still scored 76% similar) — generic cooking vocabulary
# (oil, salt, chop, simmer, tbsp...) dominates the embedding more than the
# dish-specific content. A vector match below the threshold above is only
# flagged as a real duplicate if title or ingredient token overlap also
# clears one of these bars; otherwise it's a false positive and is dropped
# silently without bothering the human.
DUPLICATE_TITLE_OVERLAP_MIN      = float(os.getenv("DUPLICATE_TITLE_OVERLAP_MIN", "0.30"))
DUPLICATE_INGREDIENT_JACCARD_MIN = float(os.getenv("DUPLICATE_INGREDIENT_JACCARD_MIN", "0.25"))

# Reflexion persistent memory (Shinn et al., 2023 — arXiv:2303.11366).
# Verbal lessons from failed transcription attempts persist here across sessions.
REFLEXION_MEMORY_PATH     = Path(os.getenv("REFLEXION_MEMORY_PATH", Path.cwd() / "db" / "reflexion_memory.json"))
# Maximum number of past reflections to load and inject into the next attempt.
# Older entries beyond this cap are retained in the file but not passed to the LLM.
REFLEXION_MAX_MEMORY_ENTRIES = int(os.getenv("REFLEXION_MAX_MEMORY_ENTRIES", "8"))

# Cross-Verifier Self-Refine (Madaan et al., 2023 — arXiv:2303.17651).
# Maximum FEEDBACK → REFINE iteration passes before accepting the current result.
# Each pass costs 2 LLM calls; 3 passes = up to 6 calls in the worst case.
CROSS_VERIFIER_MAX_REFINE_STEPS = int(os.getenv("CROSS_VERIFIER_MAX_REFINE_STEPS", "3"))

# LLM backend: "ollama" (local) or "anthropic" (cloud)
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama")

# Ollama settings (used when LLM_BACKEND=ollama)
OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
VISION_MODEL      = os.getenv("VISION_MODEL", "llama3.2-vision:11b")   # express transcription
TEXT_MODEL        = os.getenv("TEXT_MODEL", "mistral:7b-instruct")      # historian, synthesizer

# Ollama HTTP timeout in seconds.
# Default is None (no timeout) — matches ollama's own default and is correct
# for slow CPU models where generation time is unpredictable.
# Set OLLAMA_TIMEOUT=600 in .env only if you want a hard ceiling.
_timeout_env = os.getenv("OLLAMA_TIMEOUT", "")
OLLAMA_TIMEOUT = int(_timeout_env) if _timeout_env.strip() else None

# Anthropic settings (used when LLM_BACKEND=anthropic — swap in later)
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_VISION_MODEL = os.getenv("CLAUDE_VISION_MODEL", "claude-opus-4-7")
CLAUDE_TEXT_MODEL   = os.getenv("CLAUDE_TEXT_MODEL", "claude-opus-4-7")

# Pre-flight auto-detect thresholds
AUTO_EXPRESS_QUALITY_THRESHOLD = 0.85     # media_quality_score >= this → candidate for express
AUTO_EXPRESS_MAX_AGE_YEARS = 2            # visible date within this many years → candidate for express

# Express path
EXPRESS_HITL_TIMEOUT_SECONDS = 60

# Phase 4 — Full path thresholds
FULL_PATH_QUALITY_THRESHOLD   = 0.5   # below this → loop restorer
FULL_PATH_MAX_RESTORER_LOOPS  = 3     # max OCR retry loops
FULL_PATH_MAX_HISTORIAN_LOOPS = 2     # max historian enrichment loops
HYBRID_SCORE_MARGIN           = 0.15  # top-two registry scores within this → is_hybrid

# Phase 5 — Historian ReAct loop
HISTORIAN_MAX_REACT_STEPS = int(os.getenv("HISTORIAN_MAX_REACT_STEPS", "4"))  # tool calls per historian invocation

# Phase 6 — Discovery Subagent
# Set DISCOVERY_ENABLED=true in .env to activate auto-drafting of new registry entries.
# When disabled (default) the graph skips discovery and goes straight to full_hitl.
DISCOVERY_ENABLED = os.getenv("DISCOVERY_ENABLED", "false").lower() == "true"
# signal_density below this → weak registry match → treat as unknown region
DISCOVERY_UNKNOWN_SCORE_THRESHOLD = float(os.getenv("DISCOVERY_UNKNOWN_SCORE_THRESHOLD", "0.15"))
# orphan_ratio above this → many recipe phrases match NO registry → unknown region
# e.g. 'fish sauce', 'kaffir lime', 'lemongrass' for Thai curry score 0.0
DISCOVERY_ORPHAN_RATIO_THRESHOLD  = float(os.getenv("DISCOVERY_ORPHAN_RATIO_THRESHOLD", "0.25"))

# OCR strategy: True = PyMuPDF + Tesseract (fast, local, default)
#               False = vision LLM via Ollama (moondream / llava)
USE_PYMUPDF = os.getenv("USE_PYMUPDF", "true").lower() == "true"
TESSDATA_PREFIX = os.getenv("TESSDATA_PREFIX", "/usr/local/opt/tesseract/share/tessdata")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DB_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)
