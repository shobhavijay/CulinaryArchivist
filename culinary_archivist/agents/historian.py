"""
Historian node — full path, Loop 2.  (Phase 5: ReAct + FastMCP tools)

Workflow per invocation:
  1. ReAct loop (up to HISTORIAN_MAX_REACT_STEPS):
       qwen picks a tool → observation fed back → repeat until DONE or max steps
  2. Single JSON-extraction call with all observations accumulated
  3. Normalise output; check completeness.

Tools available to qwen (dispatched in-process via TOOL_MAP):
  - search_wikipedia(query)
  - search_web(query)
  - lookup_registry(region_id)
  - search_landmark_dishes(dish_name)
  - list_regions()

historian_output schema:
{
  "title"               : str | null,
  "ingredients"         : list[str],   # corrected — archaic_substitutions applied
  "steps"               : list[str],   # corrected — archaic_substitutions applied
  "ingredients_raw"      : list[str],  # verbatim from transcription, untouched
  "steps_raw"            : list[str],  # verbatim from transcription, untouched
  "source_text"         : str | null,
  "origin"              : str | null,
  "sub_region"          : str | null,
  "era"                 : str | null,
  "region_id"           : str | null,
  "technique_notes"     : str | null,
  "provenance"          : str | null,
  "archaic_substitutions": dict,
  "vibe_keywords"       : list[str],
  "tags"                : list[str],
  "notes"               : str | null,
  "geo_tag"             : str | null
}
"""
import json
import logging
import re

from culinary_archivist import config, llm_client
from culinary_archivist.state import ArchivistState
from culinary_archivist.registry.loader import get_region
from culinary_archivist.tools.mcp_server import TOOL_MAP, TOOL_DESCRIPTIONS

log = logging.getLogger(__name__)


# ── ReAct prompt (step 1: tool selection) ────────────────────────────────────

_REACT_SYSTEM = """\
You are a culinary historian researching a recipe for archival.
You will receive a recipe and must research its cultural and historical context.

{tool_descriptions}

At each step, output EXACTLY this format (no other text):
Thought: <one sentence explaining what you want to find>
Action: <tool name from the list above, or DONE>
Input: <the argument to pass to the tool, or leave blank if Action is DONE>

Rules:
- Call one tool at a time.
- When you have enough context to write a complete provenance paragraph, output Action: DONE.
- If you already know the region and era with confidence, you may output DONE immediately.
- Never call the same tool with the same input twice.
"""

_REACT_USER_TEMPLATE = """\
Recipe to research:
Title: {title}
Region hint: {region_id}
Ingredients (first 10): {ingredients_short}

{history}
What is your next step?"""


# ── JSON-extraction prompt (step 2: final synthesis) ─────────────────────────

_SYNTHESIS_PROMPT = """\
You are a culinary historian. Using the recipe context and your research observations,
produce a JSON enrichment record for archival.

Rules:
1. Output ONLY the enrichment fields below — title, ingredients, steps and source_text
   are already captured and must NOT appear in your output.
2. Fill every field from your research observations.
3. If uncertain, write your best guess followed by " [inferred]" — EXCEPT in
   archaic_substitutions (see rule 4, which has its own format).
4. For archaic_substitutions: map every archaic, regional-dialect, or
   handwriting/OCR-garbled term actually present in the ingredients or steps
   to ONE clean, modern, cookable term. Use the region profile's archaic_terms
   below plus your own knowledge of the cuisine to decide the most plausible
   reading — reason from the region/dish context, not just the letters.
     - Cover BOTH genuine regional vocabulary (e.g. "vengaya" → "shallot")
       AND likely transcription corruption of common pantry items
       (e.g. "Chona daal" → "chana dal", "lnnd" → "urad dal").
     - Give exactly one term per key — no "X or Y" alternatives, no
       "[inferred]" tags, no parenthetical hedging. A cook needs one
       shoppable word, not a hedge. Pick your single best-reasoned answer.
     - Do not include terms you're not reasonably confident about; it's
       fine to leave an odd term uncorrected rather than guess wildly.
5. Return ONLY a valid JSON object — no explanation, no markdown.

Output schema (all fields required, null if unknown):
{{
  "origin":                 <string — e.g. "Thailand, Central region">,
  "region_id":              <string — registry key e.g. "THA-CEN">,
  "sub_region":             <string or null — e.g. "Bangkok / Central Plains">,
  "era":                    <string or null — e.g. "mid-20th century">,
  "technique_notes":        <string — key cooking methods and their cultural significance>,
  "provenance":             <string — 2-3 sentences of cultural/historical context>,
  "archaic_substitutions":  {{<old term>: <modern equivalent>}},
  "vibe_keywords":          [<evocative descriptors e.g. "coconut-forward", "aromatic">],
  "tags":                   [<e.g. "vegetarian", "festive", "quick", "spicy">],
  "notes":                  <string or null>,
  "geo_tag":                <string or null — e.g. "Southeast Asia / Thailand">
}}

--- REGISTRY PROFILE ---
{registry_context}

--- RECIPE CONTEXT (for reference only — do not reproduce these in your output) ---
Title: {title}
Key ingredients: {ingredients_short}

--- RESEARCH OBSERVATIONS ---
{observations}

--- NOW OUTPUT ONLY THE JSON OBJECT ---
"""


# ── Registry context builder ──────────────────────────────────────────────────

def _build_registry_context(region_id: str) -> str:
    reg = get_region(region_id)
    if not reg:
        return f"Region: {region_id} (no registry entry found)"

    lines = [
        f"Region: {reg.get('display_name', region_id)}",
        f"Sub-regions: {', '.join(reg.get('sub_regions', [])[:5])}",
    ]
    flavor = reg.get("flavor_profile", {})
    if flavor:
        lines.append(f"Flavor: {flavor.get('dominant', '')} | heat={flavor.get('heat_level', '')} | sour={flavor.get('sourness', '')}")

    spices = reg.get("signature_spices", {})
    tempering = spices.get("tempering_base", spices.get("whole", []))
    if tempering:
        lines.append(f"Key spices: {', '.join(str(s) for s in tempering[:8])}")

    techniques = reg.get("cooking_techniques", [])[:5]
    if techniques:
        lines.append(f"Techniques: {'; '.join(str(t) for t in techniques)}")

    archaic = reg.get("archaic_terms", {})
    if archaic:
        sample = list(archaic.items())[:6]
        lines.append("Archaic: " + ", ".join(f"{k}={v}" for k, v in sample))

    landmark = reg.get("landmark_dishes", [])
    if isinstance(landmark, list):
        lines.append(f"Landmark: {', '.join(str(d) for d in landmark[:5])}")

    vibe = reg.get("vibe_keywords", [])[:6]
    if vibe:
        lines.append(f"Vibe: {', '.join(vibe)}")

    return "\n".join(lines)


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _parse_historian_json(raw: str) -> dict | None:
    text = raw.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            text = m.group(1).strip()

    brace = re.search(r"\{[\s\S]*\}", text)
    if brace:
        text = brace.group(0)

    text = re.sub(r'\\([^"\\/bfnrtu])', lambda m: m.group(1), text)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    for closing in ["}", "}}", "\"]}", "\"]}}"]:
        try:
            result = json.loads(text + closing)
            if isinstance(result, dict):
                log.warning("Historian: repaired truncated JSON with %r", closing)
                return result
        except Exception:
            pass

    return None


def _clean_substitution_value(value: str) -> str:
    """
    Safety net in case the LLM hedges anyway: reduce a substitution value to
    a single clean term suitable for inline replacement into the recipe text.
    The prompt asks for one clean term, but this strips common hedging
    patterns ("[inferred]", "(maybe ...)") if they slip through.
    """
    v = (value or "").strip()
    v = re.sub(r"\s*\[[^\]]*\]\s*$", "", v)   # trailing [inferred]
    v = re.sub(r"\s*\([^)]*\)\s*$", "", v)    # trailing (inferred)
    return v.strip() or value


def _apply_substitutions(text: str, substitutions: dict) -> str:
    """
    Replace archaic/garbled terms in `text` with their historian-inferred
    modern equivalents. Longest terms are matched first so multi-word terms
    (e.g. "Chona daal") are swapped before any single-word substring of them
    ("daal" alone). Whole-word matching, case-insensitive.
    """
    if not text or not substitutions:
        return text
    for old_term in sorted(substitutions, key=len, reverse=True):
        old_term = (old_term or "").strip()
        new_term = _clean_substitution_value(substitutions.get(old_term, ""))
        if not old_term or not new_term:
            continue
        pattern = re.compile(r"\b" + re.escape(old_term) + r"\b", re.IGNORECASE)
        text = pattern.sub(new_term, text)
    return text


def _apply_substitutions_list(items: list, substitutions: dict) -> list:
    return [_apply_substitutions(item, substitutions) for item in (items or [])]


def _normalise_historian(d: dict, transcription: dict) -> dict:
    # Verbatim transcription, exactly as OCR'd — kept untouched for provenance.
    raw_ingredients = transcription.get("ingredients") or []
    raw_steps       = transcription.get("steps") or []
    substitutions   = d.get("archaic_substitutions") or {}

    return {
        "title":                transcription.get("title"),
        # Cookable versions — archaic/regional/OCR-garbled terms swapped for
        # their region-reasoned modern equivalents (see archaic_substitutions
        # below, which is what drives this replacement).
        "ingredients":          _apply_substitutions_list(raw_ingredients, substitutions),
        "steps":                _apply_substitutions_list(raw_steps, substitutions),
        # Verbatim originals — preserved for audit/archival fidelity.
        "ingredients_raw":      raw_ingredients,
        "steps_raw":            raw_steps,
        "source_text":          transcription.get("source_text"),
        # Historian-owned enrichment fields — filled from web research
        "origin":               d.get("origin"),
        "sub_region":           d.get("sub_region"),
        "era":                  d.get("era"),
        "region_id":            d.get("region_id"),
        "technique_notes":      d.get("technique_notes"),
        "provenance":           d.get("provenance"),
        "archaic_substitutions": substitutions,
        "vibe_keywords":        d.get("vibe_keywords") or [],
        "tags":                 d.get("tags") or [],
        "notes":                d.get("notes"),
        "geo_tag":              d.get("geo_tag"),
    }


def _check_completeness(output: dict) -> tuple[bool, bool]:
    key_fields = ["origin", "provenance", "technique_notes", "era"]
    populated  = sum(1 for f in key_fields if output.get(f))
    complete   = populated == len(key_fields)
    partial    = populated >= 2
    return complete, partial


# ── ReAct loop ────────────────────────────────────────────────────────────────

def _parse_react_response(text: str) -> tuple[str, str, str]:
    """
    Parse a ReAct response into (thought, action, input).
    Returns ("", "DONE", "") if parsing fails.
    """
    thought = ""
    action  = "DONE"
    inp     = ""

    for line in text.strip().splitlines():
        line = line.strip()
        if line.lower().startswith("thought:"):
            thought = line[len("thought:"):].strip()
        elif line.lower().startswith("action:"):
            action = line[len("action:"):].strip()
        elif line.lower().startswith("input:"):
            inp = line[len("input:"):].strip()

    return thought, action, inp


def _run_react_loop(
    title: str,
    region_id: str,
    ingredients_short: str,
    max_steps: int,
) -> list[str]:
    """
    Run up to `max_steps` rounds of Thought→Action→Observation.
    Returns a list of observation strings to be injected into the synthesis prompt.
    """
    observations: list[str] = []
    called: set[str] = set()          # deduplicate (tool, input) pairs
    history_lines: list[str] = []

    system_msg = _REACT_SYSTEM.format(tool_descriptions=TOOL_DESCRIPTIONS)

    for step in range(max_steps):
        history = "\n".join(history_lines) if history_lines else "(no previous steps)"
        user_msg = _REACT_USER_TEMPLATE.format(
            title=title,
            region_id=region_id,
            ingredients_short=ingredients_short,
            history=history,
        )

        log.info("Historian ReAct step %d/%d ...", step + 1, max_steps)
        try:
            raw = llm_client.chat(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=256,
                temperature=0,
            )
        except Exception as e:
            log.warning("Historian ReAct: LLM call failed at step %d: %s", step + 1, e)
            break
        log.debug("Historian ReAct raw:\n%s", raw)

        thought, action, inp = _parse_react_response(raw)
        log.info("  Thought: %s", thought)
        log.info("  Action : %s  Input: %r", action, inp)

        if action.upper() == "DONE" or not action:
            log.info("Historian ReAct: DONE after %d step(s)", step + 1)
            break

        # Normalise action name (strip parentheses if qwen included them)
        action_clean = re.sub(r"\(.*\)$", "", action).strip()

        tool_fn = TOOL_MAP.get(action_clean)
        if not tool_fn:
            # Try case-insensitive match
            action_lower = action_clean.lower()
            tool_fn = next(
                (fn for name, fn in TOOL_MAP.items() if name.lower() == action_lower),
                None,
            )

        if not tool_fn:
            log.warning("Historian ReAct: unknown tool %r — stopping loop", action_clean)
            break

        # Deduplicate
        call_key = f"{action_clean}::{inp}"
        if call_key in called:
            log.info("Historian ReAct: duplicate call skipped — %s(%r)", action_clean, inp)
            break
        called.add(call_key)

        # Execute tool
        try:
            observation = tool_fn(inp) if inp else tool_fn()
        except Exception as e:
            observation = f"[Tool error: {e}]"
            log.warning("Historian ReAct: tool %r raised: %s", action_clean, e)

        # Truncate observations that are too long
        if len(observation) > 1500:
            observation = observation[:1500] + " …[truncated]"

        log.info("  Observation (%d chars): %s…", len(observation), observation[:120])

        observations.append(f"[{action_clean}({inp!r})]\n{observation}")
        history_lines.append(
            f"Step {step+1}: Action={action_clean}  Input={inp!r}\n"
            f"Observation: {observation[:300]}…"
        )

    return observations


# ── Node ─────────────────────────────────────────────────────────────────────

def historian_node(state: ArchivistState) -> dict:
    transcription = state.get("repaired_transcription") or {}
    region_id     = state.get("best_match_region", "UNKNOWN")
    loop_count    = state.get("historian_loop_count", 0)

    log.info("Historian [loop %d]: region=%s", loop_count + 1, region_id)

    # ── Phase A: ReAct tool loop ──────────────────────────────────────────────
    title = transcription.get("title") or "(unknown)"
    ingredients_list = transcription.get("ingredients", [])
    ingredients_short = ", ".join(str(i) for i in ingredients_list[:10])

    observations = _run_react_loop(
        title=title,
        region_id=region_id,
        ingredients_short=ingredients_short,
        max_steps=config.HISTORIAN_MAX_REACT_STEPS,
    )

    total_tool_calls = len(observations)
    log.info("Historian: %d tool observation(s) collected", total_tool_calls)

    # ── Phase B: JSON synthesis call ──────────────────────────────────────────
    registry_context  = _build_registry_context(region_id)
    observations_text = "\n\n".join(observations) if observations else "(no external research performed)"

    # Only pass title + short ingredient list as read-only context.
    # Full ingredients / steps / source_text stay in state (restorer owns them).
    synthesis_prompt = _SYNTHESIS_PROMPT.format(
        registry_context=registry_context,
        title=title,
        ingredients_short=ingredients_short,
        observations=observations_text[:3000],
    )

    log.info("Historian: calling LLM for JSON synthesis...")
    try:
        raw = llm_client.chat(
            messages=[{"role": "user", "content": synthesis_prompt}],
            max_tokens=2048,
            temperature=0,
        )
        log.info("Historian synthesis raw (first 1000 chars):\n%s", raw[:1000])

        parsed = _parse_historian_json(raw)
    except Exception as e:
        log.error("Historian synthesis call failed: %s", e)
        parsed = None

    if parsed:
        output = _normalise_historian(parsed, transcription)
        log.info("Historian: parsed OK — origin=%r  era=%r", output.get("origin"), output.get("era"))
    else:
        log.warning("Historian: JSON parse failed — falling back to transcription only")
        output = _normalise_historian({}, transcription)

    complete, partial = _check_completeness(output)
    log.info("Historian: complete=%s  partial=%s", complete, partial)

    return {
        "historian_output":     output,
        "historian_loop_count": loop_count + 1,
        "enrichment_complete":  complete,
        "enrichment_partial":   partial,
        "historian_tool_calls": observations,          # stored for traceability
        "llm_calls":  state.get("llm_calls",  0) + 1 + total_tool_calls,   # ReAct + synthesis
        "tool_calls": state.get("tool_calls", 0) + total_tool_calls,
    }
