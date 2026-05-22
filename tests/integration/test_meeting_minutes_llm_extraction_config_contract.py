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


# Multi-batch variant of the contract test. The chunk-132 fix changed
# the success-path aggregation (it filters malformed grounding items),
# which is only exercised when ``len(chunks) > _CHUNKS_PER_BATCH``. The
# single-batch contract above never reaches that seam, so a regression
# in the multi-batch stamping or the aggregation filter would slip past
# it. This test runs the REAL CLI through subprocess with a 30-turn
# transcript (> _CHUNKS_PER_BATCH = 25) so the multi-batch aggregator
# fires; it then asserts the same end-to-end extraction_config contract
# the single-batch test asserts.
def _multi_batch_transcript(n_turns: int = 30) -> str:
    lines = []
    for i in range(n_turns):
        lines.append(
            f"SPEAKER {chr(65 + i // 26)}{chr(65 + i % 26)}: "
            f"NTIA approved the 7 GHz downlink threshold of minus 47 "
            f"dBm per megahertz for band {i}."
        )
    return "\n".join(lines)


def _multi_batch_stub_response(text_for_band_0: str) -> str:
    # A single deterministic per-call response that is well-formed for
    # EVERY batch (the stub is shown the same response on every call —
    # the file-backed transport is one fixture per process).
    return json.dumps(
        {
            "decisions": [
                {"text": text_for_band_0, "verb": "approved"}
            ],
            "action_items": [],
            "open_questions": [],
            "technical_parameters": [
                {
                    "param_id": "p-band-0",
                    "parameter_name": "interference threshold",
                    "value": text_for_band_0,
                }
            ],
            "grounding": [
                {
                    "kind": "decision",
                    "text": text_for_band_0,
                    "source_turns": ["t0000"],
                },
            ],
        }
    )


def test_multi_batch_sonnet_unconstrained_stamps_prompt_variant(
    tmp_path: Path,
) -> None:
    """End-to-end multi-batch sonnet-unconstrained run stamps the right
    prompt_variant in the on-disk artifact.

    The chunk-132 fix touches the multi-batch aggregation seam. This
    contract test pins the property that the cli's extraction_config
    stamping continues to fire on a multi-batch run, so a regression
    in the aggregation filter (e.g. accidentally short-circuiting the
    success path) cannot silently strip ``prompt_variant`` from the
    artifact and roll back Phase 5's apples-to-apples comparison.

    Pre-existing test (single-batch) covers the same property for
    smaller inputs; this test covers the >138-chunk production shape
    that the chunk-132 bisect identified.
    """
    lake = tmp_path / "dl"
    staged = lake / "store" / "raw" / "meetings" / SOURCE_ID
    staged.mkdir(parents=True)
    text_for_band_0 = (
        "NTIA approved the 7 GHz downlink threshold of minus 47 dBm "
        "per megahertz for band 0."
    )
    staged.joinpath("source.txt").write_text(
        _multi_batch_transcript(30), encoding="utf-8"
    )
    stub = tmp_path / "stub_response.json"
    stub.write_text(_multi_batch_stub_response(text_for_band_0), encoding="utf-8")

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
            "sonnet-unconstrained",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"CLI exit={proc.returncode}\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )

    artifact = _read_produced_artifact(lake)
    ec = artifact["payload"]["provenance"]["extraction_config"]
    assert ec.get("prompt_variant") == "opus_prompt_with_sonnet_model", ec
    # chunk_count > 25 proves the multi-batch path actually fired.
    assert ec.get("chunk_count", 0) > 25, ec
    assert "claude-sonnet" in ec["seed_inputs"]["model_id"]

    jsonschema.Draft202012Validator(_schema()).validate(_flat_for_schema(artifact))
