"""Integration contract test for ``scripts/autonomous_llm_fix.py``.

This script does NOT read a pipeline artifact — it reads source files
and calls Opus — so the ``fixtures.py`` factory clause of the CLAUDE.md
integration rule does not apply (there is no writer/reader artifact
drift to catch). What MUST be defended here are the script's trust
properties, because this tool's whole danger is that its success gate
("make the CLI exit 0 with promoted=True") is exactly the prompt that
produces a governance-weakening "fix":

* the PR_FAILURE_PROTOCOL **Class VI static guard is structural** — a
  weakening fix (verdict flip, gate flip, assertion/raise removed on an
  eval path, test disabled, source_id allowlist) makes the real
  subprocess exit 3 and write nothing, even in --dry-run;
* a legitimate producer-side fix passes the guard;
* parsing is fail-closed — a present-but-unparseable FIX or an empty
  OLD raises rather than applying a partial/blind change;
* --dry-run is side-effect-free — DIAGNOSIS is printed, exit 0, and no
  tracked file is modified;
* the Opus context actually contains every pinned file plus the
  binding governance docs.

The script is driven as a real subprocess against a temp ``--data-lake``
with the offline stub transport (no API key, no network).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS / "autonomous_llm_fix.py"
STUB_ENV = "AUTONOMOUS_LLM_FIX_STUB_RESPONSE"
SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import autonomous_llm_fix as alf  # noqa: E402

_BENIGN_RESPONSE = (
    "DIAGNOSIS: The simulation injects a stub transport and a "
    "hand-built grounded payload, so strict-schema/grounding pass; the "
    "real CLI sends the raw prompt and the producer never appends the "
    "turn block, so grounding turn_ids are absent and the within-source "
    "eval blocks. The divergence is the producer not the eval.\n\n"
    "FIX:\n"
    "FILE: src/spectrum_systems_core/workflows/meeting_minutes_llm.py\n"
    "OLD: PRODUCED_BY = \"meeting_minutes_llm\"\n"
    "NEW: PRODUCED_BY = \"meeting_minutes_llm\"  # producer id\n"
)


def _run(stub: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
        STUB_ENV: stub,
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
    )


def _is_dirty() -> bool:
    cp = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    # Only the (untracked) script + this test are expected to differ;
    # any tracked-file modification is the failure we are guarding.
    modified = [
        ln
        for ln in cp.stdout.splitlines()
        if ln and not ln.startswith("??") and not ln[3:].strip().startswith(
            ("scripts/autonomous_llm_fix.py", "tests/integration/test_autonomous")
        )
    ]
    return bool(modified)


# --------------------------------------------------------------------------
# Class VI static guard — the binding trust property.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "old,new,path,sig",
    [
        ('status="fail"', 'status="pass"',
         "src/spectrum_systems_core/evals/regulatory_verb.py", "verdict_flip"),
        ("sys.exit(1)", "sys.exit(0)",
         "scripts/_env_validate.py", "gate_flip"),
        ("    assert payload\n    raise ValueError(x)",
         "    return True",
         "src/spectrum_systems_core/evals/llm_extraction.py",
         "assertion_removed"),
        ("def test_blocks_on_bad_verb():\n    assert x",
         "def _disabled():\n    assert x",
         "tests/evals/test_regulatory_verb_eval.py", "test_deleted"),
        ("verb = declared",
         "if source_id == 'special': verb = ok\n    verb = declared",
         "src/spectrum_systems_core/evals/regulatory_verb.py",
         "source_id_allowlist"),
    ],
)
def test_classify_weakening_catches_class_vi(old, new, path, sig) -> None:
    finding = alf.classify_weakening(alf.FixChange(file=path, old=old, new=new))
    assert finding is not None, f"Class VI {sig} not detected"
    assert finding.signature == sig


def test_legitimate_producer_fix_passes_guard() -> None:
    change = alf.FixChange(
        file="src/spectrum_systems_core/workflows/meeting_minutes_llm.py",
        old='user = prompt',
        new='user = prompt + turn_block',
    )
    assert alf.classify_weakening(change) is None


def test_weakening_fix_blocks_subprocess_and_writes_nothing() -> None:
    weak = (
        "DIAGNOSIS: regulatory_verb blocks; flip its verdict.\n\n"
        "FIX:\n"
        "FILE: src/spectrum_systems_core/evals/regulatory_verb.py\n"
        'OLD: status="fail"\n'
        'NEW: status="pass"\n'
    )
    cp = _run(weak, "--data-lake", "/tmp/nope", "--source-id", SOURCE_ID,
              "--dry-run")
    assert cp.returncode == 3, cp.stdout + cp.stderr
    assert "Class VI" in (cp.stdout + cp.stderr)
    assert not _is_dirty(), "a blocked weakening fix mutated a tracked file"


# --------------------------------------------------------------------------
# Fail-closed parsing.
# --------------------------------------------------------------------------
def test_parse_diagnosis_and_fixes_roundtrip() -> None:
    diag = alf.parse_diagnosis(_BENIGN_RESPONSE)
    assert "divergence is the producer" in diag
    fixes = alf.parse_fixes(_BENIGN_RESPONSE)
    assert len(fixes) == 1
    assert fixes[0].file.endswith("meeting_minutes_llm.py")


def test_present_but_unparseable_fix_is_fatal() -> None:
    with pytest.raises(alf.FixToolError):
        alf.parse_fixes("DIAGNOSIS: x\n\nFIX:\nnonsense with no triple\n")


def test_empty_old_is_refused() -> None:
    with pytest.raises(alf.FixToolError):
        alf.parse_fixes(
            "FIX:\nFILE: a.py\nOLD: \nNEW: something\n"
        )


def test_missing_diagnosis_is_fatal() -> None:
    with pytest.raises(alf.FixToolError):
        alf.parse_diagnosis("no diagnosis section here")


# --------------------------------------------------------------------------
# --dry-run is side-effect-free; context is complete.
# --------------------------------------------------------------------------
def test_dry_run_prints_diagnosis_and_changes_nothing() -> None:
    cp = _run(_BENIGN_RESPONSE, "--data-lake", "/tmp/nope",
              "--source-id", SOURCE_ID, "--dry-run")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "DIAGNOSIS" in cp.stdout
    assert "divergence is the producer" in cp.stdout
    assert "[dry-run]" in cp.stdout
    assert not _is_dirty(), "--dry-run mutated a tracked file"


def test_user_message_contains_every_pinned_file_and_governance() -> None:
    msg = alf.build_user_message(source_id=SOURCE_ID, data_lake="/tmp/x")
    for rel in alf._CONTEXT_FILES:
        assert f"BEGIN {rel}" in msg, f"missing pinned file {rel}"
    for rel in alf._GOVERNANCE_FILES:
        assert f"BEGIN {rel}" in msg, f"missing governance doc {rel}"
    assert "failed:regulatory_verb" in msg
    assert SOURCE_ID in msg


def test_system_prompt_carries_class_vi_constraint() -> None:
    sp = alf.system_prompt()
    assert "DIAGNOSIS:" in sp and "FILE:" in sp
    assert "Class VI" in sp
    assert "FORBIDDEN" in sp
