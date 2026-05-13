#!/usr/bin/env python3
"""Generate ``ground_truth_pair`` artifacts from a meeting_extraction.

Phase X2 follow-up. Seeds the rubric-annotation pipeline by turning each
decision in the latest ``meeting_extraction`` artifact for a transcript
into a schema-valid ``ground_truth_pair`` written under
``<sdl_root>/ground_truth/<pair_id>.json``.

Without this seed step ``scripts/annotate_rubric.py --source-id <slug>``
exits with "source_id matched 0 pairs" because the GroundTruthLinker
only produces pairs when there is a separate ``minutes_record`` document
to date-match against — which the debug-single-transcript workflow does
not produce.

The script is deterministic and idempotent: ``pair_id`` is derived via
``uuid.uuid5(NAMESPACE, source_id|decision_text)`` so a re-run on the
same extraction overwrites the same files instead of multiplying them.

Usage::

    python scripts/generate_gt_pairs.py \\
        --source-id <slug> \\
        --data-lake data-lake/
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

# Stable namespace for ``uuid.uuid5`` so re-running the script against
# the same extraction produces the SAME pair_ids and rewrites the same
# files (idempotency). The namespace value is arbitrary but must not
# change once shipped.
_GT_PAIR_NAMESPACE = uuid.UUID("9d4a2c1e-3c12-4f63-9a4f-1b3c2c4d5e6a")

# Deterministic timestamp for pair_created_at / confirmed_at so the
# pair files are byte-identical across re-runs. The data-lake pipeline
# uses the same epoch sentinel for the same reason
# (data_lake/pipeline.py::_DETERMINISTIC_CREATED_AT). Without this,
# every ``extract-typed --force`` re-run would touch every GT pair
# file's timestamp and noisily re-commit them in the debug workflow.
_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"

_SOURCE_FAMILIES: Tuple[str, ...] = ("meetings",)

# YYYYMMDD trailing-date pattern in the transcript slug (e.g.
# ``...transcript-20251218``). Used as a best-effort meeting_date
# derivation so the schema's required ``meeting_date`` field is
# populated with the real meeting date rather than today's date.
_SLUG_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _meeting_date_from_slug(source_id: str) -> Optional[str]:
    """Best-effort YYYY-MM-DD extraction from a transcript slug.

    Returns the first YYYYMMDD substring formatted as YYYY-MM-DD if it
    parses as a real calendar date, else None. Callers fall back to a
    safe sentinel when None is returned.
    """
    for m in _SLUG_DATE_RE.finditer(source_id):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            continue
    return None


def _resolve_source_artifact_id(
    data_lake: Path, source_id: str
) -> Optional[str]:
    """Resolve a source_id slug to the source_record artifact_id.

    Mirrors ``scripts/select_few_shot_examples.py::_resolve_source_artifact_id``
    so the GT-pair writer locates the same extraction the
    typed_extraction_runner wrote.
    """
    store_root = data_lake / "store"
    for family in _SOURCE_FAMILIES:
        sr_path = (
            store_root / "processed" / family / source_id / "source_record.json"
        )
        if sr_path.is_file():
            try:
                data = json.loads(sr_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            aid = data.get("artifact_id") if isinstance(data, dict) else None
            if isinstance(aid, str) and aid:
                return aid
    return None


def _load_meeting_extraction(
    data_lake: Path, source_id: str
) -> Optional[Tuple[Dict[str, Any], Path]]:
    """Return (artifact, path) for the most recent meeting_extraction.

    Matches the integration-hardening contract: validate the loaded
    artifact against the ``meeting_extraction`` schema BEFORE the
    caller reads fields off it, so a writer-side rename surfaces here
    with the failing field named rather than as silent zero-decision
    output.
    """
    extraction_dir = data_lake / "store" / "artifacts" / "extractions"
    candidates: List[Path] = []
    if extraction_dir.is_dir():
        candidates.extend(sorted(extraction_dir.glob("*.json")))

    resolved_artifact_id = _resolve_source_artifact_id(data_lake, source_id)

    chosen: Optional[Dict[str, Any]] = None
    chosen_path: Optional[Path] = None
    for path in candidates:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        doc_source_id = doc.get("source_id")
        doc_source_artifact_id = doc.get("source_artifact_id")
        matches = (
            (doc_source_id == source_id)
            or (
                resolved_artifact_id is not None
                and doc_source_artifact_id == resolved_artifact_id
            )
        )
        if not matches:
            continue
        if chosen_path is None or path.stat().st_mtime > chosen_path.stat().st_mtime:
            chosen = doc
            chosen_path = path

    if chosen is None or chosen_path is None:
        return None
    validate_artifact(chosen, "meeting_extraction", str(chosen_path))
    return chosen, chosen_path


def _meeting_name_from_slug(source_id: str) -> str:
    """Human-readable fallback meeting_name derived from the slug."""
    name = " ".join(source_id.replace("-", " ").split())
    return name or source_id


def build_pair(
    *,
    source_id: str,
    source_artifact_id: str,
    minutes_artifact_id: str,
    meeting_date: str,
    meeting_name: str,
    decision: Dict[str, Any],
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a schema-valid ``ground_truth_pair`` dict from a decision.

    The ``pair_id`` is deterministic (``uuid.uuid5`` of
    ``source_id|decision_text``) so re-runs on the same extraction
    overwrite the same file instead of producing duplicates.
    """
    decision_text = decision.get("decision_text") or ""
    pair_id = str(
        uuid.uuid5(_GT_PAIR_NAMESPACE, f"{source_id}|{decision_text}")
    )
    ts = created_at or _now_iso()
    expected_outcome = decision.get("decision_outcome")
    pair: Dict[str, Any] = {
        "pair_id": pair_id,
        "source_artifact_id": source_artifact_id,
        "minutes_artifact_id": minutes_artifact_id,
        "meeting_date": meeting_date,
        "meeting_name": meeting_name,
        "match_confidence": "high",
        "status": "confirmed",
        "created_at": ts,
        "confirmed_at": ts,
        "confirmed_by": "GenerateGTPairs",
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "GenerateGTPairs"},
        "source_id": source_id,
        "ground_truth_text": decision_text or None,
        "target_type": "decision",
        "expected_decision_outcome": (
            expected_outcome if isinstance(expected_outcome, str) else None
        ),
    }
    return pair


def _write_pair(path: Path, pair: Dict[str, Any]) -> None:
    """Write the pair as canonical JSON (sorted keys, trailing newline)."""
    path.write_text(
        json.dumps(pair, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def generate_pairs(
    *,
    data_lake: Path,
    source_id: str,
) -> Dict[str, Any]:
    """Generate GT pairs for ``source_id`` from the latest extraction.

    Returns a dict with ``status``, ``pairs_written``, ``pairs_skipped``,
    ``output_dir``, and ``reason`` so callers (including the debug
    workflow's step summary) can report counts without re-parsing
    stdout.
    """
    loaded = _load_meeting_extraction(data_lake, source_id)
    if loaded is None:
        return {
            "status": "failure",
            "pairs_written": 0,
            "pairs_skipped": 0,
            "output_dir": "",
            "reason": "no_meeting_extraction_for_source_id",
        }
    artifact, artifact_path = loaded
    decisions = artifact.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        return {
            "status": "failure",
            "pairs_written": 0,
            "pairs_skipped": 0,
            "output_dir": "",
            "reason": "extraction_has_zero_decisions",
        }

    source_artifact_id = artifact.get("source_artifact_id")
    if not isinstance(source_artifact_id, str) or not source_artifact_id:
        return {
            "status": "failure",
            "pairs_written": 0,
            "pairs_skipped": 0,
            "output_dir": "",
            "reason": "extraction_missing_source_artifact_id",
        }

    # minutes_artifact_id must be a non-empty string per the schema.
    # GenerateGTPairs is not running against a real minutes_record, so
    # a deterministic source_id-derived sentinel is used instead of the
    # meeting_extraction_id (which is a fresh UUID on every
    # ``extract-typed --force`` and would make the GT pair file
    # non-byte-identical across re-runs, defeating idempotency).
    minutes_artifact_id = f"synthesized-from-extraction:{source_id}"

    meeting_date = (
        _meeting_date_from_slug(source_id) or "1970-01-01"
    )
    meeting_name = _meeting_name_from_slug(source_id)

    out_dir = data_lake / "store" / "artifacts" / "ground_truth"
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs_written = 0
    pairs_skipped = 0
    created_at = _DETERMINISTIC_CREATED_AT
    for decision in decisions:
        if not isinstance(decision, dict):
            pairs_skipped += 1
            continue
        if not (decision.get("decision_text") or "").strip():
            pairs_skipped += 1
            continue
        pair = build_pair(
            source_id=source_id,
            source_artifact_id=source_artifact_id,
            minutes_artifact_id=minutes_artifact_id,
            meeting_date=meeting_date,
            meeting_name=meeting_name,
            decision=decision,
            created_at=created_at,
        )
        # Validate every pair against the schema BEFORE writing so a
        # silent shape drift never lands an unreadable artifact on
        # disk. annotate_rubric.py will refuse to apply annotations
        # against a malformed pair anyway; refusing at write time is
        # the earlier, more diagnosable failure mode.
        try:
            validate_artifact(
                pair,
                "ground_truth_pair",
                None,
                require_artifact_type_field=False,
            )
        except ArtifactValidationError as exc:
            print(
                f"skip: pair derived from decision_text={decision.get('decision_text', '')[:60]!r} "
                f"failed schema: {exc}",
                file=sys.stderr,
            )
            pairs_skipped += 1
            continue
        target = out_dir / f"{pair['pair_id']}.json"
        _write_pair(target, pair)
        pairs_written += 1

    return {
        "status": "success" if pairs_written > 0 else "failure",
        "pairs_written": pairs_written,
        "pairs_skipped": pairs_skipped,
        "output_dir": str(out_dir),
        "reason": "" if pairs_written > 0 else "no_pairs_passed_validation",
        "extraction_path": str(artifact_path),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--data-lake", required=True)
    args = parser.parse_args(argv)

    # Mobile workflow_dispatch inputs frequently arrive with a trailing
    # space pasted from a phone keyboard; strip every string arg before
    # the slug is used in an exact-string match against source_record.
    for _attr in vars(args):
        _val = getattr(args, _attr)
        if isinstance(_val, str):
            setattr(args, _attr, _val.strip())

    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            f"error: --data-lake path is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 1

    result = generate_pairs(data_lake=data_lake, source_id=args.source_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "success":
        print(
            f"FAIL: {result['reason']} (source_id={args.source_id})",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
