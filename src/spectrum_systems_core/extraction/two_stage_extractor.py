"""Phase R.1: two-stage extraction — candidate -> normalize.

Stage 1 produces *candidates* — items the extractor model emits along
with a ``candidate_evidence`` quote pinning the claim to the transcript.
Stage 2 is a normalization pass: a second model call (same model, same
backoff envelope) re-reads the chunk text, verifies the
``candidate_evidence`` actually appears in the source, and either
*confirms* or *rejects* each candidate. Only confirmed items become
canonical typed_extraction output.

Rollback: setting ``TWO_STAGE_EXTRACTION_ENABLED=false`` short-circuits
``normalize_candidates`` so callers can disable the second model call
without code changes. The bypass returns every candidate as confirmed
(legacy behaviour).

Cost note: stage 2 is a second model call per chunk. The normalize
prompt is "confirm or reject" only -- no new generation -- so token
output is small, but the input includes both the candidate list and the
source text. Document this 2x increase in the PR description.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ._failure_artifacts import emit_empty_result
from ._chunk_counters import ChunkCounters


_LOG = logging.getLogger(__name__)


TWO_STAGE_EXTRACTION_ENABLED_ENV: str = "TWO_STAGE_EXTRACTION_ENABLED"
_DISABLED_VALUES: frozenset = frozenset({"false", "0", "no", "off"})

# Item status emitted by the normalize stage.
STATUS_CONFIRMED: str = "confirmed"
STATUS_REJECTED: str = "rejected"
ALLOWED_STATUSES: tuple = (STATUS_CONFIRMED, STATUS_REJECTED)


def two_stage_enabled() -> bool:
    raw = os.environ.get(TWO_STAGE_EXTRACTION_ENABLED_ENV, "").strip().lower()
    if raw in _DISABLED_VALUES:
        return False
    return True


def build_candidate_prompt_block() -> str:
    """The text block appended to every stage-1 extractor prompt.

    Adds the candidate_evidence requirement. Callers concatenate this
    onto the existing extractor prompt.
    """
    return (
        "\n\nFor each item you emit, include a `candidate_evidence` field "
        "quoting the exact phrase(s) from the transcript that support the "
        "extraction (maximum 50 words). If you cannot find supporting text "
        "in the chunk, do not emit the item.\n"
    )


def build_normalize_prompt(
    candidates: List[Dict[str, Any]],
    chunk_text: str,
) -> str:
    """Stage-2 prompt body.

    The model is asked to read the source chunk and either confirm or
    reject each candidate. It MUST NOT add new items -- that is the
    contract that keeps stage 2 cheap (confirm/reject only).
    """
    lines: List[str] = []
    for idx, c in enumerate(candidates):
        ev = c.get("candidate_evidence") or ""
        text = c.get("text") or c.get("decision_text") or c.get("claim_text") or c.get("action") or ""
        lines.append(
            f"  - candidate_index: {idx}\n"
            f"    text: {text!r}\n"
            f"    candidate_evidence: {ev!r}"
        )
    candidates_yaml = "\n".join(lines) if lines else "  (empty)"
    return (
        "You are reviewing extraction candidates produced by a previous "
        "model pass. For each candidate, decide whether the "
        "`candidate_evidence` quote actually appears in the SOURCE CHUNK "
        "below and supports the candidate's claim.\n\n"
        "SOURCE CHUNK:\n"
        "```\n"
        f"{chunk_text}\n"
        "```\n\n"
        "CANDIDATES:\n"
        f"{candidates_yaml}\n\n"
        "Rules:\n"
        "1. If the evidence is present in the chunk AND supports the claim: "
        "output {\"candidate_index\": <i>, \"status\": \"confirmed\"}.\n"
        "2. If the evidence is absent OR does not support the claim: "
        "output {\"candidate_index\": <i>, \"status\": \"rejected\", "
        "\"rejection_reason\": \"<short reason>\"}.\n"
        "3. Do NOT add new items. Confirm or reject only.\n\n"
        "Return JSON only:\n"
        "{\n"
        "  \"normalized\": [\n"
        "    {\"candidate_index\": 0, \"status\": \"confirmed\"},\n"
        "    {\"candidate_index\": 1, \"status\": \"rejected\", "
        "\"rejection_reason\": \"...\"}\n"
        "  ]\n"
        "}\n"
    )


def parse_normalize_response(
    response: Dict[str, Any],
    candidate_count: int,
) -> List[Dict[str, Any]]:
    """Parse the stage-2 response into a fixed-length list of decisions.

    Output index ``i`` is the decision for ``candidates[i]``. Indices not
    present in the response default to rejected with reason
    ``"absent_from_normalize_response"`` so missing entries cannot
    silently auto-confirm (fail-closed).
    """
    decisions: List[Dict[str, Any]] = [
        {"status": STATUS_REJECTED,
         "rejection_reason": "absent_from_normalize_response"}
        for _ in range(candidate_count)
    ]
    raw = response.get("normalized") if isinstance(response, dict) else None
    if not isinstance(raw, list):
        return decisions
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("candidate_index")
        if not isinstance(idx, int) or idx < 0 or idx >= candidate_count:
            continue
        status = entry.get("status")
        if status not in ALLOWED_STATUSES:
            # Unrecognised status defaults to reject (fail-closed).
            decisions[idx] = {
                "status": STATUS_REJECTED,
                "rejection_reason": f"unrecognised_status:{status!r}",
            }
            continue
        if status == STATUS_CONFIRMED:
            decisions[idx] = {"status": STATUS_CONFIRMED}
        else:
            reason = entry.get("rejection_reason") or ""
            decisions[idx] = {
                "status": STATUS_REJECTED,
                "rejection_reason": str(reason)[:200] or "rejected_no_reason",
            }
    return decisions


def normalize_candidates(
    candidates: List[Dict[str, Any]],
    chunk_text: str,
    *,
    api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run stage 2 on a list of candidates.

    Returns ``(confirmed, rejected)``. ``confirmed`` items carry a
    ``normalize_status="confirmed"`` annotation; ``rejected`` items
    carry both ``normalize_status="rejected"`` and a
    ``rejection_reason``. Callers feed ``confirmed`` into the canonical
    extraction artifact and ``rejected`` into the staging artifact for
    forensic review.

    When ``TWO_STAGE_EXTRACTION_ENABLED=false`` is set, the function
    returns ``(candidates_copy, [])`` -- the legacy single-stage
    behaviour. Callers therefore do not need to branch on the env var.

    The model call goes through ``api_caller``; the runner is expected
    to inject a Haiku/Sonnet-shaped callable already wrapped in
    ``call_with_backoff``. A None ``api_caller`` (offline/test default)
    rejects every candidate so the no-API behaviour is fail-closed,
    NOT auto-confirm.
    """
    confirmed: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    if not candidates:
        return confirmed, rejected

    if not two_stage_enabled():
        # Bypass: legacy behaviour -- treat every candidate as confirmed.
        for c in candidates:
            entry = dict(c)
            entry["normalize_status"] = STATUS_CONFIRMED
            entry["normalize_bypass"] = True
            confirmed.append(entry)
        return confirmed, rejected

    if api_caller is None:
        # Fail-closed: no model available -> reject everything. Callers
        # must inject a caller to enable two-stage; otherwise stage 1
        # alone cannot ship items.
        for c in candidates:
            entry = dict(c)
            entry["normalize_status"] = STATUS_REJECTED
            entry["rejection_reason"] = "no_normalize_api_caller"
            rejected.append(entry)
        return confirmed, rejected

    prompt = build_normalize_prompt(candidates, chunk_text)
    try:
        response = api_caller(prompt)
    except Exception as exc:  # never raise out
        _LOG.warning(
            "two_stage_normalize_api_failed: %s: %s",
            type(exc).__name__, exc,
        )
        # On API failure: reject everything. Fail-closed.
        for c in candidates:
            entry = dict(c)
            entry["normalize_status"] = STATUS_REJECTED
            entry["rejection_reason"] = f"normalize_api_error:{type(exc).__name__}"
            rejected.append(entry)
        return confirmed, rejected

    if not isinstance(response, dict):
        response = {}

    decisions = parse_normalize_response(response, len(candidates))
    for cand, decision in zip(candidates, decisions):
        entry = dict(cand)
        if decision["status"] == STATUS_CONFIRMED:
            entry["normalize_status"] = STATUS_CONFIRMED
            confirmed.append(entry)
        else:
            entry["normalize_status"] = STATUS_REJECTED
            entry["rejection_reason"] = decision.get("rejection_reason") or "rejected"
            rejected.append(entry)
    return confirmed, rejected


def emit_all_rejected_failure(
    *,
    counters: ChunkCounters,
    chunk_id: str,
    source_id: str,
    extraction_run_id: Optional[str],
    sdl_root: Optional[Any],
    rejection_summary: str = "",
) -> Dict[str, Any]:
    """All candidates were rejected: emit empty_result + bump ``other``.

    Phase R.1 requires this path so the orchestrator can attribute a
    blocked chunk to "stage 2 rejected every candidate" rather than
    silently writing zero items. Re-uses the existing
    ``typed_extraction_empty_result`` artifact_type so we do not
    introduce a new schema for what is logically the same outcome.
    """
    detail = "two_stage_normalize_all_rejected"
    if rejection_summary:
        detail = f"{detail}:{rejection_summary[:200]}"
    return emit_empty_result(
        counters,
        chunk_id=chunk_id,
        source_id=source_id,
        component="two_stage_extractor",
        detail=detail,
        extraction_run_id=extraction_run_id,
        sdl_root=sdl_root,
    )


__all__ = [
    "ALLOWED_STATUSES",
    "STATUS_CONFIRMED",
    "STATUS_REJECTED",
    "TWO_STAGE_EXTRACTION_ENABLED_ENV",
    "build_candidate_prompt_block",
    "build_normalize_prompt",
    "emit_all_rejected_failure",
    "normalize_candidates",
    "parse_normalize_response",
    "two_stage_enabled",
]
