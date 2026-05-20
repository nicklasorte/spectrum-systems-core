"""Phase 4 — corpus manifest loader tests.

Every gate the loader emits has a paired rejection test here. The
test suite is structured to give a clear PASS/FAIL on each loader
contract clause: schema, hash, source_id uniqueness, expected_path
uniqueness (with supersedes exception), and supersedes
cross-references.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from spectrum_systems_core.corpus.manifest_loader import (
    CorpusManifestError,
    HASH_MISMATCH,
    MANIFEST_NOT_FOUND,
    MANIFEST_UNREADABLE,
    PATH_DUPLICATE_NO_SUPERSEDES,
    SCHEMA_VIOLATION,
    SOURCE_ID_DUPLICATE,
    SUPERSEDES_SELF,
    SUPERSEDES_UNKNOWN,
    compute_manifest_hash,
    load_manifest,
    rewrite_manifest_with_observed,
)


def _minimal_source(sid: str, path: str, **overrides) -> Dict[str, Any]:
    declared = {
        "expected_path": path,
        "meeting_date": "2026-05-20",
        "meeting_type": "working_group",
        "supersedes": None,
    }
    declared.update(overrides.get("declared", {}))
    observed = {
        "detected_speaker_count": None,
        "detected_word_count": None,
        "ingestion_status": "pending",
        "last_updated": None,
    }
    observed.update(overrides.get("observed", {}))
    return {
        "source_id": sid,
        "declared": declared,
        "observed": observed,
    }


def _write_manifest(
    path: Path, sources: List[Dict[str, Any]], *, hash_override: str | None = None
) -> Path:
    payload = {
        "artifact_type": "corpus_manifest",
        "schema_version": "1.0.0",
        "manifest_hash": hash_override or compute_manifest_hash(sources),
        "sources": sources,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_load_minimal_manifest(tmp_path: Path) -> None:
    """A 1-source manifest with a valid hash loads cleanly."""
    sources = [_minimal_source("src-a", "raw/transcripts/a.txt")]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    m = load_manifest(p)
    assert len(m.payload["sources"]) == 1
    assert m.manifest_hash == compute_manifest_hash(sources)


def test_load_shipped_manifest_validates() -> None:
    """The 13-source manifest committed in this PR loads under the
    default path with no errors."""
    m = load_manifest()
    assert len(m.payload["sources"]) == 13


# ---------------------------------------------------------------------------
# Missing / unreadable file.
# ---------------------------------------------------------------------------


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(tmp_path / "nope.json")
    assert ei.value.reason_code == MANIFEST_NOT_FOUND


def test_invalid_json_fails_closed(tmp_path: Path) -> None:
    p = tmp_path / "manifest.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == MANIFEST_UNREADABLE


# ---------------------------------------------------------------------------
# Hash gate.
# ---------------------------------------------------------------------------


def test_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    """A hand-edited manifest whose hash is stale fails closed."""
    sources = [_minimal_source("src-a", "raw/transcripts/a.txt")]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    # Mutate one source field without updating the hash.
    data = json.loads(p.read_text())
    data["sources"][0]["declared"]["meeting_type"] = "internal_review"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == HASH_MISMATCH


def test_placeholder_hash_rejected(tmp_path: Path) -> None:
    """An un-hashed manifest is rejected even though the placeholder
    string passes schema validation."""
    sources = [_minimal_source("src-a", "raw/transcripts/a.txt")]
    p = _write_manifest(
        tmp_path / "manifest.json",
        sources,
        hash_override="PLACEHOLDER_HASH_REGENERATED_BY_LOADER",
    )
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == HASH_MISMATCH


# ---------------------------------------------------------------------------
# Schema rejection paths.
# ---------------------------------------------------------------------------


def test_unknown_meeting_type_rejected(tmp_path: Path) -> None:
    sources = [
        _minimal_source(
            "src-a", "raw/transcripts/a.txt", declared={"meeting_type": "lunch"}
        )
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SCHEMA_VIOLATION


def test_unknown_status_rejected(tmp_path: Path) -> None:
    sources = [
        _minimal_source(
            "src-a",
            "raw/transcripts/a.txt",
            observed={"ingestion_status": "borked"},
        )
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SCHEMA_VIOLATION


def test_source_id_with_underscore_rejected(tmp_path: Path) -> None:
    """The schema's pattern `^[a-z0-9-]+$` forbids underscores."""
    sources = [_minimal_source("src_a", "raw/transcripts/a.txt")]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SCHEMA_VIOLATION


def test_additional_property_rejected(tmp_path: Path) -> None:
    """additionalProperties:false catches typos at the envelope."""
    sources = [_minimal_source("src-a", "raw/transcripts/a.txt")]
    data = {
        "artifact_type": "corpus_manifest",
        "schema_version": "1.0.0",
        "manifest_hash": compute_manifest_hash(sources),
        "sources": sources,
        "garbage": True,
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SCHEMA_VIOLATION


# ---------------------------------------------------------------------------
# Custom rule rejection paths.
# ---------------------------------------------------------------------------


def test_source_id_duplicate_rejected(tmp_path: Path) -> None:
    sources = [
        _minimal_source("src-a", "raw/transcripts/a.txt"),
        _minimal_source("src-a", "raw/transcripts/b.txt"),
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SOURCE_ID_DUPLICATE


def test_path_duplicate_without_supersedes_rejected(tmp_path: Path) -> None:
    sources = [
        _minimal_source("src-a", "raw/transcripts/shared.txt"),
        _minimal_source("src-b", "raw/transcripts/shared.txt"),
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == PATH_DUPLICATE_NO_SUPERSEDES


def test_path_duplicate_with_supersedes_allowed(tmp_path: Path) -> None:
    """A new entry that supersedes an existing one MAY reuse the path."""
    sources = [
        _minimal_source("src-a", "raw/transcripts/shared.txt"),
        _minimal_source(
            "src-b",
            "raw/transcripts/shared.txt",
            declared={"supersedes": "src-a"},
        ),
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    m = load_manifest(p)
    assert len(m.payload["sources"]) == 2


def test_supersedes_unknown_source_rejected(tmp_path: Path) -> None:
    sources = [
        _minimal_source(
            "src-a",
            "raw/transcripts/a.txt",
            declared={"supersedes": "ghost"},
        ),
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SUPERSEDES_UNKNOWN


def test_supersedes_self_rejected(tmp_path: Path) -> None:
    sources = [
        _minimal_source(
            "src-a",
            "raw/transcripts/a.txt",
            declared={"supersedes": "src-a"},
        ),
    ]
    p = _write_manifest(tmp_path / "manifest.json", sources)
    with pytest.raises(CorpusManifestError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == SUPERSEDES_SELF


# ---------------------------------------------------------------------------
# Hash determinism: order-independent.
# ---------------------------------------------------------------------------


def test_hash_is_order_independent(tmp_path: Path) -> None:
    """Two manifests with the same source set in different order
    produce identical hashes."""
    a = _minimal_source("src-a", "raw/transcripts/a.txt")
    b = _minimal_source("src-b", "raw/transcripts/b.txt")
    h1 = compute_manifest_hash([a, b])
    h2 = compute_manifest_hash([b, a])
    assert h1 == h2


# ---------------------------------------------------------------------------
# Writer round-trip.
# ---------------------------------------------------------------------------


def test_rewrite_with_observed_updates_atomically(tmp_path: Path) -> None:
    sources = [_minimal_source("src-a", "raw/transcripts/a.txt")]
    p = _write_manifest(tmp_path / "manifest.json", sources)

    new_observed = {
        "detected_speaker_count": 4,
        "detected_word_count": 1500,
        "ingestion_status": "validated",
        "last_updated": "2026-05-20T01:02:03+00:00",
    }
    refreshed = rewrite_manifest_with_observed(
        path=p, observed_updates={"src-a": new_observed}
    )
    assert refreshed.payload["sources"][0]["observed"] == new_observed

    # Re-load: hash matches, no error.
    reloaded = load_manifest(p)
    assert reloaded.payload["sources"][0]["observed"]["ingestion_status"] == "validated"
    assert reloaded.manifest_hash == refreshed.manifest_hash
