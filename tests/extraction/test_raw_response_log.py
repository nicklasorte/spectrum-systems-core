"""Phase O.1 unit tests for the raw API response logger."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spectrum_systems_core.extraction import _raw_response_log as logger_mod


@pytest.fixture(autouse=True)
def _reset_logger_state(monkeypatch):
    """Each test starts with logging disabled and default max chars."""
    monkeypatch.delenv(logger_mod.RAW_RESPONSE_LOG_ENABLED_ENV, raising=False)
    monkeypatch.delenv(logger_mod.RAW_RESPONSE_LOG_MAX_CHARS_ENV, raising=False)
    logger_mod.reload_from_env()
    yield
    logger_mod.reload_from_env()


def _enable_logger(monkeypatch, *, max_chars=None):
    monkeypatch.setenv(logger_mod.RAW_RESPONSE_LOG_ENABLED_ENV, "true")
    if max_chars is not None:
        monkeypatch.setenv(
            logger_mod.RAW_RESPONSE_LOG_MAX_CHARS_ENV, str(max_chars),
        )
    logger_mod.reload_from_env()


def _read_first(dir_path: Path) -> dict:
    files = list(dir_path.rglob("*.json"))
    assert len(files) == 1, f"expected exactly one log file, got {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ----- O.1 test 1: empty response -----------------------------------------


def test_empty_response_classified_and_written(tmp_path, monkeypatch):
    _enable_logger(monkeypatch)
    path = logger_mod.write_log(
        "",
        chunk_id="c1",
        source_id="s1",
        model="claude-haiku-4-5-20251001",
        call_type="extraction",
        sdl_root=tmp_path,
    )
    assert path is not None and path.is_file()
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["response_type"] == "empty"
    assert artifact["chunk_id"] == "c1"
    assert artifact["source_id"] == "s1"
    assert (
        path.parent
        == tmp_path / "debug" / "raw_responses" / "s1"
    )
    assert path.name == "c1_extraction.json"


# ----- O.1 test 2: fence-only response ------------------------------------


def test_fence_only_response_classified(monkeypatch):
    _enable_logger(monkeypatch)
    assert logger_mod.classify_response("```json\n```") == "fence_only"
    assert logger_mod.classify_response("```\n   \n```") == "fence_only"


# ----- O.1 test 3: valid JSON response ------------------------------------


def test_valid_json_response_classified(monkeypatch):
    _enable_logger(monkeypatch)
    assert logger_mod.classify_response('{"x": 1}') == "valid_json"
    assert logger_mod.classify_response("[1, 2, 3]") == "valid_json"
    # Fenced JSON also classifies as valid_json.
    assert (
        logger_mod.classify_response('```json\n{"x":1}\n```')
        == "valid_json"
    )


# ----- O.1 test 4: malformed JSON -----------------------------------------


def test_malformed_json_classified(monkeypatch):
    _enable_logger(monkeypatch)
    assert logger_mod.classify_response('{"x": 1,') == "malformed_json"
    assert logger_mod.classify_response("just some words") == "malformed_json"


# ----- O.1 test 5: refusal markers ----------------------------------------


def test_refusal_classified(monkeypatch):
    _enable_logger(monkeypatch)
    assert (
        logger_mod.classify_response("I cannot help with that")
        == "refusal"
    )
    assert logger_mod.classify_response("I'm unable to do this") == "refusal"
    # Valid JSON that mentions "I cannot" in a string field must NOT
    # be classified as refusal. Valid JSON always wins.
    txt = '{"decision_text": "Bob said I cannot attend"}'
    assert logger_mod.classify_response(txt) == "valid_json"


# ----- O.1 test 6: truncated when over the limit --------------------------


def test_truncated_when_over_limit(tmp_path, monkeypatch):
    _enable_logger(monkeypatch, max_chars=50)
    long = "x" * 5000
    assert logger_mod.classify_response(long) == "truncated"
    path = logger_mod.write_log(
        long,
        chunk_id="c1",
        source_id="s1",
        model="claude-haiku-4-5-20251001",
        call_type="extraction",
        sdl_root=tmp_path,
    )
    assert path is not None
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["response_type"] == "truncated"
    assert artifact["raw_response_chars"] == 5000
    assert len(artifact["raw_response_preview"]) == 50


# ----- O.1 test 7: disabled mode = zero writes, zero overhead -------------


def test_disabled_mode_writes_nothing(tmp_path, monkeypatch):
    # Logger left disabled (autouse fixture cleared the env var).
    assert logger_mod.is_enabled() is False
    path = logger_mod.write_log(
        "anything",
        chunk_id="c1",
        source_id="s1",
        model="m",
        call_type="extraction",
        sdl_root=tmp_path,
    )
    assert path is None
    # Disabled write_log_from_context returns None without inspecting context.
    assert logger_mod.write_log_from_context("x") is None
    # And nothing landed on disk.
    assert not any(tmp_path.rglob("*.json"))


# ----- O.1 test 8: call_type identifies the stage -------------------------


def test_call_type_field_correctly_set(tmp_path, monkeypatch):
    _enable_logger(monkeypatch)
    for call_type in (
        "extraction",
        "story",
        "two_stage_stage1",
        "two_stage_stage2",
        "classifier",
    ):
        path = logger_mod.write_log(
            '{"x": 1}',
            chunk_id=f"c_{call_type}",
            source_id="s1",
            model="m",
            call_type=call_type,
            sdl_root=tmp_path,
        )
        assert path is not None
        artifact = json.loads(path.read_text(encoding="utf-8"))
        assert artifact["call_type"] == call_type


# ----- O.1 test 9: schema validates as JSON Schema 2020-12 ----------------


def test_schema_is_draft_2020_12():
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "spectrum_systems_core"
        / "schemas"
        / "raw_api_response_log.schema.json"
    )
    doc = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(doc)
    assert doc["properties"]["artifact_type"]["const"] == "raw_api_response_log"
    assert doc["properties"]["schema_version"]["const"] == "1.0.0"


# ----- O.1 test 10: write path includes source_id + chunk_id --------------


def test_write_path_includes_source_and_chunk(tmp_path, monkeypatch):
    _enable_logger(monkeypatch)
    path = logger_mod.write_log(
        '{"x": 1}',
        chunk_id="chunk-abc",
        source_id="src-xyz",
        model="m",
        call_type="extraction",
        sdl_root=tmp_path,
    )
    assert path is not None
    # Path: <sdl_root>/debug/raw_responses/<source_id>/<chunk_id>_<call_type>.json
    assert path.parent.name == "src-xyz"
    assert path.name.startswith("chunk-abc_")
    assert path.name.endswith("_extraction.json")


# ----- Defensive: call_context propagates chunk + source_id ---------------


def test_call_context_propagation(tmp_path, monkeypatch):
    _enable_logger(monkeypatch)
    with logger_mod.call_context(
        chunk_id="ctx-chunk",
        source_id="ctx-source",
        sdl_root=tmp_path,
        call_type="story",
        model="m-from-ctx",
    ):
        path = logger_mod.write_log_from_context("hello")
    assert path is not None
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["chunk_id"] == "ctx-chunk"
    assert artifact["source_id"] == "ctx-source"
    assert artifact["model"] == "m-from-ctx"
    assert artifact["call_type"] == "story"


def test_call_context_resets_after_block(monkeypatch):
    _enable_logger(monkeypatch)
    assert logger_mod.current_context() == {}
    with logger_mod.call_context(chunk_id="x", source_id="y"):
        assert logger_mod.current_context()["chunk_id"] == "x"
    assert logger_mod.current_context() == {}


def test_unknown_call_type_normalised_to_other(tmp_path, monkeypatch):
    _enable_logger(monkeypatch)
    path = logger_mod.write_log(
        '{"x": 1}',
        chunk_id="c1",
        source_id="s1",
        model="m",
        call_type="not_in_enum",
        sdl_root=tmp_path,
    )
    assert path is not None
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["call_type"] == "other"


def test_artifact_path_segments_sanitised(tmp_path, monkeypatch):
    _enable_logger(monkeypatch)
    path = logger_mod.write_log(
        "raw",
        chunk_id="../../escape",
        source_id="../etc/passwd",
        model="m",
        call_type="extraction",
        sdl_root=tmp_path,
    )
    assert path is not None
    # Path must stay under <tmp_path>/debug/raw_responses/.
    rel = path.relative_to(tmp_path)
    assert rel.parts[0] == "debug"
    assert rel.parts[1] == "raw_responses"
