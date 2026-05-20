"""Phase 2P live-artifact tests.

These tests bind the committed ``data/glossary/`` artifact to the
schema and the manifest. They guard against drift: a hand-edit of
the JSONL that forgets to refresh the manifest fails the hash gate;
a hand-edit that introduces a modal verb fails ``validate_entry``;
a hand-edit that breaks the schema fails the meta-validated schema
in ``tests/ci/test_phase_x_schemas.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.glossary.loader import (
    GLOSSARY_SCHEMA_VERSION,
    MODAL_VERBS,
    load_glossary,
    validate_entry,
)
from spectrum_systems_core.schemas import schema_path

REPO_ROOT = Path(__file__).resolve().parents[2]
GLOSSARY_DIR = REPO_ROOT / "data" / "glossary"
JSONL_PATH = GLOSSARY_DIR / "ntia_dod_spectrum_v1.jsonl"
MANIFEST_PATH = GLOSSARY_DIR / "MANIFEST.json"


def _read_entries() -> list[dict]:
    raw = JSONL_PATH.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return [json.loads(line) for line in lines]


def test_glossary_loads_via_loader() -> None:
    g = load_glossary(JSONL_PATH, MANIFEST_PATH)
    assert g.version == "1.0.0"
    assert len(g.entries) >= 50


def test_glossary_has_at_least_50_entries() -> None:
    entries = _read_entries()
    assert len(entries) >= 50


@pytest.mark.parametrize("modal", sorted(MODAL_VERBS))
def test_no_modal_verb_in_terms_or_aliases(modal: str) -> None:
    entries = _read_entries()
    for entry in entries:
        forms = [entry.get("term", "")] + list(entry.get("aliases", []))
        for form in forms:
            assert form.strip().lower() != modal, (
                f"modal verb {modal!r} found in entry {entry.get('term')!r}"
            )


def test_required_domain_coverage() -> None:
    """The phase requires at least these spectrum-policy terms."""
    entries = _read_entries()
    seen = {e["term"] for e in entries}
    required = {
        "CBRS",
        "FSS",
        "MSS",
        "FS",
        "allocation",
        "EIRP",
        "PFD",
        "TIG",
        "SAS",
        "primary",
        "secondary",
        "co-primary",
    }
    missing = required - seen
    assert not missing, f"missing required domain terms: {missing}"


def test_every_entry_passes_schema() -> None:
    schema = json.loads(schema_path("glossary_entry").read_text(encoding="utf-8"))
    for entry in _read_entries():
        jsonschema.Draft202012Validator(schema).validate(entry)


def test_every_entry_passes_custom_validator() -> None:
    allowed = json.loads(
        (GLOSSARY_DIR / "allowed_sources.json").read_text(encoding="utf-8")
    )["allowed_sources"]
    for entry in _read_entries():
        errors = validate_entry(entry, allowed)
        assert not errors, f"entry {entry.get('term')!r} failed: {errors}"


def test_artifact_type_and_schema_version_are_consistent() -> None:
    for entry in _read_entries():
        assert entry["artifact_type"] == "glossary_entry"
        assert entry["schema_version"] == GLOSSARY_SCHEMA_VERSION


def test_no_artifact_kind_anywhere() -> None:
    """Belt and suspenders: zero tolerance for ``artifact_kind``."""
    raw = JSONL_PATH.read_text(encoding="utf-8")
    assert "artifact_kind" not in raw


def test_schema_rejects_stray_field() -> None:
    """Schema must enforce additionalProperties: false."""
    schema = json.loads(schema_path("glossary_entry").read_text(encoding="utf-8"))
    sample = _read_entries()[0].copy()
    sample["unexpected_extra_field"] = "anything"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(sample)


def test_schema_rejects_wrong_artifact_type() -> None:
    schema = json.loads(schema_path("glossary_entry").read_text(encoding="utf-8"))
    sample = _read_entries()[0].copy()
    sample["artifact_type"] = "something_else"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(sample)


def test_schema_rejects_wrong_schema_version() -> None:
    schema = json.loads(schema_path("glossary_entry").read_text(encoding="utf-8"))
    sample = _read_entries()[0].copy()
    sample["schema_version"] = "0.9.0"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(sample)


def test_schema_rejects_term_too_long() -> None:
    schema = json.loads(schema_path("glossary_entry").read_text(encoding="utf-8"))
    sample = _read_entries()[0].copy()
    sample["term"] = "x" * 81
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(sample)
