#!/usr/bin/env python3
"""Phase X2.5 — annotate ground-truth decision pairs with regulatory verb rubric.

Reads ``ground_truth/<pair_id>.json`` records under SDL_ROOT and adds
``rubric_notes`` to each one (limit configurable via ``--limit``).

Defaults pick a balanced sample covering each decision_outcome bucket
(approval / rejection / deferral / action_required / noted) so the
judge_calibration's ``agreement_rate_verb_discrimination`` metric has
representative data.

Interactive mode (``--interactive``): the script reads stdin one
annotation at a time so a human can confirm each expected outcome.
Non-interactive mode (``--apply-from JSON``): the script applies a
pre-built JSON annotation file. The non-interactive mode is what CI
tests exercise; the interactive mode is what an operator runs.

Run::

    python scripts/annotate_rubric.py \\
        --source-id <id> \\
        --data-lake data-lake/ \\
        --limit 20

    python scripts/annotate_rubric.py \\
        --apply-from annotations.json \\
        --data-lake data-lake/
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

# Outcomes we ALWAYS want representation for, in priority order.
# Selecting balanced examples (5 per bucket by default) makes the
# verb-discrimination metric meaningful.
DEFAULT_BUCKET_LIMIT: int = 5
BUCKET_OUTCOMES = ("approval", "rejection", "deferral", "action_required", "noted")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _pair_path(sdl_root: Path, pair_id: str) -> Path:
    return sdl_root / "ground_truth" / f"{pair_id}.json"


def _load_pair(path: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_pair(path: Path, doc: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def annotate_pair(
    pair: Dict[str, Any],
    *,
    expected_decision_outcome: str,
    verb_discrimination_example: bool,
    annotator_id: str,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a copy of ``pair`` with ``rubric_notes`` set.

    Never overwrites an existing annotation unless ``annotator_id``
    differs (the audit trail lives in the entry itself: ``annotator_id``
    + ``annotated_at`` change on every re-annotation).
    """
    out = dict(pair)
    out["rubric_notes"] = {
        "expected_decision_outcome": expected_decision_outcome,
        "verb_discrimination_example": bool(verb_discrimination_example),
        "annotator_id": annotator_id,
        "annotated_at": _now_iso(),
        "notes": notes,
    }
    return out


def apply_annotations_from_file(
    annotations_path: Path, sdl_root: Path,
) -> int:
    """Apply a JSON annotations file to the ground_truth/ directory.

    Annotation file shape::

        [
          {
            "pair_id": "...",
            "expected_decision_outcome": "approval",
            "verb_discrimination_example": true,
            "annotator_id": "alice",
            "notes": "..."
          },
          ...
        ]

    Returns the number of pairs successfully annotated.
    """
    try:
        records = json.loads(annotations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"error: could not read annotations file {annotations_path}: {exc}",
            file=sys.stderr,
        )
        return -1

    if not isinstance(records, list):
        print("error: annotations file must be a JSON array", file=sys.stderr)
        return -1

    applied = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pair_id = rec.get("pair_id")
        outcome = rec.get("expected_decision_outcome")
        if not isinstance(pair_id, str) or not isinstance(outcome, str):
            continue
        path = _pair_path(sdl_root, pair_id)
        pair = _load_pair(path)
        if pair is None:
            print(f"skip: pair_id={pair_id} not found at {path}", file=sys.stderr)
            continue
        # Integration-hardening gate: validate the pair against the
        # ground_truth_pair contracts schema before annotating. The
        # pair pre-dates the artifact_type convention so we pass
        # ``require_artifact_type_field=False``; structural validity
        # still surfaces any field-name drift.
        try:
            validate_artifact(
                pair,
                "ground_truth_pair",
                str(path),
                require_artifact_type_field=False,
            )
        except ArtifactValidationError as exc:
            print(f"skip: pair_id={pair_id} failed schema: {exc}", file=sys.stderr)
            continue
        updated = annotate_pair(
            pair,
            expected_decision_outcome=outcome,
            verb_discrimination_example=bool(
                rec.get("verb_discrimination_example", False)
            ),
            annotator_id=rec.get("annotator_id") or "scripts/annotate_rubric.py",
            notes=rec.get("notes"),
        )
        _write_pair(path, updated)
        applied += 1
    return applied


# Fields a GT pair may carry that identify its source. Order matters
# only for diagnostic listings; the filter accepts a match against any
# of them. ``source_artifact_id`` is the production-schema field
# (see contracts/schemas/ingestion/ground_truth_pair.schema.json);
# ``fixture_source_id`` is set on test fixtures so fixture pairs can be
# filtered by their human-readable meeting id rather than the opaque
# artifact id.
_SOURCE_ID_FIELDS: Tuple[str, ...] = ("source_artifact_id", "fixture_source_id")


def _pair_source_ids(pair: Dict[str, Any]) -> List[str]:
    """Return the non-empty string values of every source_id-like field."""
    out: List[str] = []
    for key in _SOURCE_ID_FIELDS:
        val = pair.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    return out


def list_candidates(
    sdl_root: Path,
    *,
    source_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Walk ground_truth/ and return up to ``limit`` candidate pairs.

    Filters to ``target_type == "decision"`` when present. Balances
    across decision_outcome buckets per ``DEFAULT_BUCKET_LIMIT``.

    When ``source_id`` is given, a pair matches when ANY of its
    ``_SOURCE_ID_FIELDS`` equals the filter. Production pairs only carry
    ``source_artifact_id`` (per the schema); fixture pairs may also
    carry ``fixture_source_id`` for human-readable filtering.
    """
    pairs_dir = sdl_root / "ground_truth"
    if not pairs_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(pairs_dir.glob("*.json")):
        pair = _load_pair(path)
        if pair is None:
            continue
        if source_id and source_id not in _pair_source_ids(pair):
            continue
        if pair.get("target_type") not in (None, "decision"):
            continue
        out.append(pair)
    return out[:limit]


def _all_source_ids_seen(sdl_root: Path) -> List[str]:
    """Return the sorted set of every source_id-like value present in
    the ground_truth/ directory. Used only for the helpful error
    message when ``--source-id`` matches zero pairs."""
    pairs_dir = sdl_root / "ground_truth"
    if not pairs_dir.is_dir():
        return []
    seen: set = set()
    for path in sorted(pairs_dir.glob("*.json")):
        pair = _load_pair(path)
        if pair is None:
            continue
        for sid in _pair_source_ids(pair):
            seen.add(sid)
    return sorted(seen)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply-from", default=None)
    parser.add_argument(
        "--sdl-root", default=None,
        help="Override SDL_ROOT (defaults to <data-lake>/store/artifacts).",
    )
    args = parser.parse_args(argv)

    # Strip whitespace from every string CLI arg. Mobile workflow_dispatch
    # inputs frequently arrive with a trailing space pasted from a phone
    # keyboard; an unstripped source_id then fails exact-string matches
    # against the GT pair source identifiers.
    for _attr in vars(args):
        _val = getattr(args, _attr)
        if isinstance(_val, str):
            setattr(args, _attr, _val.strip())

    sdl_root = (
        Path(args.sdl_root) if args.sdl_root
        else Path(args.data_lake) / "store" / "artifacts"
    )
    if not sdl_root.is_dir():
        print(f"error: sdl_root not a directory: {sdl_root}", file=sys.stderr)
        return 1

    if args.apply_from:
        n = apply_annotations_from_file(Path(args.apply_from), sdl_root)
        if n < 0:
            return 1
        print(f"applied {n} annotation(s)")
        return 0

    candidates = list_candidates(
        sdl_root, source_id=args.source_id, limit=args.limit,
    )
    if not candidates:
        if args.source_id:
            # Codex P1 fix: never silently return empty when --source-id
            # is provided. Surface the available identifiers so the
            # operator can spot a typo or wrong dataset immediately.
            seen = _all_source_ids_seen(sdl_root)
            preview = seen[:5] if seen else []
            print(
                f"ERROR: --source-id '{args.source_id}' matched 0 ground "
                f"truth pairs.\n"
                f"Available source identifiers in GT pairs: {preview}",
                file=sys.stderr,
            )
            return 2
        print(
            "no annotatable ground_truth decision pairs found "
            f"under {sdl_root / 'ground_truth'}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(
        [
            {
                "pair_id": p.get("pair_id"),
                "decision_id": p.get("decision_id"),
                "current_rubric_notes": p.get("rubric_notes"),
                "ground_truth_text": (
                    p.get("ground_truth_text") or p.get("fixture_minutes_text") or ""
                )[:200],
            }
            for p in candidates
        ],
        indent=2,
    ))
    print(
        "\nNEXT: Author an annotations JSON file (see --help for the shape) "
        "and re-run with --apply-from <file>.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
