"""Health finding artifact model.

Every silent-failure detection writes one of these envelopes. The set
of finding codes is exhaustive: any new code must be added to
:data:`ALL_FINDING_CODES` and to the enum in
``schemas/health_finding.schema.json``. The two are kept in sync by
``tests/test_health_finding_enum_matches_schema``.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

SCHEMA_VERSION: str = "1.0.0"

Severity = str  # "halt" | "warn" | "info"

ALLOWED_SEVERITIES: frozenset[str] = frozenset({"halt", "warn", "info"})

ALL_FINDING_CODES: frozenset[str] = frozenset(
    {
        "upstream_failure_eval_blocked",
        "upstream_failure_eval_invalid",
        "eval_zero_cause_upstream",
        "eval_zero_cause_extraction",
        "feature_flag_missing",
        "feature_flag_disabled",
        "eval_pairs_excluded",
        "stale_artifact_in_bundle",
        "smoke_test_skipped",
        "model_registry_drift",
        "artifact_not_indexed",
        "no_prior_orchestration_artifact",
        # Phase S.2: emitted by ``confidence_field_check`` when a live
        # meeting_extraction artifact is missing the ``confidence`` field
        # on a decision or claim.
        "confidence_field_missing",
        # Phase O.2: emitted by ``blocked_chunk_text_check`` when a
        # legacy blocked_chunk artifact (v1.0.0) lacks chunk_text.
        # Informational only -- the scanner reports, it does not
        # force a migration.
        "blocked_artifact_missing_chunk_text",
        # Phase O.4: emitted by the eval runner when a ground_truth
        # pair lacks a source_id field. Informational; the eval still
        # produces a summary without per_source_metrics for that pair.
        "eval_pair_missing_source_id",
        # Phase O.5: emitted by the pipeline run diff tool when one
        # of the requested pipeline_run_summary artifacts is absent.
        # Used to halt the diff with a clear error rather than report
        # bogus zero-deltas.
        "pipeline_run_summary_missing",
        # Phase T.1: emitted by the binding validator when
        # BINDING_VALIDATOR_HALT_ENABLED=true and a decision had no
        # regulatory verb. severity halt (gated) or warn (default).
        "taxonomy_regulatory_verb_missing",
        # Phase T.2: emitted by the eval runner when a transcript's
        # spurious_add_rate exceeds the configured threshold. severity
        # warn -- blocks --set-baseline but does NOT halt the run.
        "spurious_add_rate_elevated",
        # Phase T.3: emitted when speaker attribution cannot resolve
        # a non-null speaker for a decision or claim. severity info.
        "speaker_attribution_missing",
        # Phase T.4: emitted when a chunk had to be split at a
        # non-turn boundary (no speaker boundary existed within the
        # MAX_CHUNK_CHARS budget). severity info.
        "chunk_split_mid_turn_detected",
        # Phase T.5: emitted when ground_truth pairs lack target_type
        # so per_type_metrics cannot be computed. severity info.
        "ground_truth_missing_type",
        # Phase T.6: emitted when more than LOW_CONF_RATE_LIMIT of
        # extracted items have confidence below LOW_CONF_THRESHOLD.
        # A correction_candidate artifact is also written. severity warn.
        "low_confidence_extraction",
        # Phase T.6: emitted by preflight when a correction_candidate
        # artifact's expires_at is in the past. severity info -- the
        # operator decides whether to act.
        "correction_candidate_expired",
        # Phase T.7: emitted when ATOMIC_DECOMPOSITION_ENABLED=true and
        # the second Haiku call produced zero atomic facts. severity warn.
        "atomic_decomposition_failed",
        # Phase V.3: emitted when the few-shot examples artifact is
        # absent. severity halt when FEW_SHOT_REQUIRED=true,
        # otherwise severity info.
        "few_shot_artifact_missing",
        # Phase V.3: emitted when the few-shot artifact is present
        # but contains zero examples with ``verified: true``. The
        # extraction continues with no examples injected.
        "few_shot_no_verified_examples",
        # Phase V.5: emitted when BINDING_TUPLE_ENABLED=true and the
        # second-pass JSON could not be parsed. severity warn; the
        # decision's binding_tuple is set to null and surrounding
        # extraction is unaffected.
        "binding_tuple_parse_failed",
        # Phase V.5: emitted when BINDING_TUPLE_ENABLED=true and a
        # decision with outcome approval/rejection had a null actor.
        # severity warn. Only fires when BINDING_TUPLE_ENABLED=true.
        "binding_tuple_incomplete",
        # Phase V.6: emitted when source_text contains a specific
        # band reference (numeric MHz/GHz/kHz) AND extracted_text
        # contains an OVERGENERALIZATION_MARKERS entry. severity warn.
        "scope_overgeneralization",
        # Phase W (integration wiring): emitted when more than 50%
        # of records scanned for the ``glossary_injection_summary``
        # rollup lack the ``glossary_terms_injected`` field. This
        # signals that the records are stale from before Phase W
        # wired per-chunk injection. Remediation: re-run extraction
        # with force=true. severity info -- never blocks the run.
        "glossary_injection_field_absent",
        # Phase P3-A T-1: emitted by the chunk-metadata gate when one
        # or more required fields (chunk_id/turn_id, speaker,
        # agenda_item_id) are missing or null on any chunk. Default
        # severity warn so a degraded transcript still produces an
        # extraction; promoted to halt when STRICT_CHUNK_METADATA=true.
        "chunk_metadata_contract_violation",
        # Phase P3-A T-1: emitted when any extracted item references a
        # chunk_id that does not exist in the live chunks.jsonl. The
        # validity eval is a fail-closed gate; this finding is a
        # rate-tracker surfaced into eval_summary. severity warn.
        "source_turn_orphan_detected",
        # Phase P3-A T-1: emitted when the model over-cites a tiny
        # cluster of chunks. severity info -- diagnostic signal only.
        "source_turn_low_diversity",
        # Phase P3-A T-3: emitted when one of the per-field population
        # rates (stakeholders / rationale / claim_type) falls below
        # RATE_WARN_THRESHOLD. severity warn; never halts. Prompts the
        # operator to tune the extraction prompt.
        "low_field_population_rate",
        # Phase X2.1: emitted when heuristic_agenda_detector scanned
        # the transcript and found zero agenda headers. severity info.
        # Context: source_id, lines_scanned. Caller assigns
        # agenda_item_id="unclassified" to every chunk -- the field
        # is never null.
        "agenda_detection_failed",
        # Phase X2.3: emitted when the LLM judge's per-decision
        # agreement rate against ground truth falls below
        # JUDGE_CALIBRATION_WARN_THRESHOLD (0.70). severity warn;
        # the operator should inspect the judge prompt and any rubric
        # drift before --set-baseline is run.
        "judge_calibration_low",
        # Phase X2.3: emitted when judge agreement falls below the
        # halt threshold (0.60). severity halt; the gate refuses
        # --set-baseline because the judge is unreliable as a
        # quality signal.
        "judge_calibration_failed",
        # Phase X2.3: emitted when the judge and the extraction model
        # belong to the same model family (e.g. both Claude); the
        # judge's verdict cannot be considered independent. severity
        # warn; the operator should pin the judge to a distinct
        # family if independence matters for their use-case.
        "judge_same_family",
        # Phase X2.3: emitted when JUDGE_STABILITY_CHECK_ENABLED=true
        # and re-running the judge on the same input produces a
        # different verdict for some item. severity warn.
        "judge_score_unstable",
        # Phase X2.4: emitted on a successful eval-ground-truth
        # --set-baseline run. severity info. Context carries the
        # coverage / precision / f1 / baseline_scope / pairs_count
        # snapshot so a future reader can answer "what IS the
        # baseline?" without reading the artifact.
        "baseline_set",
        # Phase X2.4: emitted when --set-baseline is invoked but the
        # last orchestration_result for the source had
        # stage_status="failed". severity halt; we refuse to install
        # a baseline measured on a broken run.
        "baseline_requires_successful_run",
        # Phase X2.6: emitted when a correction_candidate has been
        # pending for more than HUMAN_REVIEW_TTL_DAYS without any
        # human_review_artifact recording a decision. severity info;
        # candidate-level state is for operator triage, not the
        # blocking gate.
        "human_review_artifact_missing",
        # Phase P1: emitted by the eval runner when the new
        # deterministic alignment path encounters a ground_truth_pair
        # with no sibling ``<pair_id>_review.json`` confirming the
        # expected_decision_outcome. severity halt; the pair is
        # skipped and the gate refuses to score it.
        "gt_pair_not_reviewed",
        # Phase P1: emitted by the eval runner when the pair's
        # review record carries ``outcome_confirmed: false``. severity
        # halt; the pair is skipped because the human reviewer rejected
        # the outcome assignment.
        "gt_pair_outcome_rejected",
    }
)

# Codes whose *default* severity is halt. Used by tests as a
# documentation of intent; the schema's severity enum is the
# authoritative gate. ``stale_artifact_in_bundle`` is warn by default
# but escalates to halt on majority-stale bundles, so it is allowed
# to be either.
HALT_FINDING_CODES: frozenset[str] = frozenset(
    {
        "upstream_failure_eval_blocked",
        "feature_flag_missing",
        "smoke_test_skipped",
        "artifact_not_indexed",
        "stale_artifact_in_bundle",
        # Phase T.1: optionally promoted to halt when
        # BINDING_VALIDATOR_HALT_ENABLED=true. Default severity is warn.
        "taxonomy_regulatory_verb_missing",
        # Phase V.3: promoted to halt when FEW_SHOT_REQUIRED=true and
        # the few-shot artifact is missing. Default severity is info.
        "few_shot_artifact_missing",
        # Phase X2.3: hard-halt threshold (0.60 default) on judge
        # agreement. The gate is fail-closed: if the judge is too
        # unreliable to disagree with, --set-baseline is refused.
        "judge_calibration_failed",
        # Phase X2.4: gate refuses --set-baseline on a failed run.
        "baseline_requires_successful_run",
        # Phase P1: gate refuses to score a pair with no review record.
        "gt_pair_not_reviewed",
        # Phase P1: gate refuses to score a pair whose review rejected
        # the outcome.
        "gt_pair_outcome_rejected",
        # Phase P3-A T-1: chunk-metadata gate promoted to halt when
        # STRICT_CHUNK_METADATA=true. Default severity is warn.
        "chunk_metadata_contract_violation",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HealthFinding:
    finding_code: str
    severity: Severity
    context: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    pipeline_run_id: str | None = None
    finding_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    detected_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if self.finding_code not in ALL_FINDING_CODES:
            raise ValueError(
                f"undeclared finding_code={self.finding_code!r}; "
                "add it to ALL_FINDING_CODES and the schema enum."
            )
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"invalid severity={self.severity!r}; "
                f"must be one of {sorted(ALLOWED_SEVERITIES)}"
            )
        if self.severity == "halt" and self.finding_code not in HALT_FINDING_CODES:
            raise ValueError(
                f"finding_code={self.finding_code!r} cannot have severity=halt; "
                f"halt-eligible codes: {sorted(HALT_FINDING_CODES)}"
            )

    def is_halt(self) -> bool:
        return self.severity == "halt"


def finding_to_artifact(finding: HealthFinding) -> dict[str, Any]:
    """Serialise a :class:`HealthFinding` into the envelope dict.

    The shape matches ``schemas/health_finding.schema.json`` exactly so
    callers can pass the result through ``validate_artifact`` before
    write.
    """
    return {
        "artifact_type": "health_finding",
        "schema_version": SCHEMA_VERSION,
        "finding_id": finding.finding_id,
        "finding_code": finding.finding_code,
        "severity": finding.severity,
        "pipeline_run_id": finding.pipeline_run_id,
        "detected_at": finding.detected_at,
        "context": dict(finding.context),
        "remediation": finding.remediation,
    }


def write_finding(
    finding: HealthFinding,
    *,
    data_lake_path: str | Path,
    validate: bool = True,
) -> Path:
    """Write a finding artifact to
    ``<data_lake>/store/artifacts/health/<finding_id>.json``.

    The artifact is validated against the schema before write so a
    malformed finding never lands on disk. The directory is created if
    it does not exist.
    """
    artifact = finding_to_artifact(finding)
    if validate:
        from ..validation import (
            ArtifactValidationError,
            SchemaNotFoundError,
            validate_artifact,
        )
        try:
            validate_artifact(artifact, "health_finding")
        except SchemaNotFoundError:
            # The schema file is shipped in the package; absence means
            # the install is corrupt. Log and continue rather than
            # crashing the whole pipeline on a packaging defect.
            _LOG.warning("health_finding_schema_missing")
        except ArtifactValidationError as exc:
            raise

    target_dir = Path(data_lake_path) / "store" / "artifacts" / "health"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{finding.finding_id}.json"
    target.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
