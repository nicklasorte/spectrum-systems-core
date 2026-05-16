"""Eval cases for the live-LLM meeting-minutes extraction path.

These four evals are the trust gates for the ``meeting_minutes_llm``
workflow. They are NOT wired into the global ``run_required_evals``
sequence — that would change the regex ``meeting_minutes`` path and the
golden / validate-and-baseline signals. They run only for the LLM
workflow, passed in via ``run_governed_loop(..., extra_evals=...)``, so
mutual exclusion is structural: the regex path never sees them and the
LLM path always does.

Every function returns an ``eval_result`` artifact and never raises
(fail-closed: a problem is a failed eval the control function blocks
on, never an exception that crashes the loop). Each uses a distinct
``eval_type`` and puts its machine-readable reason code in
``reason_codes`` so a gate reads a field on an artifact, never prose.

Eval map:

- ``llm_extraction_strict_schema``        reason ``schema_violation``
- ``llm_extraction_nonempty_required``    reason ``extraction_empty_with_content``
- ``extraction_within_source_required``   reason ``extraction_not_in_source``
- ``extraction_vs_human_minutes_coverage``observe-only; numeric
  ``coverage_percent`` + ``threshold`` fields (never blocks).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import Artifact, new_artifact

# ---- eval_type constants (stable, machine-grepable) ----------------------

STRICT_SCHEMA_EVAL_TYPE = "llm_extraction_strict_schema"
NONEMPTY_EVAL_TYPE = "llm_extraction_nonempty_required"
WITHIN_SOURCE_EVAL_TYPE = "extraction_within_source_required"
GT_COVERAGE_EVAL_TYPE = "extraction_vs_human_minutes_coverage"

# ---- reason codes -------------------------------------------------------

SCHEMA_VIOLATION = "schema_violation"
EXTRACTION_EMPTY_WITH_CONTENT = "extraction_empty_with_content"
EXTRACTION_NOT_IN_SOURCE = "extraction_not_in_source"
NO_GT_PAIRS = "no_gt_pairs"

# The three arrays the strict LLM schema requires. The regex
# meeting_minutes extractor also emits exactly these (as ``[]`` when
# empty), so a legacy artifact run through this eval still passes —
# schema additivity holds.
_REQUIRED_ARRAYS = ("decisions", "action_items", "open_questions")

# "Content present" threshold for the nonempty eval. A transcript with
# real content is both long enough AND has speaker turns. A short
# procedural snippet is legitimately "no content" so an empty
# extraction is allowed (constitution: never invent).
MIN_CONTENT_CHARS = 400

# A speaker turn looks like ``Name: utterance`` — a short label, a
# colon, then content. Bounded label length so a sentence containing a
# colon mid-stream is not mistaken for a turn.
_SPEAKER_TURN_RE = re.compile(r"^[ \t]*[A-Za-z][\w .,'\-]{0,40}:[ \t]+\S")

# Observe-only threshold for the GT-coverage eval. 0.0 means it never
# blocks on the first run — it only reports. Written onto the
# eval_result payload AND into reason_codes so it is auditable in
# eval_history.jsonl (which projects reason_codes verbatim).
GT_COVERAGE_THRESHOLD = 0.0

# GT extraction_type -> meeting_minutes payload key. ``claim`` has no
# meeting_minutes bucket (minutes carry decisions/actions/questions, not
# free claims); claim pairs therefore count toward the denominator and
# are never matched. That is honest for an observe-only metric and is
# documented rather than silently dropped.
_GT_TYPE_TO_KEY = {
    "decision": "decisions",
    "action_item": "action_items",
}
_VALID_GT_TYPES = ("decision", "action_item", "claim")


def _eval_result(
    eval_type: str,
    target: Artifact,
    *,
    passed: bool,
    reason_codes: list[str],
    extra_payload: dict[str, Any] | None = None,
) -> Artifact:
    payload: dict[str, Any] = {
        "eval_type": eval_type,
        "target_artifact_id": target.artifact_id,
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "reason_codes": reason_codes,
    }
    if extra_payload:
        payload.update(extra_payload)
    return new_artifact(
        artifact_type="eval_result",
        payload=payload,
        trace_id=target.trace_id,
        status="evaluated",
        input_refs=[target.artifact_id],
    )


def _normalize(text: str) -> str:
    """Lowercase, collapse all whitespace runs to a single space, strip.

    This is THE match algorithm for both the within-source eval and the
    GT-coverage eval. Defined once so the two cannot drift.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _has_speaker_turns(transcript_text: str) -> bool:
    for line in (transcript_text or "").splitlines():
        if _SPEAKER_TURN_RE.match(line):
            return True
    return False


def _content_present(transcript_text: str) -> bool:
    return (
        len(transcript_text or "") > MIN_CONTENT_CHARS
        and _has_speaker_turns(transcript_text)
    )


def _iter_item_texts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Yield ``(array_key, item_text)`` for every string item in the
    three required arrays. Non-list arrays and non-string items are
    skipped here — the strict-schema eval is what fails on those, so
    this function stays robust and never raises."""
    out: list[tuple[str, str]] = []
    for key in _REQUIRED_ARRAYS:
        arr = payload.get(key)
        if not isinstance(arr, list):
            continue
        for item in arr:
            if isinstance(item, str) and item.strip():
                out.append((key, item))
    return out


# ---- Step 2 gate: strict output schema ---------------------------------


def run_llm_strict_schema_eval(artifact: Artifact) -> Artifact:
    """Strict shape gate for the LLM extraction payload.

    Requires: payload is an object that contains ``decisions``,
    ``action_items`` and ``open_questions``, each a list of strings.
    A raw string at the top level, a missing array, or a non-list /
    non-string-item array all fail with the ``schema_violation`` reason
    code → control blocks → artifact unpromoted. Empty arrays are
    valid (the constitution permits an empty, faithful extraction).
    """
    payload = artifact.payload
    reasons: list[str] = []
    if not isinstance(payload, dict):
        return _eval_result(
            STRICT_SCHEMA_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=[f"{SCHEMA_VIOLATION}:payload_not_object"],
        )
    for key in _REQUIRED_ARRAYS:
        if key not in payload:
            reasons.append(f"{SCHEMA_VIOLATION}:missing_array:{key}")
            continue
        value = payload[key]
        if not isinstance(value, list):
            reasons.append(f"{SCHEMA_VIOLATION}:not_a_list:{key}")
            continue
        for idx, item in enumerate(value):
            if not isinstance(item, str):
                reasons.append(
                    f"{SCHEMA_VIOLATION}:item_not_string:{key}[{idx}]"
                )
    return _eval_result(
        STRICT_SCHEMA_EVAL_TYPE,
        artifact,
        passed=not reasons,
        reason_codes=reasons,
    )


# ---- Step 4 gate: non-empty extraction when content present ------------


def run_llm_nonempty_eval(
    artifact: Artifact, transcript_text: str
) -> Artifact:
    """If the transcript has content, the extraction must not be empty.

    Content present := transcript longer than ``MIN_CONTENT_CHARS`` AND
    has speaker turns. When content is present, the combined length of
    decisions + action_items + open_questions must be > 0; otherwise
    the eval fails with ``extraction_empty_with_content`` and control
    blocks. Empty-on-empty (a short / procedural transcript) is allowed
    — that is the constitution's "never invent" rule, not a failure.
    """
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}
    combined = 0
    for key in _REQUIRED_ARRAYS:
        arr = payload.get(key)
        if isinstance(arr, list):
            combined += len(arr)

    if _content_present(transcript_text) and combined == 0:
        return _eval_result(
            NONEMPTY_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=[EXTRACTION_EMPTY_WITH_CONTENT],
        )
    return _eval_result(
        NONEMPTY_EVAL_TYPE, artifact, passed=True, reason_codes=[]
    )


# ---- Step 5 gate: within-source attribution ----------------------------


def run_llm_within_source_eval(
    artifact: Artifact, transcript_text: str
) -> Artifact:
    """Every extracted item must appear in the transcript.

    Match algorithm (binding, shared with GT-coverage): lowercase both
    sides, collapse whitespace runs to a single space, then substring
    check. Any item that is not a substring of the normalized
    transcript fails the eval with ``extraction_not_in_source`` →
    control blocks. The eval_result carries numeric ``items_in_source``
    and ``items_not_in_source`` counts so the measurement is a field on
    an artifact, never prose.
    """
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}
    haystack = _normalize(transcript_text)
    items = _iter_item_texts(payload)

    in_source = 0
    not_in_source = 0
    reasons: list[str] = []
    for key, text in items:
        if _normalize(text) and _normalize(text) in haystack:
            in_source += 1
        else:
            not_in_source += 1
            reasons.append(
                f"{EXTRACTION_NOT_IN_SOURCE}:{key}:{text[:60]}"
            )

    return _eval_result(
        WITHIN_SOURCE_EVAL_TYPE,
        artifact,
        passed=(not_in_source == 0),
        reason_codes=reasons,
        extra_payload={
            "items_in_source": in_source,
            "items_not_in_source": not_in_source,
        },
    )


# ---- Step 6 gate: coverage vs human GT pairs (observe-only) -------------


def _gt_pairs_path(lake_root: Path | str, source_id: str) -> Path:
    """Path to the human GT pairs JSONL.

    Mirrors ``scripts/create_human_gt_pairs._output_path`` exactly so
    the eval reads precisely what that writer produces (the data-lake
    contract's ``store/`` rooted layout, NOT the core
    ``processed_meeting_dir`` layout).
    """
    return (
        Path(lake_root)
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "ground_truth"
        / "human_minutes_gt_pairs.jsonl"
    )


def _load_gt_pairs(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    pairs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            pairs.append(obj)
    return pairs


def run_llm_gt_coverage_eval(
    artifact: Artifact,
    *,
    source_id: str | None,
    lake_root: Path | str | None,
) -> Artifact:
    """Observe-only coverage of extraction against human GT pairs.

    For each GT pair whose ``extraction_type`` is in
    {decision, action_item, claim}, the pair is "covered" when some
    extracted item of the matching kind contains the pair's
    ``ground_truth_text`` as a normalized substring (same algorithm as
    the within-source eval). ``coverage_percent`` is matched/total as a
    float (0.0 when there are no pairs to score).

    Threshold is ``GT_COVERAGE_THRESHOLD`` (0.0): this eval NEVER
    blocks on the first run — it reports. ``coverage_percent`` and
    ``threshold`` are numeric fields on the eval_result AND are echoed
    into ``reason_codes`` so they survive into ``eval_history.jsonl``
    (which projects reason_codes verbatim) for auditability.
    """
    payload = artifact.payload if isinstance(artifact.payload, dict) else {}

    pairs: list[dict[str, Any]] = []
    if source_id and lake_root is not None:
        pairs = _load_gt_pairs(_gt_pairs_path(lake_root, source_id))

    scored = 0
    matched = 0
    for pair in pairs:
        etype = pair.get("extraction_type")
        gt_text = pair.get("ground_truth_text")
        if etype not in _VALID_GT_TYPES or not isinstance(gt_text, str):
            continue
        scored += 1
        needle = _normalize(gt_text)
        if not needle:
            continue
        key = _GT_TYPE_TO_KEY.get(etype)
        bucket: list[str] = []
        if key is not None:
            arr = payload.get(key)
            if isinstance(arr, list):
                bucket = [x for x in arr if isinstance(x, str)]
        if any(needle in _normalize(x) for x in bucket):
            matched += 1

    coverage_percent = float(matched) / float(scored) if scored else 0.0

    reasons: list[str] = [
        f"coverage_percent:{coverage_percent}",
        f"coverage_threshold:{float(GT_COVERAGE_THRESHOLD)}",
    ]
    if not pairs:
        reasons.append(NO_GT_PAIRS)

    # Observe-only: threshold 0.0 and coverage is always >= 0.0, so the
    # eval always passes. It reports; it never blocks the first run.
    passed = coverage_percent >= GT_COVERAGE_THRESHOLD
    return _eval_result(
        GT_COVERAGE_EVAL_TYPE,
        artifact,
        passed=passed,
        reason_codes=reasons,
        extra_payload={
            "coverage_percent": coverage_percent,
            "threshold": float(GT_COVERAGE_THRESHOLD),
            "gt_pairs_scored": scored,
            "gt_pairs_matched": matched,
        },
    )


__all__ = [
    "STRICT_SCHEMA_EVAL_TYPE",
    "NONEMPTY_EVAL_TYPE",
    "WITHIN_SOURCE_EVAL_TYPE",
    "GT_COVERAGE_EVAL_TYPE",
    "SCHEMA_VIOLATION",
    "EXTRACTION_EMPTY_WITH_CONTENT",
    "EXTRACTION_NOT_IN_SOURCE",
    "NO_GT_PAIRS",
    "GT_COVERAGE_THRESHOLD",
    "MIN_CONTENT_CHARS",
    "run_llm_strict_schema_eval",
    "run_llm_nonempty_eval",
    "run_llm_within_source_eval",
    "run_llm_gt_coverage_eval",
]
