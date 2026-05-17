"""Phase T.6 tests: low-confidence gate + correction_candidate artifact."""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from spectrum_systems_core.extraction.low_confidence_gate import (
    CORRECTION_CANDIDATE_TTL_DAYS_ENV,
    LOW_CONF_CONFIDENCE_THRESHOLD_ENV,
    LOW_CONF_RATE_LIMIT_ENV,
    LOW_CONFIDENCE_GATE_ENABLED_ENV,
    build_correction_candidate,
    check_low_confidence,
    count_pending_candidates,
    scan_expired_candidates,
)
from spectrum_systems_core.validation import validate_artifact


def _extraction_artifact(
    decisions: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "artifact_type": "meeting_extraction",
        "decisions": decisions,
        "claims": claims,
        "action_items": [],
    }


class GateFiringTests(unittest.TestCase):

    def setUp(self) -> None:
        for env in (
            LOW_CONFIDENCE_GATE_ENABLED_ENV,
            LOW_CONF_CONFIDENCE_THRESHOLD_ENV,
            LOW_CONF_RATE_LIMIT_ENV,
        ):
            os.environ.pop(env, None)

    def tearDown(self) -> None:
        for env in (
            LOW_CONFIDENCE_GATE_ENABLED_ENV,
            LOW_CONF_CONFIDENCE_THRESHOLD_ENV,
            LOW_CONF_RATE_LIMIT_ENV,
        ):
            os.environ.pop(env, None)

    def test_no_finding_when_low_rate(self) -> None:
        decisions = [{"decision_text": "d", "confidence": 0.9} for _ in range(8)]
        claims = [{"claim_text": "c", "confidence": 0.2}]  # 1/9 low
        findings, path = check_low_confidence(
            _extraction_artifact(decisions, claims),
            source_id="s",
            sdl_root=None,
        )
        self.assertEqual(findings, [])
        self.assertIsNone(path)

    def test_finding_emitted_when_rate_exceeds_limit(self) -> None:
        decisions = [{"decision_text": "d", "confidence": 0.2} for _ in range(4)]
        claims = [{"claim_text": "c", "confidence": 0.9} for _ in range(6)]
        with tempfile.TemporaryDirectory() as td:
            findings, path = check_low_confidence(
                _extraction_artifact(decisions, claims),
                source_id="s-1",
                sdl_root=Path(td),
            )
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].finding_code, "low_confidence_extraction")
            self.assertEqual(findings[0].severity, "warn")
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())

    def test_gate_disabled_returns_empty(self) -> None:
        os.environ[LOW_CONFIDENCE_GATE_ENABLED_ENV] = "false"
        decisions = [{"decision_text": "d", "confidence": 0.1} for _ in range(10)]
        findings, path = check_low_confidence(
            _extraction_artifact(decisions, []),
            source_id="s",
            sdl_root=None,
        )
        self.assertEqual(findings, [])
        self.assertIsNone(path)


class CorrectionCandidateArtifactTests(unittest.TestCase):

    def test_artifact_validates_schema(self) -> None:
        artifact = build_correction_candidate(
            source_id="src",
            low_conf_decisions=[{"decision_text": "x"}],
            low_conf_claims=[],
            rate=0.4,
            threshold=0.6,
            rate_limit=0.3,
        )
        # Must not raise.
        validate_artifact(artifact, "correction_candidate")

    def test_expires_at_30_days_after_created_at(self) -> None:
        # RT pass 2: assert the date arithmetic, not just presence.
        artifact = build_correction_candidate(
            source_id="src",
            low_conf_decisions=[],
            low_conf_claims=[{"claim_text": "x"}],
            rate=0.5,
            threshold=0.6,
            rate_limit=0.3,
        )
        created = datetime.datetime.fromisoformat(artifact["created_at"])
        expires = datetime.datetime.fromisoformat(artifact["expires_at"])
        delta = expires - created
        self.assertEqual(delta.days, 30)

    def test_custom_ttl_via_env_var(self) -> None:
        try:
            os.environ[CORRECTION_CANDIDATE_TTL_DAYS_ENV] = "7"
            artifact = build_correction_candidate(
                source_id="src",
                low_conf_decisions=[],
                low_conf_claims=[],
                rate=0.5,
                threshold=0.6,
                rate_limit=0.3,
            )
            created = datetime.datetime.fromisoformat(artifact["created_at"])
            expires = datetime.datetime.fromisoformat(artifact["expires_at"])
            self.assertEqual((expires - created).days, 7)
        finally:
            os.environ.pop(CORRECTION_CANDIDATE_TTL_DAYS_ENV, None)


class ExpiredCandidateScanTests(unittest.TestCase):

    def test_expired_candidate_emits_info_finding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sdl_root = Path(td)
            # Build an artifact that expired yesterday.
            past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
            now = datetime.datetime.now(datetime.timezone.utc)
            artifact = {
                "artifact_type": "correction_candidate",
                "schema_version": "1.0.0",
                "correction_candidate_id": "cc-1",
                "source_id": "src",
                "created_at": past.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "expires_at": (past + datetime.timedelta(days=1)).strftime(
                    "%Y-%m-%dT%H:%M:%S+00:00"
                ),
                "low_confidence_decisions": [],
                "low_confidence_claims": [],
                "low_confidence_rate": 0.4,
                "status": "pending",
            }
            d = sdl_root / "correction_candidates" / "src"
            d.mkdir(parents=True)
            (d / "cc-1.json").write_text(json.dumps(artifact), encoding="utf-8")

            findings = scan_expired_candidates(sdl_root)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].finding_code, "correction_candidate_expired")
            self.assertEqual(findings[0].severity, "info")

    def test_pending_count_excludes_expired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sdl_root = Path(td)
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=10)
            past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)

            for label, exp in (("alive", future), ("dead", past)):
                a = {
                    "artifact_type": "correction_candidate",
                    "schema_version": "1.0.0",
                    "correction_candidate_id": label,
                    "source_id": "src",
                    "created_at": "1970-01-01T00:00:00+00:00",
                    "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "low_confidence_decisions": [],
                    "low_confidence_claims": [],
                    "low_confidence_rate": 0.4,
                    "status": "pending",
                }
                d = sdl_root / "correction_candidates" / "src"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{label}.json").write_text(json.dumps(a), encoding="utf-8")

            self.assertEqual(count_pending_candidates(sdl_root), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
