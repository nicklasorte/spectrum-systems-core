"""Phase O.2: scan blocked_chunk artifacts for missing chunk_text.

Walks ``<data_lake>/store/artifacts/blocked_chunks/`` and reports any
artifact whose ``schema_version == "1.0.0"`` OR that lacks the
``chunk_text`` field. The finding is informational (severity ``info``):
the scanner identifies which artifacts predate Phase O.2; it does NOT
force a migration. New runs always emit at ``2.0.0`` with
``chunk_text`` populated.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Union

from .finding import HealthFinding

_LOG = logging.getLogger(__name__)


def scan_blocked_chunks(
    data_lake_path: Union[str, Path],
    *,
    pipeline_run_id: str | None = None,
) -> List[HealthFinding]:
    """Return one info finding per blocked_chunk artifact lacking chunk_text.

    Looks for v1.0.0 envelopes or any v2.0.0+ envelope that somehow
    landed without the field (defence in depth -- the write-time gate
    already requires it, but if validation was disabled the artifact
    can still slip through).
    """
    dl = Path(data_lake_path)
    blocked_dir = dl / "store" / "artifacts" / "blocked_chunks"
    findings: List[HealthFinding] = []
    if not blocked_dir.is_dir():
        return findings
    for path in sorted(blocked_dir.glob("*.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("blocked_chunk_unreadable: %s: %s", path, exc)
            continue
        if not isinstance(artifact, dict):
            continue
        if artifact.get("artifact_type") != "blocked_chunk":
            continue
        version = str(artifact.get("schema_version") or "")
        has_text = "chunk_text" in artifact and isinstance(
            artifact.get("chunk_text"), str
        )
        if version == "1.0.0" or not has_text:
            findings.append(
                HealthFinding(
                    finding_code="blocked_artifact_missing_chunk_text",
                    severity="info",
                    pipeline_run_id=pipeline_run_id,
                    context={
                        "artifact_path": str(path),
                        "schema_version": version or "<missing>",
                        "chunk_id": str(artifact.get("chunk_id") or ""),
                        "source_id": str(artifact.get("source_id") or ""),
                    },
                    remediation=(
                        "Phase O.2 added chunk_text/chunk_char_count/"
                        "chunk_speaker/chunk_index to the blocked_chunk "
                        "envelope (schema_version 2.0.0). Re-run the "
                        "affected source through extract-typed to "
                        "regenerate the artifact with chunk text inline."
                    ),
                )
            )
    return findings
