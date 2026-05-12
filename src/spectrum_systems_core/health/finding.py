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
from typing import Any, Dict, Optional

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
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HealthFinding:
    finding_code: str
    severity: Severity
    context: Dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    pipeline_run_id: Optional[str] = None
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


def finding_to_artifact(finding: HealthFinding) -> Dict[str, Any]:
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
