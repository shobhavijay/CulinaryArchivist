"""Chainlit web UI for Culinary Archivist — Phase 2/4 (Express + Full Path)."""
import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import chainlit as cl
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from culinary_archivist.graph import build_graph
from culinary_archivist import config

log = logging.getLogger(__name__)


# ── File logging setup ────────────────────────────────────────────────────────

def _setup_file_logging() -> Path:
    """
    Add a FileHandler to the root logger so all pipeline log.info() calls
    are written to a timestamped file in logs/ alongside the terminal output.
    Also writes to logs/latest.log (overwritten each run) for easy reading.
    """
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"archivist_{timestamp}.log"
    latest   = logs_dir / "latest.log"

    fmt = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Timestamped file — permanent record per run
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # latest.log — overwritten each run for easy assistant access
    lh = logging.FileHandler(latest, mode="w", encoding="utf-8")
    lh.setLevel(logging.DEBUG)
    lh.setFormatter(fmt)

    root = logging.getLogger()
    root.addHandler(fh)
    root.addHandler(lh)

    # Ensure root logger passes DEBUG+ to handlers
    if root.level == logging.NOTSET or root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    root.info("=== Logging started — file: %s ===", log_file)
    return log_file


_log_file = _setup_file_logging()

_checkpointer = MemorySaver()
_graph = build_graph().compile(checkpointer=_checkpointer)


async def _keepalive(step: cl.Step, interval: int = 30) -> None:
    """
    Sends a heartbeat update to the Chainlit step every `interval` seconds.
    Prevents the browser WebSocket from going silent during long single-node
    operations (e.g. GOT-OCR or large vision models on CPU).
    Cancel the returned task when processing finishes.
    """
    elapsed = 0
    while True:
        await asyncio.sleep(interval)
        elapsed += interval
        step.output = f"Still working... ({elapsed}s elapsed)"
        try:
            await step.update()
        except Exception:
            break   # step already closed — stop silently


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _initial_state(media_path: str, media_type: str, mode: str) -> dict:
    return {
        "media_path": media_path,
        "media_type": media_type,
        "mode": mode,
        "user_region_hint": "",
        "media_quality_score": 0.0,
        "media_date_detected": 0,
        "llm_calls": 0,
        "tool_calls": 0,
        "total_loop_iterations": 0,
        "hitl_escalations": 0,
        "errors": [],
    }


@cl.on_chat_start
async def on_start():
    await cl.Message(
        content=(
            "**Culinary Archivist**\n\n"
            "Upload a recipe image (JPEG/PNG) or PDF to archive it.\n\n"
            "**Modes:**\n"
            "- **Express** *(default)* — fast transcription + your metadata → PDF\n"
            "- **Full** — OCR → quality check → historian enrichment → cross-verify → annotated PDF\n\n"
            "To use full mode, type `full` anywhere in the message when you upload your file.\n"
            "_(e.g. upload the file and type: `full` or `archive this in full mode`)_"
        )
    ).send()


# Single @cl.on_message handler — routes based on session state
@cl.on_message
async def on_message(msg: cl.Message):
    session_id = cl.context.session.id
    config = _config(session_id)

    # ── HITL response branch ───────────────────────────────────────────
    if cl.user_session.get("awaiting_hitl"):
        await _handle_hitl_response(msg, config)
        return

    # ── File upload branch ─────────────────────────────────────────────
    files = [e for e in (msg.elements or []) if hasattr(e, "path") and e.path]
    if not files:
        await cl.Message("Please upload a recipe image or PDF first.").send()
        return

    file = files[0]
    suffix = Path(file.name).suffix.lower()
    media_type = "pdf" if suffix == ".pdf" else "image"

    tmp = tempfile.mktemp(suffix=suffix)
    shutil.copy(file.path, tmp)

    # Detect mode — only switch to full if user typed exactly "full" or "full mode"
    # Avoid accidentally triggering on words like "careful", "handful", "full recipe"
    import re as _re
    text_lower = (msg.content or "").lower().strip()
    mode = "full" if _re.search(r'\bfull\b', text_lower) else "express"

    await cl.Message(
        f"Got it — transcribing **{file.name}** in **{mode.upper()} mode** ..."
    ).send()

    initial = _initial_state(tmp, media_type, mode)

    async with cl.Step(name="Transcribing", show_input=False) as step:
        pulse = asyncio.create_task(_keepalive(step))
        try:
            async for chunk in _graph.astream(initial, config=config, stream_mode="updates"):
                node_name = next(iter(chunk))
                node_data = chunk[node_name]
                log.info("Graph node completed: %s", node_name)
                step.output = f"Completed: {node_name}"
                await step.update()
                # Show meaningful findings as they come out of each node
                await _emit_node_findings(node_name, node_data)
        except Exception as e:
            log.exception("Graph stream error: %s", e)
            await cl.Message(f"⚠️ Processing error: `{e}`").send()
            return
        finally:
            pulse.cancel()

    state = _graph.get_state(config)
    log.info("Graph state after stream — next=%s  keys=%s", state.next, list(state.values.keys()))

    if state.next:
        next_node = state.next[0] if state.next else ""
        log.info("Graph paused at: %s", next_node)
        if next_node == "full_hitl":
            await _show_full_hitl_form(state, config)
        elif next_node == "discovery":
            await _show_discovery_form(state, config)
        elif next_node == "indexer":
            # Indexer paused — check interrupt type
            await _show_duplicate_form(state, config)
        else:
            await _show_hitl_form(state, config)
    else:
        log.info("Graph complete — going to finish")
        await _finish(state)


async def _emit_node_findings(node_name: str, node_data: dict):
    """
    After each graph node completes, emit a brief findings message to the UI
    so the user can see what each agent discovered — not just 'Completed: X'.
    Only emits when there are interesting findings; silent otherwise.
    """
    if not isinstance(node_data, dict):
        return

    # ── Quality evaluator ──────────────────────────────────────────────────────
    if node_name == "quality_evaluator":
        region  = node_data.get("best_match_region", "")
        density = node_data.get("signal_density", 0)
        meal    = node_data.get("meal_type", "")
        if region:
            region_label = "unknown cuisine" if region == "UNKNOWN" else region.replace("_", " ").title()
            parts = [f"📊 **Registry match:** {region_label}"]
            if meal and meal != "unknown":
                parts.append(f"meal type: **{meal}**")
            if density:
                parts.append(f"signal density: `{density:.2f}`")
            await cl.Message(" · ".join(parts)).send()

    # ── Historian ──────────────────────────────────────────────────────────────
    elif node_name == "historian":
        out = node_data.get("historian_output") or {}
        tool_calls = node_data.get("historian_tool_calls") or []
        lines = ["🏛️ **Historian findings:**"]
        if out.get("origin"):
            lines.append(f"- **Origin:** {out['origin']}")
        if out.get("sub_region"):
            lines.append(f"- **Sub-region:** {out['sub_region']}")
        if out.get("era"):
            lines.append(f"- **Era:** {out['era']}")
        if out.get("technique_notes"):
            lines.append(f"- **Technique:** {out['technique_notes'][:200]}")
        if out.get("provenance"):
            lines.append(f"- **Provenance:** {out['provenance'][:300]}")
        if out.get("tags"):
            lines.append(f"- **Tags:** {', '.join(out['tags'])}")
        if tool_calls:
            lines.append(f"- **Tools used:** {len(tool_calls)} research call(s)")
            for tc in tool_calls[:3]:                   # show up to 3 tool headers
                header = str(tc).split("\n")[0][:80]
                lines.append(f"  › `{header}`")
        if len(lines) > 1:
            await cl.Message("\n".join(lines)).send()

    # ── Cross-verifier ────────────────────────────────────────────────────────
    elif node_name == "cross_verifier":
        consensus = node_data.get("origin_consensus", "")
        conflict  = node_data.get("conflict_note", "")
        unknown   = node_data.get("unknown_region", False)
        if consensus:
            msg = f"🔍 **Origin consensus:** {consensus}"
            if unknown:
                msg += "\n⚠️ This cuisine is **not yet in the registry** — Discovery will draft a new entry."
            elif conflict:
                msg += f"\n_{conflict}_"
            await cl.Message(msg).send()

    # ── Discovery ─────────────────────────────────────────────────────────────
    elif node_name == "discovery" and node_data.get("discovery_approved") is not None:
        approved  = node_data.get("discovery_approved", False)
        region_id = node_data.get("provisional_region_id", "")
        if approved:
            await cl.Message(f"✅ **New registry entry saved:** `{region_id}`").send()

    # ── Indexer ───────────────────────────────────────────────────────────────
    elif node_name == "indexer":
        indexed   = node_data.get("indexed", False)
        dup_flag  = node_data.get("duplicate_flag", False)
        dup_of    = node_data.get("duplicate_of", "")
        if indexed and dup_flag and dup_of:
            await cl.Message(
                f"📦 **Indexed** · ⚠️ duplicate of `{dup_of[:8]}…` — kept both as requested."
            ).send()
        elif indexed:
            await cl.Message("📦 **Indexed** — recipe saved to archive.").send()

    # ── Restorer ──────────────────────────────────────────────────────────────
    elif node_name == "restorer":
        repaired = node_data.get("repaired_transcription") or {}
        title    = repaired.get("title")
        n_ingr   = len(repaired.get("ingredients") or [])
        n_steps  = len(repaired.get("steps") or [])
        if title or n_ingr:
            await cl.Message(
                f"🔬 **OCR complete:** title=`{title or '(none)'}` · "
                f"{n_ingr} ingredient(s) · {n_steps} step(s)"
            ).send()


async def _show_hitl_form(state, config):
    low_conf    = state.values.get("express_low_conf_flag", False)

    # Suggestions written by express_suggest_node — reliably in state.values
    suggestions = state.values.get("hitl_suggestions") or {}
    log.info("_show_hitl_form: suggestions=%s", suggestions)
    title              = suggestions.get("title") or ""
    origin             = suggestions.get("origin") or ""
    tags               = suggestions.get("tags") or []
    category           = suggestions.get("category") or ""
    title_is_suggested = suggestions.get("title_is_suggested", False)

    # Store in session so _handle_hitl_response can use them on "yes"
    cl.user_session.set("hitl_suggestions", suggestions)

    if low_conf:
        await cl.Message(
            "⚠️  Transcription confidence is low — some fields may be incomplete. "
            "Please review carefully."
        ).send()

    # Build the suggestion display
    title_label = f"{title} *(suggested — not found in image)*" if title_is_suggested else (title or "—")
    tags_str    = ", ".join(tags) if tags else "—"
    origin_str  = origin or "—"
    category_str = category or "—"

    await cl.Message(
        "**Transcription complete.** Here is what I found and suggest:\n\n"
        f"```\n"
        f"title:    {title_label}\n"
        f"category: {category_str}\n"
        f"origin:   {origin_str}\n"
        f"tags:     {tags_str}\n"
        f"era:      (unknown — please fill in if you know)\n"
        f"notes:    (optional)\n"
        f"```\n\n"
        "Reply **`yes`** to accept these, or correct any field using the same format:\n"
        "```\n"
        "title: My Recipe Name\n"
        "origin: Kerala\n"
        "tags: festive, vegetarian\n"
        "era: 1980s\n"
        "notes: From my grandmother\n"
        "```"
    ).send()

    cl.user_session.set("awaiting_hitl", True)
    cl.user_session.set("graph_config", config)


async def _show_full_hitl_form(state, config):
    """HITL form for the full path — shows historian findings for confirmation."""
    historian = state.values.get("historian_output") or {}
    consensus = state.values.get("origin_consensus") or historian.get("origin") or "—"
    conflict  = state.values.get("conflict_note") or ""

    title  = historian.get("title") or "—"
    era    = historian.get("era")   or "—"
    tags   = ", ".join(historian.get("tags") or []) or "—"
    tech   = historian.get("technique_notes") or "—"

    suggestions = {
        "title":  historian.get("title") or "",
        "origin": consensus,
        "era":    historian.get("era") or "",
        "tags":   historian.get("tags") or [],
    }
    cl.user_session.set("hitl_suggestions", suggestions)
    cl.user_session.set("hitl_mode", "full")

    conflict_line = f"\n⚠️  _{conflict}_\n" if conflict else ""

    await cl.Message(
        f"**Historian findings — please confirm or correct:**\n{conflict_line}\n"
        f"```\n"
        f"title:  {title}\n"
        f"origin: {consensus}\n"
        f"era:    {era}\n"
        f"tags:   {tags}\n"
        f"technique notes: {tech}\n"
        f"```\n\n"
        "Reply **`yes`** to accept, or correct any field:\n"
        "```\n"
        "title: Corrected Name\n"
        "origin: Kerala\n"
        "era: 1970s\n"
        "tags: vegetarian, spicy\n"
        "notes: any extra notes\n"
        "```"
    ).send()

    cl.user_session.set("awaiting_hitl", True)
    cl.user_session.set("graph_config", config)


async def _show_discovery_form(state, config):
    """Discovery HITL — shows the auto-drafted registry YAML for approval.

    The discovery_node hasn't returned yet (it's paused at interrupt()), so
    pending_registry_entry is NOT in state.values yet.
    The draft is in the interrupt payload: state.tasks[0].interrupts[0].value
    """
    import yaml as _yaml

    # ── Pull draft from interrupt payload (reliable) ──────────────────────────
    interrupt_payload: dict = {}
    for task in (state.tasks or []):
        for intr in getattr(task, "interrupts", []):
            v = getattr(intr, "value", None)
            if isinstance(v, dict) and v.get("type") == "discovery_form":
                interrupt_payload = v
                break
        if interrupt_payload:
            break

    entry     = interrupt_payload.get("entry") or {}
    region_id = interrupt_payload.get("region_id") or entry.get("region_id", "UNKNOWN")
    draft_str = interrupt_payload.get("draft_yaml") or ""

    # Fallback: regenerate YAML from entry dict if draft_yaml wasn't in payload
    if not draft_str and entry:
        draft_str = _yaml.dump(entry, allow_unicode=True, sort_keys=False, default_flow_style=False)

    if not draft_str:
        draft_str = "(draft not available — check logs)"

    log.info("_show_discovery_form: region_id=%r  draft_len=%d", region_id, len(draft_str))

    cl.user_session.set("hitl_mode", "discovery")

    await cl.Message(
        f"🔍 **Unknown cuisine detected** — I couldn't match this recipe to any known region "
        f"in the registry.\n\n"
        f"I've drafted a new registry entry for **{region_id}** based on the historian's findings:\n\n"
        f"```yaml\n{draft_str}\n```\n\n"
        f"Reply **`approve`** to add this region to the registry, "
        f"or **`skip`** to discard and continue without saving."
    ).send()

    cl.user_session.set("awaiting_hitl", True)
    cl.user_session.set("graph_config", config)


async def _show_duplicate_form(state, config):
    """
    Duplicate detection HITL — shown when the indexer finds a similar recipe
    already in the archive.

    Reads from interrupt payload (state.tasks[0].interrupts[0].value).
    Shows new recipe vs existing recipe side by side.
    Human picks:
      index   — keep both in the archive
      replace — overwrite the existing record with the new one
      skip    — discard the new upload, don't save
    """
    payload: dict = {}
    for task in (state.tasks or []):
        for intr in getattr(task, "interrupts", []):
            v = getattr(intr, "value", None)
            if isinstance(v, dict) and v.get("type") == "duplicate_check":
                payload = v
                break
        if payload:
            break

    if not payload:
        log.warning("_show_duplicate_form: no duplicate_check interrupt payload found")
        await cl.Message("⚠️  Duplicate check interrupted but payload is missing — replying `index` to continue.").send()
        cl.user_session.set("awaiting_hitl", True)
        cl.user_session.set("hitl_mode", "duplicate")
        cl.user_session.set("graph_config", config)
        return

    new_r    = payload.get("new_recipe") or {}
    old_r    = payload.get("existing_recipe") or {}
    sim      = payload.get("similarity", 0.0)

    def _fmt_ingredients(lst):
        if not lst:
            return "—"
        preview = ", ".join(lst[:5])
        return preview + (f" … (+{len(lst)-5} more)" if len(lst) > 5 else "")

    new_tags = ", ".join(new_r.get("tags") or []) or "—"
    old_tags = ", ".join(old_r.get("tags") or []) or "—"

    await cl.Message(
        f"⚠️  **Possible duplicate detected** — {sim:.1f}% similar to an existing recipe.\n\n"
        f"**New upload** *(being archived now)*\n"
        f"```\n"
        f"title:       {new_r.get('title', '—')}\n"
        f"region:      {new_r.get('region', '—')}\n"
        f"meal type:   {new_r.get('meal_type', '—')}\n"
        f"era:         {new_r.get('era', '—')}\n"
        f"tags:        {new_tags}\n"
        f"ingredients: {_fmt_ingredients(new_r.get('ingredients'))}\n"
        f"```\n\n"
        f"**Existing archive entry** *(archived {old_r.get('archived_at', '—')[:10]})*\n"
        f"```\n"
        f"title:       {old_r.get('title', '—')}\n"
        f"region:      {old_r.get('region', '—')}\n"
        f"meal type:   {old_r.get('meal_type', '—')}\n"
        f"era:         {old_r.get('era', '—')}\n"
        f"tags:        {old_tags}\n"
        f"ingredients: {_fmt_ingredients(old_r.get('ingredients'))}\n"
        f"```\n\n"
        f"What would you like to do?\n"
        f"- **`index`** — keep both versions in the archive\n"
        f"- **`replace`** — overwrite the existing entry with this new one\n"
        f"- **`skip`** — discard this upload, keep the existing entry"
    ).send()

    cl.user_session.set("awaiting_hitl", True)
    cl.user_session.set("hitl_mode", "duplicate")
    cl.user_session.set("graph_config", config)


async def _handle_hitl_response(msg: cl.Message, config: dict):
    saved_config = cl.user_session.get("graph_config") or config
    suggestions  = cl.user_session.get("hitl_suggestions") or {}
    hitl_mode    = cl.user_session.get("hitl_mode", "express")
    cl.user_session.set("awaiting_hitl", False)
    cl.user_session.set("hitl_mode", None)

    text = msg.content.strip().lower()

    # ── Duplicate decision branch ─────────────────────────────────────────────
    if hitl_mode == "duplicate":
        if text in ("replace",):
            action = "replace"
            reply  = "Replacing the existing entry with this new one..."
        elif text in ("skip", "discard", "no", "cancel"):
            action = "skip"
            reply  = "Got it — discarding this upload. The existing entry is unchanged."
        else:
            # default: "index", "keep", "keep both", "yes", or anything else
            action = "index"
            reply  = "Keeping both versions in the archive..."

        await cl.Message(reply).send()

        async with cl.Step(name="Indexing", show_input=False) as step:
            pulse = asyncio.create_task(_keepalive(step))
            try:
                async for chunk in _graph.astream(
                    Command(resume={"action": action}),
                    config=saved_config, stream_mode="updates",
                ):
                    node_name = next(iter(chunk))
                    step.output = f"Completed: {node_name}"
                    await step.update()
            finally:
                pulse.cancel()

        state = _graph.get_state(saved_config)
        if state.next:
            next_node = state.next[0]
            if next_node == "indexer":
                await _show_duplicate_form(state, saved_config)
            elif next_node == "full_hitl":
                await _show_full_hitl_form(state, saved_config)
            elif next_node == "discovery":
                await _show_discovery_form(state, saved_config)
            else:
                await _show_hitl_form(state, saved_config)
        else:
            await _finish(state)
        return

    # ── Discovery approval branch ─────────────────────────────────────────────
    if hitl_mode == "discovery":
        approved = text in ("approve", "yes", "y", "ok", "save", "add")
        if approved:
            form_data = {"approved": True}
            await cl.Message("✅ Region saved to registry. Continuing with recipe archival...").send()
        else:
            form_data = {"approved": False}
            await cl.Message("Skipped — registry not updated. Continuing with recipe archival...").send()

        async with cl.Step(name="Finalising", show_input=False) as step:
            pulse = asyncio.create_task(_keepalive(step))
            try:
                async for chunk in _graph.astream(
                    Command(resume=form_data), config=saved_config, stream_mode="updates"
                ):
                    node_name = next(iter(chunk))
                    step.output = f"Completed: {node_name}"
            finally:
                pulse.cancel()

        # After discovery the graph pauses again at full_hitl — handle it
        state = _graph.get_state(saved_config)
        if state.next:
            next_node = state.next[0] if state.next else ""
            if next_node == "full_hitl":
                await _show_full_hitl_form(state, saved_config)
            elif next_node == "discovery":
                await _show_discovery_form(state, saved_config)
            else:
                await _show_hitl_form(state, saved_config)
        else:
            await _finish(state)
        return

    # ── Express / Full HITL branch ────────────────────────────────────────────
    accepted = text in ("yes", "y", "ok", "okay", "looks good", "correct", "confirm")

    if accepted:
        form_data = {"accepted": True}
        await cl.Message("Got it — using these details as-is.").send()
    else:
        form_data = _parse_form(msg.content, suggestions)
        await cl.Message("Got it — using your corrections.").send()

    async with cl.Step(name="Generating PDF & indexing", show_input=False) as step:
        pulse = asyncio.create_task(_keepalive(step))
        try:
            async for chunk in _graph.astream(
                Command(resume=form_data), config=saved_config, stream_mode="updates"
            ):
                node_name = next(iter(chunk))
                step.output = f"Completed: {node_name}"
        finally:
            pulse.cancel()

    state = _graph.get_state(saved_config)
    if state.next:
        next_node = state.next[0]
        if next_node == "indexer":
            await _show_duplicate_form(state, saved_config)
        elif next_node == "full_hitl":
            await _show_full_hitl_form(state, saved_config)
        elif next_node == "discovery":
            await _show_discovery_form(state, saved_config)
        else:
            await _show_hitl_form(state, saved_config)
    else:
        await _finish(state)


def _parse_form(text: str, suggestions: dict | None = None) -> dict:
    """
    Parse key: value lines from human reply.
    Any field not provided falls back to the agent's suggestion.
    """
    suggestions = suggestions or {}
    parsed: dict = {}

    for line in text.strip().splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not val:
            continue
        if key == "title":
            parsed["title"] = val
        elif key == "origin":
            parsed["origin"] = val
        elif key == "era":
            parsed["era"] = val
        elif key == "tags":
            parsed["tags"] = [t.strip() for t in val.split(",") if t.strip()]
        elif key == "notes":
            parsed["notes"] = val

    # Fall back to suggestions for any field the human left out
    result = {
        "accepted": False,
        "title":    parsed.get("title")  or suggestions.get("title")  or None,
        "origin":   parsed.get("origin") or suggestions.get("origin") or None,
        "era":      parsed.get("era")    or None,
        "tags":     parsed.get("tags")   or suggestions.get("tags")   or [],
        "notes":    parsed.get("notes")  or None,
    }
    return result


async def _finish(state):
    pdf_path = state.values.get("pdf_path")
    indexed = state.values.get("indexed", False)
    title = (
        (state.values.get("express_hitl_metadata") or {}).get("title")
        or (state.values.get("express_transcription") or {}).get("title")
        or (state.values.get("historian_output") or {}).get("title")
        or (state.values.get("repaired_transcription") or {}).get("title")
        or "Recipe"
    )

    if pdf_path and Path(pdf_path).exists():
        elements = [cl.File(name=Path(pdf_path).name, path=pdf_path, display="inline")]
        await cl.Message(
            content=f"✅ **{title}** archived successfully."
                    + (" Indexed for search." if indexed else ""),
            elements=elements,
        ).send()
    else:
        await cl.Message(
            f"⚠️  Processing complete but PDF was not created. "
            f"Check logs for errors. Indexed: {indexed}"
        ).send()
