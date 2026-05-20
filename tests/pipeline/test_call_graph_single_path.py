"""Call-graph CI gate: every extraction-producing function calls governed_pipeline_run.

Phase 2 — Step 2.1.

The correction miner evaluated candidate ``2a583c76`` at F1 = 41.4%
but the live extraction with the same prompt produced 38.9% and
37.3% on two runs. The 4.1-point gap was larger than the 5-point
promotion threshold and meant the miner could promote candidates
that did not actually clear the threshold in production. The fix:
make the miner and production use ONE execution path, which is
:func:`spectrum_systems_core.pipeline.governed_pipeline_run`.

This test is the structural enforcement. It walks the AST of every
module under ``src/spectrum_systems_core/`` and asserts that every
function that takes BOTH a prompt-shaped argument AND a transcript-
shaped argument (the structural signature of the alternate-path
class) either IS ``governed_pipeline_run`` or calls into it.

A grep is not sufficient — the test must actually walk the AST so a
function that produces an extraction without going through the
single path fails the CI gate.

The hard-line signature for "extraction-producing": a function
parameter whose name matches one of ``prompt|prompt_text|
prompt_content`` AND a parameter whose name matches one of
``transcript|transcript_text|input_text``. That captures the
genuine alternate-path shape (the miner USED to take prompt +
transcript in evaluate_candidate before Phase 2; production has
always taken them; any new caller of the LLM extraction primitives
will too).

Synthetic alternate paths MUST fail this test (see
``test_synthetic_alternate_path_fails``).
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
from typing import Iterable, List, Set

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "spectrum_systems_core"

GOVERNED_RUN_NAMES: frozenset[str] = frozenset(
    {
        "governed_pipeline_run",
    }
)

# Transitive routes that ARE governed_pipeline_run one hop away. These
# functions are called BY governed_pipeline_run; a callable that calls
# THEM reaches the single path one hop later.
TRANSITIVE_NAMES: frozenset[str] = frozenset(
    {
        "run_meeting_minutes_llm_workflow",
    }
)

PROMPT_PARAM_NAMES: frozenset[str] = frozenset(
    {"prompt", "prompt_text", "prompt_content", "full_prompt"}
)
TRANSCRIPT_PARAM_NAMES: frozenset[str] = frozenset(
    {"transcript", "transcript_text", "input_text"}
)

# Functions explicitly exempted (each comes with a structural reason).
# Keep this set TIGHT. Every entry must come with a justification — see
# the comments below.
EXEMPT_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        # governed_pipeline_run is the canonical single path.
        "governed_pipeline_run",
        # The workflow that governed_pipeline_run wraps. By definition
        # the inner workflow itself doesn't call its own wrapper.
        "run_meeting_minutes_llm_workflow",
        # Build helpers that take a transcript text to render chunks —
        # they do not produce an extraction artifact.
        "_render_turn_block",
        "_build_user_message",
        "_slice_transcript_for_batch",
        "_derive_title",
        # Debug helpers; do not produce extraction artifacts.
        "build_chunk_debug_report",
        "_build_chunk_debug_report",
        "_emit_single_chunk_debug",
        # The extraction_config builder lives inside the pipeline
        # module; it consumes prompt+transcript only to hash them.
        # It does not produce an extraction artifact.
        "build_extraction_config_from_run",
    }
)


def _python_files() -> Iterable[pathlib.Path]:
    for path in SRC.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        yield path


def _function_calls(node: ast.AST) -> List[str]:
    names: List[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    return names


def _arg_names(fn: ast.FunctionDef) -> Set[str]:
    args = fn.args
    names: Set[str] = set()
    for arg in (
        list(args.args)
        + list(args.posonlyargs or [])
        + list(args.kwonlyargs)
    ):
        names.add(arg.arg)
    return names


def _is_extraction_producer(fn: ast.FunctionDef) -> bool:
    names = _arg_names(fn)
    has_prompt = bool(names & PROMPT_PARAM_NAMES)
    has_transcript = bool(names & TRANSCRIPT_PARAM_NAMES)
    return has_prompt and has_transcript


def _collect_extraction_funcs(
    tree: ast.AST,
) -> List[ast.FunctionDef]:
    out: List[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in EXEMPT_FUNCTION_NAMES:
            continue
        if _is_extraction_producer(node):
            out.append(node)
    return out


def test_governed_pipeline_run_exists() -> None:
    """Foundation: the named function exists and is importable."""
    from spectrum_systems_core.pipeline import governed_pipeline_run

    assert callable(governed_pipeline_run)


def test_every_extraction_producer_routes_through_governed_run() -> None:
    """The CI gate (Phase 2 Step 2.1).

    Every function in src/spectrum_systems_core/ that takes BOTH a
    prompt-shaped arg AND a transcript-shaped arg must either BE
    governed_pipeline_run or call into it.
    """
    offenders: List[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except SyntaxError:
            continue
        for fn in _collect_extraction_funcs(tree):
            if fn.name in GOVERNED_RUN_NAMES:
                continue
            calls = _function_calls(fn)
            if any(name in GOVERNED_RUN_NAMES for name in calls):
                continue
            if any(name in TRANSITIVE_NAMES for name in calls):
                continue
            offenders.append(f"{path.relative_to(ROOT)}::{fn.name}")

    assert not offenders, (
        "Phase 2 Step 2.1 violation: the following functions take a "
        "prompt + transcript pair but do NOT route through "
        "governed_pipeline_run:\n  - "
        + "\n  - ".join(offenders)
        + "\nAdd a call to governed_pipeline_run (or to "
        "run_meeting_minutes_llm_workflow which it wraps), or "
        "add the function name to EXEMPT_FUNCTION_NAMES with a "
        "concrete structural justification."
    )


def test_synthetic_alternate_path_fails(tmp_path) -> None:
    """The walker's self-test. A synthetic alternate path MUST fail.

    A new engineer adding a function ``my_shadow_extract(prompt,
    transcript)`` that DOES NOT call governed_pipeline_run must
    trigger this check. This proves the gate has teeth.
    """
    src = (
        "def my_shadow_extract(prompt, transcript):\n"
        "    return {'payload': {}}\n"
    )
    fake = tmp_path / "shadow.py"
    fake.write_text(src, encoding="utf-8")
    tree = ast.parse(fake.read_text(encoding="utf-8"))
    fns = _collect_extraction_funcs(tree)
    assert len(fns) == 1
    fn = fns[0]
    assert fn.name == "my_shadow_extract"
    calls = _function_calls(fn)
    assert not any(name in GOVERNED_RUN_NAMES for name in calls)
    assert not any(name in TRANSITIVE_NAMES for name in calls)


def test_synthetic_compliant_path_passes(tmp_path) -> None:
    """The walker's positive self-test: a compliant path is NOT flagged."""
    src = (
        "def my_good_extract(prompt, transcript):\n"
        "    return governed_pipeline_run(prompt_content=prompt, "
        "transcript=transcript)\n"
    )
    fake = tmp_path / "good.py"
    fake.write_text(src, encoding="utf-8")
    tree = ast.parse(fake.read_text(encoding="utf-8"))
    fns = _collect_extraction_funcs(tree)
    assert len(fns) == 1
    calls = _function_calls(fns[0])
    assert "governed_pipeline_run" in calls
