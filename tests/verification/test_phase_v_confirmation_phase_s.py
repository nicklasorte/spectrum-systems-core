"""Phase S.4: confirm Phase V runs end-to-end and produces an artifact.

Every prior pipeline run failed before this point so the verification
artifact was never observed in the data lake. The tests here drive
``apply_phase_v_if_enabled`` against a tmp_path (real filesystem, not a
mock) with the flag seeded enabled / disabled / missing, then assert:

  1. Phase V runs when the flag JSON declares ``enabled: true``.
  2. Phase V skips when the flag JSON declares ``enabled: false``.
  3. Phase V skips fail-closed when the flag file is missing entirely
     (no crash, no half-state).
  4. The ``source_verification_result`` artifact is written to
     ``<sdl_root>/verifications/`` and matches the on-disk path the
     ``synthesize`` step would later read.
  5. The artifact carries a ``summary.spurious_add_rate`` field and
     passes write-time schema validation.
"""
from __future__ import annotations

import json
import logging
import pathlib
import unittest
import uuid

from spectrum_systems_core.config.feature_flag import PHASE_V_FLAG_NAME
from spectrum_systems_core.verification._schemas import (
    validate_source_verification_result,
)
from spectrum_systems_core.verification.pipeline_integration import (
    apply_phase_v_if_enabled,
)


def _enable_flag(data_lake: pathlib.Path, enabled: bool) -> None:
    cfg = data_lake / "store" / "artifacts" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
        json.dumps({"enabled": bool(enabled)}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _claim(text: str, turn_ids):
    return {
        "claim_text": text,
        "claim_type": "technical",
        "speaker": "Alice",
        "source_turn_ids": list(turn_ids),
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }


def _meeting_extraction(claims):
    return {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": "1.1.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "decisions": [],
        "claims": list(claims),
        "action_items": [],
        "total_chunks_classified": len(claims),
        "off_topic_count": 0,
        "regulatory_verb_fallback_count": 0,
        "routing_quality_warning": False,
        "requires_human_dedup_count": 0,
        "extraction_run_id": "tex-phase-s",
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": True,
        "confidence_threshold": 0.5,
        "low_confidence_item_count": 0,
        "provenance": {"produced_by": "ExtractionMerger"},
    }


def _chunks(turn_ids):
    return {
        tid: {
            "chunk_id": tid,
            "text": "DoD agreed to the proposed adjacent-channel mask.",
            "speaker": "Alice",
            "timestamp": "00:00",
        }
        for tid in turn_ids
    }


class PhaseVConfirmationTests(unittest.TestCase):
    def _run(
        self,
        tmp_path: pathlib.Path,
        *,
        claims,
        api_caller,
        flag_state: str,
    ):
        if flag_state == "enabled":
            _enable_flag(tmp_path, enabled=True)
        elif flag_state == "disabled":
            _enable_flag(tmp_path, enabled=False)
        elif flag_state == "missing":
            pass
        else:
            raise ValueError(f"unknown flag_state {flag_state!r}")
        sdl_root = tmp_path / "store" / "artifacts"
        extraction = _meeting_extraction(claims)
        result = apply_phase_v_if_enabled(
            extraction, _chunks(t for c in claims for t in c["source_turn_ids"]),
            data_lake_path=tmp_path,
            sdl_root=sdl_root,
            api_caller=api_caller,
        )
        return result, extraction, sdl_root

    def test_runs_when_flag_enabled(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            caller = lambda p: {  # noqa: E731
                "verification_status": "verified",
                "supporting_text_excerpts": ["DoD agreed."],
                "verifier_confidence": 0.92,
                "verifier_rationale": "matches",
            }
            result, extraction, sdl_root = self._run(
                tmp_path,
                claims=[_claim("c1", ["t-1"])],
                api_caller=caller,
                flag_state="enabled",
            )
            self.assertIsNotNone(result)
            self.assertEqual(extraction["schema_version"], "2.0.0")
            self.assertTrue(extraction["verification_artifact_id"])
            self.assertEqual(
                extraction["claims"][0]["verification_status"], "verified",
            )

    def test_skips_when_flag_disabled(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            calls = []
            caller = lambda p: (calls.append(p), {})[1]  # noqa: E731
            result, extraction, sdl_root = self._run(
                tmp_path,
                claims=[_claim("c1", ["t-1"])],
                api_caller=caller,
                flag_state="disabled",
            )
            self.assertIsNone(result)
            self.assertEqual(extraction["schema_version"], "1.1.0")
            self.assertEqual(calls, [])
            self.assertFalse((sdl_root / "verifications").exists())

    def test_skips_fail_closed_when_flag_missing(self) -> None:
        """The flag-missing branch must short-circuit without raising --
        a fresh data lake with no seeded flag still has to complete the
        extraction call gracefully."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            result, extraction, sdl_root = self._run(
                tmp_path,
                claims=[_claim("c1", ["t-1"])],
                api_caller=lambda p: {},
                flag_state="missing",
            )
            self.assertIsNone(result)
            self.assertEqual(extraction["schema_version"], "1.1.0")

    def test_artifact_written_to_correct_path(self) -> None:
        """S.4 unit 4: the source_verification_result lives under
        ``<sdl_root>/verifications/`` with the canonical filename pattern.
        Synthesize's bundle assembler reads the same root."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            result, extraction, sdl_root = self._run(
                tmp_path,
                claims=[_claim("c1", ["t-1"])],
                api_caller=lambda p: {
                    "verification_status": "verified",
                    "supporting_text_excerpts": ["DoD agreed."],
                    "verifier_confidence": 0.9,
                    "verifier_rationale": "ok",
                },
                flag_state="enabled",
            )
            verif_dir = sdl_root / "verifications"
            self.assertTrue(verif_dir.is_dir())
            files = sorted(verif_dir.glob("*_source_verification_result.json"))
            self.assertEqual(len(files), 1)
            written = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(
                written["artifact_type"], "source_verification_result"
            )
            self.assertIn("summary", written)

    def test_summary_carries_spurious_add_rate(self) -> None:
        """S.4 unit 5: the verification artifact's summary contains
        ``spurious_add_rate`` and the artifact passes write-time
        validation."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            # Two claims; verifier marks first verified and second
            # unsupported so spurious_add_rate is 0.5 (>0, <1).
            state = {"i": 0}

            def caller(_prompt):
                idx = state["i"]
                state["i"] += 1
                if idx == 0:
                    return {
                        "verification_status": "verified",
                        "supporting_text_excerpts": ["DoD agreed."],
                        "verifier_confidence": 0.95,
                        "verifier_rationale": "matches",
                    }
                return {
                    "verification_status": "unsupported",
                    "supporting_text_excerpts": [],
                    "verifier_confidence": 0.8,
                    "verifier_rationale": "no overlap",
                }

            result, extraction, sdl_root = self._run(
                tmp_path,
                claims=[
                    _claim("c1", ["t-1"]),
                    _claim("c2", ["t-1"]),
                ],
                api_caller=caller,
                flag_state="enabled",
            )
            assert result is not None
            self.assertIn("summary", result)
            self.assertIn("spurious_add_rate", result["summary"])
            self.assertGreater(result["summary"]["spurious_add_rate"], 0.0)
            # Schema validation must pass for the artifact persisted on disk.
            files = list(
                (sdl_root / "verifications").glob(
                    "*_source_verification_result.json"
                )
            )
            self.assertEqual(len(files), 1)
            written = json.loads(files[0].read_text(encoding="utf-8"))
            # Calling validate_source_verification_result re-raises on any
            # schema drift, which would fail this test loudly.
            validate_source_verification_result(written)


class PhaseVDiagnosticLogTests(unittest.TestCase):
    def test_logs_flag_state_for_observability(self) -> None:
        """Phase S.4: emit ``Phase V enabled: ...`` so an operator
        watching the orchestrator output sees the gate state without
        having to inspect the seed JSON."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            _enable_flag(tmp_path, enabled=True)
            sdl_root = tmp_path / "store" / "artifacts"
            extraction = _meeting_extraction([_claim("c1", ["t-1"])])
            with self.assertLogs(
                "spectrum_systems_core.verification.pipeline_integration",
                level=logging.INFO,
            ) as captured:
                apply_phase_v_if_enabled(
                    extraction, _chunks(["t-1"]),
                    data_lake_path=tmp_path,
                    sdl_root=sdl_root,
                    api_caller=lambda p: {
                        "verification_status": "verified",
                        "supporting_text_excerpts": ["DoD agreed."],
                        "verifier_confidence": 0.9,
                        "verifier_rationale": "ok",
                    },
                )
            joined = "\n".join(captured.output)
            self.assertIn("Phase V enabled", joined)


if __name__ == "__main__":
    unittest.main()
