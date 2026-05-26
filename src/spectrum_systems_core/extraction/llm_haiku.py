"""Phase AB.1 — Haiku extraction adapter.

Calls Claude Haiku via the Anthropic SDK with a structured output
schema matching the meeting_minutes payload. Fails closed if
ANTHROPIC_API_KEY is missing or empty.

Two paths:
  - real_extract(): real API call (used by compare-extraction CLI)
  - stub_extract(): deterministic test stub (used by unit tests)

The Haiku model string is sourced from ``workflows.llm_client.EXTRACTION_MODEL``
so the comparison instrument and the live-LLM workflow can never drift
to different Haiku revisions. It is NOT in
``tests/ci/test_no_deprecated_model_strings.DEPRECATED_MODEL_STRINGS``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from ..workflows.llm_client import EXTRACTION_MODEL

# Single source of truth for the Haiku revision. Re-pointing the live
# workflow re-points this instrument too — they cannot diverge.
HAIKU_MODEL = EXTRACTION_MODEL

# Haiku 4.5 pricing (USD per 1M tokens). Verified against the public
# Anthropic price list at authoring time. If Anthropic re-prices, this
# is the single place to update — telemetry cost is derived from here.
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00

# Top-level keys the structured Haiku output MUST carry. Validated
# before a result is treated as a success so a valid-JSON-but-wrong-shape
# response fails closed instead of silently reporting zero items.
REQUIRED_OUTPUT_KEYS: tuple[str, ...] = ("decisions", "actions", "questions")


@dataclass
class HaikuExtractionResult:
    output: dict          # parsed structured output
    raw_response: str     # raw model response (for audit)
    cost_usd: float       # estimated from token counts
    latency_ms: int       # wall-clock latency
    model: str            # model string used


HAIKU_EXTRACTION_SYSTEM_PROMPT = """\
You are an extraction agent. Read the meeting transcript and extract:
- decisions (with governing verb if present)
- action items (with owner if mentioned)
- open questions

Rules:
- If a fact is not in the transcript, omit it. Do not infer.
- Every extracted item must include source_turns: list of turn_ids
  that support the claim.
- Use only verbs from this taxonomy for decisions:
  approved, rejected, deferred, noted, directed, considered

Output strict JSON matching this schema:
{
  "decisions": [{"text": str, "verb": str, "source_turns": [str]}],
  "actions": [{"text": str, "owner": str|null, "source_turns": [str]}],
  "questions": [{"text": str, "source_turns": [str]}]
}
"""


class HaikuOutputError(RuntimeError):
    """Haiku returned something that is not a usable structured output.

    Distinct from a missing-credentials RuntimeError so the comparison
    runner can record a precise ``failed:<reason>`` status without
    confusing a transport problem with a schema problem.
    """


def _validate_output_shape(parsed: object) -> dict:
    """Fail closed unless ``parsed`` is a dict carrying every required
    list key. A valid-JSON-but-wrong-shape response must NOT be treated
    as a success that reports zero items (red-team Pass 1)."""
    if not isinstance(parsed, dict):
        raise HaikuOutputError(
            f"haiku_output_not_object:got_{type(parsed).__name__}"
        )
    missing = [k for k in REQUIRED_OUTPUT_KEYS if k not in parsed]
    if missing:
        raise HaikuOutputError(
            f"haiku_output_missing_keys:{','.join(missing)}"
        )
    for key in REQUIRED_OUTPUT_KEYS:
        if not isinstance(parsed[key], list):
            raise HaikuOutputError(
                f"haiku_output_key_not_list:{key}"
            )
    return parsed


def real_extract(transcript_with_turn_ids: str) -> HaikuExtractionResult:
    """Real Haiku API call. Requires a non-empty ANTHROPIC_API_KEY.

    A missing OR empty-string key fails closed with
    ``missing_credentials:ANTHROPIC_API_KEY`` before any network call.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not api_key.strip():
        raise RuntimeError("missing_credentials:ANTHROPIC_API_KEY")

    # Lazy import: unit tests never exercise this path and must not
    # require the SDK to be importable to collect.
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    start = time.monotonic()
    # Stream so a long-transcript extraction does not trip the SDK's
    # 10-minute non-streaming cap. ``get_final_message`` exposes the
    # same Message shape as ``create`` (content / usage / stop_reason).
    with client.messages.stream(
        model=HAIKU_MODEL,
        max_tokens=4096,
        temperature=0,
        system=HAIKU_EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": transcript_with_turn_ids}],
    ) as stream:
        response = stream.get_final_message()
    latency_ms = int((time.monotonic() - start) * 1000)

    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    raw = "\n".join(parts).strip()
    if not raw:
        raise HaikuOutputError("haiku_output_empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HaikuOutputError(f"haiku_output_not_json:{e}") from e
    parsed = _validate_output_shape(parsed)

    cost_usd = (
        response.usage.input_tokens * HAIKU_INPUT_USD_PER_MTOK / 1_000_000
        + response.usage.output_tokens * HAIKU_OUTPUT_USD_PER_MTOK / 1_000_000
    )

    return HaikuExtractionResult(
        output=parsed,
        raw_response=raw,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        model=HAIKU_MODEL,
    )


def stub_extract(transcript_with_turn_ids: str) -> HaikuExtractionResult:
    """Deterministic stub for unit tests. Returns the same output every
    time regardless of input. NEVER makes a network call and NEVER
    constructs an Anthropic client — that is what makes it safe in CI.
    The comparison runner uses ``real_extract``; this is the test seam.
    """
    return HaikuExtractionResult(
        output={
            "decisions": [
                {"text": "STUB decision", "verb": "approved",
                 "source_turns": ["t0001"]}
            ],
            "actions": [
                {"text": "STUB action", "owner": "stub-owner",
                 "source_turns": ["t0002"]}
            ],
            "questions": [
                {"text": "STUB question?", "source_turns": ["t0003"]}
            ],
        },
        raw_response='{"stub": true}',
        cost_usd=0.0,
        latency_ms=0,
        model="stub",
    )


__all__ = [
    "HAIKU_MODEL",
    "HAIKU_INPUT_USD_PER_MTOK",
    "HAIKU_OUTPUT_USD_PER_MTOK",
    "REQUIRED_OUTPUT_KEYS",
    "HaikuExtractionResult",
    "HaikuOutputError",
    "HAIKU_EXTRACTION_SYSTEM_PROMPT",
    "real_extract",
    "stub_extract",
]
