"""End-to-end pipeline smoke test (no API calls, runs in <10 seconds).

Defends the bug class that motivated Phase R: an artifact silently
gitignored, a missing ``source_record.json``, or a path-template drift
between writer and reader. Each of those bugs was invisible to the
unit suite because no test ran the actual scripts against an
on-disk repo with real gitignore rules.

Approach:

  1. Seed a temp ``data-lake/`` using
     ``tests/integration/fixtures.py`` factory functions — those
     factories call the REAL writer (``ExtractionMerger.merge``) so
     a writer-side schema drift breaks the fixture build, not the
     downstream assertion.
  2. Initialise the temp dir as a git repo and commit the seed
     artifacts so ``git ls-files`` queries are meaningful.
  3. Run the pipeline scripts via ``subprocess`` — never import the
     module under test. The subprocess exit code is the contract
     the live workflow relies on.
  4. Assert (a) the preflight passes, (b) the few-shot selector
     overwrites every placeholder, (c) every artifact path the
     manifest declares as git-tracked actually appears in
     ``git ls-files``.

Also runs the gitignore audit against the live repo so a manifest
or ``.gitignore`` regression fails the smoke test directly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest

# Test fixtures live under ``tests/`` and the package is importable
# only after ``pip install -e .``; the existing
# ``tests/integration/test_few_shot_preflight_contract.py`` already
# imports from ``tests.integration.fixtures`` the same way.
from tests.integration.fixtures import (
    make_meeting_extraction_artifact,
    make_source_record,
)


@dataclass
class SmokeWorkspace:
    """Wraps the temp data-lake path and the artifact_id used to seed it."""
    data_lake: Path
    artifact_id: str

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_FIXTURE = REPO_ROOT / "tests" / "e2e" / "golden_fixture"
SOURCE_ID = "smoke-test-transcript-golden"


def _git(args: Iterable[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command with deterministic identity vars set.

    Bare ``git commit`` in CI fails when no user.name/user.email is
    configured; passing the identity through env keeps the temp repo
    self-contained and avoids touching the host's git config.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke-test",
        "GIT_AUTHOR_EMAIL": "smoke@test.invalid",
        "GIT_COMMITTER_NAME": "smoke-test",
        "GIT_COMMITTER_EMAIL": "smoke@test.invalid",
    }
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        check=True,
    )


@pytest.fixture
def smoke_data_lake(tmp_path: Path) -> SmokeWorkspace:
    """Seed a temp data-lake with the golden fixture and commit it.

    Returns the path to the data-lake root. The temp dir is a fresh
    git repo (NOT a clone of this repo) so the smoke test does not
    depend on the host repo's history or remotes.
    """
    dl = tmp_path / "data-lake"

    # 1. Seed the raw transcript into the layout the pipeline expects.
    raw_dir = dl / "store" / "raw" / "transcripts"
    raw_dir.mkdir(parents=True)
    shutil.copy(GOLDEN_FIXTURE / "source.txt", raw_dir / f"{SOURCE_ID}.txt")

    # 2. Seed source_record.json via the real-shape factory.
    artifact_id = str(uuid.uuid4())
    proc_dir = dl / "store" / "processed" / "meetings" / SOURCE_ID
    proc_dir.mkdir(parents=True)
    (proc_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, artifact_id)),
        encoding="utf-8",
    )

    # 3. Seed the meeting_extraction artifact via the real merger.
    ext_dir = dl / "store" / "artifacts" / "extractions"
    ext_dir.mkdir(parents=True)
    (ext_dir / f"{artifact_id}_meeting_extraction.json").write_text(
        json.dumps(make_meeting_extraction_artifact(artifact_id)),
        encoding="utf-8",
    )

    # 4. Initialise git in the temp dir and commit the seed.
    #    The host may have ``commit.gpgsign=true`` configured globally
    #    (some sandboxed CI environments do); we disable signing in
    #    the temp repo's LOCAL config only — this never touches the
    #    user's global git config and only affects this throwaway
    #    repo. The smoke test is asserting workspace shape, not
    #    cryptographic provenance.
    _git(["init", "-q", "-b", "main"], cwd=dl)
    _git(["config", "--local", "commit.gpgsign", "false"], cwd=dl)
    _git(["config", "--local", "tag.gpgsign", "false"], cwd=dl)
    _git(["add", "."], cwd=dl)
    _git(["commit", "-q", "-m", "smoke: seed golden fixture"], cwd=dl)

    return SmokeWorkspace(data_lake=dl, artifact_id=artifact_id)


def _run_script(script_relpath: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script_relpath), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )


def test_full_pipeline_smoke(smoke_data_lake: SmokeWorkspace) -> None:
    """Full pipeline smoke: preflight -> few-shot selection -> tracked-files audit."""
    dl = smoke_data_lake.data_lake
    artifact_id = smoke_data_lake.artifact_id

    # 1. Preflight must pass — proves source_record.json + meeting_extraction
    #    on disk wires up correctly through the slug -> UUID resolver.
    preflight = _run_script(
        "scripts/_few_shot_preflight.py",
        "--source-id", SOURCE_ID,
        "--data-lake", str(dl),
    )
    assert preflight.returncode == 0, (
        f"Preflight failed:\n"
        f"  stdout: {preflight.stdout}\n"
        f"  stderr: {preflight.stderr}"
    )

    # 2. Few-shot selection must overwrite the Phase V placeholder
    #    template with REAL examples from the golden extraction.
    select = _run_script(
        "scripts/select_few_shot_examples.py",
        "--source-id", SOURCE_ID,
        "--data-lake", str(dl),
        "--max-examples", "3",
    )
    assert select.returncode == 0, (
        f"select_few_shot_examples failed:\n"
        f"  stdout: {select.stdout}\n"
        f"  stderr: {select.stderr}"
    )

    few_shot_path = (
        dl / "store" / "artifacts" / "evals" / "few_shot"
        / "decision_examples_v1.json"
    )
    assert few_shot_path.is_file(), "selector did not write decision_examples_v1.json"

    doc = json.loads(few_shot_path.read_text(encoding="utf-8"))
    examples = doc.get("examples") or []
    assert examples, "selector wrote zero examples"
    placeholders = [
        e for e in examples
        if str(e.get("example_id", "")).startswith("phase-v-placeholder")
    ]
    assert not placeholders, (
        f"Placeholder examples remain after selection: {placeholders}"
    )

    # 3. Every git-tracked artifact path declared in the manifest
    #    must appear in ``git ls-files`` for the temp data-lake.
    ls = subprocess.run(
        ["git", "ls-files", "store/"],
        capture_output=True, text=True, cwd=str(dl), check=True,
    )
    tracked = set(ls.stdout.strip().splitlines())

    expected_tracked = (
        f"store/artifacts/extractions/{artifact_id}_meeting_extraction.json",
        f"store/processed/meetings/{SOURCE_ID}/source_record.json",
    )
    for path in expected_tracked:
        assert path in tracked, (
            f"Expected git-tracked path missing from temp data-lake: {path}\n"
            f"Tracked under store/: {sorted(tracked)}"
        )


def test_gitignore_audit_passes_against_live_repo() -> None:
    """The audit must pass against the real repo on every PR.

    This catches the case where someone adds a new artifact path that
    is shadowed by an existing rule, OR where a `.gitignore` change
    accidentally ignores an existing tracked artifact.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "_gitignore_audit.py")],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
    )
    assert result.returncode == 0, (
        f"Gitignore audit failed against the live repo:\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )
