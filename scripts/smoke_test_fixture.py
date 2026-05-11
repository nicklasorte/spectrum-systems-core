#!/usr/bin/env python3
"""Fast smoke test using a small hand-authored fixture transcript.

No data-lake required. In mock mode, no API calls are made (just import
checks). In real mode, runs the full extractor stack against 10 fixture
chunks. Target runtime: < 15 seconds.

Usage:
  python scripts/smoke_test_fixture.py
  python scripts/smoke_test_fixture.py --mock  # skip API calls, test wiring only
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys


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

    classifier = ChunkClassifier()
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
        decisions = DecisionExtractor().extract(
            decision_chunks, "", available_turn_ids,
        )
    if claim_chunks:
        claims = ClaimExtractor().extract(
            claim_chunks, "", available_turn_ids,
        )
    if action_chunks:
        action_items = ActionItemExtractor().extract(
            action_chunks, "", available_turn_ids,
        )

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Skip API calls, test imports and wiring only",
    )
    args = parser.parse_args()

    success = run_fixture_smoke_test(mock=args.mock)
    sys.exit(0 if success else 1)
