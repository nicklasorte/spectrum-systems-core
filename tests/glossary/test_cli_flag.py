"""Phase 2P CLI flag tests.

The ``--enable-glossary-injection`` flag is CLI-only. These tests
prove:

- Without the flag, no Terminology block appears in the output.
- Without the flag, the env var ``ENABLE_GLOSSARY_INJECTION=true``
  is IGNORED (the flag does not consult env vars).
- With the flag, the chunk context contains a Terminology block when
  the chunk has matching terms.
- With the flag but a missing chunk text or no matches, no
  Terminology block appears.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cli_glossary.py"


def _run(args: list[str], env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


def test_disabled_by_default(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk.txt"
    chunk.write_text("Talk about CBRS spectrum policy.", encoding="utf-8")
    result = _run(["--chunk-file", str(chunk)])
    assert result.returncode == 0, result.stderr
    assert "Terminology relevant to this section:" not in result.stdout
    # The original chunk content is passed through unchanged.
    assert "CBRS spectrum policy" in result.stdout


def test_env_var_bypass_is_ignored(tmp_path: Path) -> None:
    """The CLI flag is CLI-only. Setting an env var must NOT enable injection."""
    chunk = tmp_path / "chunk.txt"
    chunk.write_text("Talk about CBRS spectrum policy.", encoding="utf-8")
    result = _run(
        ["--chunk-file", str(chunk)],
        env={"ENABLE_GLOSSARY_INJECTION": "true"},
    )
    assert result.returncode == 0, result.stderr
    assert "Terminology relevant to this section:" not in result.stdout


def test_enabled_produces_terminology_block(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk.txt"
    chunk.write_text("Talk about CBRS spectrum policy in the band.", encoding="utf-8")
    result = _run(
        ["--enable-glossary-injection", "--chunk-file", str(chunk)],
    )
    assert result.returncode == 0, result.stderr
    assert "Terminology relevant to this section:" in result.stdout
    assert "CBRS:" in result.stdout


def test_enabled_with_no_match_produces_no_block(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk.txt"
    chunk.write_text("a piece of generic prose with no spectrum jargon.", encoding="utf-8")
    result = _run(
        ["--enable-glossary-injection", "--chunk-file", str(chunk)],
    )
    assert result.returncode == 0, result.stderr
    assert "Terminology relevant to this section:" not in result.stdout


def test_enabled_truncation_announced(tmp_path: Path) -> None:
    # The repo glossary has many entries -- a chunk packed with terms
    # exceeds the default max_terms=3 and the truncation count is
    # announced in the block.
    chunk = tmp_path / "chunk.txt"
    chunk.write_text(
        "spectrum allocation in this band: FSS, MSS, SAS, PAL, GAA, "
        "CBRS, NTIA, FCC, ITU, AFC, DPA",
        encoding="utf-8",
    )
    result = _run(
        ["--enable-glossary-injection", "--chunk-file", str(chunk)],
    )
    assert result.returncode == 0, result.stderr
    assert "Terminology relevant to this section:" in result.stdout
    assert "additional terms truncated by priority" in result.stdout


def test_enabled_with_corrupt_manifest_fails_closed(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk.txt"
    chunk.write_text("CBRS", encoding="utf-8")
    bad_dir = tmp_path / "glossary"
    bad_dir.mkdir()
    # No MANIFEST, no JSONL -> fail closed.
    result = _run(
        [
            "--enable-glossary-injection",
            "--chunk-file",
            str(chunk),
            "--glossary-dir",
            str(bad_dir),
        ],
    )
    assert result.returncode != 0
    assert "glossary_manifest_unreadable" in result.stderr
