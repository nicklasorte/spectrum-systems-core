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
  (full meeting_minutes.schema.json validation, before the write)
- ``llm_extraction_nonempty_required``    reason
  ``extraction_empty_with_content`` and/or
  ``extraction_empty_with_content:proxy_types``
- ``extraction_within_source_required``   reason ``extraction_not_in_source``
  (legacy arrays — string AND object form — plus commitment_text /
  risk_text / technical_parameters.value)
- ``extraction_vs_human_minutes_coverage``observe-only; numeric
  ``coverage_percent`` + ``threshold`` fields (never blocks).
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..artifacts import Artifact, new_artifact
from ..validation import (
    ArtifactValidationError,
    SchemaNotFoundError,
    validate_artifact,
)

# ---- eval_type constants (stable, machine-grepable) ----------------------

STRICT_SCHEMA_EVAL_TYPE = "llm_extraction_strict_schema"
NONEMPTY_EVAL_TYPE = "llm_extraction_nonempty_required"
WITHIN_SOURCE_EVAL_TYPE = "extraction_within_source_required"
GT_COVERAGE_EVAL_TYPE = "extraction_vs_human_minutes_coverage"

# ---- reason codes -------------------------------------------------------

SCHEMA_VIOLATION = "schema_violation"
EXTRACTION_EMPTY_WITH_CONTENT = "extraction_empty_with_content"
# Step 5: even when the three legacy arrays are non-empty, a
# content-bearing transcript must yield at least one of the
# fact-bearing proxy arrays. Distinct suffix so the operator can tell
# the legacy-empty case from the proxy-empty case in eval_history.
EXTRACTION_EMPTY_PROXY_TYPES = "extraction_empty_with_content:proxy_types"
EXTRACTION_NOT_IN_SOURCE = "extraction_not_in_source"
NO_GT_PAIRS = "no_gt_pairs"

# The flat-artifact projection meeting_minutes.schema.json validates:
# ``{"artifact_type": <type>, **payload}`` — exactly the shape
# tests/test_meeting_minutes_schema.py validates and the documented
# pattern meeting_extraction.schema.json uses.
_MEETING_MINUTES_TYPE = "meeting_minutes"

# Step 5 proxy arrays. At least one must be non-empty on a
# content-bearing transcript (fact-bearing extraction must not be
# entirely missing the technical record).
_PROXY_NONEMPTY_ARRAYS = (
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
)

# Step 4: structured arrays whose listed text field must appear in the
# transcript (same normalized-substring algorithm as the legacy
# within-source check). Deliberately excludes attendees / topics /
# scheduled_events / regulatory_references / cross_references /
# named_artifacts — those legitimately carry paraphrased or
# proper-noun text per the PR #123 design.
_WITHIN_SOURCE_STRUCTURED = (
    ("commitments", "commitment_text"),
    ("risks", "risk_text"),
    ("technical_parameters", "value"),
)

# Primary text field for the structured (object) form of each legacy
# required array. Used so an item switched from a plain string to the
# schema's ``oneOf`` object form cannot bypass the within-source gate.
_LEGACY_OBJECT_TEXT_KEY = {
    "decisions": "text",
    "action_items": "action",
    "open_questions": "question_text",
}

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


# Invisible / zero-width format characters (Unicode category Cf and the
# soft hyphen). They carry NO visible meaning but break a naive
# substring check: a transcription or copy-paste pipeline can splice one
# between words — notably between stutter-repeated words like
# ``that<ZWSP>that`` / ``our<ZWSP>our`` — so text that is verbatim to a
# human reader is not a byte substring of the raw transcript. ``\s``
# does NOT match these (they are format chars, not whitespace), so the
# whitespace collapse below cannot remove them; strip them explicitly.
#   U+00AD SOFT HYPHEN          U+200B ZERO WIDTH SPACE
#   U+200C ZERO WIDTH NON-JOINER  U+200D ZERO WIDTH JOINER
#   U+2060 WORD JOINER          U+FEFF ZERO WIDTH NO-BREAK SPACE (BOM)
_ZERO_WIDTH_RE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")


def _normalize(text: str) -> str:
    """Lowercase, drop invisible zero-width chars, collapse all
    whitespace runs to a single space, strip.

    This is THE match algorithm for both the within-source eval and the
    GT-coverage eval. Defined once so the two cannot drift.

    Hardening (non-weakening): Unicode NFKC folds compatibility-
    equivalent forms (non-breaking / narrow spaces, full-width digits,
    ligatures) to their canonical form, then zero-width / soft-hyphen
    format characters are deleted. Both steps only neutralise characters
    a human reading the transcript treats as identical. Neither lets a
    semantically different (paraphrased) extraction pass: a dropped
    word, substituted token, or dropped punctuation still fails the
    substring check, so the within-source gate keeps its full strength.
    """
    folded = unicodedata.normalize("NFKC", text or "")
    folded = _ZERO_WIDTH_RE.sub("", folded)
    return re.sub(r"\s+", " ", folded).strip().lower()


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
    """Yield ``(array_key, item_text)`` for every grounded text in the
    three required arrays.

    A string item yields its text. An object item (the schema
    ``oneOf`` form for action_items / open_questions, and the
    Step 6 structured-decision form) yields its primary text field
    (``decisions.text`` / ``action_items.action`` /
    ``open_questions.question_text``) so switching an item from a
    string to an object cannot bypass the within-source gate. Non-list
    arrays and unrecognised item shapes are skipped — the strict-schema
    eval fails closed on those, so this function stays robust and never
    raises."""
    out: list[tuple[str, str]] = []
    for key in _REQUIRED_ARRAYS:
        arr = payload.get(key)
        if not isinstance(arr, list):
            continue
        text_key = _LEGACY_OBJECT_TEXT_KEY.get(key)
        for item in arr:
            if isinstance(item, str) and item.strip():
                out.append((key, item))
            elif isinstance(item, dict) and text_key:
                val = item.get(text_key)
                if isinstance(val, str) and val.strip():
                    out.append((key, val))
    return out


def _iter_structured_source_texts(
    payload: dict[str, Any]
) -> list[tuple[str, str]]:
    """Yield ``(array_key, text)`` for the Step 4 structured arrays
    whose listed text field must be grounded in the transcript.

    Robust by construction: non-list arrays, non-dict items and
    missing / non-string text fields are skipped (the strict-schema
    eval validates their shape and blocks closed on a violation, so a
    skip here can never let an ungrounded item through — the item is
    already blocked upstream)."""
    out: list[tuple[str, str]] = []
    for key, text_field in _WITHIN_SOURCE_STRUCTURED:
        arr = payload.get(key)
        if not isinstance(arr, list):
            continue
        for item in arr:
            if not isinstance(item, dict):
                continue
            val = item.get(text_field)
            if isinstance(val, str) and val.strip():
                out.append((key, val))
    return out


# ---- Step 2 gate: strict output schema ---------------------------------


def run_llm_strict_schema_eval(artifact: Artifact) -> Artifact:
    """Strict schema gate for the LLM extraction payload.

    This is the gate Step 3 specifies: it validates the assembled
    payload against ``schemas/meeting_minutes.schema.json`` and fails
    closed with ``schema_violation`` on any mismatch. It runs as an
    eval inside the governed loop, BEFORE the control decision and the
    promotion gate, so a schema-violating payload is never promoted and
    therefore never written under ``processed/`` — validation strictly
    precedes the artifact write.

    Two layers, fail-closed:

    1. Cheap structural pre-checks emit precise, stable reason codes
       (``payload_not_object``, ``missing_array:<k>``,
       ``not_a_list:<k>``) so the operator and the existing
       rejection-path tests get an exact pointer.
    2. The authoritative check: the WHOLE flat artifact
       ``{"artifact_type": <type>, **payload}`` is validated against
       the real JSON Schema. This is what carries the nine PR #123
       structured arrays and the Step 6 ``stakeholders`` /
       ``confidence`` fields — the legacy ``list[str]`` forms and the
       ``oneOf`` object forms both validate (schema additivity); an
       explicit ``null`` array, a malformed structured item, an
       out-of-enum value, or an unknown key all fail
       ``schema_violation`` → control blocks → unpromoted.

    Empty arrays are valid (the constitution permits an empty, faithful
    extraction). The eval never raises: any unexpected validator error
    is itself a fail-closed ``schema_violation``.
    """
    payload = artifact.payload
    if not isinstance(payload, dict):
        return _eval_result(
            STRICT_SCHEMA_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=[f"{SCHEMA_VIOLATION}:payload_not_object"],
        )

    reasons: list[str] = []
    for key in _REQUIRED_ARRAYS:
        if key not in payload:
            reasons.append(f"{SCHEMA_VIOLATION}:missing_array:{key}")
            continue
        if not isinstance(payload[key], list):
            reasons.append(f"{SCHEMA_VIOLATION}:not_a_list:{key}")
    if reasons:
        # A required array is missing or not a list. The flat-schema
        # check below would also reject this, but the precise codes
        # above are a better operator pointer — return them directly.
        return _eval_result(
            STRICT_SCHEMA_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=reasons,
        )

    flat = {"artifact_type": artifact.artifact_type, **payload}
    try:
        validate_artifact(flat, _MEETING_MINUTES_TYPE)
    except ArtifactValidationError as exc:
        return _eval_result(
            STRICT_SCHEMA_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=[f"{SCHEMA_VIOLATION}:{exc}"],
        )
    except SchemaNotFoundError:
        # The schema file is part of the repo; its absence is a
        # fail-closed condition, never a silent pass.
        return _eval_result(
            STRICT_SCHEMA_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=[f"{SCHEMA_VIOLATION}:no_schema"],
        )
    except Exception as exc:  # noqa: BLE001 — eval never raises
        return _eval_result(
            STRICT_SCHEMA_EVAL_TYPE,
            artifact,
            passed=False,
            reason_codes=[
                f"{SCHEMA_VIOLATION}:validator_error:{type(exc).__name__}"
            ],
        )

    return _eval_result(
        STRICT_SCHEMA_EVAL_TYPE,
        artifact,
        passed=True,
        reason_codes=[],
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

    proxy_total = 0
    for key in _PROXY_NONEMPTY_ARRAYS:
        arr = payload.get(key)
        if isinstance(arr, list):
            proxy_total += len(arr)

    if not _content_present(transcript_text):
        # Empty-on-empty (short / procedural transcript) is allowed —
        # the constitution's "never invent" rule, not a failure.
        return _eval_result(
            NONEMPTY_EVAL_TYPE, artifact, passed=True, reason_codes=[]
        )

    reasons: list[str] = []
    if combined == 0:
        reasons.append(EXTRACTION_EMPTY_WITH_CONTENT)
    if proxy_total == 0:
        # Step 5: a content-bearing transcript records a technical /
        # artifact / scheduling fact somewhere — at least one of
        # technical_parameters / named_artifacts / scheduled_events
        # must be non-empty. An all-empty trio is a silent
        # under-extraction the legacy three arrays cannot catch.
        reasons.append(EXTRACTION_EMPTY_PROXY_TYPES)

    return _eval_result(
        NONEMPTY_EVAL_TYPE,
        artifact,
        passed=not reasons,
        reason_codes=reasons,
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
    # Legacy required arrays (string OR object form) PLUS the Step 4
    # structured arrays (commitment_text / risk_text /
    # technical_parameters.value). All share the one binding
    # normalized-substring algorithm — paraphrase-tolerant arrays
    # (attendees, topics, scheduled_events, regulatory_references,
    # cross_references, named_artifacts) are deliberately NOT here.
    items = _iter_item_texts(payload) + _iter_structured_source_texts(
        payload
    )

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
    "EXTRACTION_EMPTY_PROXY_TYPES",
    "EXTRACTION_NOT_IN_SOURCE",
    "NO_GT_PAIRS",
    "GT_COVERAGE_THRESHOLD",
    "MIN_CONTENT_CHARS",
    "run_llm_strict_schema_eval",
    "run_llm_nonempty_eval",
    "run_llm_within_source_eval",
    "run_llm_gt_coverage_eval",
]
