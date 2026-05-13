"""Fixture factories for integration contract tests.

These factories produce artifacts in the EXACT format the live pipeline
writes them. They call the real writer (``ExtractionMerger.merge``)
instead of hand-rolling dicts so that if the writer changes its output
format, tests that depend on these fixtures change automatically and
catch the drift.

Rule (CLAUDE.md, Integration test requirement): every factory function
in this module MUST call the actual writer for the artifact it produces.
Hand-rolled dicts here defeat the entire point of the integration layer.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List, Optional

from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _default_decisions() -> List[Dict[str, Any]]:
    """Minimal valid decisions covering all three target outcome types.

    Each decision carries every field the meeting_extraction schema
    requires on a decision item (``decision_text``, ``decision_type``,
    ``stakeholders``, ``rationale``, ``source_turn_ids``,
    ``source_turn_validation``, ``confidence``) plus the optional
    schema-declared fields ``decision_outcome``, ``speaker``, and
    ``grounding_verified`` that ``select_few_shot_examples.py`` reads.

    ``regulatory_verb`` and ``source_text`` are intentionally omitted:
    they are NOT declared on the meeting_extraction schema (the schema
    is closed via ``additionalProperties: false``) and the script
    reads them via ``.get(...)`` with fallback so their absence is
    safe.
    """
    return [
        {
            "decision_text": "NTIA approved the 7 GHz downlink threshold.",
            "decision_type": "approved",
            "decision_outcome": "approval",
            "stakeholders": ["NTIA"],
            "rationale": "Threshold consistent with prior comment cycle.",
            "speaker": "NTIA Lead",
            "confidence": 0.92,
            "grounding_verified": True,
            "source_turn_ids": ["real-turn-001"],
            "source_turn_validation": "verified",
        },
        {
            "decision_text": "Deferred aggregate interference methodology.",
            "decision_type": "deferred",
            "decision_outcome": "deferral",
            "stakeholders": ["Chair"],
            "rationale": None,
            "speaker": "Chair Smith",
            "confidence": 0.88,
            "grounding_verified": True,
            "source_turn_ids": ["real-turn-010"],
            "source_turn_validation": "verified",
        },
        {
            "decision_text": "DoD required to submit revised ERP values.",
            "decision_type": "action_required",
            "decision_outcome": "action_required",
            "stakeholders": ["DoD"],
            "rationale": "Outstanding requirement from prior session.",
            "speaker": "Chair Smith",
            "confidence": 0.85,
            "grounding_verified": True,
            "source_turn_ids": ["real-turn-020"],
            "source_turn_validation": "verified",
        },
    ]


def make_meeting_extraction_artifact(
    source_artifact_id: str,
    decisions: Optional[List[Dict[str, Any]]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
    action_items: Optional[List[Dict[str, Any]]] = None,
    classifications: Optional[List[Dict[str, Any]]] = None,
    extraction_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce a meeting_extraction artifact via the real merger.

    Calls ``ExtractionMerger.merge`` so the output is byte-shape
    identical to what the live typed_extraction_runner writes. If the
    merger ever adds, renames, or removes a top-level field, every test
    that depends on this factory rebuilds against the new shape on the
    next run — no per-test dict edits required.

    Args:
      source_artifact_id: the UUID the runner resolved from
        ``source_record.json``. Tests that wire ``source_record.json``
        on disk MUST pass the same id here or the script's resolver
        won't match.
      decisions / claims / action_items: optional lists. ``decisions``
        defaults to one item per target outcome (``approval`` /
        ``deferral`` / ``action_required``); claims and action_items
        default to empty so the artifact stays minimal.
      classifications / extraction_run_id: optional. Default empty
        list and fresh UUID, both safe for tests that don't care about
        routing metrics.
    """
    merger = ExtractionMerger()
    return merger.merge(
        source_artifact_id=source_artifact_id,
        extraction_run_id=extraction_run_id or str(uuid.uuid4()),
        classifications=classifications or [],
        decisions=decisions if decisions is not None else _default_decisions(),
        claims=claims or [],
        action_items=action_items or [],
    )


def make_source_record(source_id: str, artifact_id: str) -> Dict[str, Any]:
    """Produce a source_record.json in the format the pipeline writes.

    The runner's resolver reads ``artifact_id`` from this file and uses
    it as the ``source_artifact_id`` of every downstream artifact. Tests
    that seed extraction artifacts MUST pass the SAME ``artifact_id``
    to ``make_meeting_extraction_artifact`` or the script's slug ->
    UUID lookup won't match.

    Conforms to ``schemas/source_record.schema.json`` (additionalProperties
    is false, so we include only declared fields).
    """
    return {
        "artifact_type": "source_record",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "source_id": source_id,
        "created_at": _now_iso(),
    }


def make_ground_truth_pair_from_decision(
    *,
    source_id: str,
    source_artifact_id: str,
    minutes_artifact_id: str,
    decision_text: str,
    decision_outcome: str = "approval",
    meeting_date: str = "2025-12-18",
    meeting_name: str = "Phase X2 follow-up GT pair fixture",
) -> Dict[str, Any]:
    """Produce a ``ground_truth_pair`` via the real writer.

    Calls ``scripts.generate_gt_pairs.build_pair`` so the output is
    byte-shape identical to what the live ``generate_gt_pairs.py``
    writes. If the writer ever renames a field, every test that
    depends on this factory rebuilds against the new shape on the
    next run — no per-test dict edits required.

    Importing the script as a module is safe: ``generate_gt_pairs.py``
    only adds ``scripts/`` to ``sys.path`` and pulls in
    ``_artifact_validator``; no I/O happens at import.
    """
    import sys as _sys

    scripts_dir = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
    )
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))

    import generate_gt_pairs  # type: ignore  # noqa: WPS433

    return generate_gt_pairs.build_pair(
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        minutes_artifact_id=minutes_artifact_id,
        meeting_date=meeting_date,
        meeting_name=meeting_name,
        decision={
            "decision_text": decision_text,
            "decision_outcome": decision_outcome,
        },
    )


def make_decision_few_shot_placeholder(
    extraction_type: str = "decision",
) -> Dict[str, Any]:
    """Produce the Phase V placeholder few-shot artifact.

    Mirrors the artifact that ships in the repo at
    ``data-lake/store/artifacts/evals/few_shot/decision_examples_v1.json``
    with ``verified: false`` placeholders. Tests use this as the seed
    state that ``scripts/select_few_shot_examples.py`` must overwrite
    with real decisions.
    """
    return {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": extraction_type,
        "verified": False,
        "created_at": _now_iso(),
        "examples": [
            {
                "example_id": "phase-v-placeholder-approval",
                "source_meeting_id": "phase-v-placeholder",
                "input_text": "placeholder",
                "expected_output": {"decision_outcome": "approval"},
                "verified": False,
                "verified_by": None,
                "verified_at": None,
            },
        ],
    }
