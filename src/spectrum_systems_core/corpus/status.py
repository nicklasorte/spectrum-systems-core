"""Phase 4 — corpus status rollup.

Reads the corpus manifest and walks the data lake to produce one
``status_report`` JSON document per invocation. The rollup is pure:
no LLM calls, no network access, no writes to the data lake.

Row construction:

* For every source in the manifest, derive (state, recommendation)
  from the manifest's observed fields plus the presence/absence of
  on-disk artifacts:

    - ``processed/meetings/<sid>/source_record.json``
    - ``processed/meetings/<sid>/meeting_minutes_opus__*.json``
    - ``processed/meetings/<sid>/comparison_result__*.json``

* For every source-id directory under
  ``processed/meetings/`` that does NOT appear in the manifest, emit
  a synthetic row with ``state=orphaned_in_lake``. The walk uses the
  same source_id pattern as the manifest schema
  (``^[a-z0-9-]+$``) so unrelated subtrees do not falsely register
  as orphans.

The functions in this module mirror the public enum values declared
in ``status_report.schema.json``. The constants below are the single
source of truth that the schema enum tests against — adding a value
in code without adding it to the schema (or vice versa) is the exact
drift the Phase 4 red-team Pass 1 #6 / #7 tests defend against.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from ..schemas import schema_path
from .manifest_loader import (
    LoadedManifest,
    load_manifest,
)


# State enum — must match status_report.schema.json::rows.items.state.
STATE_PENDING: str = "pending"
STATE_VALIDATED: str = "validated"
STATE_UNDER_REVIEW: str = "under_review"
STATE_QUARANTINED: str = "quarantined"
STATE_BASELINE_COMPLETE: str = "baseline_complete"
STATE_COMPARISON_COMPLETE: str = "comparison_complete"
STATE_SUPERSEDED: str = "superseded"
STATE_ORPHANED_IN_LAKE: str = "orphaned_in_lake"

ALL_STATES: frozenset[str] = frozenset(
    {
        STATE_PENDING,
        STATE_VALIDATED,
        STATE_UNDER_REVIEW,
        STATE_QUARANTINED,
        STATE_BASELINE_COMPLETE,
        STATE_COMPARISON_COMPLETE,
        STATE_SUPERSEDED,
        STATE_ORPHANED_IN_LAKE,
    }
)

# Recommendation enum — must match status_report.schema.json::rows.items.recommendation.
RECOMMENDATION_NONE: str = "none"
RECOMMENDATION_INGEST: str = "run_ingest_corpus"
RECOMMENDATION_REVIEW_QUARANTINED: str = "force_review_quarantined"
RECOMMENDATION_INVESTIGATE_ORPHAN: str = "investigate_orphan_in_lake"
RECOMMENDATION_BASELINE_OPUS: str = "run_baseline_opus"
RECOMMENDATION_RUN_COMPARISON: str = "run_comparison"

ALL_RECOMMENDATIONS: frozenset[str] = frozenset(
    {
        RECOMMENDATION_NONE,
        RECOMMENDATION_INGEST,
        RECOMMENDATION_REVIEW_QUARANTINED,
        RECOMMENDATION_INVESTIGATE_ORPHAN,
        RECOMMENDATION_BASELINE_OPUS,
        RECOMMENDATION_RUN_COMPARISON,
    }
)


_SOURCE_ID_RE = re.compile(r"^[a-z0-9-]+$")


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _has_source_record(processed_dir: Path) -> bool:
    return (processed_dir / "source_record.json").is_file()


def _has_opus_baseline(processed_dir: Path) -> bool:
    if not processed_dir.is_dir():
        return False
    for _ in processed_dir.glob("meeting_minutes_opus__*.json"):
        return True
    return False


def _has_comparison_result(processed_dir: Path) -> bool:
    if not processed_dir.is_dir():
        return False
    for _ in processed_dir.glob("comparison_result__*.json"):
        return True
    return False


def _processed_root(lake_root: Path) -> Path:
    """Resolve the processed/meetings/ root.

    Production lakes use ``<lake>/processed/meetings/`` (data lake
    contract §2). A few historical layouts in this repo wrap the
    contents under ``store/`` — the status rollup checks BOTH paths
    so an operator running against either layout sees a real result.
    """
    p1 = lake_root / "processed" / "meetings"
    if p1.is_dir():
        return p1
    p2 = lake_root / "store" / "processed" / "meetings"
    return p2


def _derive_state_and_recommendation(
    *,
    manifest_status: str,
    has_source_record: bool,
    has_opus_baseline: bool,
    has_comparison_result: bool,
) -> tuple[str, str]:
    """Map (manifest observed state + disk artifacts) -> (state, recommendation).

    The manifest's ``observed.ingestion_status`` is authoritative for
    the lifecycle phase (pending / validated / under_review /
    quarantined / superseded). On-disk artifacts upgrade the state
    further (baseline_complete / comparison_complete) so a stale
    manifest does not under-report progress.
    """
    if manifest_status == "superseded":
        return STATE_SUPERSEDED, RECOMMENDATION_NONE
    if manifest_status == "quarantined":
        return STATE_QUARANTINED, RECOMMENDATION_REVIEW_QUARANTINED
    if manifest_status == "pending":
        return STATE_PENDING, RECOMMENDATION_INGEST
    if manifest_status == "under_review":
        return STATE_UNDER_REVIEW, RECOMMENDATION_BASELINE_OPUS
    # The remaining manifest statuses (validated / baseline_complete /
    # comparison_complete) all imply a source_record exists; the
    # on-disk artifact set decides which terminal state to report.
    if has_comparison_result:
        return STATE_COMPARISON_COMPLETE, RECOMMENDATION_NONE
    if has_opus_baseline:
        return STATE_BASELINE_COMPLETE, RECOMMENDATION_RUN_COMPARISON
    if has_source_record:
        return STATE_VALIDATED, RECOMMENDATION_BASELINE_OPUS
    # Manifest says progressed but no source_record on disk — the
    # operator likely deleted artifacts or pointed at the wrong lake.
    # Surface as pending with the ingest recommendation.
    return STATE_PENDING, RECOMMENDATION_INGEST


def _build_manifest_rows(
    manifest: LoadedManifest, lake_root: Path
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    processed_root = _processed_root(lake_root)
    for entry in manifest.payload["sources"]:
        sid = entry["source_id"]
        declared = entry["declared"]
        observed = entry["observed"]
        manifest_status = observed["ingestion_status"]
        processed_dir = processed_root / sid
        hsr = _has_source_record(processed_dir)
        hob = _has_opus_baseline(processed_dir)
        hcr = _has_comparison_result(processed_dir)
        state, rec = _derive_state_and_recommendation(
            manifest_status=manifest_status,
            has_source_record=hsr,
            has_opus_baseline=hob,
            has_comparison_result=hcr,
        )
        rows.append(
            {
                "source_id": sid,
                "state": state,
                "recommendation": rec,
                "has_source_record": hsr,
                "has_opus_baseline": hob,
                "has_comparison_result": hcr,
                "detected_speaker_count": observed["detected_speaker_count"],
                "detected_word_count": observed["detected_word_count"],
                "last_updated": observed["last_updated"],
                "meeting_date": declared["meeting_date"],
                "meeting_type": declared["meeting_type"],
            }
        )
    return rows


def _build_orphan_rows(
    manifest: LoadedManifest, lake_root: Path
) -> List[Dict[str, Any]]:
    """Synthesise rows for processed/meetings/<sid>/ directories not in the manifest."""
    processed_root = _processed_root(lake_root)
    if not processed_root.is_dir():
        return []
    manifest_ids = {e["source_id"] for e in manifest.payload["sources"]}
    rows: List[Dict[str, Any]] = []
    for sub in sorted(processed_root.iterdir()):
        if not sub.is_dir():
            continue
        sid = sub.name
        if sid in manifest_ids:
            continue
        if not _SOURCE_ID_RE.match(sid):
            continue
        # Only flag directories that actually contain pipeline output
        # so an empty directory the operator created by accident does
        # not surface as an orphan.
        if not (
            _has_source_record(sub)
            or _has_opus_baseline(sub)
            or _has_comparison_result(sub)
        ):
            continue
        rows.append(
            {
                "source_id": sid,
                "state": STATE_ORPHANED_IN_LAKE,
                "recommendation": RECOMMENDATION_INVESTIGATE_ORPHAN,
                "has_source_record": _has_source_record(sub),
                "has_opus_baseline": _has_opus_baseline(sub),
                "has_comparison_result": _has_comparison_result(sub),
                "detected_speaker_count": None,
                "detected_word_count": None,
                "last_updated": None,
                "meeting_date": None,
                "meeting_type": None,
            }
        )
    return rows


def build_corpus_status_report(
    *,
    lake_root: Path | str,
    manifest_path: Path | str | None = None,
) -> Dict[str, Any]:
    """Build the corpus-mode status_report payload.

    The function loads the manifest (which validates schema + hash +
    custom rules; the CLI catches CorpusManifestError) and then
    walks the data lake to produce one row per source plus one row
    per orphan. The result is a plain dict matching
    ``status_report.schema.json``; the caller schema-validates it
    before serialising so a drift between this code and the schema
    fails the test suite.
    """
    lake = Path(lake_root)
    manifest = load_manifest(manifest_path)
    rows = _build_manifest_rows(manifest, lake)
    rows.extend(_build_orphan_rows(manifest, lake))
    rows.sort(key=lambda r: (r["state"], r["source_id"]))
    report: Dict[str, Any] = {
        "artifact_type": "status_report",
        "schema_version": "1.0.0",
        "manifest_hash": manifest.manifest_hash,
        "generated_at": _now_iso(),
        "rows": rows,
    }
    _validate_against_schema(report)
    return report


def _validate_against_schema(report: Dict[str, Any]) -> None:
    """Internal validator: the rollup's own schema catches drift.

    Called from ``build_corpus_status_report`` so a code change that
    emits a new state or recommendation value without updating
    ``status_report.schema.json`` fails inline — not at some later
    consumer's read site.
    """
    import jsonschema

    schema = json.loads(
        schema_path("status_report").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(report)


__all__ = [
    "ALL_RECOMMENDATIONS",
    "ALL_STATES",
    "RECOMMENDATION_BASELINE_OPUS",
    "RECOMMENDATION_INGEST",
    "RECOMMENDATION_INVESTIGATE_ORPHAN",
    "RECOMMENDATION_NONE",
    "RECOMMENDATION_REVIEW_QUARANTINED",
    "RECOMMENDATION_RUN_COMPARISON",
    "STATE_BASELINE_COMPLETE",
    "STATE_COMPARISON_COMPLETE",
    "STATE_ORPHANED_IN_LAKE",
    "STATE_PENDING",
    "STATE_QUARANTINED",
    "STATE_SUPERSEDED",
    "STATE_UNDER_REVIEW",
    "STATE_VALIDATED",
    "build_corpus_status_report",
]
