"""Phase S.2: scan live meeting_extraction artifacts for missing confidence.

Phase X added the ``confidence`` field to the typed-extraction schema and
the model prompt, but pipeline runs #20-#23 produced zero artifacts with
the field populated -- so the gate was never proven in a live run.

This module provides a small, deterministic check the pipeline can call
after extraction completes:

  scan_extractions(data_lake_path) -> list[HealthFinding]

Returns one ``confidence_field_missing`` finding per offending item.
Severity is ``warn`` -- the runtime ``validate_artifact`` gate already
rejects malformed extractions at write time, so the live check is a
diagnostic surface for items that landed before that gate was enabled.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .finding import HealthFinding

_LOG = logging.getLogger(__name__)


_ITEM_KEYS = ("decisions", "claims", "action_items")


def _missing_confidence(item: dict[str, Any]) -> bool:
    """An item is missing confidence when the field is absent OR not numeric."""
    if "confidence" not in item:
        return True
    val = item.get("confidence")
    if isinstance(val, bool):
        return True
    return not isinstance(val, (int, float))


def scan_extractions(
    data_lake_path: str | Path,
    *,
    pipeline_run_id: str | None = None,
) -> list[HealthFinding]:
    """Scan every meeting_extraction artifact under
    ``<data_lake>/store/artifacts/extractions/`` and report any item
    that is missing the ``confidence`` field.

    Returns a list of HealthFinding objects (one per offending item).
    Caller is expected to persist them via
    ``spectrum_systems_core.health.finding.write_finding``.
    """
    dl = Path(data_lake_path)
    ext_dir = dl / "store" / "artifacts" / "extractions"
    findings: list[HealthFinding] = []
    if not ext_dir.is_dir():
        return findings
    for path in sorted(ext_dir.glob("*_meeting_extraction.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "confidence_field_check_unreadable: %s: %s", path, exc,
            )
            continue
        source_artifact_id = str(artifact.get("source_artifact_id") or "")
        extraction_run_id = str(artifact.get("extraction_run_id") or "")
        for kind in _ITEM_KEYS:
            # Phase S.2 only gates decisions + claims (action_items still
            # have confidence in the v2 schema, but the prompt block is
            # named CONFIDENCE_SCORING_BLOCK and the task spec calls out
            # "decisions or claims" specifically -- we scan all three to
            # err on the side of detection).
            for idx, item in enumerate(artifact.get(kind, []) or []):
                if not isinstance(item, dict):
                    continue
                if not _missing_confidence(item):
                    continue
                findings.append(
                    HealthFinding(
                        finding_code="confidence_field_missing",
                        severity="warn",
                        pipeline_run_id=pipeline_run_id,
                        context={
                            "artifact_path": str(path),
                            "source_artifact_id": source_artifact_id,
                            "extraction_run_id": extraction_run_id,
                            "item_kind": kind,
                            "item_index": idx,
                        },
                        remediation=(
                            "Phase X added the CONFIDENCE_SCORING_BLOCK to "
                            "the extractor prompts and the ``confidence`` "
                            "field to the meeting_extraction schema. "
                            "Re-run extract-typed for this source after "
                            "confirming CONFIDENCE_SCORING_BLOCK is in the "
                            "rendered prompt."
                        ),
                    )
                )
    return findings
