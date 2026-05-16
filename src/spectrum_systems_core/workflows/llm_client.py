"""Injectable LLM client for the live meeting-minutes extraction path.

The workflow never imports ``anthropic`` at module import time and never
hard-codes the call site. A client is a callable

    client(*, system: str, user: str) -> str

returning the model's raw text response. Production uses
:class:`AnthropicJSONClient` (lazy SDK import,
``claude-haiku-4-5-20251001``). Tests inject a deterministic stub so the
suite runs with no API key and no network — the same seam pattern used
by ``ai/adapter.py`` (``api_caller``) and ``create_human_gt_pairs.py``
(``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE``).

Fail-closed: a transport/SDK error raises :class:`LLMClientError`. The
caller turns that into a blocked artifact — it must never degrade to a
text-mode guess or to the regex extractor silently.
"""
from __future__ import annotations

from typing import Protocol

# Haiku-class extraction model. Non-deprecated (see
# tests/ci/test_no_deprecated_model_strings.py — this string is NOT in
# DEPRECATED_MODEL_STRINGS). Kept as one module constant so there is a
# single place to repoint; the value matches verification/model_registry
# .py's "extraction" default so the two cannot drift to different Haiku
# revisions.
EXTRACTION_MODEL = "claude-haiku-4-5-20251001"

# Generous but bounded — meeting minutes JSON is small. Bounding the
# token budget keeps a runaway response from silently truncating into
# invalid JSON without the strict-schema eval noticing (it would, but
# the bound makes the failure cheap).
_MAX_TOKENS = 4000


class LLMClientError(RuntimeError):
    """Raised on any transport/SDK failure. Fail-closed signal.

    The extraction wrapper catches this and produces a schema-violation
    artifact so the control gate blocks — it does NOT fall back to text
    mode or to the regex extractor.
    """


class LLMClient(Protocol):
    """Structural type for an extraction client.

    A client takes a system prompt and a user message and returns the
    model's raw text. Parsing/validation is the workflow's job, not the
    client's — the client is a thin transport so it is trivially
    stubbable.
    """

    def __call__(self, *, system: str, user: str) -> str:  # pragma: no cover - protocol
        ...


class AnthropicJSONClient:
    """Default production client. Lazily constructs the Anthropic SDK."""

    def __init__(self, *, model: str = EXTRACTION_MODEL, max_tokens: int = _MAX_TOKENS):
        self._model = model
        self._max_tokens = max_tokens

    def __call__(self, *, system: str, user: str) -> str:
        try:
            import anthropic  # lazy: offline/tests never need the SDK
        except ImportError as exc:  # pragma: no cover - SDK is a declared dep
            raise LLMClientError(
                "anthropic SDK not importable; cannot make the live "
                "extraction call"
            ) from exc

        try:
            client = anthropic.Anthropic()
            call_kwargs: dict = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            # claude-opus-4-7 does not support sampling params; omit entirely
            if "opus-4-7" in self._model:
                call_kwargs.pop("temperature", None)
                call_kwargs.pop("top_p", None)
                call_kwargs.pop("top_k", None)
            message = client.messages.create(**call_kwargs)
        except Exception as exc:  # noqa: BLE001 - transport is opaque; fail closed
            raise LLMClientError(
                f"live extraction call failed: {type(exc).__name__}: {exc}"
            ) from exc

        parts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        joined = "\n".join(parts).strip()
        if not joined:
            raise LLMClientError("live extraction call returned no text content")
        return joined


__all__ = [
    "EXTRACTION_MODEL",
    "LLMClient",
    "LLMClientError",
    "AnthropicJSONClient",
]
