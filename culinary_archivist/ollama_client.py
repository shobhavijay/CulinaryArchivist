"""
Shared Ollama client.

ollama's default timeout is None (infinite) — matching that default here.
OLLAMA_TIMEOUT in config is None unless the user explicitly sets it in .env.
Setting it gives a hard ceiling for runaway model calls; leave it unset for
local CPU models where generation time is genuinely unpredictable.

Usage:
    from culinary_archivist.ollama_client import chat
    response = chat(model=config.TEXT_MODEL, messages=[...], options={...})
"""
import ollama
from culinary_archivist import config

_client = ollama.Client(
    host=config.OLLAMA_BASE_URL,
    timeout=config.OLLAMA_TIMEOUT,   # None by default = no timeout (ollama default)
)


def chat(**kwargs) -> dict:
    """Thin wrapper — passes all kwargs straight to the shared client."""
    return _client.chat(**kwargs)
