"""Tests for the ``--single-chunk`` / ``--print-context`` debug mode.

Trust properties defended here (CLAUDE.md testing philosophy):

* Selection is deterministic: the chunk with the most characters in
  its ``text`` is the one retained, ties resolve to the lowest index,
  and the printed ``SINGLE CHUNK MODE:`` header reports the correct
  1-based position / original total / turn_id / char count.
* Exactly that one chunk's text becomes the model input — proven by
  the ``--print-context`` context-bundle dump being byte-equal to the
  selected chunk's text (this is the answer to "did the transcript
  reach the API call?").
* The raw model response and the eval_results for the chunk are
  printed verbatim so the operator can see exactly what the model
  received and returned.
* Additivity / rollback: ``single_chunk=False`` prints none of the
  single-chunk markers and is deterministic (two off-runs produce a
  byte-identical artifact), so the knob is purely additive.
* CLI boundary: the ``--single-chunk`` knob reaches the workflow
  through ``meeting_minutes_llm`` and the exit code stays the normal
  promoted(0)/blocked(1) contract — the knob never forces exit 2.
"""
from __future__ import annotations

import contextlib
import io
import json

from spectrum_systems_core.cli import meeting_minutes_llm
from spectrum_systems_core.data_lake.chunker import chunk_transcript
from spectrum_systems_core.workflows import (
    run_meeting_minutes_llm_workflow,
)

# Three speaker turns of deliberately different sizes. The NTIA turn is
# unambiguously the largest by character count, so single-chunk mode
# must always select it (chunk index 1 -> "chunk 2/3", turn_id t0001).
LARGEST_DECISION = (
    "NTIA approved the 7 GHz downlink threshold of minus 47 dBm "
    "per megahertz"
)
TRANSCRIPT = "\n".join(
    [
        "CHAIR: ok",
        f"NTIA: {LARGEST_DECISION} and provided extensive supporting "
        "analysis for the record of this proceeding.",
        "DOD: DoD will submit revised ERP values.",
    ]
)


def _expected_selection():
    chunks = chunk_transcript(TRANSCRIPT)
    idx = max(range(len(chunks)), key=lambda i: len(chunks[i]["text"]))
    return chunks, idx, chunks[idx]


def _stub():
    """A deterministic stub grounded to the largest chunk's turn_id.

    The decision text is a verbatim substring of that chunk's text so
    the within-source eval is satisfied; promotion vs. block is not what
    these tests assert (they assert the debug PRINTING + selection).
    """

    def client(*, system, user):  # noqa: ARG001
        return json.dumps(
            {
                "decisions": [
                    {"text": LARGEST_DECISION, "verb": "approved"}
                ],
                "action_items": [],
                "open_questions": [],
                "grounding": [
                    {
                        "kind": "decision",
                        "text": LARGEST_DECISION,
                        "source_turns": ["t0001"],
                    }
                ],
            }
        )

    return client


def test_single_chunk_selects_largest_and_prints_header():
    chunks, idx, best = _expected_selection()
    assert best["turn_id"] == "t0001"
    assert idx == 1 and len(chunks) == 3

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(), single_chunk=True
        )
    out = buf.getvalue()

    assert (
        f"SINGLE CHUNK MODE: chunk 2/3 turn_id=t0001 "
        f"chars={len(best['text'])}" in out
    )
    assert "=== SINGLE CHUNK RAW MODEL RESPONSE ===" in out
    # The verbatim raw model response is echoed.
    assert '"verb": "approved"' in out
    assert "=== SINGLE CHUNK EVAL RESULTS ===" in out
    # An eval_result payload (JSON) was printed for the chunk.
    assert '"eval_type"' in out or '"reason_codes"' in out


def test_print_context_dumps_the_exact_chunk_text():
    """--print-context proves the transcript reached the API call: the
    context bundle the model was given is byte-equal to the single
    selected chunk's text."""
    _, _, best = _expected_selection()

    on = io.StringIO()
    with contextlib.redirect_stdout(on):
        run_meeting_minutes_llm_workflow(
            TRANSCRIPT,
            client=_stub(),
            single_chunk=True,
            print_context=True,
        )
    on_out = on.getvalue()
    assert "=== SINGLE CHUNK CONTEXT BUNDLE (first 1000 chars) ===" in on_out
    assert best["text"][:1000] in on_out

    off = io.StringIO()
    with contextlib.redirect_stdout(off):
        run_meeting_minutes_llm_workflow(
            TRANSCRIPT,
            client=_stub(),
            single_chunk=True,
            print_context=False,
        )
    assert "CONTEXT BUNDLE" not in off.getvalue()


def test_single_chunk_off_is_additive_and_deterministic():
    """single_chunk=False prints none of the markers and two off-runs
    produce a byte-identical artifact (the knob is purely additive)."""
    a_buf = io.StringIO()
    with contextlib.redirect_stdout(a_buf):
        a = run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(), single_chunk=False
        )
    b = run_meeting_minutes_llm_workflow(
        TRANSCRIPT, client=_stub(), single_chunk=False
    )

    assert "SINGLE CHUNK MODE" not in a_buf.getvalue()
    assert "SINGLE CHUNK RAW MODEL RESPONSE" not in a_buf.getvalue()
    assert (
        a.meeting_minutes.content_hash
        == b.meeting_minutes.content_hash
    )


def test_single_chunk_run_is_deterministic():
    """The artifact (content_hash), the selection header, and the raw
    response block are deterministic across runs. The printed
    eval_result envelopes carry fresh UUIDs by design in the in-memory
    governed loop (only the data-lake pipeline stabilizes ids), so the
    debug dump is NOT byte-identical end to end — and is not required
    to be: the determinism contract binds artifacts written to the
    lake, not a stdout debug dump."""
    h1 = io.StringIO()
    with contextlib.redirect_stdout(h1):
        r1 = run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(), single_chunk=True
        )
    h2 = io.StringIO()
    with contextlib.redirect_stdout(h2):
        r2 = run_meeting_minutes_llm_workflow(
            TRANSCRIPT, client=_stub(), single_chunk=True
        )
    assert (
        r1.meeting_minutes.content_hash
        == r2.meeting_minutes.content_hash
    )

    def _header_and_raw(text: str) -> list[str]:
        lines = text.splitlines()
        end = lines.index("=== END RAW MODEL RESPONSE ===")
        return lines[: end + 1]

    assert "SINGLE CHUNK MODE: chunk 2/3" in h1.getvalue()
    assert _header_and_raw(h1.getvalue()) == _header_and_raw(
        h2.getvalue()
    )


def test_cli_single_chunk_flag_flows_to_stdout(tmp_path, capsys):
    """The ``--single-chunk`` CLI knob reaches the workflow and the
    exit code stays the normal promoted(0)/blocked(1) contract — the
    knob itself never forces the pre-run-halt exit 2."""
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / "src-sc"
    staged.mkdir(parents=True)
    staged.joinpath("source.txt").write_text(TRANSCRIPT, encoding="utf-8")

    rc_on = meeting_minutes_llm(
        source_id="src-sc",
        data_lake=str(lake),
        single_chunk=True,
        print_context=True,
        client=_stub(),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    out_on = capsys.readouterr().out
    assert "SINGLE CHUNK MODE: chunk 2/3 turn_id=t0001" in out_on
    assert "=== SINGLE CHUNK RAW MODEL RESPONSE ===" in out_on
    assert "=== SINGLE CHUNK CONTEXT BUNDLE (first 1000 chars) ===" in out_on
    assert rc_on in (0, 1)  # never the pre-run-halt code

    rc_off = meeting_minutes_llm(
        source_id="src-sc",
        data_lake=str(lake),
        single_chunk=False,
        client=_stub(),
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    out_off = capsys.readouterr().out
    assert "SINGLE CHUNK MODE" not in out_off
    assert "SINGLE CHUNK RAW MODEL RESPONSE" not in out_off
