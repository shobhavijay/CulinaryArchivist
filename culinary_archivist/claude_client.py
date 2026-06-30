"""
Shared Anthropic Claude client — mirrors ollama_client.py structure.

Provides a singleton Anthropic client initialised once at import time.
All LLM calls go through llm_client.py which calls this module when
LLM_BACKEND=anthropic.

Usage (internal — called by llm_client.py only):
    from culinary_archivist import claude_client
    response = claude_client.chat(
        model="claude-3-5-haiku-20241022",
        messages=[...],
        max_tokens=2048,
        system="...",   # optional
    )
    text = response.content[0].text
"""
import anthropic

from culinary_archivist import config

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file: ANTHROPIC_API_KEY=sk-ant-..."
            )
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def chat(
    model:      str,
    messages:   list[dict],
    max_tokens: int = 2048,
    system:     str = "",
    **kwargs,
) -> anthropic.types.Message:
    """
    Thin wrapper around Anthropic messages.create().
    Returns the full Message object — caller reads .content[0].text.

    Args:
        model:      Claude model name (e.g. "claude-3-5-haiku-20241022")
        messages:   List of {"role": "user"|"assistant", "content": str|list}
        max_tokens: Maximum tokens to generate
        system:     Optional system prompt (separate from messages)
        **kwargs:   Any extra Anthropic API params (temperature, stop_sequences, etc.)
    """
    call_kwargs = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        **kwargs,
    )
    if system:
        call_kwargs["system"] = system

    return _get_client().messages.create(**call_kwargs)
