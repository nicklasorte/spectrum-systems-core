"""Phase 3P speaker-name scanner tests.

The scanner is heuristic but must catch a deliberately-inserted real
name. Tests use a tmp registry copy so the live file stays clean.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_fewshot_speaker_names.py"
LIVE = REPO_ROOT / "data" / "few_shot" / "examples_v1.jsonl"


def _run(examples_path: Path, strict: bool = False) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT), "--examples", str(examples_path)]
    if strict:
        args.append("--strict")
    return subprocess.run(args, capture_output=True, text=True, check=False)


def test_live_registry_passes_scanner() -> None:
    result = _run(LIVE, strict=True)
    assert result.returncode == 0, (
        f"live registry triggered speaker-name scanner: {result.stderr}"
    )


def test_inserted_real_name_is_flagged(tmp_path: Path) -> None:
    """Insert 'John Smith' into chunk_text and confirm the scanner
    catches it in --strict mode."""
    target = tmp_path / "examples_v1.jsonl"
    rows = [
        json.loads(line)
        for line in LIVE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[0]["chunk_text"] = (
        rows[0]["chunk_text"]
        + " John Smith later added that he disagreed."
    )
    target.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    result = _run(target, strict=True)
    assert result.returncode != 0
    assert "John Smith" in result.stderr


def test_inserted_real_name_in_gold_extraction_is_flagged(tmp_path: Path) -> None:
    target = tmp_path / "examples_v1.jsonl"
    rows = [
        json.loads(line)
        for line in LIVE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Add a real name into the gold_extraction's attendees array.
    rows[0]["gold_extraction"]["attendees"] = [
        {"name": "John Smith", "role": "chair"}
    ]
    target.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    result = _run(target, strict=True)
    assert result.returncode != 0
    assert "John Smith" in result.stderr
