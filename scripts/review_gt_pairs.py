#!/usr/bin/env python3
"""Phase P1: confirm a ground_truth_pair's expected_decision_outcome.

The Phase P1 eval gate refuses to score a ground_truth_pair until a
sibling ``<pair_id>_review.json`` (artifact_type ``gt_pair_review``) is
present on disk with ``outcome_confirmed: true``. This script is the
human-in-the-loop seam that produces those review records.

Default behaviour (no ``--pair-id``): list every pair under
``<data-lake>/store/artifacts/ground_truth/`` with its current
``expected_decision_outcome`` and whether a review artifact already
exists. Useful as a dry-run inventory before the operator commits to
review records.

``--confirm-all`` writes one ``gt_pair_review`` with
``outcome_confirmed=true`` for every pair that is missing a review.
Existing reviews are NOT overwritten unless ``--overwrite`` is also
passed; this avoids silently re-confirming a pair the operator
previously rejected.

``--pair-id <uuid>`` targets a single pair. ``--reject`` writes the
review with ``outcome_confirmed=false`` so the eval gate emits
``gt_pair_outcome_rejected`` and halts on that pair.

The script reads pipeline artifacts (ground_truth_pair) and writes
artifacts (gt_pair_review), so per CLAUDE.md it MUST have a contract
test in ``tests/integration/`` — see
``tests/integration/test_review_gt_pairs_contract.py``.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running as a standalone script: add the src/ directory so the
# package import resolves without a prior ``pip install -e .``.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from spectrum_systems_core.validation import (  # noqa: E402
    ArtifactValidationError,
    SchemaNotFoundError,
    validate_artifact,
)


REVIEW_SUFFIX: str = "_review.json"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _ground_truth_dir(data_lake: Path) -> Path:
    return data_lake / "store" / "artifacts" / "ground_truth"


def _is_review_filename(name: str) -> bool:
    return name.endswith(REVIEW_SUFFIX)


def _is_pair_filename(name: str) -> bool:
    return name.endswith(".json") and not _is_review_filename(name)


def _load_pairs(data_lake: Path) -> List[Dict[str, Any]]:
    gt_dir = _ground_truth_dir(data_lake)
    if not gt_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(gt_dir.glob("*.json")):
        if not _is_pair_filename(path.name):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if not isinstance(doc.get("pair_id"), str):
            continue
        out.append(doc)
    return out


def _review_path(data_lake: Path, pair_id: str) -> Path:
    return _ground_truth_dir(data_lake) / f"{pair_id}{REVIEW_SUFFIX}"


def _load_review(data_lake: Path, pair_id: str) -> Optional[Dict[str, Any]]:
    path = _review_path(data_lake, pair_id)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def build_review(
    *,
    pair_id: str,
    reviewer_id: str,
    outcome_confirmed: bool,
    expected_decision_outcome: Optional[str],
    notes: Optional[str] = None,
    reviewed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a schema-valid ``gt_pair_review`` artifact dict."""
    return {
        "artifact_type": "gt_pair_review",
        "schema_version": "1.0.0",
        "pair_id": pair_id,
        "reviewer_id": reviewer_id,
        "outcome_confirmed": bool(outcome_confirmed),
        "expected_decision_outcome": expected_decision_outcome,
        "notes": notes,
        "reviewed_at": reviewed_at or _now_iso(),
    }


def write_review(data_lake: Path, review: Dict[str, Any]) -> Path:
    """Validate and write a ``gt_pair_review`` artifact to disk.

    Raises ``ArtifactValidationError`` if the artifact does not pass
    schema validation — the file is NOT written in that case, so a
    malformed review can never block a downstream eval gate by accident.
    """
    validate_artifact(review, "gt_pair_review")
    pair_id = review["pair_id"]
    target = _review_path(data_lake, pair_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(review, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _list_pairs(data_lake: Path, out) -> int:
    pairs = _load_pairs(data_lake)
    if not pairs:
        print(
            f"no ground_truth pairs under {_ground_truth_dir(data_lake)}",
            file=out,
        )
        return 0
    print(f"{len(pairs)} ground_truth pairs under {_ground_truth_dir(data_lake)}:", file=out)
    for pair in pairs:
        pid = pair["pair_id"]
        outcome = pair.get("expected_decision_outcome") or "<none>"
        text = (pair.get("ground_truth_text") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        review = _load_review(data_lake, pid)
        if review is None:
            review_state = "MISSING"
        elif review.get("outcome_confirmed"):
            review_state = f"confirmed (by {review.get('reviewer_id', '<unknown>')})"
        else:
            review_state = f"REJECTED (by {review.get('reviewer_id', '<unknown>')})"
        print(f"  - {pid}", file=out)
        print(f"      outcome:  {outcome}", file=out)
        print(f"      text:     {text}", file=out)
        print(f"      review:   {review_state}", file=out)
    return 0


def _confirm_one(
    data_lake: Path,
    pair: Dict[str, Any],
    *,
    reviewer_id: str,
    notes: Optional[str],
    outcome_confirmed: bool,
    overwrite: bool,
    out,
) -> bool:
    pid = pair["pair_id"]
    existing = _load_review(data_lake, pid)
    if existing is not None and not overwrite:
        print(
            f"skip pair_id={pid}: review already exists "
            f"(outcome_confirmed={existing.get('outcome_confirmed')}). "
            "Pass --overwrite to replace it.",
            file=out,
        )
        return False

    outcome = pair.get("expected_decision_outcome")
    review = build_review(
        pair_id=pid,
        reviewer_id=reviewer_id,
        outcome_confirmed=outcome_confirmed,
        expected_decision_outcome=outcome if isinstance(outcome, str) else None,
        notes=notes,
    )
    try:
        target = write_review(data_lake, review)
    except (ArtifactValidationError, SchemaNotFoundError) as exc:
        print(f"error: review for pair_id={pid} failed validation: {exc}", file=out)
        return False
    status = "confirmed" if outcome_confirmed else "REJECTED"
    print(f"wrote: {target.relative_to(data_lake)} ({status})", file=out)
    return True


def main(argv: Optional[List[str]] = None, out=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase P1: list / confirm / reject ground_truth_pair outcomes "
            "via gt_pair_review artifacts."
        )
    )
    parser.add_argument(
        "--data-lake",
        default=os.environ.get("DATA_LAKE_PATH", "data-lake"),
        help=(
            "Path to the data-lake root. Defaults to DATA_LAKE_PATH or "
            "the ./data-lake directory."
        ),
    )
    parser.add_argument(
        "--pair-id",
        default=None,
        help="Target a single pair_id. Without --confirm-all this is required.",
    )
    parser.add_argument(
        "--confirm-all",
        action="store_true",
        help=(
            "Write a confirming gt_pair_review for every pair missing one. "
            "Existing reviews are not overwritten unless --overwrite is set."
        ),
    )
    parser.add_argument(
        "--reject",
        action="store_true",
        help=(
            "Write a rejecting gt_pair_review (outcome_confirmed=false). "
            "Requires --pair-id. The eval gate will halt on this pair "
            "with gt_pair_outcome_rejected."
        ),
    )
    parser.add_argument(
        "--reviewer-id",
        default=os.environ.get("USER", "operator"),
        help="Reviewer id stamped on the review artifact.",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="Optional free-text notes recorded on the review artifact.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing review when targeting an already-reviewed pair.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=(
            "Print every pair with its current review state and exit 0. "
            "This is the default when no other action flag is set."
        ),
    )

    args = parser.parse_args(argv)
    stream = out if out is not None else sys.stdout

    data_lake = Path(args.data_lake).resolve()
    if not data_lake.is_dir():
        print(f"error: data-lake path does not exist: {data_lake}", file=stream)
        return 1

    if args.confirm_all and args.reject:
        print(
            "error: --confirm-all and --reject are mutually exclusive",
            file=stream,
        )
        return 1
    if args.reject and not args.pair_id:
        print("error: --reject requires --pair-id", file=stream)
        return 1

    # Default action when no flag is set is to list.
    if not (args.confirm_all or args.reject or args.pair_id) or args.list:
        return _list_pairs(data_lake, stream)

    pairs = _load_pairs(data_lake)
    if not pairs:
        print(
            f"no ground_truth pairs under {_ground_truth_dir(data_lake)}",
            file=stream,
        )
        return 1

    targets: List[Dict[str, Any]]
    if args.pair_id:
        targets = [p for p in pairs if p["pair_id"] == args.pair_id]
        if not targets:
            print(
                f"error: pair_id={args.pair_id} not found in "
                f"{_ground_truth_dir(data_lake)}",
                file=stream,
            )
            return 1
    else:
        targets = pairs

    outcome_confirmed = not args.reject
    written = 0
    for pair in targets:
        if _confirm_one(
            data_lake,
            pair,
            reviewer_id=args.reviewer_id,
            notes=args.notes,
            outcome_confirmed=outcome_confirmed,
            overwrite=args.overwrite,
            out=stream,
        ):
            written += 1

    print(f"reviews written: {written} (targets: {len(targets)})", file=stream)
    return 0 if (written or not targets) else 1


if __name__ == "__main__":
    sys.exit(main())
