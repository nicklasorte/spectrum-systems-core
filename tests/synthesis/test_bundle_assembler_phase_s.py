"""Phase S.1: bundle assembly + Phase V verified extraction items.

These tests exercise the *real* ``BundleAssembler.assemble`` function --
the thing being fixed -- not a mock. Each test seeds a tmp directory
with a ``meeting_extraction`` artifact, optionally seeds the Phase V
feature flag, and asserts the assembled bundle contains (or excludes)
the verified item as appropriate.

Coverage maps 1:1 to S.1's unit test list:
  1. phase_v_verified=True item is included in the bundle.
  2. phase_v_verified=False item is excluded when the flag is enabled.
  3. Phase V flag enabled=false skips verification and includes all items.
  4. Bundle assembly emits candidate / eligible / phase_v_enabled log lines.
  5. Empty candidate list returns ``bundle_assembly_no_candidates`` (no crash).
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.bundle_assembler import BundleAssembler


def _seed_phase_v_flag(data_lake: Path, enabled: bool) -> None:
    cfg = data_lake / "store" / "artifacts" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "phase_v_post_hoc_verification_enabled.json").write_text(
        json.dumps({"enabled": bool(enabled)}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _seed_meeting_extraction(
    data_lake: Path,
    *,
    decision_status: str = "verified",
    claim_status: str = "verified",
) -> Path:
    sdl_root = data_lake / "store" / "artifacts"
    target_dir = sdl_root / "extractions"
    target_dir.mkdir(parents=True, exist_ok=True)
    source_artifact_id = str(uuid.uuid4())
    artifact = {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": source_artifact_id,
        "artifact_type": "meeting_extraction",
        "schema_version": "2.0.0",
        "created_at": "2026-01-01T00:00:00+00:00",
        "decisions": [
            {
                "decision_text": (
                    "Adopt the higher OOBE mask for adjacent channels."
                ),
                "decision_type": "approved",
                "stakeholders": ["FCC", "carriers"],
                "rationale": None,
                "source_turn_ids": ["chunk-1"],
                "source_turn_validation": "verified",
                "confidence": 0.9,
                "verification_status": decision_status,
            }
        ],
        "claims": [
            {
                "claim_text": (
                    "Adjacent channel interference exceeds threshold."
                ),
                "claim_type": "technical",
                "speaker": "Engineer A",
                "source_turn_ids": ["chunk-2"],
                "source_turn_validation": "verified",
                "confidence": 0.8,
                "verification_status": claim_status,
            }
        ],
        "action_items": [],
    }
    target_path = (
        target_dir / f"{source_artifact_id}_meeting_extraction.json"
    )
    target_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
    )
    return target_path


class BundleAssemblerPhaseSTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_lake = Path(self._tmp.name)
        self.repo_root = self.data_lake / "store"
        self.repo_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assemble(self) -> dict:
        return BundleAssembler(data_lake_path=self.data_lake).assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )

    def test_phase_v_verified_item_included(self) -> None:
        """S.1 unit 1: phase_v_verified=True item lands in the bundle."""
        _seed_phase_v_flag(self.data_lake, enabled=True)
        _seed_meeting_extraction(
            self.data_lake,
            decision_status="verified",
            claim_status="verified",
        )
        result = self._assemble()
        self.assertEqual(result["status"], "success", result.get("reason"))
        verified_items = [
            it for it in result["bundle"]["items"]
            if it["artifact_type"] == "verified_extraction_item"
        ]
        self.assertGreaterEqual(len(verified_items), 1)
        for it in verified_items:
            self.assertEqual(it["promoted_status"], "evidenced")
            self.assertEqual(
                it["inclusion_reason"], "phase_v_verified_extraction_item"
            )

    def test_phase_v_unverified_item_excluded(self) -> None:
        """S.1 unit 2: phase_v_verified=False item is excluded when flag on."""
        _seed_phase_v_flag(self.data_lake, enabled=True)
        _seed_meeting_extraction(
            self.data_lake,
            decision_status="unsupported",
            claim_status="contradicted",
        )
        result = self._assemble()
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["reason"], "no_eligible_artifacts")

    def test_phase_v_disabled_includes_all_items(self) -> None:
        """S.1 unit 3: flag=enabled:false skips Phase V and includes all."""
        _seed_phase_v_flag(self.data_lake, enabled=False)
        _seed_meeting_extraction(
            self.data_lake,
            decision_status="unsupported",
            claim_status="contradicted",
        )
        result = self._assemble()
        self.assertEqual(result["status"], "success", result.get("reason"))
        verified_items = [
            it for it in result["bundle"]["items"]
            if it["artifact_type"] == "verified_extraction_item"
        ]
        self.assertGreaterEqual(len(verified_items), 1)
        for it in verified_items:
            self.assertEqual(
                it["inclusion_reason"],
                "verification_disabled_extraction_item",
            )

    def test_assembly_logs_counts_and_flag_state(self) -> None:
        """S.1 unit 4: assemble() emits the diagnostic log lines."""
        _seed_phase_v_flag(self.data_lake, enabled=True)
        _seed_meeting_extraction(self.data_lake)
        with self.assertLogs(
            "spectrum_systems_core.synthesis.bundle_assembler",
            level=logging.INFO,
        ) as captured:
            self._assemble()
        joined = "\n".join(captured.output)
        self.assertIn("Bundle assembly: found", joined)
        self.assertIn("eligible after Phase V gate", joined)
        self.assertIn("phase_v_enabled=True", joined)

    def test_no_candidates_returns_finding_not_crash(self) -> None:
        """S.1 unit 5: empty candidate list emits a structured finding."""
        # Flag enabled, but no meeting_extraction on disk.
        _seed_phase_v_flag(self.data_lake, enabled=True)
        result = self._assemble()
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["reason"], "no_eligible_artifacts")
        finding = result.get("finding") or {}
        self.assertEqual(
            finding.get("artifact_type"), "bundle_assembly_no_candidates"
        )
        self.assertEqual(finding.get("candidate_count"), 0)
        self.assertTrue(finding.get("phase_v_enabled"))

    def test_phase_v_flag_missing_treated_as_disabled(self) -> None:
        """Defensive: a missing flag file is treated as disabled, so the
        verified-item fallback still produces eligible candidates."""
        # No flag file seeded at all.
        _seed_meeting_extraction(
            self.data_lake,
            decision_status="unsupported",
            claim_status="contradicted",
        )
        result = self._assemble()
        self.assertEqual(result["status"], "success", result.get("reason"))


if __name__ == "__main__":
    unittest.main()
