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
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Phase 5 — per-model F1 readouts (only emitted with --show-all-models).
# ---------------------------------------------------------------------------


def _opus_item_count(processed_dir: Path) -> Optional[int]:
    """Read the Opus baseline item count, or None when no baseline exists.

    The baseline file is a JSONL written by the Opus baseliner; each
    line is one extracted item. The count is just the line count
    (skipping blank lines so a trailing newline doesn't inflate the
    total).
    """
    if not processed_dir.is_dir():
        return None
    candidates = sorted(processed_dir.glob("meeting_minutes_opus__*.json"))
    if not candidates:
        return None
    try:
        text = candidates[-1].read_text(encoding="utf-8")
    except OSError:
        return None
    # The Opus baseline file is a single JSON object whose `payload`
    # carries the arrays. Count the items across the standard
    # extraction-types list.
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        return None
    types = (
        "decisions",
        "action_items",
        "open_questions",
        "regulatory_verbs",
        "agency_positions",
        "agency_relationships",
        "agency_objections",
        "constraints",
        "milestones",
        "topics",
        "structured_items",
    )
    total = 0
    for t in types:
        v = payload.get(t)
        if isinstance(v, list):
            total += len(v)
    return total


def _latest_f1_by_variant_with_mtime(
    processed_dir: Path, want_variant: str
) -> Optional[tuple[float, float]]:
    """Read the most-recent comparison F1 for a variant + source mtime.

    Returns ``(f1, mtime)`` for the most-recent comparison that
    references ``want_variant``, or ``None`` when no such comparison
    exists. ``mtime`` is the source artifact's filesystem mtime; the
    caller uses it to break ties when the same source has comparisons
    for multiple Sonnet variants and the operator wants the freshest.

    ``want_variant`` is one of the four Phase-5 prompt variants. The
    function scans both two-way (``comparison_result__*.json``) and
    three-way (``comparisons/three_way_*.json``) artifacts.
    """
    if not processed_dir.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    for p in processed_dir.glob("comparison_result__*.json"):
        candidates.append((p.stat().st_mtime, p))
    three_way_dir = processed_dir / "comparisons"
    if three_way_dir.is_dir():
        for p in three_way_dir.glob("three_way_*.json"):
            candidates.append((p.stat().st_mtime, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)

    for mtime, path in candidates:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Two-way: `haiku_prompt_variant` plus `summary.haiku_f1_vs_opus`.
        # Three-way: `haiku_prompt_variant` for Haiku, `sonnet_prompt_variant`
        # for Sonnet, plus the matching `*_summary.haiku_f1_vs_opus`.
        is_three_way = doc.get("comparison_mode") == "three_way"
        if is_three_way:
            hv = doc.get("haiku_prompt_variant", "production_haiku")
            sv = doc.get("sonnet_prompt_variant", "production_haiku")
            if hv == want_variant:
                f1 = (doc.get("haiku_summary") or {}).get("haiku_f1_vs_opus")
                if isinstance(f1, (int, float)):
                    return float(f1), mtime
            if sv == want_variant:
                f1 = (doc.get("sonnet_summary") or {}).get("haiku_f1_vs_opus")
                if isinstance(f1, (int, float)):
                    return float(f1), mtime
        else:
            hv = doc.get("haiku_prompt_variant", "production_haiku")
            if hv == want_variant:
                f1 = (doc.get("summary") or {}).get("haiku_f1_vs_opus")
                if isinstance(f1, (int, float)):
                    return float(f1), mtime
    return None


def _latest_f1_by_variant(
    processed_dir: Path, want_variant: str
) -> Optional[float]:
    """Backward-compatible thin wrapper that drops the mtime."""
    rec = _latest_f1_by_variant_with_mtime(processed_dir, want_variant)
    return rec[0] if rec is not None else None


def _newest_sonnet_f1(processed_dir: Path) -> Optional[float]:
    """Pick the most-recent Sonnet F1 across the two Sonnet variants.

    Phase 5 has two Sonnet variants — ``haiku_prompt_with_sonnet_model``
    (apples-to-apples vs Haiku) and ``opus_prompt_with_sonnet_model``
    (Sonnet's unconstrained capability). When a source has comparisons
    for both, the rollup should surface the FRESHER one (operators run
    both as iterative measurements; the older one is stale). Tie
    breaking by mtime is deterministic given the data lake's
    append-only invariant. ``None`` when neither variant has run.
    """
    haiku_prompt = _latest_f1_by_variant_with_mtime(
        processed_dir, "haiku_prompt_with_sonnet_model"
    )
    opus_prompt = _latest_f1_by_variant_with_mtime(
        processed_dir, "opus_prompt_with_sonnet_model"
    )
    if haiku_prompt is None and opus_prompt is None:
        return None
    if haiku_prompt is None:
        return opus_prompt[0]
    if opus_prompt is None:
        return haiku_prompt[0]
    # Both present — pick the newer source artifact by mtime.
    return haiku_prompt[0] if haiku_prompt[1] >= opus_prompt[1] else opus_prompt[0]


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
    manifest: LoadedManifest,
    lake_root: Path,
    *,
    show_all_models: bool = False,
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
        row: Dict[str, Any] = {
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
        if show_all_models:
            # Phase 5 — additive per-model F1 readouts. Absent in the
            # default (Phase 4-byte-identical) output. The fields here
            # are read from the most recent matching comparison_result
            # for the source; `null` means "not yet extracted".
            row["haiku_latest_f1"] = _latest_f1_by_variant(
                processed_dir, "production_haiku"
            )
            row["sonnet_latest_f1"] = _newest_sonnet_f1(processed_dir)
            row["opus_item_count"] = _opus_item_count(processed_dir)
        rows.append(row)
    return rows


def _build_orphan_rows(
    manifest: LoadedManifest,
    lake_root: Path,
    *,
    show_all_models: bool = False,
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
        row: Dict[str, Any] = {
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
        if show_all_models:
            row["haiku_latest_f1"] = _latest_f1_by_variant(
                sub, "production_haiku"
            )
            row["sonnet_latest_f1"] = _newest_sonnet_f1(sub)
            row["opus_item_count"] = _opus_item_count(sub)
        rows.append(row)
    return rows


def build_corpus_status_report(
    *,
    lake_root: Path | str,
    manifest_path: Path | str | None = None,
    show_all_models: bool = False,
) -> Dict[str, Any]:
    """Build the corpus-mode status_report payload.

    The function loads the manifest (which validates schema + hash +
    custom rules; the CLI catches CorpusManifestError) and then
    walks the data lake to produce one row per source plus one row
    per orphan. The result is a plain dict matching
    ``status_report.schema.json``; the caller schema-validates it
    before serialising so a drift between this code and the schema
    fails the test suite.

    ``show_all_models`` (Phase 5) defaults to ``False`` so the output
    is byte-identical to the Phase-4 rollup on the same lake state.
    When ``True`` each row carries three additive optional fields —
    ``haiku_latest_f1``, ``sonnet_latest_f1``, ``opus_item_count`` —
    each ``null`` when the corresponding artifact has not been
    produced for that source.
    """
    lake = Path(lake_root)
    manifest = load_manifest(manifest_path)
    rows = _build_manifest_rows(manifest, lake, show_all_models=show_all_models)
    rows.extend(
        _build_orphan_rows(manifest, lake, show_all_models=show_all_models)
    )
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
