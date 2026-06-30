"""
LLM client abstraction — routes calls to Ollama or Anthropic Claude
based on LLM_BACKEND env variable.

Usage (same interface regardless of backend):

    from culinary_archivist import llm_client

    # Text call
    text = llm_client.chat(
        messages=[{"role": "user", "content": "..."}],
        max_tokens=2048,
    )

    # Vision call (image + text)
    text = llm_client.vision_chat(
        prompt="Read this recipe image...",
        image_b64="<base64 string>",
        max_tokens=1024,
    )

.env switching:

    # Ollama (local)
    LLM_BACKEND=ollama
    TEXT_MODEL=qwen2.5:7b-instruct
    VISION_MODEL=llama3.2-vision:11b

    # Anthropic Claude (cloud)
    LLM_BACKEND=anthropic
    CLAUDE_TEXT_MODEL=claude-3-5-haiku-20241022
    CLAUDE_VISION_MODEL=claude-3-5-sonnet-20241022
    ANTHROPIC_API_KEY=sk-ant-...

PyMuPDF path:
    PDF → Tesseract OCR → text → chat()   (no vision_chat needed — text only)
    This path is unaffected by LLM_BACKEND for the OCR step itself.
    Only the qwen structuring call goes through chat().
"""
import logging

from culinary_archivist import config

log = logging.getLogger(__name__)


# ── Ollama backend ────────────────────────────────────────────────────────────

def _ollama_chat(messages: list[dict], max_tokens: int, temperature: float) -> str:
    from culinary_archivist import ollama_client
    response = ollama_client.chat(
        model=config.TEXT_MODEL,
        messages=messages,
        options={"num_predict": max_tokens, "temperature": temperature},
    )
    return response["message"]["content"]


def _ollama_vision_chat(prompt: str, image_b64: str, max_tokens: int, temperature: float) -> str:
    from culinary_archivist import ollama_client
    response = ollama_client.chat(
        model=config.VISION_MODEL,
        messages=[{
            "role":    "user",
            "content": prompt,
            "images":  [image_b64],
        }],
        options={"num_predict": max_tokens, "temperature": temperature},
    )
    return response["message"]["content"]


# ── Anthropic Claude backend ──────────────────────────────────────────────────

def _detect_media_type(image_b64: str) -> str:
    """Detect image MIME type from base64 prefix bytes."""
    if image_b64.startswith("iVBOR"):
        return "image/png"
    if image_b64.startswith("R0lGOD"):
        return "image/gif"
    if image_b64.startswith("UklGR"):
        return "image/webp"
    return "image/jpeg"   # default


def _claude_chat(messages: list[dict], max_tokens: int, temperature: float) -> str:
    from culinary_archivist import claude_client

    # Extract system message — Claude API takes it as a separate parameter
    system_msg = ""
    user_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            user_messages.append({"role": m["role"], "content": m["content"]})

    response = claude_client.chat(
        model=config.CLAUDE_TEXT_MODEL,
        messages=user_messages,
        max_tokens=max_tokens,
        system=system_msg,
    )
    return response.content[0].text


def _claude_vision_chat(prompt: str, image_b64: str, max_tokens: int, temperature: float) -> str:
    from culinary_archivist import claude_client

    response = claude_client.chat(
        model=config.CLAUDE_VISION_MODEL,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type":       "base64",
                        "media_type": _detect_media_type(image_b64),
                        "data":       image_b64,
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
    )
    return response.content[0].text


# ── Public API ────────────────────────────────────────────────────────────────

def chat(
    messages:    list[dict],
    max_tokens:  int   = 2048,
    temperature: float = 0.0,
) -> str:
    """
    Text LLM call. Routes to Ollama or Claude based on LLM_BACKEND.

    Args:
        messages:    List of {"role": "user"|"assistant"|"system", "content": str}
        max_tokens:  Maximum tokens to generate
        temperature: Sampling temperature (0 = deterministic)

    Returns:
        Response text as a plain string.
    """
    backend = config.LLM_BACKEND.lower()
    log.debug("llm_client.chat: backend=%s  max_tokens=%d", backend, max_tokens)

    if backend == "anthropic":
        return _claude_chat(messages, max_tokens, temperature)
    else:
        return _ollama_chat(messages, max_tokens, temperature)


def vision_chat(
    prompt:      str,
    image_b64:   str,
    max_tokens:  int   = 1024,
    temperature: float = 0.0,
) -> str:
    """
    Vision LLM call — text prompt + one image. Routes to Ollama or Claude.

    Args:
        prompt:    Text instruction (e.g. "Read every word in this recipe image")
        image_b64: Base64-encoded image string (no data: URI prefix)
        max_tokens:  Maximum tokens to generate
        temperature: Sampling temperature (0 = deterministic)

    Returns:
        Response text as a plain string.
    """
    backend = config.LLM_BACKEND.lower()
    log.debug("llm_client.vision_chat: backend=%s  max_tokens=%d", backend, max_tokens)

    if backend == "anthropic":
        return _claude_vision_chat(prompt, image_b64, max_tokens, temperature)
    else:
        return _ollama_vision_chat(prompt, image_b64, max_tokens, temperature)
