#!/usr/bin/env python3
"""Phase X2.6 — submit a human review decision for a correction_candidate.

The minimum viable HITL gate: a single CLI that lands a structured
``human_review_artifact`` on disk and updates the underlying
``correction_candidate.review_status`` to ``reviewed``. No web UI, no
email, just a deterministic CLI script.

Reviewer policy (documented in CLAUDE.md; the system stores the field
but does not enforce uniqueness):

    Reviewer must be a different person from the operator who ran the
    extraction. The audit trail is the reviewer_id on the artifact.

Run::

    python scripts/submit_review.py \\
        --candidate-id <uuid> \\
        --reviewer-id <your-name> \\
        --decision accept|reject|revise \\
        --notes "..." \\
        --data-lake <path>
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

CORRECTION_CANDIDATES_RELPATH = "store/artifacts/correction_candidates"
HUMAN_REVIEWS_RELPATH = "store/artifacts/human_reviews"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _find_candidate(
    data_lake: Path, candidate_id: str
) -> Optional[Path]:
    """Locate the correction_candidate file by id.

    The candidate may be stored at the top of correction_candidates/
    or nested under a source_id subdir; we walk both.
    """
    root = data_lake / CORRECTION_CANDIDATES_RELPATH
    if not root.exists():
        return None
    for path in root.rglob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("correction_candidate_id") == candidate_id:
            return path
    return None


def _candidate_expired(candidate: Dict[str, Any]) -> bool:
    """Return True if candidate.expires_at is in the past."""
    expires_at = candidate.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        return False
    try:
        expiry = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    return expiry < now


def submit_review(
    data_lake: Path,
    candidate_id: str,
    reviewer_id: str,
    decision: str,
    severity_assessment: str,
    notes: Optional[str],
) -> int:
    if decision not in ("accept", "reject", "revise"):
        print(
            f"error: --decision must be accept|reject|revise (got {decision!r})",
            file=sys.stderr,
        )
        return 2

    candidate_path = _find_candidate(data_lake, candidate_id)
    if candidate_path is None:
        print(
            f"error: correction_candidate {candidate_id} not found under "
            f"{data_lake / CORRECTION_CANDIDATES_RELPATH}",
            file=sys.stderr,
        )
        return 1

    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    source_id = candidate.get("source_id") or "unknown"
    expired = _candidate_expired(candidate)
    if expired:
        print(
            f"warn: candidate {candidate_id} has already expired "
            f"({candidate.get('expires_at')}); review will be recorded "
            "but flagged review_after_expiry=true.",
            file=sys.stderr,
        )

    review_id = str(uuid.uuid4())
    artifact: Dict[str, Any] = {
        "artifact_type": "human_review_artifact",
        "schema_version": "1.0.0",
        "human_review_artifact_id": review_id,
        "correction_candidate_id": candidate_id,
        "source_id": source_id,
        "reviewer_id": reviewer_id,
        "reviewed_at": _now_iso(),
        "decision": decision,
        "severity_assessment": severity_assessment,
        "notes": notes,
        "review_after_expiry": bool(expired),
        "provenance": {"produced_by": "scripts/submit_review.py"},
    }

    # Validate via the package validator so a malformed artifact never
    # lands on disk. Import lazily so the script still works when
    # invoked from a fresh checkout that hasn't installed the package.
    try:
        from spectrum_systems_core.validation import (
            ArtifactValidationError, validate_artifact,
        )
        try:
            validate_artifact(artifact, "human_review_artifact")
        except ArtifactValidationError as exc:
            print(f"error: artifact validation failed: {exc}", file=sys.stderr)
            return 1
    except ImportError:
        pass

    out_dir = data_lake / HUMAN_REVIEWS_RELPATH / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{review_id}.json"
    out_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Update the candidate's review_status. Two states distinguish
    # post-expiry reviews from on-time reviews so a future audit can
    # tell whether the gate was respected.
    candidate["review_status"] = (
        "reviewed_after_expiry" if expired else "reviewed"
    )
    candidate["review_artifact_id"] = review_id
    candidate_path.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        f"wrote {out_path}\n"
        f"updated {candidate_path} -> review_status="
        f"{candidate['review_status']}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument(
        "--decision", required=True,
        choices=["accept", "reject", "revise"],
    )
    parser.add_argument(
        "--severity-assessment", default="ok",
        choices=["ok", "warn", "halt"],
    )
    parser.add_argument("--notes", default=None)
    parser.add_argument("--data-lake", required=True)
    args = parser.parse_args(argv)

    # Strip whitespace from every string CLI arg. Mobile workflow_dispatch
    # inputs frequently arrive with a trailing space pasted from a phone
    # keyboard; an unstripped candidate_id then fails exact-string matches
    # against the correction_candidate's correction_candidate_id field.
    for _attr in vars(args):
        _val = getattr(args, _attr)
        if isinstance(_val, str):
            setattr(args, _attr, _val.strip())

    data_lake = Path(args.data_lake).resolve()
    if not data_lake.is_dir():
        print(f"error: data-lake does not exist: {data_lake}", file=sys.stderr)
        return 1
    return submit_review(
        data_lake=data_lake,
        candidate_id=args.candidate_id,
        reviewer_id=args.reviewer_id,
        decision=args.decision,
        severity_assessment=args.severity_assessment,
        notes=args.notes,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
