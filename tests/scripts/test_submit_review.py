"""Phase X2.6 — submit_review.py tests.

Defends the trust property that a human_review_artifact is the ONLY
way to mark a correction_candidate as reviewed, and that the artifact
itself validates against its schema.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import submit_review  # noqa: E402

from spectrum_systems_core.validation import validate_artifact


CC_REL = "store/artifacts/correction_candidates"


def _seed_candidate(
    data_lake: Path,
    candidate_id: str,
    *,
    source_id: str = "src1",
    expires_at: str | None = None,
    status: str = "pending",
) -> Path:
    cc_dir = data_lake / CC_REL / source_id
    cc_dir.mkdir(parents=True, exist_ok=True)
    path = cc_dir / f"{candidate_id}.json"
    if expires_at is None:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=7)
        ).isoformat()
    rec = {
        "artifact_type": "correction_candidate",
        "schema_version": "1.0.0",
        "correction_candidate_id": candidate_id,
        "source_id": source_id,
        "created_at": "2026-05-01T00:00:00+00:00",
        "expires_at": expires_at,
        "low_confidence_rate": 0.4,
        "low_confidence_decisions": [],
        "low_confidence_claims": [],
        "status": status,
    }
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return path


def _read_first_review(data_lake: Path, source_id: str = "src1") -> Dict[str, Any]:
    review_dir = data_lake / "store" / "artifacts" / "human_reviews" / source_id
    files = list(review_dir.glob("*.json"))
    assert files, f"no review artifacts found under {review_dir}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# --- Required fields + schema ----------------------------------


def test_submit_review_writes_artifact_with_all_required_fields(
    tmp_path: Path,
) -> None:
    _seed_candidate(tmp_path, "cand-1")
    rc = submit_review.main([
        "--candidate-id", "cand-1",
        "--reviewer-id", "alice",
        "--decision", "accept",
        "--notes", "verified against source",
        "--data-lake", str(tmp_path),
    ])
    assert rc == 0
    review = _read_first_review(tmp_path)
    assert review["correction_candidate_id"] == "cand-1"
    assert review["reviewer_id"] == "alice"
    assert review["decision"] == "accept"
    assert review["notes"] == "verified against source"
    assert review["reviewed_at"]
    assert review["human_review_artifact_id"]


def test_submit_review_artifact_passes_schema(tmp_path: Path) -> None:
    _seed_candidate(tmp_path, "cand-2")
    submit_review.main([
        "--candidate-id", "cand-2", "--reviewer-id", "alice",
        "--decision", "accept", "--data-lake", str(tmp_path),
    ])
    review = _read_first_review(tmp_path)
    validate_artifact(review, "human_review_artifact")


# --- Decision enum -------------------------------------------


def test_submit_review_rejects_unknown_decision(tmp_path: Path) -> None:
    _seed_candidate(tmp_path, "cand-3")
    with pytest.raises(SystemExit):
        submit_review.main([
            "--candidate-id", "cand-3", "--reviewer-id", "alice",
            "--decision", "frobnicate", "--data-lake", str(tmp_path),
        ])


# --- Side-effect on candidate -----------------------------------


def test_submit_review_marks_candidate_reviewed(tmp_path: Path) -> None:
    cc_path = _seed_candidate(tmp_path, "cand-4")
    submit_review.main([
        "--candidate-id", "cand-4", "--reviewer-id", "alice",
        "--decision", "revise", "--data-lake", str(tmp_path),
    ])
    cc = json.loads(cc_path.read_text(encoding="utf-8"))
    assert cc["review_status"] == "reviewed"
    assert cc["review_artifact_id"]


def test_expired_candidate_records_review_after_expiry(tmp_path: Path) -> None:
    past = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=1)
    ).isoformat()
    cc_path = _seed_candidate(tmp_path, "cand-5", expires_at=past)
    rc = submit_review.main([
        "--candidate-id", "cand-5", "--reviewer-id", "alice",
        "--decision", "accept", "--data-lake", str(tmp_path),
    ])
    assert rc == 0
    cc = json.loads(cc_path.read_text(encoding="utf-8"))
    assert cc["review_status"] == "reviewed_after_expiry"
    review = _read_first_review(tmp_path)
    assert review["review_after_expiry"] is True


# --- Unknown candidate / missing data -------------------------


def test_unknown_candidate_returns_nonzero(tmp_path: Path) -> None:
    (tmp_path / "store" / "artifacts" / "correction_candidates").mkdir(
        parents=True
    )
    rc = submit_review.main([
        "--candidate-id", "does-not-exist", "--reviewer-id", "alice",
        "--decision", "accept", "--data-lake", str(tmp_path),
    ])
    assert rc == 1


# --- Severity assessment ------------------------------------


def test_severity_assessment_recorded_in_artifact(tmp_path: Path) -> None:
    _seed_candidate(tmp_path, "cand-6")
    submit_review.main([
        "--candidate-id", "cand-6", "--reviewer-id", "bob",
        "--decision", "reject", "--severity-assessment", "warn",
        "--data-lake", str(tmp_path),
    ])
    review = _read_first_review(tmp_path)
    assert review["severity_assessment"] == "warn"


# --- Idempotency on already-reviewed candidate ---------------


def test_double_review_records_second_artifact(tmp_path: Path) -> None:
    _seed_candidate(tmp_path, "cand-7")
    submit_review.main([
        "--candidate-id", "cand-7", "--reviewer-id", "alice",
        "--decision", "accept", "--data-lake", str(tmp_path),
    ])
    rc = submit_review.main([
        "--candidate-id", "cand-7", "--reviewer-id", "bob",
        "--decision", "revise", "--data-lake", str(tmp_path),
    ])
    # Allowed (idempotent); the candidate's review_artifact_id
    # is updated to the latest one.
    assert rc == 0
    review_dir = tmp_path / "store" / "artifacts" / "human_reviews" / "src1"
    assert len(list(review_dir.glob("*.json"))) == 2
