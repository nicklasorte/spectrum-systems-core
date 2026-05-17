"""Wiring tests for the meeting_minutes_llm -> validate-and-baseline path.

Covers the six properties the wiring change must hold:

1. Fence-stripping in the extract-typed parse path: a markdown-fenced
   Haiku response parses correctly (the PR #134 parity fix).
2. Fence-stripping regression: a clean (un-fenced) JSON response is
   unaffected.
3. ``llm_extraction_enabled`` default off -> the LLM step is skipped
   and the deterministic stages are unconditional (no behaviour change
   for existing consumers).
4. ``llm_extraction_enabled`` on -> the LLM step runs AFTER the
   deterministic extractor stages (additive, never instead of them)
   and produces an artifact with
   ``provenance.produced_by == "meeting_minutes_llm"``.
5. The compare-opus-haiku trigger fires AFTER the LLM extraction step.
6. An LLM extraction failure (control gate block, or no API key) makes
   the step exit non-zero so validate-and-baseline FAILS rather than
   silently skipping.

The fence + CLI tests run the REAL parser / governed loop (a
deterministic stub is the only thing injected, the same seam the
existing LLM integration tests use). The workflow tests assert the
YAML structure; CI-runtime correctness is out of scope for unit tests.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from spectrum_systems_core.cli import meeting_minutes_llm
from spectrum_systems_core.extraction.typed_extraction_runner import (
    _parse_json_response,
)
from spectrum_systems_core.extraction._resilience import strip_markdown_fence
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    json_stub,
    load_fixture,
    text_stub,
)

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    YAML_AVAILABLE = False

# tests/ is a sibling of .github/ under the repo root.
WORKFLOW_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "validate-and-baseline.yml"
)

SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
DEC18 = load_fixture("dec18_transcript.txt")


# ---------------------------------------------------------------------------
# 1 + 2. Fence-stripping in the extract-typed parse path.
#
# extract-typed parses Haiku JSON via
# typed_extraction_runner._parse_json_response ->
# _parse_json_response_strict -> _resilience.strip_markdown_fence.
# The PR #134 parity fix changed only the degenerate single-line
# (no-newline) fence branch from "" to text[3:]; multi-line fences and
# clean JSON must be unaffected.
# ---------------------------------------------------------------------------


def test_strip_fence_multiline_json_tag_parses() -> None:
    text = '```json\n{"items": [{"x": 1}]}\n```'
    assert strip_markdown_fence(text) == '{"items": [{"x": 1}]}'


def test_strip_fence_single_line_no_newline_preserves_body() -> None:
    """The PR #134 parity case: an opening fence with NO newline after
    it must drop only the three backticks, never the body. This is the
    exact shape that produced ``typed_extraction_llm_json_parse_failed``
    in the validate output."""
    text = '```{"items": [{"x": 1}]}```'
    assert strip_markdown_fence(text) == '{"items": [{"x": 1}]}'


def test_strip_fence_bare_fence_only_still_empty() -> None:
    """Fail-closed invariant preserved: a body that is ONLY a fence
    still collapses to "" ("```"[3:] == ""), so the X-1 'model wrote
    only a fence' halt is unchanged."""
    assert strip_markdown_fence("```") == ""
    assert strip_markdown_fence("``````") == ""


def test_strip_fence_clean_json_regression() -> None:
    """Regression: an un-fenced response is returned verbatim."""
    text = '{"items": [{"claim_text": "x"}]}'
    assert strip_markdown_fence(text) == text


def test_extract_typed_parser_handles_fenced_response() -> None:
    """The actual extract-typed entry point parses a fenced Haiku
    claims response into a dict (previously this fenced-no-newline
    shape fell through to the {} parse-failed path)."""
    fenced = '```json\n{"items": [{"claim_text": "the band is shared"}]}\n```'
    assert _parse_json_response(fenced) == {
        "items": [{"claim_text": "the band is shared"}]
    }

    fenced_no_nl = '```{"items": [{"claim_text": "the band is shared"}]}```'
    assert _parse_json_response(fenced_no_nl) == {
        "items": [{"claim_text": "the band is shared"}]
    }


def test_extract_typed_parser_clean_response_regression() -> None:
    """Regression: a clean (un-fenced) claims response still parses."""
    clean = '{"items": [{"claim_text": "the band is shared"}]}'
    assert _parse_json_response(clean) == {
        "items": [{"claim_text": "the band is shared"}]
    }


# ---------------------------------------------------------------------------
# 4 + 6. The meeting-minutes-llm CLI: provenance + fail-closed.
#
# The command reads the canonical text the deterministic run-pipeline
# stage stages at <lake>/store/raw/meetings/<sid>/source.txt and writes
# the promoted artifact to <lake>/store/processed/meetings/<sid>/ —
# exactly where compare_opus_haiku.py looks.
# ---------------------------------------------------------------------------


def _stage_source_txt(tmp_path: pathlib.Path) -> pathlib.Path:
    """Mirror pipeline_orchestrator._stage_transcript_into_meetings:
    the deterministic stage writes the canonical text here BEFORE the
    LLM step runs."""
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / SOURCE_ID
    staged.mkdir(parents=True)
    (staged / "source.txt").write_text(DEC18, encoding="utf-8")
    return lake


def test_cli_success_path_writes_llm_provenance(tmp_path, capsys) -> None:
    """Happy path (stub client, no network): a promoted meeting_minutes
    artifact lands in the SDL store layout where compare_opus_haiku.py
    looks, carrying provenance.produced_by == 'meeting_minutes_llm'."""
    lake = _stage_source_txt(tmp_path)

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )

    assert rc == 0, capsys.readouterr()

    proc = lake / "store" / "processed" / "meetings" / SOURCE_ID
    promoted = sorted(proc.glob("meeting_minutes__*.json"))
    assert len(promoted) == 1, promoted
    body = json.loads(promoted[0].read_text(encoding="utf-8"))
    assert (
        body["payload"]["provenance"]["produced_by"]
        == "meeting_minutes_llm"
    )
    assert body["status"] == "promoted"
    assert body["artifact_type"] == "meeting_minutes"
    assert body["payload"]["decisions"] == DEC18_DECISIONS


def test_cli_missing_staged_transcript_halts(tmp_path, capsys) -> None:
    """Fail-closed: no staged source.txt (run-pipeline did not run) ->
    exit 2, nothing inferred, nothing written."""
    lake = tmp_path / "dl"
    (lake / "store").mkdir(parents=True)

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        client=json_stub(decisions=DEC18_DECISIONS),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )

    assert rc == 2
    out = capsys.readouterr().out
    assert "staged transcript missing" in out
    assert not (lake / "store" / "processed").exists()


def test_cli_no_api_key_halts_fail_closed(tmp_path, capsys) -> None:
    """Fail-closed: flag-on path with no ANTHROPIC_API_KEY halts pre-run
    with reason_code=config_error and exits non-zero (NOT a silent
    fall-back to the regex extractor). client=None so the workflow's
    own preflight gate runs; the env has no key so it never reaches a
    network call."""
    lake = _stage_source_txt(tmp_path)

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        client=None,
        env={},  # explicit empty env: no ANTHROPIC_API_KEY
    )

    assert rc == 2
    out = capsys.readouterr().out
    assert "reason_code=config_error" in out
    proc = lake / "store" / "processed" / "meetings" / SOURCE_ID
    assert not proc.exists() or not sorted(proc.glob("meeting_minutes__*.json"))


def test_cli_blocked_run_exits_nonzero(tmp_path, capsys) -> None:
    """Fail-closed: a control-blocked run (the stub returns non-JSON so
    the strict-schema eval fails) exits 1 and writes NOTHING. The
    comparison is meaningless without a promoted Haiku artifact; a
    blocked run must NOT pass as success."""
    lake = _stage_source_txt(tmp_path)

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        client=text_stub("this is not json at all"),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    proc = lake / "store" / "processed" / "meetings" / SOURCE_ID
    assert not proc.exists() or not sorted(proc.glob("meeting_minutes__*.json"))


# ---------------------------------------------------------------------------
# 3 + 4 + 5. validate-and-baseline.yml structure.
#
# PyYAML maps the top-level ``on:`` key to the boolean True (YAML 1.1),
# so the inputs block is read via doc[True]; step ordering is asserted
# on the raw text the same way the existing workflow tests do.
# ---------------------------------------------------------------------------


def _skip_if_missing() -> None:
    if not WORKFLOW_PATH.is_file():
        pytest.skip("validate-and-baseline.yml not present on this branch")


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_llm_input_defaults_off_and_boolean() -> None:
    _skip_if_missing()
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    assert "llm_extraction_enabled" in inputs
    spec = inputs["llm_extraction_enabled"]
    assert spec["type"] == "boolean"
    # Default MUST be off: no behaviour change for existing consumers.
    assert spec["default"] is False


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_deterministic_stages_unconditional_and_before_llm() -> None:
    """The deterministic extractor stages must always run (no ``if``
    gating them on the flag) and the LLM step must come AFTER them —
    additive, never instead of them."""
    _skip_if_missing()
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = doc["jobs"]["validate-and-baseline"]["steps"]
    by_name = {s.get("name"): s for s in steps}
    names = [s.get("name") for s in steps]

    run_pipeline = "Run pipeline for target transcript"
    extract_typed = "Run typed extraction for target transcript"
    llm_step = "Run LLM extraction (meeting_minutes_llm)"

    assert by_name[run_pipeline].get("if") is None
    assert by_name[extract_typed].get("if") is None
    assert names.index(run_pipeline) < names.index(llm_step)
    assert names.index(extract_typed) < names.index(llm_step)


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_llm_step_conditional_and_fail_closed() -> None:
    """The LLM step is gated on llm_extraction_enabled, calls the
    meeting-minutes-llm CLI, and has NO continue-on-error (a non-zero
    exit fails the workflow — never a silent skip while enabled)."""
    _skip_if_missing()
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = doc["jobs"]["validate-and-baseline"]["steps"]
    llm = next(
        s
        for s in steps
        if s.get("name") == "Run LLM extraction (meeting_minutes_llm)"
    )
    assert "llm_extraction_enabled" in llm["if"]
    assert "meeting-minutes-llm" in llm["run"]
    assert "--source-id" in llm["run"]
    assert "--data-lake" in llm["run"]
    # Fail-closed: a failure of this step must fail the workflow.
    assert "continue-on-error" not in llm
    assert llm.get("continue-on-error") is not True


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_no_hardcoded_model_string_in_llm_step() -> None:
    """The Haiku model comes from the registry via the workflow's
    AnthropicJSONClient; no model id is pinned in the YAML step."""
    _skip_if_missing()
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = doc["jobs"]["validate-and-baseline"]["steps"]
    llm = next(
        s
        for s in steps
        if s.get("name") == "Run LLM extraction (meeting_minutes_llm)"
    )
    blob = json.dumps(llm)
    assert "claude-" not in blob
    assert "haiku-" not in blob.lower()


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_compare_trigger_fires_after_llm_step() -> None:
    """The compare-opus-haiku trigger must be ordered AFTER the LLM
    extraction step (and after the Haiku push) — the comparison is only
    meaningful once the meeting_minutes_llm artifact exists."""
    _skip_if_missing()
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    llm_idx = body.find("Run LLM extraction (meeting_minutes_llm)")
    push_idx = body.find("Push Haiku meeting_minutes_llm artifact")
    trigger_idx = body.find("Trigger Haiku-vs-Opus comparison")
    assert llm_idx != -1 and push_idx != -1 and trigger_idx != -1
    assert llm_idx < push_idx < trigger_idx, (
        "compare-opus-haiku trigger must fire AFTER the LLM extraction "
        "and Haiku push so the comparison reads a present Haiku artifact"
    )


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_haiku_push_gated_on_flag_keeps_default_path_identical() -> None:
    """The Haiku-artifact push + its .gitignore negation are gated on
    llm_extraction_enabled so the default (flag-off) path commits
    exactly the same paths as before this change."""
    _skip_if_missing()
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = doc["jobs"]["validate-and-baseline"]["steps"]
    by_name = {s.get("name"): s for s in steps}

    for name in (
        "Ensure data-lake .gitignore negates the Haiku artifact",
        "Push Haiku meeting_minutes_llm artifact",
    ):
        assert name in by_name, f"missing step {name!r}"
        assert "llm_extraction_enabled" in by_name[name]["if"]

    # The pre-existing baseline push is unchanged (still flag-agnostic).
    baseline_push = by_name["Push baseline artifacts"]
    assert "llm_extraction_enabled" not in str(baseline_push.get("if"))


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML required")
def test_trigger_still_checks_llm_provenance() -> None:
    """Regression of the PR #131 trigger contract: the trigger only
    dispatches when a meeting_minutes artifact with
    provenance.produced_by == 'meeting_minutes_llm' exists (a regex
    artifact must not count)."""
    _skip_if_missing()
    body = WORKFLOW_PATH.read_text(encoding="utf-8")
    trigger_idx = body.find("Trigger Haiku-vs-Opus comparison")
    trigger_body = body[trigger_idx:]
    assert 'produced_by") == "meeting_minutes_llm"' in trigger_body
