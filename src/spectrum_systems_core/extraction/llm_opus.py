"""Phase AB.2 — Opus unconstrained extraction adapter (out-of-loop).

Calls Claude Opus with NO schema constraint. The output is captured
as an opaque string. The governed pipeline NEVER parses Opus output:
the ONLY code permitted to read ``OpusExtractionResult.raw_output`` (or
the ``extraction_unconstrained.payload.raw_output`` it is persisted
into) is the approximate parser in ``evals/extraction_gap.py``. This
adapter exists only to measure the unaided LLM ceiling.

Model string note: the Phase AB prompt specified the older Opus 4.5
revision, but that exact string is listed in
``tests/ci/test_no_deprecated_model_strings.DEPRECATED_MODEL_STRINGS``
(deprecated). The current non-deprecated Opus revision is
``claude-opus-4-7``; using it keeps the deprecated-model CI gate green
instead of introducing a gate that would only fail post-merge. The
deprecated literal is deliberately NOT written anywhere in this file —
that scanner is a naive substring match and does not exempt comments.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

# Current non-deprecated Opus revision. NOT in DEPRECATED_MODEL_STRINGS.
OPUS_MODEL = "claude-opus-4-7"

# Opus 4.x pricing (USD per 1M tokens). Verified against the public
# Anthropic price list at authoring time. Single place to update on a
# re-price; telemetry cost is derived from here.
OPUS_INPUT_USD_PER_MTOK = 15.00
OPUS_OUTPUT_USD_PER_MTOK = 75.00


@dataclass
class OpusExtractionResult:
    raw_output: str        # opaque, never parsed by the pipeline
    cost_usd: float
    latency_ms: int
    model: str
    prompt: str            # captured for audit


OPUS_EXTRACTION_PROMPT = """\
Read this meeting transcript and tell me:
1. What decisions were made
2. What action items were assigned and to whom
3. What open questions remained

Be thorough. Include verbatim quotes when useful.
"""


def real_extract(transcript: str) -> OpusExtractionResult:
    """Real Opus API call. Unconstrained — no schema.

    A missing OR empty-string ANTHROPIC_API_KEY fails closed with
    ``missing_credentials:ANTHROPIC_API_KEY`` before any network call.
    The returned ``raw_output`` is opaque; the pipeline does not parse
    it. There is intentionally NO stub: Opus is for the comparison
    runner only and is never exercised in unit tests.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not api_key.strip():
        raise RuntimeError("missing_credentials:ANTHROPIC_API_KEY")

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    start = time.monotonic()
    # Stream so a long-transcript Opus extraction does not trip the
    # SDK's 10-minute non-streaming cap.
    with client.messages.stream(
        model=OPUS_MODEL,
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": f"{OPUS_EXTRACTION_PROMPT}\n\n{transcript}",
        }],
    ) as stream:
        response = stream.get_final_message()
    latency_ms = int((time.monotonic() - start) * 1000)

    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    raw = "\n".join(parts).strip()

    cost_usd = (
        response.usage.input_tokens * OPUS_INPUT_USD_PER_MTOK / 1_000_000
        + response.usage.output_tokens * OPUS_OUTPUT_USD_PER_MTOK / 1_000_000
    )

    return OpusExtractionResult(
        raw_output=raw,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        model=OPUS_MODEL,
        prompt=OPUS_EXTRACTION_PROMPT,
    )


__all__ = [
    "OPUS_MODEL",
    "OPUS_INPUT_USD_PER_MTOK",
    "OPUS_OUTPUT_USD_PER_MTOK",
    "OpusExtractionResult",
    "OPUS_EXTRACTION_PROMPT",
    "real_extract",
]
