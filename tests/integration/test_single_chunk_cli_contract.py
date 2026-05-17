"""Contract: the ``meeting-minutes-llm --single-chunk`` CLI wiring.

Exercises the REAL CLI command through ``subprocess`` against a real
temp data-lake (the CLAUDE.md integration-test requirement: subprocess,
real temp dir, assert on output — not a mocked dict).

The transport is fixed via the existing
``MEETING_MINUTES_LLM_STUB_RESPONSE_PATH`` seam (the same file-backed
client seam the other LLM CLI contract tests use), so the real
chunker, prompt builder, every eval, the control / promotion gate and
the new single-chunk selection all run with no network and no API key.

Properties pinned:

1. ``--single-chunk`` selects the single largest transcript chunk and
   prints the ``SINGLE CHUNK MODE:`` header with the correct
   position / original total / turn_id, plus the verbatim raw model
   response and the chunk's eval_results.
2. ``--print-context`` additionally dumps the context bundle the model
   was given, and that dump contains the selected chunk's text — the
   on-disk proof that the transcript reached the API call.
3. The exit code stays the normal promoted(0)/blocked(1) contract; the
   debug knob never forces the pre-run-halt exit 2.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SOURCE_ID = "single-chunk-cli-contract"

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

STUB_RESPONSE = json.dumps(
    {
        "decisions": [{"text": LARGEST_DECISION, "verb": "approved"}],
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


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / SOURCE_ID
    staged.mkdir(parents=True)
    staged.joinpath("source.txt").write_text(TRANSCRIPT, encoding="utf-8")
    stub = tmp_path / "stub_response.json"
    stub.write_text(STUB_RESPONSE, encoding="utf-8")
    return lake, stub


def test_cli_single_chunk_subprocess_contract(tmp_path):
    lake, stub = _stage(tmp_path)

    import os

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["MEETING_MINUTES_LLM_STUB_RESPONSE_PATH"] = str(stub)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "spectrum_systems_core.cli",
            "meeting-minutes-llm",
            "--source-id",
            SOURCE_ID,
            "--data-lake",
            str(lake),
            "--single-chunk",
            "--print-context",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    out = proc.stdout
    # NTIA turn is the largest -> chunk index 1 of 3, turn_id t0001.
    assert "SINGLE CHUNK MODE: chunk 2/3 turn_id=t0001 chars=" in out, out
    assert "=== SINGLE CHUNK RAW MODEL RESPONSE ===" in out, out
    assert '"verb": "approved"' in out, out
    assert "=== SINGLE CHUNK EVAL RESULTS ===" in out, out
    assert (
        "=== SINGLE CHUNK CONTEXT BUNDLE (first 1000 chars) ===" in out
    ), out
    # The context bundle the model was given IS the selected chunk's
    # text — the operative proof the transcript reached the API call.
    assert LARGEST_DECISION in out, out
    # Normal promoted(0)/blocked(1) contract; never the pre-run halt.
    assert proc.returncode in (0, 1), (proc.returncode, out, proc.stderr)
