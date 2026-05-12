#!/usr/bin/env python3
"""Fast smoke test using a small hand-authored fixture transcript.

No data-lake required. In mock mode, no API calls are made (just import
checks). In real mode, runs the full extractor stack against 10 fixture
chunks. Target runtime: < 15 seconds.

Usage:
  python scripts/smoke_test_fixture.py
  python scripts/smoke_test_fixture.py --mock  # skip API calls, test wiring only
  python scripts/smoke_test_fixture.py --enable-phase-v  # also run PostHocVerifier
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import uuid


FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "smoke_test_transcript.jsonl"
)


def _load_fixture_chunks() -> list[dict]:
    return [
        json.loads(line)
        for line in FIXTURE_PATH.read_text().splitlines()
        if line.strip()
    ]


def run_phase_v_smoke_test() -> bool:
    """Run the Phase V verifier on the fixture transcript.

    No real LLM is invoked; a deterministic stub api_caller returns
    ``verified`` for the first item (proving the gate's happy path) and
    ``unsupported`` for the rest (proving the post-hoc routing path).
    The verifier produces a source_verification_result artifact under a
    temporary data lake, and the smoke test confirms:

      * the artifact is written under sdl_root/verifications/
      * at least one item carries verification_status=="verified"
      * the meeting_extraction is bumped to schema_version 2.0.0
      * the verification_artifact_id field is populated

    Returns True on success.
    """
    print("\n--- Phase V smoke test ---")
    chunks = _load_fixture_chunks()

    try:
        from spectrum_systems_core.config.feature_flag import (
            PHASE_V_FLAG_NAME,
        )
        from spectrum_systems_core.verification.pipeline_integration import (
            apply_phase_v_if_enabled,
        )
        from spectrum_systems_core.verification.post_hoc_verifier import (
            _coerce_item_id,
        )
    except ImportError as exc:
        print(f"PHASE V SMOKE TEST FAILED: import error: {exc}")
        return False

    # Build a tiny meeting_extraction synthesized from the fixture: one
    # claim per chunk, citing the chunk's id. The verifier does not
    # require the chunks have any particular shape beyond chunk_id.
    claims = []
    chunks_by_id = {}
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id")
        if not cid:
            continue
        text = c.get("text") or ""
        chunks_by_id[cid] = {
            "chunk_id": cid,
            "text": text,
            "speaker": c.get("speaker", "Alice"),
            "timestamp": c.get("timestamp", "00:00:00"),
        }
        # Each claim claims the same words as the chunk -- the verifier
        # should mark it verified.
        claims.append({
            "claim_text": text[:120].strip() or "stand-in claim",
            "claim_type": "regulatory",
            "speaker": c.get("speaker", "Alice"),
            "source_turn_ids": [cid],
            "source_turn_validation": "verified",
            "confidence": 0.9,
        })
    if not claims:
        print("PHASE V SMOKE TEST FAILED: fixture produced zero claims")
        return False

    extraction = {
        "meeting_extraction_id": str(uuid.uuid4()),
        "source_artifact_id": str(uuid.uuid4()),
        "artifact_type": "meeting_extraction",
        "schema_version": "1.1.0",
        "created_at": "2026-05-12T00:00:00+00:00",
        "decisions": [],
        "claims": claims,
        "action_items": [],
        "total_chunks_classified": len(chunks),
        "off_topic_count": 0,
        "regulatory_verb_fallback_count": 0,
        "routing_quality_warning": False,
        "requires_human_dedup_count": 0,
        "extraction_run_id": "tex-phase-v-smoke",
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": True,
        "confidence_threshold": 0.5,
        "low_confidence_item_count": 0,
        "provenance": {"produced_by": "ExtractionMerger"},
    }

    # Deterministic mock verifier: returns ``verified`` for the first
    # item and ``unsupported`` for the rest so the smoke test exercises
    # both branches.
    state = {"i": 0}

    def stub_caller(_prompt):
        idx = state["i"]
        state["i"] += 1
        if idx == 0:
            return {
                "verification_status": "verified",
                "supporting_text_excerpts": [
                    chunks_by_id[claims[0]["source_turn_ids"][0]]["text"][:80]
                ],
                "verifier_confidence": 0.95,
                "verifier_rationale": "smoke-test verified.",
            }
        return {
            "verification_status": "unsupported",
            "supporting_text_excerpts": [],
            "verifier_confidence": 0.85,
            "verifier_rationale": "smoke-test unsupported (mock).",
        }

    with tempfile.TemporaryDirectory(prefix="phase-v-smoke-") as tmp:
        tmp_path = pathlib.Path(tmp)
        # Enable Phase V flag.
        flag_dir = tmp_path / "store" / "artifacts" / "config"
        flag_dir.mkdir(parents=True)
        (flag_dir / f"{PHASE_V_FLAG_NAME}_enabled.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8",
        )
        sdl_root = tmp_path / "store" / "artifacts"

        result = apply_phase_v_if_enabled(
            extraction, chunks_by_id,
            data_lake_path=tmp_path,
            sdl_root=sdl_root,
            pipeline_run_id="tex-phase-v-smoke",
            api_caller=stub_caller,
        )

        if result is None:
            print("PHASE V SMOKE TEST FAILED: verifier returned None")
            return False

        files = list((sdl_root / "verifications").glob("*.json"))
        if not files:
            print("PHASE V SMOKE TEST FAILED: no verification artifact written")
            return False

        written = json.loads(files[0].read_text(encoding="utf-8"))
        print(
            f"Verification artifact: verified={written['summary']['verified_count']}, "
            f"unsupported={written['summary']['unsupported_count']}, "
            f"total={written['summary']['total_items_count']}"
        )

        if written["summary"]["verified_count"] < 1:
            print("PHASE V SMOKE TEST FAILED: no verified items")
            return False

        if extraction["schema_version"] != "2.0.0":
            print("PHASE V SMOKE TEST FAILED: schema_version not bumped to 2.0.0")
            return False

        if not extraction.get("verification_artifact_id"):
            print("PHASE V SMOKE TEST FAILED: verification_artifact_id missing")
            return False

        for item in extraction["claims"]:
            if "verification_status" not in item:
                print(
                    "PHASE V SMOKE TEST FAILED: claim missing verification_status"
                )
                return False

        print("PHASE V SMOKE TEST PASSED")
        return True


def run_fixture_smoke_test(mock: bool = False) -> bool:
    """Return True if the smoke test passes."""
    if not FIXTURE_PATH.exists():
        print(f"FIXTURE SMOKE TEST FAILED: fixture not found at {FIXTURE_PATH}")
        return False

    chunks = _load_fixture_chunks()
    print(f"Loaded {len(chunks)} fixture chunks")

    try:
        from spectrum_systems_core.extraction.action_item_extractor import (
            ActionItemExtractor,
        )
        from spectrum_systems_core.extraction.chunk_classifier import (
            ChunkClassifier,
        )
        from spectrum_systems_core.extraction.claim_extractor import (
            ClaimExtractor,
        )
        from spectrum_systems_core.extraction.decision_extractor import (
            DecisionExtractor,
        )
        from spectrum_systems_core.extraction.extraction_merger import (  # noqa: F401
            ExtractionMerger,
        )
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _resolve_api_callers,
        )
    except ImportError as exc:
        print(f"FIXTURE SMOKE TEST FAILED: import error: {exc}")
        return False

    if mock:
        print("Mock mode: testing imports and wiring only")
        print("All extractor classes imported successfully")
        print("FIXTURE SMOKE TEST PASSED (mock mode)")
        return True

    try:
        import anthropic  # noqa: F401
        print(f"anthropic SDK: {anthropic.__version__}")
    except ImportError:
        print("FIXTURE SMOKE TEST FAILED: anthropic SDK not installed")
        return False

    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("FIXTURE SMOKE TEST FAILED: ANTHROPIC_API_KEY not set")
        return False

    callers = _resolve_api_callers(None)
    missing = [
        k for k in ("classifier", "decision", "claim", "action_item")
        if k not in callers
    ]
    if missing:
        print(
            f"FIXTURE SMOKE TEST FAILED: real API callers unavailable for "
            f"{missing} (would silently classify everything as off_topic)"
        )
        return False

    classifier = ChunkClassifier(api_caller=callers["classifier"])
    classifications: dict[str, str] = {}
    for chunk in chunks:
        result = classifier.classify(chunk, source_id="smoke-test-fixture")
        classifications[chunk["chunk_id"]] = result.get(
            "classification", "off_topic"
        )

    print(f"Classifications: {classifications}")

    non_off_topic = [c for c in classifications.values() if c != "off_topic"]
    print(f"Non-off_topic chunks: {len(non_off_topic)} of {len(chunks)}")

    if not non_off_topic:
        print("FIXTURE SMOKE TEST FAILED: all chunks classified as off_topic")
        print("ChunkClassifier is not routing correctly")
        return False

    decision_chunks = [
        c for c in chunks if classifications.get(c["chunk_id"]) == "decision"
    ]
    claim_chunks = [
        c for c in chunks if classifications.get(c["chunk_id"]) == "claim"
    ]
    action_chunks = [
        c
        for c in chunks
        if classifications.get(c["chunk_id"]) == "action_item"
    ]

    available_turn_ids = {c["chunk_id"] for c in chunks}

    decisions: list = []
    claims: list = []
    action_items: list = []

    if decision_chunks:
        decisions = DecisionExtractor(
            api_caller=callers["decision"],
        ).extract(decision_chunks, "", available_turn_ids)
    if claim_chunks:
        claims = ClaimExtractor(
            api_caller=callers["claim"],
        ).extract(claim_chunks, "", available_turn_ids)
    if action_chunks:
        action_items = ActionItemExtractor(
            api_caller=callers["action_item"],
        ).extract(action_chunks, "", available_turn_ids)

    total = len(decisions) + len(claims) + len(action_items)
    print(
        f"Extracted: decisions={len(decisions)} claims={len(claims)} "
        f"action_items={len(action_items)}"
    )

    if total == 0:
        print("FIXTURE SMOKE TEST FAILED: zero extractions from fixture transcript")
        print("Typed extractors are not producing output")
        return False

    print("FIXTURE SMOKE TEST PASSED")
    return True


def run_phase_w_smoke_test() -> bool:
    """Run AgendaDetector + the pipeline integration on the fixture.

    No real LLM is invoked: a deterministic stub api_caller returns a
    canned agenda JSON with two distinct labels so the detector's
    happy-path (>=2 distinct labels, ``llm_detected``) is exercised
    end-to-end. The smoke test confirms:

      * AgendaDetector finishes within 30 seconds on a 10-chunk
        fixture (per RT1 attack 9 -- catch slow detector).
      * At least 1 agenda_item artifact is written under
        ``<sdl_root>/agenda/<source_id>/``.
      * Chunks are annotated with non-null agenda_item_id values.
      * The pre-flight validation passes (artifacts on disk match
        chunk references).

    Returns True on success.
    """
    print("\n--- Phase W smoke test ---")
    chunks = _load_fixture_chunks()

    try:
        from spectrum_systems_core.agenda import (
            apply_phase_w_if_enabled,
        )
        from spectrum_systems_core.config import PHASE_W_FLAG_NAME
        from spectrum_systems_core.verification.model_registry import (
            ModelRegistry,
        )
    except ImportError as exc:
        print(f"PHASE W SMOKE TEST FAILED: import error: {exc}")
        return False

    def stub_caller(_prompt: str) -> dict:
        return {"text": json.dumps({
            "agenda_items": [
                {"ordinal": 1, "label": "FSS Protection Criteria",
                 "approximate_start_chunk_index": 0},
                {"ordinal": 2, "label": "COA Selection Review",
                 "approximate_start_chunk_index": 4},
                {"ordinal": 3, "label": "Action Items Roundup",
                 "approximate_start_chunk_index": 8},
            ],
            "detection_confidence": 0.88,
            "rationale": "Three-topic working-group agenda detected.",
        })}

    with tempfile.TemporaryDirectory(prefix="phase-w-smoke-") as tmp:
        tmp_path = pathlib.Path(tmp)
        flag_dir = tmp_path / "store" / "artifacts" / "config"
        flag_dir.mkdir(parents=True)
        (flag_dir / f"{PHASE_W_FLAG_NAME}_enabled.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8",
        )
        sdl_root = tmp_path / "store" / "artifacts"

        # Re-attach the fixture chunks under a single source_id so the
        # pipeline can write per-source artifacts.
        source_id = "smoke-test-fixture"
        for chunk in chunks:
            chunk.setdefault("source_id", source_id)

        metrics = apply_phase_w_if_enabled(
            chunks,
            source_id=source_id,
            data_lake_path=tmp_path,
            sdl_root=sdl_root,
            pipeline_run_id="phase-w-smoke-run",
            model_registry=ModelRegistry(),
            api_caller=stub_caller,
        )

        print(
            "Phase W metrics: "
            f"attempted={metrics['agenda_detection_attempted']} "
            f"succeeded={metrics['agenda_detection_succeeded']} "
            f"items_count={metrics['agenda_items_detected_count']} "
            f"duration={metrics['detection_duration_seconds']:.3f}s "
            f"method={metrics['detection_method']}"
        )

        if metrics["detection_duration_seconds"] >= 30.0:
            print(
                "PHASE W SMOKE TEST FAILED: AgendaDetector took "
                f">= 30s on the fixture (got "
                f"{metrics['detection_duration_seconds']:.1f}s)"
            )
            return False

        agenda_dir = sdl_root / "agenda" / source_id
        files = list(agenda_dir.glob("*.json"))
        if not files:
            print("PHASE W SMOKE TEST FAILED: no agenda_item artifact written")
            return False

        annotated = [c for c in chunks if c.get("agenda_item_id")]
        if not annotated:
            print(
                "PHASE W SMOKE TEST FAILED: chunks not annotated with "
                "agenda_item_id"
            )
            return False

        print(
            f"PHASE W SMOKE TEST PASSED: {len(files)} agenda artifacts, "
            f"{len(annotated)}/{len(chunks)} chunks annotated"
        )
        return True


def run_phase_w_compare() -> bool:
    """Run extract-style smoke fixture twice (flag off / on) and report.

    The "extraction" step is mocked (we just track the canned classifier
    result counts), but the AgendaDetector path runs for real so the
    PR description can carry the actual numbers.
    """
    print("\n--- Phase W compare (flag off vs on) ---")
    chunks_template = _load_fixture_chunks()

    def _run(flag_enabled: bool) -> dict:
        from spectrum_systems_core.agenda import apply_phase_w_if_enabled
        from spectrum_systems_core.config import PHASE_W_FLAG_NAME
        from spectrum_systems_core.verification.model_registry import (
            ModelRegistry,
        )

        chunks = [dict(c) for c in chunks_template]
        with tempfile.TemporaryDirectory(prefix="pw-compare-") as tmp:
            tmp_path = pathlib.Path(tmp)
            flag_dir = tmp_path / "store" / "artifacts" / "config"
            flag_dir.mkdir(parents=True)
            (flag_dir / f"{PHASE_W_FLAG_NAME}_enabled.json").write_text(
                json.dumps({"enabled": flag_enabled}), encoding="utf-8",
            )
            sdl_root = tmp_path / "store" / "artifacts"
            metrics = apply_phase_w_if_enabled(
                chunks,
                source_id="smoke-test-fixture",
                data_lake_path=tmp_path,
                sdl_root=sdl_root,
                pipeline_run_id="phase-w-compare",
                model_registry=ModelRegistry(),
                api_caller=lambda _p: {"text": json.dumps({
                    "agenda_items": [
                        {"ordinal": 1, "label": "FSS Protection Criteria",
                         "approximate_start_chunk_index": 0},
                        {"ordinal": 2, "label": "COA Selection Review",
                         "approximate_start_chunk_index": 4},
                    ],
                    "detection_confidence": 0.85,
                })},
            )
            return {
                "flag_enabled": flag_enabled,
                "agenda_detection_attempted":
                    metrics["agenda_detection_attempted"],
                "agenda_detection_succeeded":
                    metrics["agenda_detection_succeeded"],
                "agenda_items_detected_count":
                    metrics["agenda_items_detected_count"],
                "detection_duration_seconds":
                    metrics["detection_duration_seconds"],
                "annotated_chunks": sum(
                    1 for c in chunks if c.get("agenda_item_id")
                ),
                "total_chunks": len(chunks),
            }

    off = _run(False)
    on = _run(True)
    print(f"Phase W OFF: {json.dumps(off, sort_keys=True)}")
    print(f"Phase W ON:  {json.dumps(on, sort_keys=True)}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Skip API calls, test imports and wiring only",
    )
    parser.add_argument(
        "--enable-phase-v",
        action="store_true",
        help="Run the Phase V post-hoc verifier smoke test (mock LLM).",
    )
    parser.add_argument(
        "--enable-phase-w",
        action="store_true",
        help="Run the Phase W agenda detection smoke test (mock LLM).",
    )
    parser.add_argument(
        "--phase-w-compare",
        action="store_true",
        help="Run Phase W comparison: flag-off vs flag-on (mock LLM).",
    )
    args = parser.parse_args()

    if args.enable_phase_v:
        success = run_phase_v_smoke_test()
    elif args.enable_phase_w:
        success = run_phase_w_smoke_test()
    elif args.phase_w_compare:
        success = run_phase_w_compare()
    else:
        success = run_fixture_smoke_test(mock=args.mock)
    sys.exit(0 if success else 1)
