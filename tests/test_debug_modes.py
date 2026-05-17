"""Tests for the four observe-only meeting_minutes_llm debug modes.

Each test pins one mode's contract: Mode 4 preserves known-good items
through the REAL parser, Mode 2 makes exactly one framework-bypassing
call with the real prompt, Mode 3 diffs without calling, Mode 1 prints
the raw response before parsing, and all four write nothing to the
data-lake. The embedded red-team passes are encoded as the
``*_red_team_*`` tests below.
"""
from __future__ import annotations

import pathlib

import pytest

from spectrum_systems_core.cli import meeting_minutes_llm
from spectrum_systems_core.data_lake.chunker import chunk_transcript
from spectrum_systems_core.workflows import meeting_minutes_llm as _mm
from spectrum_systems_core.workflows.debug_modes import (
    SYNTHETIC_PARSER_TEST_RESPONSE,
    build_opus_vs_llm_diff,
    build_parser_isolation_report,
)
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    json_stub,
    load_fixture,
)

SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
DEC18 = load_fixture("dec18_transcript.txt")


def _stage_source_txt(tmp_path: pathlib.Path) -> pathlib.Path:
    """Mirror the deterministic run-pipeline stage: canonical text at
    <lake>/store/raw/meetings/<sid>/source.txt BEFORE the LLM step."""
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / SOURCE_ID
    staged.mkdir(parents=True)
    (staged / "source.txt").write_text(DEC18, encoding="utf-8")
    return lake


def _snapshot(root: pathlib.Path) -> dict[str, bytes]:
    """Path -> bytes for every file under ``root`` (observe-only proof)."""
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class RecordingClient:
    """Counts calls and captures the last (system, user) pair."""

    def __init__(self, response: str = '{"decisions":[],"action_items":[],'
                 '"open_questions":[],"grounding":[]}'):
        self.calls = 0
        self.system: str | None = None
        self.user: str | None = None
        self._response = response

    def __call__(self, *, system: str, user: str) -> str:
        self.calls += 1
        self.system = system
        self.user = user
        return self._response


def _parse_report(report: str) -> dict[str, str]:
    """Flatten ``key: value`` lines of a single-level report block."""
    fields: dict[str, str] = {}
    for line in report.splitlines():
        if ": " in line and not line.startswith("==="):
            k, _, v = line.partition(": ")
            fields[k.strip()] = v.strip()
    return fields


# ---------------------------------------------------------------------------
# Mode 4 — parser isolation.
# ---------------------------------------------------------------------------
def test_parser_isolation_passes_with_synthetic_input() -> None:
    """The known-good synthetic must survive the REAL parser intact.

    This asserts on the output of ``_parse_llm_payload`` (the exact code
    path the workflow runs on a real model response). If the parser ever
    regresses to drop items, g/d/a stop equalling 2/1/1 and this test
    FAILS — which is the whole point: it validates the failure mode.
    """
    report = build_parser_isolation_report()
    fields = _parse_report(report)

    assert fields["output_grounding_entries"] == "2", report
    assert fields["output_decisions"] == "1", report
    assert fields["output_action_items"] == "1", report
    assert fields["parser_result"] == "PASS (items preserved)", report
    assert "=== PARSER ISOLATION TEST ===" in report
    assert "=== END PARSER TEST ===" in report


def test_parser_isolation_red_team_2_fails_when_parser_drops_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded red-team pass 2: prove the Mode-4 test would FAIL if the
    parser dropped items. Simulate a parser that silently zeroes
    grounding; the report must then read FAIL, not PASS."""

    real_parser = _mm._parse_llm_payload

    def _dropping_parser(raw: str):
        parsed = real_parser(raw)
        if parsed is not None:
            parsed["grounding"] = []  # simulate the production failure
        return parsed

    monkeypatch.setattr(_mm, "_parse_llm_payload", _dropping_parser)
    report = build_parser_isolation_report()
    fields = _parse_report(report)
    assert fields["output_grounding_entries"] == "0", report
    assert fields["parser_result"] == "FAIL (items dropped)", report


def test_synthetic_red_team_3_covers_every_parser_array() -> None:
    """Embedded red-team pass 3 + pass 1: the synthetic must carry every
    array the real parser carries (legacy + structured + grounding) with
    schema-shaped field names, or a green test proves nothing."""
    required = set(_mm._LEGACY_ARRAYS) | set(_mm._STRUCTURED_ARRAYS) | {
        "grounding"
    }
    assert required.issubset(set(SYNTHETIC_PARSER_TEST_RESPONSE)), (
        required - set(SYNTHETIC_PARSER_TEST_RESPONSE)
    )
    # Field names match the canonical prompt's documented item shapes.
    g0 = SYNTHETIC_PARSER_TEST_RESPONSE["grounding"][0]
    assert set(g0) == {"kind", "text", "source_turns"}
    d0 = SYNTHETIC_PARSER_TEST_RESPONSE["decisions"][0]
    assert {"text", "verb", "source_turns"}.issubset(d0)


# ---------------------------------------------------------------------------
# Mode 2 — minimal repro.
# ---------------------------------------------------------------------------
def test_minimal_repro_makes_one_api_call(tmp_path, capsys) -> None:
    lake = _stage_source_txt(tmp_path)
    rec = RecordingClient()

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        minimal_repro=True,
        client=rec,
    )

    assert rc == 0, capsys.readouterr()
    assert rec.calls == 1, "minimal repro must make EXACTLY one call"
    first_turn_text = chunk_transcript(DEC18)[0]["text"]
    assert first_turn_text in rec.user, (
        "the user message must contain the real transcript turns"
    )
    out = capsys.readouterr().out
    assert "=== MINIMAL REPRO ===" in out
    assert "model_call: 1" in out


def test_minimal_repro_red_team_1_uses_real_prompt_file(
    tmp_path, capsys
) -> None:
    """Embedded red-team pass 1/2: Mode 2 must send the REAL prompt file
    verbatim as the system message, never a hardcoded string."""
    lake = _stage_source_txt(tmp_path)
    rec = RecordingClient()

    meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        minimal_repro=True,
        client=rec,
    )
    capsys.readouterr()

    real_prompt = _mm._PROMPT_PATH.read_text(encoding="utf-8")
    assert rec.system == real_prompt, (
        "system message must be the canonical prompt file verbatim"
    )


# ---------------------------------------------------------------------------
# Mode 3 — diff vs opus.
# ---------------------------------------------------------------------------
def test_diff_vs_opus_does_not_make_api_call(tmp_path, capsys) -> None:
    lake = _stage_source_txt(tmp_path)
    rec = RecordingClient()

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        diff_vs_opus=True,
        client=rec,
    )

    assert rc == 0, capsys.readouterr()
    assert rec.calls == 0, "Mode 3 must NOT make an API call"
    out = capsys.readouterr().out
    assert "user_message_contains_transcript:" in out
    assert "=== API CALL DIFF: meeting_minutes_llm vs opus_baseline ===" in out


def test_diff_vs_opus_red_team_3_transcript_bool_always_present() -> None:
    """Embedded red-team pass 3: the single most important field —
    user_message_contains_transcript — is ALWAYS printed as a bool for
    both sides. DEC18 has content, so llm/opus are both True; the turn
    block is llm-only."""
    diff = build_opus_vs_llm_diff(transcript_text=DEC18)
    lines = diff.splitlines()
    i = lines.index("user_message_contains_transcript:")
    llm_line = lines[i + 1].strip()
    opus_line = lines[i + 2].strip()
    assert llm_line.startswith("llm:") and llm_line.endswith(("True", "False"))
    assert opus_line.startswith("opus:") and opus_line.endswith(
        ("True", "False")
    )
    # DEC18 has content, so the transcript reaches both calls.
    assert llm_line.endswith("True")
    assert opus_line.endswith("True")
    j = lines.index("user_message_contains_turn_ids:")
    assert lines[j + 1].strip().endswith("True")  # llm has the turn block
    assert lines[j + 2].strip().endswith("False")  # opus sends raw text


# ---------------------------------------------------------------------------
# Mode 1 — print raw response before parsing.
# ---------------------------------------------------------------------------
def test_print_raw_response_prints_before_parsing(tmp_path, capsys) -> None:
    lake = _stage_source_txt(tmp_path)

    rc = meeting_minutes_llm(
        source_id=SOURCE_ID,
        data_lake=str(lake),
        print_raw_response=True,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )

    assert rc == 0, capsys.readouterr()
    out = capsys.readouterr().out
    raw_idx = out.find("=== RAW API RESPONSE")
    parsed_idx = out.find("OBSERVE-ONLY (print-raw-response)")
    assert raw_idx != -1, out
    assert parsed_idx != -1, out
    assert raw_idx < parsed_idx, (
        "the raw response must be printed BEFORE any parsed output"
    )
    assert "=== END RAW RESPONSE ===" in out
    assert "raw_contains_grounding=" in out


# ---------------------------------------------------------------------------
# All four modes are observe-only — they write NOTHING to the data-lake.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"print_raw_response": True},
        {"minimal_repro": True},
        {"diff_vs_opus": True},
        {"test_parser_only": True},
    ],
    ids=["mode1", "mode2", "mode3", "mode4"],
)
def test_modes_are_observe_only(tmp_path, capsys, kwargs) -> None:
    lake = _stage_source_txt(tmp_path)
    before = _snapshot(lake)

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
        **kwargs,
    )

    assert rc == 0, capsys.readouterr()
    after = _snapshot(lake)
    assert before == after, (
        f"{kwargs} mutated the data-lake (must be observe-only)"
    )
    proc = lake / "store" / "processed"
    assert not proc.exists() or not list(proc.rglob("meeting_minutes__*.json"))


def test_red_team_3_all_modes_default_false_no_behavior_change(
    tmp_path, capsys
) -> None:
    """Embedded red-team pass 3: with NO mode flag the command behaves
    exactly as before — a promoted artifact is written to disk."""
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
    assert sorted(proc.glob("meeting_minutes__*.json")), (
        "default (no debug mode) run must still write the promoted artifact"
    )
