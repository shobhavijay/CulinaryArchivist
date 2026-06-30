# CulinaryArchivist

A multi-agent AI pipeline that digitises handwritten and printed recipe cards into structured, culturally annotated PDF archive entries.

Upload a photo or PDF of a recipe card — the system transcribes it, identifies its cultural origin and historical era, corrects archaic ingredient terminology, detects duplicates, and produces a fully annotated PDF ready for long-term storage.

See [ARCHDOC.md](ARCHDOC.md) for the full agent architecture and academic foundations, and [BLOG_POST.md](BLOG_POST.md) for a narrative overview of the agentic AI techniques used.

---

## Prerequisites

### Python
Python 3.11 or higher.

### Tesseract OCR
Required when `USE_PYMUPDF=true` (the default, and strongly recommended for handwritten recipes).

**macOS:**
```bash
brew install tesseract
```

**Ubuntu / Debian:**
```bash
sudo apt-get install tesseract-ocr
```

**Windows:** Download the installer from [github.com/tesseract-ocr/tesseract](https://github.com/tesseract-ocr/tesseract/releases) and add it to PATH.

After installing, note the path to the tessdata folder — you'll need it for `TESSDATA_PREFIX` in `.env`.

### LLM backend — pick one

**Option A: Ollama (local, private — default)**

Install from [ollama.com](https://ollama.com), then pull the required models:

```bash
ollama pull llama3.2-vision:11b   # vision model (image OCR)
ollama pull mistral:7b-instruct   # text model (structuring, historian, verifier)
ollama pull nomic-embed-text      # embedding model (duplicate detection)
```

**Option B: Anthropic API (cloud)**

Obtain an API key from [console.anthropic.com](https://console.anthropic.com/keys). Set `LLM_BACKEND=anthropic` and `ANTHROPIC_API_KEY` in `.env`.

Note: the embedding model (`nomic-embed-text`) always runs via Ollama regardless of `LLM_BACKEND` — Ollama must be running even when using the Anthropic backend.

---

## Installation

```bash
# 1. Clone / download the project
git clone <repo-url>
cd CulinaryArchivist

# 2. Create a virtual environment
python3.11 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install the package and all dependencies
pip install -e .

# 4. Configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration below)
```

---

## Configuration

All settings live in `.env`. Copy `.env.example` to get started:

```bash
cp .env.example .env
```

The minimum changes required:

| Setting | What to set |
|---|---|
| `LLM_BACKEND` | `ollama` or `anthropic` |
| `ANTHROPIC_API_KEY` | Your key (only if `LLM_BACKEND=anthropic`) |
| `TESSDATA_PREFIX` | Path to your Tesseract tessdata folder |

Everything else has sensible defaults. See `.env.example` for the full reference with explanations.

---

## Running

### Web UI (Chainlit)

```bash
chainlit run app.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser. Upload a recipe image (JPEG/PNG) or PDF.

- Default mode is **Express** — fast transcription with human tag confirmation.
- Type `full` in your message when uploading to use **Full mode** — historian enrichment, cultural origin, archaic substitutions, annotated PDF.

### CLI

```bash
# Express mode (default: auto-detected from media quality)
archivist --media /path/to/recipe.jpg

# Force full mode (historian + cross-verifier + annotated PDF)
archivist --media /path/to/recipe.jpg --mode full

# Provide a region hint to guide the historian
archivist --media /path/to/recipe.pdf --mode full --region "South India"
```

Output PDFs are saved to `output/`. The recipe is indexed in `db/recipes.db` and `chroma/`.

---

## Pipeline modes

### Express path
Designed for clean, modern, typed recipes. Fast.

```
pre_flight_router → express_transcribe → recipe_count_check
    → express_suggest → express_hitl [HUMAN REVIEW] → pdf_generator → indexer
```

### Full path
Designed for handwritten, ambiguous, or historically significant recipes. Thorough.

```
pre_flight_router → restorer → quality_evaluator
    → [low score] → reflexion_agent → restorer  (up to 3 retries)
    → historian (ReAct loop: Wikipedia + web search)
    → cross_verifier (Self-Refine loop)
    → [unknown region] → discovery [HUMAN APPROVAL]
    → full_hitl [HUMAN REVIEW]
    → pdf_generator → indexer
```

---

## Project structure

```
CulinaryArchivist/
├── app.py                          # Chainlit web UI entry point
├── pyproject.toml                  # Package definition and dependencies
├── .env.example                    # Environment template (copy to .env)
├── culinary_archivist/
│   ├── graph.py                    # LangGraph StateGraph definition
│   ├── state.py                    # Shared ArchivistState TypedDict
│   ├── config.py                   # All configuration (reads from .env)
│   ├── llm_client.py               # Unified LLM interface (Ollama / Anthropic)
│   ├── ollama_client.py            # Ollama backend
│   ├── claude_client.py            # Anthropic backend
│   ├── router.py                   # pre_flight_router heuristics
│   ├── main.py                     # CLI entry point
│   ├── agents/
│   │   ├── express_transcribe.py   # Two-step OCR (PyMuPDF or vision LLM)
│   │   ├── express_hitl.py         # Heuristic tagger + HITL interrupt
│   │   ├── recipe_splitter.py      # Multi-recipe card support
│   │   ├── restorer.py             # Full-path OCR node (with Reflexion injection)
│   │   ├── quality_evaluator.py    # Completeness + registry scoring
│   │   ├── reflexion_agent.py      # Reflexion: verbal lessons → persistent memory
│   │   ├── historian.py            # ReAct loop: Wikipedia + web search + synthesis
│   │   ├── cross_verifier.py       # Self-Refine: FEEDBACK→REFINE attribution loop
│   │   ├── cross_verifier_original.py  # Rule-based fallback (backup)
│   │   ├── full_hitl.py            # Historian output review interrupt
│   │   ├── discovery.py            # Unknown region → draft registry entry
│   │   ├── pdf_generator.py        # Express + annotated PDF generation
│   │   └── indexer.py              # SQLite + ChromaDB dual-store persistence
│   ├── registry/                   # Regional cuisine YAML profiles
│   │   ├── south_indian.yaml
│   │   ├── north_indian.yaml
│   │   ├── american.yaml
│   │   └── ...
│   └── tools/
│       ├── search_tools.py         # Wikipedia + DuckDuckGo search functions
│       ├── registry_tools.py       # Registry lookup tools
│       └── mcp_server.py           # Standalone FastMCP tool server (optional)
├── db/                             # SQLite + Reflexion memory (auto-created)
├── chroma/                         # ChromaDB vector store (auto-created)
├── output/                         # Generated PDFs (auto-created)
└── logs/                           # Run logs (auto-created)
```

---

## Resetting the database

To start fresh (wipe all indexed recipes and Reflexion memory):

```bash
rm -rf db/ chroma/ output/ logs/
```

The registry YAML files in `culinary_archivist/registry/` are not affected.

---

## Agentic AI techniques

| Technique | Paper | Agent |
|---|---|---|
| ReAct | Yao et al., 2022 — [arXiv:2210.03629](https://arxiv.org/abs/2210.03629) | `historian` |
| Self-Refine | Madaan et al., 2023 — [arXiv:2303.17651](https://arxiv.org/abs/2303.17651) | `cross_verifier` |
| Reflexion | Shinn et al., 2023 — [arXiv:2303.11366](https://arxiv.org/abs/2303.11366) | `reflexion_agent` |
