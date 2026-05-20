"""Contract: every ``meeting-minutes-llm --model X`` writes a complete
Phase 2 extraction_config block.

Regression test for the Phase 5 hotfix where ``--model sonnet`` (and the
other non-haiku tokens) wrote an artifact containing
``provenance.extraction_config = {"prompt_variant": "..."}`` — a partial
stamp the meeting_minutes schema rejects because the seven Phase 2
required fields are missing.

Runs the REAL CLI through ``subprocess`` against a real temp data-lake
(the CLAUDE.md integration-test rule: subprocess + real temp dir +
assert on disk). The LLM transport is fixed via the existing
``MEETING_MINUTES_LLM_STUB_RESPONSE_PATH`` seam so no API key is
required.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SOURCE_ID = "ec-contract-source"

DECISION_TEXT = (
    "NTIA approved the 7 GHz downlink threshold of minus 47 dBm "
    "per megahertz"
)
TRANSCRIPT = "\n".join(
    [
        "CHAIR: ok",
        f"NTIA: {DECISION_TEXT} and provided extensive supporting "
        "analysis for the record of this proceeding.",
        "DOD: DoD will submit revised ERP values.",
    ]
)

STUB_RESPONSE = json.dumps(
    {
        "decisions": [{"text": DECISION_TEXT, "verb": "approved"}],
        "action_items": [],
        "open_questions": [],
        "grounding": [
            {
                "kind": "decision",
                "text": DECISION_TEXT,
                "source_turns": ["t0001"],
            }
        ],
    }
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = (
    REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / SOURCE_ID
    staged.mkdir(parents=True)
    staged.joinpath("source.txt").write_text(TRANSCRIPT, encoding="utf-8")
    stub = tmp_path / "stub_response.json"
    stub.write_text(STUB_RESPONSE, encoding="utf-8")
    return lake, stub


def _read_produced_artifact(lake: Path) -> dict:
    proc_dir = lake / "store" / "processed" / "meetings" / SOURCE_ID
    paths = sorted(proc_dir.glob("meeting_minutes__*.json"))
    assert paths, (
        f"no meeting_minutes artifact written under {proc_dir}; "
        f"contents: {[p.name for p in proc_dir.iterdir()] if proc_dir.is_dir() else 'missing'}"
    )
    # Use the newest produced file. The CLI is idempotent on a single
    # input but a previous run could have left a stale file.
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def _flat_for_schema(artifact: dict) -> dict:
    payload = artifact.get("payload") or {}
    return {"artifact_type": "meeting_minutes", **payload}


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


REQUIRED_EC_FIELDS = (
    "temperature",
    "seed_inputs",
    "chunks_full_hash",
    "chunk_count",
    "first_chunk_hash",
    "last_chunk_hash",
    "prompt_content_hash",
)


@pytest.mark.parametrize(
    "model_token, expected_variant, expected_model_id_substring",
    [
        ("haiku", "production_haiku", "claude-haiku"),
        ("sonnet", "haiku_prompt_with_sonnet_model", "claude-sonnet"),
        (
            "sonnet-unconstrained",
            "opus_prompt_with_sonnet_model",
            "claude-sonnet",
        ),
        ("opus", "opus_baseline", "claude-opus"),
    ],
)
def test_cli_writes_full_extraction_config_for_every_model(
    tmp_path: Path,
    model_token: str,
    expected_variant: str,
    expected_model_id_substring: str,
) -> None:
    """Every ``--model X`` produces an artifact whose
    ``provenance.extraction_config`` carries every Phase 2 required
    field AND validates against the meeting_minutes schema.

    The pre-fix behaviour stamped only ``prompt_variant``, which made
    the schema fail with
    ``'temperature' is a required property in provenance -> extraction_config``.
    """
    lake, stub = _stage(tmp_path)

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
            "--model",
            model_token,
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    # Non-zero exit only when the artifact is blocked; we want a
    # promoted write so the on-disk extraction_config is observable.
    assert proc.returncode == 0, (
        f"CLI exit={proc.returncode}\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )

    artifact = _read_produced_artifact(lake)
    payload = artifact["payload"]
    provenance = payload.get("provenance", {})
    ec = provenance.get("extraction_config")

    assert ec is not None, (
        f"extraction_config missing on {model_token} artifact; "
        f"provenance keys={list(provenance.keys())}"
    )
    for key in REQUIRED_EC_FIELDS:
        assert key in ec, (
            f"{model_token}: extraction_config missing required field "
            f"{key!r}; got keys={sorted(ec.keys())}"
        )

    # Phase 5 discriminator must be the resolver's value for this token.
    assert ec.get("prompt_variant") == expected_variant

    # The seed_inputs.model_id should reflect what was actually run.
    assert expected_model_id_substring in ec["seed_inputs"]["model_id"], (
        f"{model_token}: expected model_id containing "
        f"{expected_model_id_substring!r}, got "
        f"{ec['seed_inputs']['model_id']!r}"
    )

    # The on-disk artifact must validate against the schema's flat
    # projection — the same shape the comparison engine rejects when
    # the partial-stamp bug is present.
    jsonschema.Draft202012Validator(_schema()).validate(_flat_for_schema(artifact))
