"""Phase Y.1 — Opus ceiling extractor.

Measures the unaided Opus extraction ceiling for ONE transcript with a
SINGLE Opus call (no chunking — Opus 4.7 has the context window for a
full ~7 GHz-Downlink-length transcript). The ceiling is the yardstick
every Haiku run is later scored against.

Fail-closed contract (Phase Y red-team Pass 3 #2): if Opus is
unavailable (missing/empty ``ANTHROPIC_API_KEY``) or returns something
that is not a usable list of items, this raises ``CeilingError``. It
NEVER returns an empty ceiling — a silent zero ceiling would make
every later Haiku comparison look perfect and defeat the entire phase.

The model call is injected via ``opus_call`` so the deterministic
governed logic (keyword hits, per-type counts, artifact shape) is unit
testable without a network call; the default performs the real call.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from ..artifacts import Artifact, new_artifact
from .ceiling_triggers import CEILING_SCHEMA_TYPES, transcript_keyword_hits
from .llm_opus import OPUS_MODEL

SCHEMA_VERSION = "1.0.0"
ARTIFACT_TYPE = "opus_ceiling"

# Same identifier rule as the data-lake contract's meeting_id.
_TRANSCRIPT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")

# What the ceiling extractor returns per item before it is normalised.
CeilingItem = dict
OpusCall = Callable[[str], list["CeilingItem"]]


class CeilingError(RuntimeError):
    """Opus was unavailable or returned an unusable result.

    A ``RuntimeError`` (not ``ValueError``) so it is not swallowed by
    the broad schema-validation ``ValueError`` handlers, and carries a
    machine-readable ``reason_code`` so a gate reads a value, never a
    message string.
    """

    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


def _real_opus_call(transcript_text: str) -> list[CeilingItem]:
    """Single real Opus call. Fail-closed; never returns ``[]`` silently.

    Imports + key check happen here so unit tests (which always inject
    ``opus_call``) never touch the SDK or the environment.
    """
    import json
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not api_key.strip():
        raise CeilingError(
            "missing_credentials:ANTHROPIC_API_KEY",
            reason_code="opus_unavailable",
        )

    from anthropic import Anthropic

    prompt = (
        "You are extracting the COMPLETE set of governed items from a "
        "meeting transcript. The transcript turns are line-numbered as "
        "t0001, t0002, ... Return STRICT JSON: a list named "
        '"items" where each item is '
        '{"schema_type": one of '
        f"{list(CEILING_SCHEMA_TYPES)}, "
        '"source_turn_ids": [turn ids that support it], '
        '"source_text": verbatim supporting text, '
        '"payload": {a typed object describing the item}}. '
        "Be exhaustive. Do not infer items not in the transcript."
    )
    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=16384,
            messages=[
                {
                    "role": "user",
                    "content": f"{prompt}\n\n---TRANSCRIPT---\n{transcript_text}",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — any SDK/transport error
        raise CeilingError(
            f"opus_call_failed:{type(exc).__name__}:{exc}",
            reason_code="opus_unavailable",
        ) from exc

    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    raw = "\n".join(parts).strip()
    if not raw:
        raise CeilingError("opus_output_empty", reason_code="opus_bad_output")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CeilingError(
            f"opus_output_not_json:{exc}", reason_code="opus_bad_output"
        ) from exc
    items = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise CeilingError(
            "opus_output_not_a_list", reason_code="opus_bad_output"
        )
    return items


def _normalise_items(raw_items: list[CeilingItem]) -> list[dict]:
    """Coerce model output into the contract item shape, deterministically.

    A non-dict entry, or one missing ``schema_type``, is a malformed
    ceiling — raise rather than drop it silently (dropping would
    understate the ceiling and inflate later Haiku recall).
    """
    out: list[dict] = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise CeilingError(
                f"ceiling_item_not_object:index_{idx}",
                reason_code="opus_bad_output",
            )
        schema_type = raw.get("schema_type")
        if not isinstance(schema_type, str) or not schema_type:
            raise CeilingError(
                f"ceiling_item_missing_schema_type:index_{idx}",
                reason_code="opus_bad_output",
            )
        source_turn_ids = raw.get("source_turn_ids") or []
        if not isinstance(source_turn_ids, list):
            raise CeilingError(
                f"ceiling_item_bad_source_turn_ids:index_{idx}",
                reason_code="opus_bad_output",
            )
        item_id = raw.get("item_id") or f"ceil-{idx:04d}"
        out.append(
            {
                "item_id": str(item_id),
                "schema_type": schema_type,
                "source_turn_ids": [str(t) for t in source_turn_ids],
                "source_text": str(raw.get("source_text") or ""),
                "payload": raw.get("payload")
                if isinstance(raw.get("payload"), dict)
                else {},
            }
        )
    out.sort(key=lambda i: (i["schema_type"], i["item_id"]))
    return out


def extract_ceiling(
    transcript_text: str,
    transcript_id: str,
    *,
    opus_call: OpusCall | None = None,
) -> Artifact:
    """Produce the ``opus_ceiling`` artifact for one transcript.

    Raises ``CeilingError`` (fail-closed) on a bad transcript id, an
    unavailable model, or an unusable model response. Never returns an
    empty-but-successful ceiling.
    """
    if not _TRANSCRIPT_ID_RE.match(transcript_id or ""):
        raise CeilingError(
            f"invalid_transcript_id:{transcript_id!r}",
            reason_code="invalid_transcript_id",
        )
    call = opus_call or _real_opus_call
    try:
        raw_items = call(transcript_text)
    except CeilingError:
        raise
    except Exception as exc:  # noqa: BLE001 — any call failure fails closed
        raise CeilingError(
            f"opus_call_failed:{type(exc).__name__}:{exc}",
            reason_code="opus_unavailable",
        ) from exc
    if not isinstance(raw_items, list):
        raise CeilingError(
            "opus_call_returned_non_list", reason_code="opus_bad_output"
        )
    items = _normalise_items(raw_items)

    # per_type_counts is 0-filled for every gate-relevant type so the
    # gate never has to treat a missing key as ambiguous, plus any
    # extra types the model surfaced.
    per_type_counts: dict[str, int] = {t: 0 for t in CEILING_SCHEMA_TYPES}
    for item in items:
        per_type_counts[item["schema_type"]] = (
            per_type_counts.get(item["schema_type"], 0) + 1
        )

    payload = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "transcript_id": transcript_id,
        "model_id": OPUS_MODEL,
        "extracted_items": items,
        "per_type_counts": per_type_counts,
        "transcript_keyword_hits": transcript_keyword_hits(transcript_text),
    }
    return new_artifact(
        artifact_type=ARTIFACT_TYPE,
        payload=payload,
        trace_id=f"ceiling-{uuid.uuid4().hex[:16]}",
        status="draft",
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "CeilingError",
    "extract_ceiling",
]
