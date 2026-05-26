"""Phase Y.5 — correction miner.

Reads ONE ``false_negative_set`` artifact, groups the misses into
clusters, and for every cluster big enough to be a *pattern* asks Opus
for a single additive prompt fragment that would have caught them. One
``correction_candidate`` (schema 1.1.0, ``candidate_source: "miner"``)
is emitted per qualifying cluster.

Pattern signature (documented here because the contract depends on it
being stable):

    pattern_signature = sha256(canonical_json({
        "speaker_role":     <ceiling_payload.speaker_role|speaker, or None>,
        "preceding_marker": <first whitespace-token of source_text>,
        "length_bucket":    len(source_text) // 50,
    }))

``preceding_marker`` is defined as the first token of the false
negative's own ``source_text``. The phase brief describes it as "first
word of the preceding turn", but a ``false_negative_set`` deliberately
does not carry transcript context (it is a pure projection of the
comparison). Using the FN's own leading token is the deterministic,
context-free proxy; this is an intentional, documented approximation,
not an oversight.

Distinguishable outcomes (the critical contract — never a silent zero):

* ``>=1`` qualifying cluster      -> candidates returned, no finding.
* 0 clusters >= threshold         -> ONE ``info`` finding
                                     ``miner_no_clusters_above_threshold``
                                     with ``context={fn_count,
                                     cluster_count, threshold}``.
* Opus error / bad output /
  schema-invalid candidate         -> ONE ``halt`` finding
                                     ``miner_failed`` carrying the
                                     exception class + message;
                                     ``blocked=True``; NO candidates.

The miner never returns "0 candidates and no finding" — that ambiguous
state is exactly what the outcome contract exists to prevent.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from ..artifacts import Artifact, new_artifact
from ..data_lake.serialize import canonical_json
from .llm_haiku import HAIKU_EXTRACTION_SYSTEM_PROMPT

CLUSTER_THRESHOLD = 3
SCHEMA_VERSION = "1.1.0"
ARTIFACT_TYPE = "correction_candidate"

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "correction_candidate.schema.json"
)

# (cluster_fn_texts, current_prompt) -> proposed additive fragment.
OpusProposalCall = Callable[[list[str], str], str]
Clock = Callable[[], str]


@dataclass
class MinerFinding:
    """Lightweight finding. Not a ``health.Finding`` on purpose:
    ``health.ALL_FINDING_CODES`` is a closed enum kept in lockstep with
    a governed schema, and adding miner codes to it is a separate,
    explicit slice. This carries exactly what the outcome contract
    needs (severity + code + context) and nothing else."""

    severity: str  # "info" | "halt"
    code: str
    context: dict


@dataclass
class MinerResult:
    candidates: list[Artifact] = field(default_factory=list)
    findings: list[MinerFinding] = field(default_factory=list)
    blocked: bool = False


def _default_clock() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _default_opus_call(fn_texts: list[str], current_prompt: str) -> str:
    """Single real Opus call. Fail-closed; never returns ``""``.

    Raises on a missing key or any SDK error so the caller records a
    ``miner_failed`` halt — there is no default-to-empty path.
    """
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not api_key.strip():
        raise RuntimeError("missing_credentials:ANTHROPIC_API_KEY")
    from anthropic import Anthropic

    from .llm_opus import OPUS_MODEL

    joined = "\n".join(f"- {t}" for t in fn_texts)
    ask = (
        "The following extraction targets were MISSED by the current "
        "prompt. Propose ONE concrete ADDITIVE instruction (<= 100 "
        "words) to append to the prompt that would have caught them. "
        "Return only the instruction text.\n\n"
        f"CURRENT PROMPT:\n{current_prompt}\n\nMISSED ITEMS:\n{joined}"
    )
    client = Anthropic(api_key=api_key)
    # Stream to stay under the SDK's 10-minute non-streaming cap.
    with client.messages.stream(
        model=OPUS_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": ask}],
    ) as stream:
        response = stream.get_final_message()
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    out = "\n".join(parts).strip()
    if not out:
        raise RuntimeError("opus_proposal_empty")
    return out


def _pattern_signature(fn: dict) -> str:
    ceiling_payload = fn.get("ceiling_payload") or {}
    speaker_role = (
        ceiling_payload.get("speaker_role")
        or ceiling_payload.get("speaker")
        or None
    )
    source_text = str(fn.get("source_text") or "")
    tokens = source_text.split()
    preceding_marker = tokens[0] if tokens else ""
    signature_obj = {
        "speaker_role": speaker_role,
        "preceding_marker": preceding_marker,
        "length_bucket": len(source_text) // 50,
    }
    return hashlib.sha256(
        canonical_json(signature_obj).encode("utf-8")
    ).hexdigest()


def _candidate_id(fn_set_id: str, schema_type: str, sig: str) -> str:
    return hashlib.sha256(
        f"{fn_set_id}|{schema_type}|{sig}".encode("utf-8")
    ).hexdigest()[:32]


def _validate_candidate(candidate_payload: dict) -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(candidate_payload, schema)


def mine_corrections(
    false_negative_set: Artifact,
    *,
    current_prompt: str | None = None,
    opus_call: OpusProposalCall | None = None,
    clock: Clock | None = None,
) -> MinerResult:
    """Mine ``correction_candidate`` artifacts from a false-negative set."""
    result = MinerResult()
    payload = false_negative_set.payload or {}
    fns = payload.get("false_negatives")
    if not isinstance(fns, list):
        result.blocked = True
        result.findings.append(
            MinerFinding(
                severity="halt",
                code="miner_failed",
                context={
                    "exception_class": "ValueError",
                    "message": "false_negative_set has no false_negatives list",
                },
            )
        )
        return result

    transcript_id = str(payload.get("transcript_id") or "")
    fn_set_id = false_negative_set.artifact_id
    prompt = (
        current_prompt
        if current_prompt is not None
        else HAIKU_EXTRACTION_SYSTEM_PROMPT
    )
    call = opus_call or _default_opus_call
    now = (clock or _default_clock)()

    # Cluster by (schema_type, pattern_signature), deterministically.
    clusters: dict[tuple[str, str], list[dict]] = {}
    for fn in fns:
        key = (str(fn.get("schema_type")), _pattern_signature(fn))
        clusters.setdefault(key, []).append(fn)

    qualifying = sorted(
        (k for k, v in clusters.items() if len(v) >= CLUSTER_THRESHOLD)
    )

    if not qualifying:
        result.findings.append(
            MinerFinding(
                severity="info",
                code="miner_no_clusters_above_threshold",
                context={
                    "fn_count": len(fns),
                    "cluster_count": len(clusters),
                    "threshold": CLUSTER_THRESHOLD,
                },
            )
        )
        return result

    for schema_type, sig in qualifying:
        cluster = clusters[(schema_type, sig)]
        fn_texts = [str(f.get("source_text") or "") for f in cluster]
        try:
            addition = call(fn_texts, prompt)
            if not isinstance(addition, str) or not addition.strip():
                raise RuntimeError("opus_proposal_empty_or_non_string")
            candidate_payload = {
                "artifact_type": ARTIFACT_TYPE,
                "schema_version": SCHEMA_VERSION,
                "correction_candidate_id": _candidate_id(
                    fn_set_id, schema_type, sig
                ),
                "source_id": transcript_id,
                "created_at": now,
                "status": "pending",
                "candidate_source": "miner",
                "target_transcript_id": transcript_id,
                "schema_type": schema_type,
                "pattern_signature": sig,
                "cluster_size": len(cluster),
                "false_negative_set_artifact_id": fn_set_id,
                "proposed_prompt_addition": addition.strip(),
            }
            _validate_candidate(candidate_payload)
        except Exception as exc:  # noqa: BLE001 — any failure -> halt
            result.blocked = True
            result.candidates = []
            result.findings = [
                MinerFinding(
                    severity="halt",
                    code="miner_failed",
                    context={
                        "exception_class": type(exc).__name__,
                        "message": str(exc),
                        "schema_type": schema_type,
                        "pattern_signature": sig,
                    },
                )
            ]
            return result
        result.candidates.append(
            new_artifact(
                artifact_type=ARTIFACT_TYPE,
                payload=candidate_payload,
                trace_id=false_negative_set.trace_id,
                status="draft",
                input_refs=[fn_set_id],
            )
        )
    return result


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "CLUSTER_THRESHOLD",
    "MinerFinding",
    "MinerResult",
    "mine_corrections",
]
