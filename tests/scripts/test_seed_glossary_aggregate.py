"""Tests for the versioned-glossary aggregate written by seed_glossary.py.

The Phase W wiring signal ``glossary_terms_injected_present`` requires
an orchestration_result whose ``glossary_injection_summary`` carries
``total_term_injections > 0`` or ``chunks_with_matches > 0``. The
runner produces those counters by injecting terms from
``<sdl_root>/glossary/spectrum_glossary_v1.json``. When that aggregate
is missing -- because ``seed_glossary.py`` historically wrote only
per-term files -- every chunk records zero matches and the signal
flips to MISSING.

These tests pin:

  * seed_glossary writes the aggregate file alongside the per-term
    files (so a fresh seed run hands the runner the artifact it
    needs).
  * The aggregate's term entries match the schema the runner expects
    (``term`` + ``abbreviation`` populated; ``find_matching_terms``
    matches on either).
  * The signal predicate behaves correctly on representative orchestration
    artifacts (positive + negative cases).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _signal_predicate(doc: dict[str, Any]) -> bool:
    """Exact predicate from validate-and-baseline.yml."""
    summary = doc.get("glossary_injection_summary")
    if not isinstance(summary, dict):
        return False
    injected = summary.get("total_term_injections") or 0
    matches = summary.get("chunks_with_matches") or 0
    return injected > 0 or matches > 0


# ----------------------------------------------------------------------
# Signal predicate.
# ----------------------------------------------------------------------


def test_signal_predicate_passes_when_injections_positive() -> None:
    doc = {
        "glossary_injection_summary": {
            "total_term_injections": 5,
            "chunks_with_matches": 2,
        },
    }
    assert _signal_predicate(doc) is True


def test_signal_predicate_passes_when_only_matches_positive() -> None:
    doc = {
        "glossary_injection_summary": {
            "total_term_injections": 0,
            "chunks_with_matches": 1,
        },
    }
    assert _signal_predicate(doc) is True


def test_signal_predicate_fails_when_both_zero() -> None:
    doc = {
        "glossary_injection_summary": {
            "total_term_injections": 0,
            "chunks_with_matches": 0,
        },
    }
    assert _signal_predicate(doc) is False


def test_signal_predicate_fails_when_summary_missing() -> None:
    doc: dict[str, Any] = {}
    assert _signal_predicate(doc) is False


def test_signal_predicate_fails_when_summary_not_dict() -> None:
    doc = {"glossary_injection_summary": "not a dict"}
    assert _signal_predicate(doc) is False


# ----------------------------------------------------------------------
# seed_glossary writes the aggregate.
# ----------------------------------------------------------------------


def _run_seed_glossary(out: Path) -> None:
    """Invoke ``scripts/seed_glossary.py --out <out>`` via subprocess.

    Subprocess keeps the script's argparse / sys.exit semantics intact
    and matches how the workflow invokes it.
    """
    repo_root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "seed_glossary.py"),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
    )


def test_seed_glossary_writes_versioned_aggregate(tmp_path: Path) -> None:
    out = tmp_path / "glossary"
    _run_seed_glossary(out)
    aggregate = out / "spectrum_glossary_v1.json"
    assert aggregate.is_file(), (
        "seed_glossary.py must write spectrum_glossary_v1.json so the "
        "typed_extraction_runner can load terms"
    )
    doc = json.loads(aggregate.read_text(encoding="utf-8"))
    assert doc["artifact_type"] == "spectrum_glossary"
    assert doc["schema_version"] == "1.0.0"
    assert isinstance(doc["terms"], list)
    assert doc["term_count"] == len(doc["terms"])
    assert doc["term_count"] >= 30  # _TERMS has 40+ entries


def test_seed_glossary_aggregate_validates_against_schema(tmp_path: Path) -> None:
    out = tmp_path / "glossary"
    _run_seed_glossary(out)
    aggregate_path = out / "spectrum_glossary_v1.json"
    doc = json.loads(aggregate_path.read_text(encoding="utf-8"))
    from spectrum_systems_core.validation import validate_artifact
    validate_artifact(doc, "spectrum_glossary")


def test_seed_glossary_aggregate_carries_fss_term(tmp_path: Path) -> None:
    """The injection predicate needs at least one term that lexically
    matches a known token in a transcript chunk. FSS is the canonical
    smoke-test term used by the wiring integration suite. If this
    test fails, the aggregate is out of sync with _TERMS."""
    out = tmp_path / "glossary"
    _run_seed_glossary(out)
    aggregate = json.loads(
        (out / "spectrum_glossary_v1.json").read_text(encoding="utf-8")
    )
    abbrevs = {t.get("abbreviation") for t in aggregate["terms"]}
    assert "FSS" in abbrevs


def test_seed_glossary_aggregate_loads_via_runner_loader(tmp_path: Path) -> None:
    """Round-trip check: the aggregate written by seed_glossary must be
    loadable by the same code the runner uses."""
    out = tmp_path / "glossary"
    _run_seed_glossary(out)
    from spectrum_systems_core.glossary.glossary_builder import (
        load_versioned_glossary,
    )
    loaded = load_versioned_glossary(out)
    assert loaded is not None
    assert isinstance(loaded.get("terms"), list)
    assert len(loaded["terms"]) >= 30


def test_seed_glossary_aggregate_drives_positive_injection(tmp_path: Path) -> None:
    """End-to-end: aggregate written by seed_glossary, then
    ``find_matching_terms`` over a chunk that mentions ``FSS`` returns
    at least one term. This is the link between the seeder fix and
    the wiring signal: with the aggregate present, injection works."""
    out = tmp_path / "glossary"
    _run_seed_glossary(out)
    from spectrum_systems_core.glossary.glossary_builder import (
        load_versioned_glossary,
    )
    from spectrum_systems_core.glossary.term_injector import find_matching_terms
    glossary = load_versioned_glossary(out)
    assert glossary is not None
    matched = find_matching_terms(
        "The FSS protection zone matters here.", glossary["terms"]
    )
    assert len(matched) >= 1


def test_seed_glossary_is_deterministic(tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    _run_seed_glossary(out_a)
    _run_seed_glossary(out_b)
    a = (out_a / "spectrum_glossary_v1.json").read_bytes()
    b = (out_b / "spectrum_glossary_v1.json").read_bytes()
    assert a == b
