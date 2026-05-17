"""Unit tests for the per-chunk debug report (``--debug-chunks``).

Trust properties defended here (CLAUDE.md testing philosophy — Unit:
pure logic, plus a fail-closed / additivity property):

* every blocking item is attributed to the EXACT chunk that produced
  it (via the model's ``grounding`` array or a structured item's own
  ``source_turns``);
* an item whose grounding does not resolve to any chunk is surfaced
  under ``UNATTRIBUTED`` — never silently dropped;
* the report is deterministic (same inputs -> byte-identical output)
  and never raises on a malformed payload;
* turning the knob on is observe-only: the artifact's ``content_hash``
  and the control decision are byte-identical with debug on vs off.
"""
from __future__ import annotations

import io
import contextlib
import json
from types import SimpleNamespace

from spectrum_systems_core.cli import meeting_minutes_llm
from spectrum_systems_core.data_lake.chunker import chunk_transcript
from spectrum_systems_core.workflows import (
    build_chunk_debug_report,
    run_meeting_minutes_llm_workflow,
)

TRANSCRIPT = "\n".join(
    [
        "7 GHZ TIG MEETING - multi turn transcript",
        "",
        "CHAIR: Good morning, this is the 7 GHz downlink TIG kickoff.",
        "NTIA: NTIA approved the 7 GHz downlink threshold of minus 47 dBm per megahertz.",
        "CHAIR: The committee blorped the plan for the 7 GHz band.",
        "DOD: DoD will submit revised ERP values before the next session.",
        "NTIA: One open question remains about coordination distance.",
        "CHAIR: Hearing none, we adjourn. Thank you all.",
    ]
)

DECISIONS = [
    {"text": "The committee blorped the plan for the 7 GHz band.",
     "verb": "blorped"},
    {"text": "NTIA approved the 7 GHz downlink threshold of minus 47 dBm per megahertz.",
     "verb": "approved"},
]
ACTION_ITEMS = ["Do something that was never actually said in the meeting"]


def _grounding(chunks):
    tid = {c["text"][:20]: c["turn_id"] for c in chunks}
    blorp = next(c["turn_id"] for c in chunks if "blorped" in c["text"])
    good = next(
        c["turn_id"] for c in chunks if "NTIA approved" in c["text"]
    )
    ws = next(
        c["turn_id"] for c in chunks if "submit revised ERP" in c["text"]
    )
    return blorp, good, ws, [
        {"kind": "decision",
         "text": "The committee blorped the plan for the 7 GHz band.",
         "source_turns": [blorp]},
        {"kind": "decision",
         "text": "NTIA approved the 7 GHz downlink threshold of minus 47 dBm per megahertz.",
         "source_turns": [good]},
        {"kind": "action_item",
         "text": "Do something that was never actually said in the meeting",
         "source_turns": [ws]},
    ]


def _stub(grounding):
    def client(*, system, user):  # noqa: ARG001
        return json.dumps(
            {
                "decisions": DECISIONS,
                "action_items": ACTION_ITEMS,
                "open_questions": [],
                "grounding": grounding,
            }
        )

    return client


def _pos(chunks, turn_id):
    return next(
        i + 1 for i, c in enumerate(chunks) if c["turn_id"] == turn_id
    )


def _block(report, pos, total):
    lines = report.splitlines()
    head = f"CHUNK {pos}/{total} [turn_id="
    for i, ln in enumerate(lines):
        if ln.startswith(head):
            return "\n".join(lines[i:i + 4])
    raise AssertionError(f"no block for chunk {pos}/{total}")


def test_blocking_items_attributed_to_producing_chunk():
    chunks = chunk_transcript(TRANSCRIPT)
    blorp, good, ws, grounding = _grounding(chunks)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(grounding), debug_chunks=True
        )
    report = buf.getvalue()
    total = len(chunks)

    assert "=== CHUNK DEBUG (meeting_minutes_llm) ===" in report
    assert not res.promoted  # bad verb + within-source both block

    blorp_block = _block(report, _pos(chunks, blorp), total)
    assert '"blorped"' in blorp_block
    assert "regulatory_verb_issues" in blorp_block

    ws_block = _block(report, _pos(chunks, ws), total)
    assert "within_source_issues" in ws_block
    assert "never actually said" in ws_block

    # The well-formed decision's chunk carries no issue.
    good_block = _block(report, _pos(chunks, good), total)
    assert "regulatory_verb_issues: []" in good_block


def test_unresolvable_grounding_surfaces_as_unattributed():
    chunks = chunk_transcript(TRANSCRIPT)
    # Ground the bad-verb decision to a turn_id that does NOT exist.
    grounding = [
        {"kind": "decision",
         "text": "The committee blorped the plan for the 7 GHz band.",
         "source_turns": ["t9999"]},
    ]
    report = build_chunk_debug_report(
        payload={
            "decisions": [DECISIONS[0]],
            "action_items": [],
            "open_questions": [],
            "grounding": grounding,
        },
        chunks=chunks,
        eval_results=[
            SimpleNamespace(
                payload={
                    "eval_type": "regulatory_verb",
                    "reason_codes": [
                        "verb_not_classified:blorped|decision[0]:"
                        "The committee blorped the plan for the 7 GHz band."
                    ],
                }
            )
        ],
    )
    assert "UNATTRIBUTED" in report
    assert '"blorped"' in report.split("UNATTRIBUTED", 1)[1]


def test_schema_enum_violation_attributed_via_source_turns():
    chunks = chunk_transcript(TRANSCRIPT)
    target_turn = chunks[2]["turn_id"]
    report = build_chunk_debug_report(
        payload={
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "issue_registry_entry": [
                {"issue_id": "i-1", "issue_type": "BOGUS_ENUM",
                 "source_turns": [target_turn]}
            ],
            "grounding": [],
        },
        chunks=chunks,
        eval_results=[
            SimpleNamespace(
                payload={
                    "eval_type": "llm_extraction_strict_schema",
                    "reason_codes": [
                        "schema_violation:artifact_type=meeting_minutes "
                        "failed schema: 'BOGUS_ENUM' is not one of "
                        "['technical', 'process'] at "
                        "path=['issue_registry_entry', 0, 'issue_type']",
                        "schema_violation:not_a_list:foo",
                    ],
                }
            )
        ],
    )
    pos = _pos(chunks, target_turn)
    block = _block(report, pos, len(chunks))
    assert "BOGUS_ENUM" in block
    # The path-less structural code is payload-global -> UNATTRIBUTED.
    assert "not_a_list:foo" in report.split("UNATTRIBUTED", 1)[1]


def test_report_is_deterministic_and_never_raises_on_garbage():
    chunks = chunk_transcript(TRANSCRIPT)
    payload = {"decisions": "not-a-list", "grounding": {"bad": 1}}
    a = build_chunk_debug_report(
        payload=payload, chunks=chunks, eval_results=[]
    )
    b = build_chunk_debug_report(
        payload=payload, chunks=chunks, eval_results=[]
    )
    assert a == b
    assert "=== CHUNK DEBUG" in a
    # No chunks -> graceful single-line report, not a crash.
    assert "no chunks (ungrounded path)" in build_chunk_debug_report(
        payload={}, chunks=None, eval_results=[]
    )


def test_cli_debug_chunks_flag_flows_to_stdout(tmp_path, capsys):
    """The ``--debug-chunks`` CLI knob reaches the workflow: ON prints
    the per-chunk report, OFF prints nothing and the exit code is
    unaffected (observe-only at the CLI boundary too)."""
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / "src-dbg"
    staged.mkdir(parents=True)
    staged.joinpath("source.txt").write_text(TRANSCRIPT, encoding="utf-8")
    chunks = chunk_transcript(TRANSCRIPT)
    _, _, _, grounding = _grounding(chunks)

    rc_on = meeting_minutes_llm(
        source_id="src-dbg",
        data_lake=str(lake),
        debug_chunks=True,
        client=_stub(grounding),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    out_on = capsys.readouterr().out
    assert "=== CHUNK DEBUG (meeting_minutes_llm) ===" in out_on
    # bad verb + within-source both block -> CLI exit 1, nothing written.
    assert rc_on == 1

    rc_off = meeting_minutes_llm(
        source_id="src-dbg",
        data_lake=str(lake),
        debug_chunks=False,
        client=_stub(grounding),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    out_off = capsys.readouterr().out
    assert "CHUNK DEBUG" not in out_off
    assert rc_off == rc_on  # the knob never changes the exit code


def test_debug_off_is_byte_identical_observe_only():
    chunks = chunk_transcript(TRANSCRIPT)
    _, _, _, grounding = _grounding(chunks)

    on_buf = io.StringIO()
    with contextlib.redirect_stdout(on_buf):
        on = run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(grounding), debug_chunks=True
        )
    off_buf = io.StringIO()
    with contextlib.redirect_stdout(off_buf):
        off = run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(grounding), debug_chunks=False
        )

    assert "CHUNK DEBUG" not in off_buf.getvalue()
    assert "CHUNK DEBUG" in on_buf.getvalue()
    assert (
        on.meeting_minutes.content_hash
        == off.meeting_minutes.content_hash
    )
    assert (
        on.control_decision.payload["decision"]
        == off.control_decision.payload["decision"]
    )
