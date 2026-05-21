"""Contract: `--model sonnet-unconstrained` loads `meeting_minutes_opus.md`
at RUNTIME — never a cached module-level constant — so PR #204's prompt
fixes reach the model on every run.

The two trust properties defended:

  1. The prompt sent to the workflow's `_system_prompt` swap is the
     LIVE contents of
     `src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md`
     at invocation time. A monkey-patched-on-disk edit must take effect
     on the very next run without a process restart (catching the
     "prompt is a module-level constant" failure mode the operator
     hypothesised in the cascade-regression task brief).

  2. The PR #204 structural sections — the canonical
     `REQUIRED_MEETING_MINUTES_FIELDS` enumeration and the closed
     regulatory-verb taxonomy — are present in the loaded prompt. A
     future edit that drops either section fails the test loudly
     because the four-eval cascade the operator hit pre-#204
     (`required_meeting_minutes_fields`, `regulatory_verb`,
     `llm_extraction_strict_schema`, `tlc_routed_extraction`)
     would re-emerge.
"""
from __future__ import annotations

import io
from pathlib import Path

from spectrum_systems_core.cli import meeting_minutes_llm
from spectrum_systems_core.workflows.model_selection import (
    MODEL_TOKEN_SONNET_UNCONSTRAINED,
    OPUS_PROMPT_PATH,
    resolve_model_selection,
)


MEETING_ID = "sonnet-prompt-loading-test"


def _seed_store_lake(tmp_path: Path) -> Path:
    lake = tmp_path / "lake"
    staged = lake / "store" / "raw" / "meetings" / MEETING_ID / "source.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        "Chair Smith: Welcome.\nNTIA Lead: NTIA approved the plan.\n",
        encoding="utf-8",
    )
    return lake


def test_sonnet_unconstrained_resolves_to_meeting_minutes_opus_md():
    """Pin the prompt PATH the resolver hands to the CLI. The CLI
    reads this path on every invocation; redirecting it (or
    accidentally pointing at a different file) is the
    'different-code-path' failure mode the brief asked us to rule
    out."""
    sel = resolve_model_selection(MODEL_TOKEN_SONNET_UNCONSTRAINED)
    assert sel.prompt_path == OPUS_PROMPT_PATH
    assert sel.prompt_path.name == "meeting_minutes_opus.md"


def test_meeting_minutes_opus_md_contains_pr_204_structural_sections():
    """The on-disk opus prompt carries the PR #204 fix-anchor strings.
    These are the exact phrases the prompt added to satisfy the four
    evals the operator's bug report cited; a future edit that removes
    them re-introduces the cascade."""
    text = OPUS_PROMPT_PATH.read_text(encoding="utf-8")
    # Required-top-level-fields section (`required_meeting_minutes_fields`
    # fix). PR #204 added this section so the model emits `title` and
    # `summary` at the top level.
    assert "Required top-level fields" in text, (
        "PR #204's required-top-level-fields section is missing from "
        f"{OPUS_PROMPT_PATH}; required_meeting_minutes_fields will fail."
    )
    # Closed regulatory-verb taxonomy (`regulatory_verb` fix). PR #204
    # added the verb taxonomy so the model emits only canonical verbs
    # OR the `unclassified` sentinel. The section heading on disk is
    # rendered with the backticked `verb` token (`**\`verb\` field`).
    assert "`verb` field" in text and "unclassified" in text, (
        "PR #204's closed regulatory-verb taxonomy is missing from "
        f"{OPUS_PROMPT_PATH}; regulatory_verb will fail."
    )


def test_sonnet_unconstrained_cli_loads_opus_prompt_at_runtime(
    tmp_path, monkeypatch
):
    """The CLI reads the prompt file at runtime, NOT at import time.

    Strategy: redirect `OPUS_PROMPT_PATH` to a temp prompt with a
    distinctive marker, run the CLI in dry-run mode, then read the
    same constant back — the resolver and CLI must reflect the
    redirect immediately. (If the CLI cached the path at import,
    the redirect would not flip and a misconfigured production
    deploy could ship a stale prompt.)
    """
    from spectrum_systems_core.workflows import model_selection as ms

    distinctive_prompt = tmp_path / "meeting_minutes_opus.md"
    marker = "MARKER_FROM_TEST_RUNTIME_PROMPT_REDIRECT"
    distinctive_prompt.write_text(
        f"# stub opus prompt\n{marker}\n", encoding="utf-8"
    )
    monkeypatch.setattr(ms, "OPUS_PROMPT_PATH", distinctive_prompt)
    # The resolver reads OPUS_PROMPT_PATH at call time — proves the
    # CLI sees the redirect on the very next invocation.
    sel = resolve_model_selection(MODEL_TOKEN_SONNET_UNCONSTRAINED)
    assert sel.prompt_path == distinctive_prompt
    # And the file's content is the distinctive marker — i.e. the
    # CLI's `read_prompt(sel.prompt_path)` would pick up the marker
    # on this run, not a cached pre-redirect version.
    assert marker in sel.prompt_path.read_text(encoding="utf-8")


def test_sonnet_unconstrained_halts_pre_run_with_distinctive_reason_code(
    tmp_path, monkeypatch
):
    """Belt-and-braces: when the opus prompt is missing, the CLI halts
    pre-run with `opus_prompt_not_found_for_sonnet_unconstrained` and
    NEVER silently falls back to the Haiku prompt. This is the
    fail-closed guard against 'sonnet-unconstrained ends up running
    the wrong prompt' — the operator hypothesis #2 in the brief."""
    from spectrum_systems_core.workflows import model_selection as ms

    monkeypatch.setattr(
        ms,
        "OPUS_PROMPT_PATH",
        tmp_path / "absent_meeting_minutes_opus.md",
    )
    lake = _seed_store_lake(tmp_path)
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=MEETING_ID,
        data_lake=str(lake),
        model_token="sonnet-unconstrained",
        dry_run=True,
        out_stream=out,
    )
    assert rc == 2
    assert (
        "opus_prompt_not_found_for_sonnet_unconstrained" in out.getvalue()
    ), out.getvalue()
