"""Phase 2P glossary internal-consistency script tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_glossary_consistency.py"


def _entry(term: str, aliases: list[str], definition: str) -> dict:
    return {
        "artifact_type": "glossary_entry",
        "schema_version": "1.0.0",
        "term": term,
        "aliases": aliases,
        "definition": definition,
        "authoritative_source": "47 CFR 2.1",
        "category": "test",
        "created_at": "2026-05-20T00:00:00Z",
        "is_acronym": False,
        "disambiguation_required": False,
        "co_occurring_terms": [],
        "priority_weight": 1.0,
    }


def _write(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "glossary.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(e, sort_keys=True, separators=(",", ":")) for e in entries
        ),
        encoding="utf-8",
    )
    return p


def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--glossary", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_repo_glossary_is_consistent() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_overlap_passes(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        [
            _entry("ALPHA", [], "definition a"),
            _entry("BETA", [], "definition b"),
        ],
    )
    result = _run(p)
    assert result.returncode == 0


def test_overlap_with_same_definition_passes(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        [
            _entry("ALPHA", ["alias"], "same definition"),
            _entry("BETA", ["alias"], "same definition"),
        ],
    )
    result = _run(p)
    assert result.returncode == 0


def test_overlap_with_different_definition_fails(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        [
            _entry("ALPHA", ["share"], "first definition"),
            _entry("BETA", ["share"], "different definition"),
        ],
    )
    result = _run(p)
    assert result.returncode != 0
    assert "alias_definition_conflict" in result.stdout


def test_overlap_when_alias_equals_other_term(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        [
            _entry("CBRS", [], "first definition"),
            _entry("OTHER", ["CBRS"], "different definition"),
        ],
    )
    result = _run(p)
    assert result.returncode != 0
    assert "alias_definition_conflict" in result.stdout
