"""Phase AA.1 — execution trace capture.

Two artifacts, both gated by the ``TRACE_CAPTURE_ENABLED`` env var
(default ``true``; rollback is ``TRACE_CAPTURE_ENABLED=false``):

1. **Per-chunk experience rows.** :func:`build_chunk_experience_rows`
   explodes one ``PipelineResult`` into N ``experience_history.jsonl``
   rows — one per chunk trace — by reusing
   ``data_lake.experience.build_experience_record`` (never duplicating
   its base-row logic) and stamping the AA.1 trace fields plus a
   chunk-unique ``experience_id`` so the rows sort deterministically
   under the existing ``(workflow_name, experience_id)`` key.

   Zero chunks is NOT a silent failure: the function returns the single
   base row (trace fields omitted). The caller still writes the file,
   so ``experience_history.jsonl`` always exists with ≥1 row — the
   Red-Team Pass-1 #2 invariant ("0-row trace, never a missing file").

2. **Harness snapshot.** :func:`write_harness_snapshot` copies the
   allowlisted harness source files + the prompts directory + the
   current commit sha into
   ``processed/meetings/<meeting_id>/harness_snapshot__<trial_id>/``.
   It is written on EVERY governed-loop run, including failed/blocked
   ones (Red-Team Pass-1 #3) — a failed trial's snapshot is exactly
   what the proposer needs to reason about the failure.

Neither artifact is a governed envelope, neither enters the data-lake
index, and neither is gitignored (they are point-in-time copies of
source files). The proposer reads them through an injected seam; this
module never reaches back into the proposer.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..data_lake.experience import (
    TRACE_FIELD_NAMES,
    build_experience_record,
)

TRACE_CAPTURE_ENV = "TRACE_CAPTURE_ENABLED"
HARNESS_SNAPSHOT_PREFIX = "harness_snapshot__"

# Allowlisted harness source files copied into a snapshot. Mirrors the
# code paths the proposer (AA.4) is permitted to mutate. A path that
# does not exist in the checkout is recorded as missing rather than
# silently skipped — the proposer must be able to tell "file absent at
# this commit" from "snapshot was never written".
_SNAPSHOT_SOURCE_FILES: tuple[tuple[str, str], ...] = (
    (
        "src/spectrum_systems_core/extraction/typed_extraction_runner.py",
        "typed_extraction_runner.py",
    ),
    (
        "src/spectrum_systems_core/extraction/chunker.py",
        "chunker.py",
    ),
    (
        "src/spectrum_systems_core/context/bundle_builder.py",
        "bundle_builder.py",
    ),
)
_SNAPSHOT_PROMPTS_SRC = "src/spectrum_systems_core/workflows/prompts"


def trace_capture_enabled(env: dict[str, str] | None = None) -> bool:
    """Read ``TRACE_CAPTURE_ENABLED``. Default ``true``.

    Only the explicit strings ``false``/``0``/``no``/``off`` (any case)
    disable capture; anything else — including an unset var — leaves it
    on. This makes the rollback explicit and impossible to trip by a
    stray value.
    """
    raw = (env or os.environ).get(TRACE_CAPTURE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def harness_snapshot_dirname(trial_id: str) -> str:
    return f"{HARNESS_SNAPSHOT_PREFIX}{trial_id}"


def _git_head_sha(repo_root: Path) -> str:
    """``git rev-parse HEAD`` for the snapshot. Fail-closed to a
    sentinel — a snapshot whose provenance is unknown is still better
    than no snapshot, and AA.2 explicitly halts on a sha mismatch so an
    ``unknown`` sha can never silently pass the score-summary gate."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


@dataclass(frozen=True)
class HarnessSnapshotResult:
    snapshot_dir: Path
    commit_sha: str
    copied_files: list[str]
    missing_files: list[str]


def write_harness_snapshot(
    *,
    processed_dir: Path | str,
    trial_id: str,
    repo_root: Path | str,
    commit_sha: str | None = None,
) -> HarnessSnapshotResult:
    """Copy allowlisted harness source + prompts + commit sha.

    Idempotent: an existing snapshot dir for ``trial_id`` is replaced so
    a re-run over the same inputs leaves a byte-identical tree (modulo
    the commit sha, which is itself stable for a given commit). Written
    unconditionally by the caller — including on a blocked run.
    """
    processed_dir = Path(processed_dir)
    repo_root = Path(repo_root)
    snap = processed_dir / harness_snapshot_dirname(trial_id)
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    missing: list[str] = []
    for rel_src, dest_name in _SNAPSHOT_SOURCE_FILES:
        src = repo_root / rel_src
        if src.is_file():
            shutil.copy2(src, snap / dest_name)
            copied.append(dest_name)
        else:
            missing.append(dest_name)

    prompts_src = repo_root / _SNAPSHOT_PROMPTS_SRC
    if prompts_src.is_dir():
        shutil.copytree(prompts_src, snap / "prompts")
        copied.append("prompts/")
    else:
        missing.append("prompts/")

    sha = commit_sha if commit_sha is not None else _git_head_sha(repo_root)
    (snap / "commit_sha.txt").write_text(sha + "\n", encoding="utf-8")

    return HarnessSnapshotResult(
        snapshot_dir=snap,
        commit_sha=sha,
        copied_files=copied,
        missing_files=missing,
    )


def _chunk_experience_id(base_id: str, chunk_id: str, ordinal: int) -> str:
    """Per-chunk id that keeps the existing
    ``(workflow_name, experience_id)`` sort total and deterministic.
    The ordinal is zero-padded so lexical order matches chunk order."""
    return f"{base_id}-c{ordinal:04d}-{chunk_id}"


def build_chunk_experience_rows(
    result: Any,
    *,
    chunk_traces: list[dict[str, Any]] | None = None,
    trace_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Explode one ``PipelineResult`` into per-chunk experience rows.

    * trace disabled  -> exactly the pre-AA.1 single base row, trace
      fields omitted (rollback is byte-clean).
    * no chunk traces -> the single base row (trace fields omitted).
      The caller still writes the file, so a zero-chunk transcript
      yields a 1-row file, never a missing one.
    * N chunk traces  -> N rows, each carrying the AA.1 trace fields and
      a chunk-unique ``experience_id``.
    """
    base = build_experience_record(result, trace_enabled=False)
    if not trace_enabled or not chunk_traces:
        return [base]

    rows: list[dict[str, Any]] = []
    for ordinal, ct in enumerate(chunk_traces):
        row = build_experience_record(
            result, chunk_trace=ct, trace_enabled=True
        )
        chunk_id = row.get("chunk_id")
        if not chunk_id:
            # A trace with no chunk_id cannot be addressed by the
            # proposer; keep it but make its id unique by ordinal so it
            # is never silently merged with another chunk's row.
            chunk_id = f"unknown-{ordinal:04d}"
            row["chunk_id"] = chunk_id
        row["experience_id"] = _chunk_experience_id(
            base["experience_id"], str(chunk_id), ordinal
        )
        rows.append(row)
    return rows


__all__ = [
    "TRACE_CAPTURE_ENV",
    "HARNESS_SNAPSHOT_PREFIX",
    "TRACE_FIELD_NAMES",
    "HarnessSnapshotResult",
    "trace_capture_enabled",
    "harness_snapshot_dirname",
    "write_harness_snapshot",
    "build_chunk_experience_rows",
]
