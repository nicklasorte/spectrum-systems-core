"""Phase O.2 tests: blocked_chunk envelope + missing-chunk-text scanner."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectrum_systems_core.extraction import _failure_artifacts as fa
from spectrum_systems_core.extraction._chunk_counters import ChunkCounters
from spectrum_systems_core.health.blocked_chunk_text_check import (
    scan_blocked_chunks,
)


@pytest.fixture(autouse=True)
def _reset_chunk_lookup():
    fa.clear_chunk_lookup()
    yield
    fa.clear_chunk_lookup()


def _read_blocked_chunks(sdl_root: Path) -> list[dict]:
    blocked_dir = sdl_root / "blocked_chunks"
    if not blocked_dir.is_dir():
        return []
    out = []
    for path in sorted(blocked_dir.glob("*.json")):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def test_blocked_chunk_envelope_carries_chunk_text(tmp_path):
    fa.install_chunk_lookup(
        [
            {
                "chunk_id": "c1",
                "text": "Oh.",
                "speaker": "Rick Ikemoto SSC SMO",
                "chunk_index": 3,
            }
        ]
    )
    counters = ChunkCounters()
    counters.record_attempt(1)
    fa.emit_empty_response(
        counters,
        chunk_id="c1",
        source_id="src-1",
        component="typed_extraction_runner",
        detail="no body",
        sdl_root=tmp_path,
    )
    blocked = _read_blocked_chunks(tmp_path)
    assert len(blocked) == 1
    bc = blocked[0]
    assert bc["artifact_type"] == "blocked_chunk"
    assert bc["schema_version"] == "2.0.0"
    assert bc["chunk_text"] == "Oh."
    assert bc["chunk_char_count"] == 3
    assert bc["chunk_speaker"] == "Rick Ikemoto SSC SMO"
    assert bc["chunk_index"] == 3
    assert bc["block_reason"] == "empty_response"
    assert bc["chunk_id"] == "c1"
    assert bc["source_id"] == "src-1"


def test_blocked_chunk_envelope_unknown_chunk_id(tmp_path):
    fa.install_chunk_lookup([{"chunk_id": "c1", "text": "hi"}])
    counters = ChunkCounters()
    counters.record_attempt(1)
    fa.emit_rate_limit_exhausted(
        counters,
        chunk_id="not-known",
        source_id="src-1",
        component="chunk_classifier",
        sdl_root=tmp_path,
    )
    blocked = _read_blocked_chunks(tmp_path)
    assert len(blocked) == 1
    bc = blocked[0]
    assert bc["chunk_text"] == "[chunk not found]"
    assert bc["chunk_char_count"] is None
    assert bc["chunk_speaker"] is None
    assert bc["chunk_index"] is None
    assert bc["block_reason"] == "rate_limit_exhausted"


def test_blocked_chunk_text_truncated_at_500_chars(tmp_path):
    long_text = "x" * 1200
    fa.install_chunk_lookup([{"chunk_id": "c1", "text": long_text}])
    counters = ChunkCounters()
    counters.record_attempt(1)
    fa.emit_json_parse_failed(
        counters,
        chunk_id="c1",
        source_id="src-1",
        component="typed_extraction_runner",
        sdl_root=tmp_path,
    )
    bc = _read_blocked_chunks(tmp_path)[0]
    assert bc["chunk_text"].endswith(" [truncated]")
    # 500 chars of payload + " [truncated]" suffix.
    assert bc["chunk_text"].startswith("x" * 500)
    # The original char count survives the truncation.
    assert bc["chunk_char_count"] == 1200


def test_blocked_chunk_for_each_block_reason(tmp_path):
    fa.install_chunk_lookup(
        [{"chunk_id": "c1", "text": "hello", "chunk_index": 1}]
    )
    counters = ChunkCounters()
    counters.record_attempt(4)
    fa.emit_rate_limit_exhausted(
        counters, chunk_id="c1", source_id="s", component="x", sdl_root=tmp_path,
    )
    fa.emit_empty_response(
        counters, chunk_id="c1", source_id="s", component="x", sdl_root=tmp_path,
    )
    fa.emit_json_parse_failed(
        counters, chunk_id="c1", source_id="s", component="x", sdl_root=tmp_path,
    )
    fa.emit_empty_result(
        counters, chunk_id="c1", source_id="s", component="x", sdl_root=tmp_path,
    )
    reasons = {b["block_reason"] for b in _read_blocked_chunks(tmp_path)}
    assert reasons == {
        "rate_limit_exhausted",
        "empty_response",
        "parse_error",
        "other",
    }


def test_scanner_emits_info_for_v1_blocked_chunks(tmp_path):
    blocked_dir = tmp_path / "store" / "artifacts" / "blocked_chunks"
    blocked_dir.mkdir(parents=True)
    legacy = {
        "artifact_type": "blocked_chunk",
        "schema_version": "1.0.0",
        "failure_id": "f1",
        "chunk_id": "c1",
        "source_id": "s1",
        "block_reason": "empty_response",
        "component": "x",
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    (blocked_dir / "f1.json").write_text(json.dumps(legacy))
    findings = scan_blocked_chunks(tmp_path, pipeline_run_id="run-1")
    assert len(findings) == 1
    f = findings[0]
    assert f.finding_code == "blocked_artifact_missing_chunk_text"
    assert f.severity == "info"
    assert f.pipeline_run_id == "run-1"
    assert f.context["chunk_id"] == "c1"
    assert f.context["schema_version"] == "1.0.0"


def test_scanner_quiet_for_v2_blocked_chunks(tmp_path):
    blocked_dir = tmp_path / "store" / "artifacts" / "blocked_chunks"
    blocked_dir.mkdir(parents=True)
    v2 = {
        "artifact_type": "blocked_chunk",
        "schema_version": "2.0.0",
        "failure_id": "f1",
        "chunk_id": "c1",
        "source_id": "s1",
        "block_reason": "empty_response",
        "chunk_text": "hi",
        "chunk_char_count": 2,
        "component": "x",
        "created_at": "2025-01-01T00:00:00+00:00",
    }
    (blocked_dir / "f1.json").write_text(json.dumps(v2))
    findings = scan_blocked_chunks(tmp_path)
    assert findings == []


def test_scanner_handles_missing_blocked_chunks_dir(tmp_path):
    assert scan_blocked_chunks(tmp_path) == []
