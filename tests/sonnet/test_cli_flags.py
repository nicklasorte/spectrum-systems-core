"""Phase 5 — CLI flag wiring tests.

Covers ``--model`` / ``--repeat`` / ``--confirm-cost`` / ``--dry-run``:

* default `--model haiku` resolves to production behaviour
* `--model sonnet-unconstrained` halts when Opus prompt is missing
* `--repeat > 1` requires `--confirm-cost`
* env-var bypass has no effect for the three CLI-only flags
* the `--dry-run` mode prints the resolution and writes nothing
"""
from __future__ import annotations

import io
import os
import sys

import pytest

from spectrum_systems_core.cli import meeting_minutes_llm


@pytest.fixture
def fake_lake(tmp_path):
    """Build a minimal data lake with a staged transcript."""
    lake = tmp_path / "lake"
    store_root = lake / "store"
    sid = "test-source"
    staged = store_root / "raw" / "meetings" / sid / "source.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("hello world transcript\n", encoding="utf-8")
    return lake, sid


def test_dry_run_with_haiku(fake_lake) -> None:
    """`--model haiku --dry-run` reports the REGISTRY-resolved model_id.

    Phase 5 honesty rule (review-comment P2): the default haiku path
    does NOT pass a model_id_override, so the real run uses the
    model_id from ``ai/registry/model_registry.json``. The dry-run
    banner must report what would actually run, not the Phase-5
    spec's nomenclature.
    """
    from spectrum_systems_core.workflows.meeting_minutes_llm import (
        _resolve_extraction_model,
    )

    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="haiku",
        repeat=1,
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 0
    s = out.getvalue()
    assert "DRY-RUN" in s
    assert "model=haiku" in s
    assert "prompt_variant=production_haiku" in s
    # Honesty: the registry's real model_id appears in the banner
    # (not the Phase-5 spec string `claude-haiku-4-7` unless the
    # registry happens to agree).
    real_model_id, _ = _resolve_extraction_model()
    assert f"model_id={real_model_id}" in s


def test_dry_run_with_sonnet(fake_lake) -> None:
    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="sonnet",
        repeat=1,
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 0
    s = out.getvalue()
    assert "model=sonnet" in s
    assert "prompt_variant=haiku_prompt_with_sonnet_model" in s
    assert "claude-sonnet-4-6" in s


def test_sonnet_unconstrained_fails_without_opus_prompt(
    fake_lake, monkeypatch
) -> None:
    """When the Opus prompt is missing, the CLI halts with the exact reason_code."""
    from spectrum_systems_core.workflows import model_selection as ms

    # Force the Opus path to a non-existent file.
    monkeypatch.setattr(
        ms,
        "OPUS_PROMPT_PATH",
        ms.OPUS_PROMPT_PATH.parent / "definitely_not_there.md",
    )
    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="sonnet-unconstrained",
        repeat=1,
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 2
    assert "opus_prompt_not_found_for_sonnet_unconstrained" in out.getvalue()


def test_repeat_gt_1_requires_confirm_cost(fake_lake) -> None:
    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="haiku",
        repeat=3,
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 2
    assert "cost_confirmation_required" in out.getvalue()


def test_repeat_gt_1_with_confirm_cost_proceeds(fake_lake) -> None:
    """`--repeat 3 --confirm-cost --dry-run` passes the gate; dry-run prints once."""
    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="haiku",
        repeat=3,
        confirm_cost=True,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 0
    s = out.getvalue()
    # dry-run exits before the loop, but the resolved repeat value is
    # printed on the banner so the operator sees it.
    assert "repeat=3" in s


def test_dry_run_emits_cost_estimate(fake_lake) -> None:
    """The dry-run banner MUST include a cost estimate.

    Review-comment P2 (Codex): the `--dry-run` help promises a cost
    estimate but the previous output omitted it, so operators could
    not verify expected spend before approving `--repeat N --confirm-cost`.
    """
    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="haiku",
        repeat=1,
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 0
    s = out.getvalue()
    assert "estimated_cost_per_run=$" in s
    assert "estimated_total_cost=$" in s
    assert "cost_keyed_on=claude-haiku-4-7" in s


def test_dry_run_cost_scales_with_repeat(fake_lake) -> None:
    """`--repeat N` multiplies the per-run cost in the dry-run banner."""
    from decimal import Decimal

    lake, sid = fake_lake
    out1 = io.StringIO()
    meeting_minutes_llm(
        source_id=sid, data_lake=str(lake), model_token="sonnet",
        repeat=1, confirm_cost=False, dry_run=True, out_stream=out1,
    )
    out3 = io.StringIO()
    meeting_minutes_llm(
        source_id=sid, data_lake=str(lake), model_token="sonnet",
        repeat=3, confirm_cost=True, dry_run=True, out_stream=out3,
    )

    def _parse_total(s: str) -> Decimal:
        # The banner emits `estimated_total_cost=$<DEC>` — extract it.
        for token in s.split():
            if token.startswith("estimated_total_cost=$"):
                return Decimal(token.split("$", 1)[1])
        raise AssertionError(f"no estimated_total_cost in: {s!r}")

    total1 = _parse_total(out1.getvalue())
    total3 = _parse_total(out3.getvalue())
    assert total3 == total1 * Decimal(3), (
        f"repeat=3 total {total3} != 3 * single-run {total1}"
    )


def test_invalid_repeat_value_rejected(fake_lake) -> None:
    lake, sid = fake_lake
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="haiku",
        repeat=0,
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 2
    assert "repeat_invalid" in out.getvalue()


def test_env_var_bypass_has_no_effect(fake_lake, monkeypatch) -> None:
    """Setting MODEL/REPEAT/CONFIRM_COST env vars must not affect the CLI's behaviour."""
    # The keyword args are the SOURCE OF TRUTH for the resolved values.
    # Verify that env vars with these names do NOT leak in.
    monkeypatch.setenv("MODEL", "sonnet")
    monkeypatch.setenv("REPEAT", "5")
    monkeypatch.setenv("CONFIRM_COST", "true")
    monkeypatch.setenv("DRY_RUN", "true")

    lake, sid = fake_lake
    out = io.StringIO()
    # The defaults are still in effect because the function takes
    # keyword args; the env vars must not override them.
    rc = meeting_minutes_llm(
        source_id=sid,
        data_lake=str(lake),
        model_token="haiku",  # NOT "sonnet"
        repeat=1,  # NOT 5
        confirm_cost=False,
        dry_run=True,
        out_stream=out,
    )
    assert rc == 0
    s = out.getvalue()
    assert "model=haiku" in s
    assert "model=sonnet" not in s
    assert "claude-sonnet" not in s


def test_argparse_dispatch_with_no_flags_resolves_to_haiku(monkeypatch) -> None:
    """`spectrum-core meeting-minutes-llm --source-id X` (no model flag) → haiku."""
    # Walk argv parsing to assert the resolver receives the haiku
    # default. We don't actually run the workflow; we monkey-patch
    # ``meeting_minutes_llm`` to capture the resolved kwargs.
    captured: dict = {}

    import spectrum_systems_core.cli as cli_mod

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "meeting_minutes_llm", _capture)
    monkeypatch.setattr(
        sys, "argv", ["spectrum-core", "meeting-minutes-llm", "--source-id", "x"]
    )
    rc = cli_mod.main()
    assert rc == 0
    assert captured.get("model_token") == "haiku"
    assert captured.get("repeat") == 1
    assert captured.get("confirm_cost") is False
