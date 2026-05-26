"""Unit tests for the Codex reference baseline plumbing.

Focused on invariants that don't need a subprocess: the model registry
entry, the path mirror against the Opus baseline, the JSONL serialisation
shape, and the constants the data-lake contract depends on.

Subprocess-driven contract tests live in
``tests/integration/test_ingest_codex_baseline_contract.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import create_opus_reference_baselines as crb  # noqa: E402
import ingest_codex_baseline as icb  # noqa: E402


def test_model_registry_has_codex_entry() -> None:
    """The registry MUST carry codex_reference_baseline; the ingest
    script reads it as the default model string."""
    registry = json.loads(
        (REPO_ROOT / "ai" / "registry" / "model_registry.json")
        .read_text(encoding="utf-8")
    )
    assert "codex_reference_baseline" in registry, (
        "ai/registry/model_registry.json must declare "
        "codex_reference_baseline"
    )
    entry = registry["codex_reference_baseline"]
    assert entry["model_id"] == "gpt-5.5"
    assert entry["provider"] == "openai"
    assert isinstance(entry["max_tokens"], int) and entry["max_tokens"] > 0


def test_codex_baseline_path_matches_opus_pattern() -> None:
    """codex_reference_minutes.jsonl lives in the SAME directory the
    Opus baseline lives in — no new directory structure."""
    # Source of truth on the Opus side.
    assert crb._OUTPUT_SUBDIR == "reference_baselines"
    assert crb._OUTPUT_FILENAME == "opus_reference_minutes.jsonl"
    # Mirror on the Codex side.
    assert icb._OUTPUT_SUBDIR == "reference_baselines"
    assert icb._OUTPUT_FILENAME == "codex_reference_minutes.jsonl"
    # Same parent directory under any data-lake root.
    fake = Path("/fake/data-lake")
    sid = "any-source-id"
    opus_dir = crb._jsonl_path(fake, sid).parent
    codex_dir = icb._output_path(fake, sid).parent
    assert opus_dir == codex_dir


def test_codex_namespace_is_distinct_from_opus_namespace() -> None:
    """The two UUID5 namespaces must NEVER collide — a Codex row and
    an Opus row for the same (source, etype, index) slot must get
    distinct pair_ids so the comparison engine can't confuse them."""
    assert icb._CODEX_REF_NAMESPACE != crb._OPUS_REF_NAMESPACE


def test_produced_by_constants_are_symmetric() -> None:
    """The two baselines stamp parallel ``provenance.produced_by``
    values so the data lake is self-describing."""
    assert crb.PRODUCED_BY == "opus_reference_baseline_workflow"
    assert icb.PRODUCED_BY == "codex_reference_baseline_workflow"
    assert icb.CODEX_LOCAL_PRODUCED_BY == "codex_local"


def test_extraction_types_match_opus_producer() -> None:
    """The ingest script reuses ``extraction_types`` from the Opus
    producer — they're the SAME types derived from the SAME schema,
    so a new type added in the schema flows through both writers."""
    assert icb.extraction_types is crb.extraction_types
    assert icb.extract_ground_truth_text is crb.extract_ground_truth_text


def test_build_codex_records_emits_jsonl_shape() -> None:
    """The row shape must mirror the Opus row shape so the comparison
    engine (when wired up) reads both baselines identically."""
    payload = {
        "decisions": [
            "the TIG approved the threshold",
            {"text": "scope deferred"},
        ],
        "action_items": [{"action": "NTIA to circulate"}],
        "open_questions": [],
    }
    rows = icb.build_codex_records(
        payload=payload,
        types=icb.extraction_types(),
        source_id="sid",
        source_artifact_id="00000000-0000-0000-0000-000000000000",
        model="gpt-5.5",
        meeting_date="2025-12-18",
        created_at="1970-01-01T00:00:00+00:00",
        operator="test-op",
    )
    assert len(rows) == 3
    expected_keys = {
        "pair_id", "source_id", "source_artifact_id", "extraction_type",
        "ground_truth_text", "item_data", "human_authored",
        "model_authored", "model_id", "verified", "status", "provenance",
        "schema_version", "meeting_date", "created_at",
        "chunking_strategy_version",
    }
    for row in rows:
        assert set(row.keys()) == expected_keys
        assert row["status"] == "reference_only"
        assert row["human_authored"] is False
        assert row["model_authored"] is True
        assert row["provenance"]["produced_by"] == (
            "codex_reference_baseline_workflow"
        )
        assert row["provenance"]["operator"] == "test-op"
        assert "artifact_kind" not in row


def test_build_codex_records_rejects_non_list_etype() -> None:
    """A content key whose value is not a list is a schema_violation
    halt — never silently coerced."""
    payload = {"decisions": "not a list"}
    with pytest.raises(icb.CodexIngestError) as excinfo:
        icb.build_codex_records(
            payload=payload,
            types=icb.extraction_types(),
            source_id="sid",
            source_artifact_id="00000000-0000-0000-0000-000000000000",
            model="gpt-5.5",
            meeting_date=None,
            created_at="1970-01-01T00:00:00+00:00",
            operator="test-op",
        )
    assert excinfo.value.reason == "schema_violation"


def test_pair_id_stable_under_reorder_of_other_fields() -> None:
    """``pair_id`` is UUID5 over ``codex-ref-<source>-<etype>-<index>``
    — neither model, meeting_date, nor operator may shift it. Operator
    audit metadata is NOT a determinism key."""
    payload = {"decisions": ["a", "b"]}
    base = dict(
        payload=payload,
        types=icb.extraction_types(),
        source_id="sid",
        source_artifact_id="00000000-0000-0000-0000-000000000000",
        meeting_date=None,
        created_at="1970-01-01T00:00:00+00:00",
    )
    rows_a = icb.build_codex_records(
        **base, model="gpt-5.5", operator="op-A",
    )
    rows_b = icb.build_codex_records(
        **base, model="gpt-5.5", operator="op-B",
    )
    rows_c = icb.build_codex_records(
        **base, model="something-else", operator="op-A",
    )
    assert [r["pair_id"] for r in rows_a] == [r["pair_id"] for r in rows_b]
    assert [r["pair_id"] for r in rows_a] == [r["pair_id"] for r in rows_c]


def test_no_artifact_kind_in_new_source() -> None:
    """CLAUDE.md pre-PR check (Spectrum Systems §3 in CLAUDE.md):
    ``artifact_kind`` must never appear in new production code. The
    ingest script's source file is the deliverable here; the contract
    test deliberately references the typo as a negative-case payload
    + assertion, which is fine — the rule is about producers, not
    about test guards."""
    src = (
        REPO_ROOT / "scripts" / "ingest_codex_baseline.py"
    ).read_text(encoding="utf-8")
    assert "artifact_kind" not in src
