"""Post-write index verification (Class 8).

After every artifact write the verifier reads the artifact index and
confirms the new artifact_id appears. If it does not, the artifact is
not yet visible to downstream stages — proceeding would silently skip
it. The finding is a halt.

Rollback: ``INDEX_VERIFY_ENABLED=false`` skips the verification. A
warning is logged on bypass.

Performance: per Red Team Pass 1 the verifier reads only the last N
lines (default 100; tunable via :data:`DEFAULT_TAIL_LINES`). Full
re-reads of a multi-megabyte JSONL after every write would add
unacceptable latency on growing data-lakes.
"""
from __future__ import annotations

import collections
import json
import logging
import os
from pathlib import Path
from typing import Optional

from .finding import HealthFinding

_LOG = logging.getLogger(__name__)

INDEX_VERIFY_ENV_VAR: str = "INDEX_VERIFY_ENABLED"
_DISABLED_VALUES: frozenset[str] = frozenset({"false", "0", "no", "off"})

DEFAULT_TAIL_LINES: int = 100


def index_verify_enabled() -> bool:
    raw = os.environ.get(INDEX_VERIFY_ENV_VAR, "")
    if raw.strip().lower() in _DISABLED_VALUES:
        _LOG.warning(
            "index_verify_disabled: %s=false -- skipping post-write index "
            "verification. This is a deliberate bypass.",
            INDEX_VERIFY_ENV_VAR,
        )
        return False
    return True


def _tail_artifact_ids(
    index_path: Path, *, tail_lines: int
) -> tuple[Optional[set[str]], Optional[str]]:
    """Return ``(ids, error)`` from the last ``tail_lines`` of the file.

    ``error`` is None on success or an explanation string when the
    file cannot be read. ``ids`` is None when read failed.
    """
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            tail = collections.deque(fh, maxlen=tail_lines)
    except OSError as exc:
        return None, f"index_read_failed: {exc.__class__.__name__}: {exc}"

    ids: set[str] = set()
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        aid = rec.get("artifact_id")
        if isinstance(aid, str) and aid:
            ids.add(aid)
    return ids, None


def verify_artifact_indexed(
    artifact_id: str,
    artifact_type: str,
    index_path: Path | str,
    *,
    tail_lines: int = DEFAULT_TAIL_LINES,
    pipeline_run_id: Optional[str] = None,
) -> Optional[HealthFinding]:
    """Return a halt finding if ``artifact_id`` is absent from the index.

    Returns ``None`` when the artifact is present in the most recent
    ``tail_lines`` lines.
    """
    if not index_verify_enabled():
        return None
    path = Path(index_path)
    if not path.is_file():
        return HealthFinding(
            finding_code="artifact_not_indexed",
            severity="halt",
            pipeline_run_id=pipeline_run_id,
            context={
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "index_path": str(path),
                "error": "index_missing",
            },
            remediation=(
                "Index file missing. Re-run write_artifact_index for "
                "this data-lake. If error persists, check write "
                "permissions on indexes/meetings/."
            ),
        )
    ids, err = _tail_artifact_ids(path, tail_lines=tail_lines)
    if ids is None:
        return HealthFinding(
            finding_code="artifact_not_indexed",
            severity="halt",
            pipeline_run_id=pipeline_run_id,
            context={
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "index_path": str(path),
                "error": err or "index_read_failed",
            },
            remediation=(
                "Index file unreadable (IOError). Re-run pipeline. If "
                "error persists, check data-lake permissions."
            ),
        )
    if artifact_id in ids:
        return None
    return HealthFinding(
        finding_code="artifact_not_indexed",
        severity="halt",
        pipeline_run_id=pipeline_run_id,
        context={
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "index_path": str(path),
            "tail_lines_scanned": tail_lines,
        },
        remediation=(
            "Artifact written but not in index. Re-run "
            "write_artifact_index after the write completes. If error "
            "persists, check data-lake write permissions and that the "
            "writer ran to completion."
        ),
    )
