"""Phase P3-A: EXTRACTION_MODE and GLOSSARY_VERSION rollback tests.

Exercise the env-driven rollback paths without invoking an LLM. Both
features must be safe to flip on or off without code changes -- the
tests assert each switch produces the expected disk and in-memory
artifact shape so a future regression on the wiring surfaces here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from spectrum_systems_core.extraction import typed_extraction_runner as runner


def test_resolve_extraction_mode_defaults_to_two_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXTRACTION_MODE", raising=False)
    assert runner._resolve_extraction_mode() == "two_stage"


def test_resolve_extraction_mode_accepts_single_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACTION_MODE", "single_pass")
    assert runner._resolve_extraction_mode() == "single_pass"


def test_resolve_extraction_mode_invalid_falls_back_to_two_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTRACTION_MODE", "yolo_mode")
    assert runner._resolve_extraction_mode() == "two_stage"


def _write_glossary(path: Path, version: int, term_count: int = 1) -> None:
    artifact = {
        "artifact_type": "spectrum_glossary",
        "schema_version": "1.0.0",
        "glossary_version": str(version),
        "term_count": term_count,
        "content_hash": "sha256:" + ("a" * 64),
        "created_at": "1970-01-01T00:00:00+00:00",
        "terms": [
            {
                "term_id": f"t-{i}",
                "term": f"term_{i}",
                "abbreviation": None,
                "definition": f"definition {i}",
                "short_definition": f"short {i}",
                "authoritative_source": "FCC",
                "domain_scope": "spectrum",
                "related_term_ids": [],
            }
            for i in range(term_count)
        ],
    }
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")


def test_resolve_pinned_glossary_path_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GLOSSARY_VERSION", raising=False)
    _write_glossary(tmp_path / "spectrum_glossary_v1.json", 1)
    _write_glossary(tmp_path / "spectrum_glossary_v2.json", 2)
    path = runner._resolve_pinned_glossary_path(tmp_path)
    assert path is not None
    assert path.name == "spectrum_glossary_v2.json"


def test_resolve_pinned_glossary_path_pinned_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOSSARY_VERSION", "1")
    _write_glossary(tmp_path / "spectrum_glossary_v1.json", 1)
    _write_glossary(tmp_path / "spectrum_glossary_v2.json", 2)
    path = runner._resolve_pinned_glossary_path(tmp_path)
    assert path is not None
    assert path.name == "spectrum_glossary_v1.json"


def test_resolve_pinned_glossary_path_missing_pinned_falls_back_to_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOSSARY_VERSION", "9999")
    _write_glossary(tmp_path / "spectrum_glossary_v1.json", 1)
    _write_glossary(tmp_path / "spectrum_glossary_v2.json", 2)
    path = runner._resolve_pinned_glossary_path(tmp_path)
    # The runner falls back to the latest version when the pinned
    # one is missing on disk; the warning is logged but the run
    # continues so a typo never fails an entire batch.
    assert path is not None
    assert path.name == "spectrum_glossary_v2.json"


def test_resolve_pinned_glossary_path_invalid_value_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLOSSARY_VERSION", "not_a_number")
    _write_glossary(tmp_path / "spectrum_glossary_v3.json", 3)
    path = runner._resolve_pinned_glossary_path(tmp_path)
    assert path is not None
    assert path.name == "spectrum_glossary_v3.json"


def test_resolve_pinned_glossary_returns_none_when_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GLOSSARY_VERSION", raising=False)
    assert runner._resolve_pinned_glossary_path(tmp_path) is None


def test_resolve_versioned_glossary_artifact_with_pinned_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: pin GLOSSARY_VERSION=1 and assert the runner
    loads the v1 artifact even though v2 is on disk."""
    # Mimic the sdl_root layout the runner expects: glossary lives
    # under <sdl_root>/glossary/.
    sdl_root = tmp_path / "artifacts"
    sdl_root.mkdir(parents=True)
    gloss_root = sdl_root / "glossary"
    gloss_root.mkdir()
    _write_glossary(gloss_root / "spectrum_glossary_v1.json", 1, term_count=2)
    _write_glossary(gloss_root / "spectrum_glossary_v2.json", 2, term_count=5)

    monkeypatch.setenv("GLOSSARY_VERSION", "1")
    # Override the SDL_VERSIONED_GLOSSARY env so the resolver looks
    # in the right place. (The runner's helper accepts either env
    # var or the conventional sdl_root/glossary/ layout.)
    monkeypatch.setenv("SDL_VERSIONED_GLOSSARY", str(gloss_root))

    artifact = runner._resolve_versioned_glossary_artifact(sdl_root)
    assert artifact is not None
    assert artifact.get("glossary_version") == "1"
    assert len(artifact.get("terms") or []) == 2
