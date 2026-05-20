"""Phase 2P glossary manifest verifier script tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


from spectrum_systems_core.glossary.loader import (
    ARTIFACT_TYPE,
    GLOSSARY_SCHEMA_VERSION,
    compute_allowed_sources_hash,
    compute_glossary_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_glossary_manifest.py"
ALLOWED = ["NTIA Manual", "47 CFR", "ITU-R", "3GPP", "NIST", "IEEE", "ANSI", "NTIA TR"]


def _entry(**overrides) -> dict:
    base = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "term": "CBRS",
        "aliases": [],
        "definition": "example.",
        "authoritative_source": "47 CFR 96",
        "category": "spectrum_sharing_framework",
        "created_at": "2026-05-20T00:00:00Z",
        "is_acronym": True,
        "disambiguation_required": False,
        "co_occurring_terms": [],
        "priority_weight": 1.0,
    }
    base.update(overrides)
    return base


def _write_glossary(tmp: Path, entries: list[dict]) -> Path:
    d = tmp / "glossary"
    d.mkdir()
    (d / "ntia_dod_spectrum_v1.jsonl").write_text(
        "\n".join(
            json.dumps(e, sort_keys=True, separators=(",", ":")) for e in entries
        ),
        encoding="utf-8",
    )
    ahash = compute_allowed_sources_hash(ALLOWED)
    (d / "allowed_sources.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "allowed_sources": ALLOWED,
                "sha256_hash": ahash,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    ghash = compute_glossary_hash(entries)
    (d / "MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "version": "1.0.0",
                "glossary_file": "ntia_dod_spectrum_v1.jsonl",
                "sha256_hash": ghash,
                "allowed_sources_hash": ahash,
                "entry_count": len(entries),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return d


def _run(glossary_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--glossary-dir", str(glossary_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_repo_manifest_is_consistent() -> None:
    repo_dir = REPO_ROOT / "data" / "glossary"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--glossary-dir", str(repo_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK glossary_manifest_verified" in result.stdout


def test_script_passes_for_happy_path(tmp_path: Path) -> None:
    d = _write_glossary(tmp_path, [_entry()])
    result = _run(d)
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_script_detects_glossary_tamper(tmp_path: Path) -> None:
    d = _write_glossary(tmp_path, [_entry()])
    jsonl_path = d / "ntia_dod_spectrum_v1.jsonl"
    jsonl_path.write_text(
        jsonl_path.read_text(encoding="utf-8").replace("example.", "TAMPERED."),
        encoding="utf-8",
    )
    result = _run(d)
    assert result.returncode != 0
    assert "glossary_manifest_hash_mismatch" in result.stdout


def test_script_detects_allowed_sources_tamper(tmp_path: Path) -> None:
    d = _write_glossary(tmp_path, [_entry()])
    allowed_path = d / "allowed_sources.json"
    doc = json.loads(allowed_path.read_text(encoding="utf-8"))
    doc["allowed_sources"].append("INSERTED")
    allowed_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    result = _run(d)
    assert result.returncode != 0
    assert "glossary_allowed_sources_hash_mismatch" in result.stdout


def test_script_detects_missing_manifest(tmp_path: Path) -> None:
    d = _write_glossary(tmp_path, [_entry()])
    (d / "MANIFEST.json").unlink()
    result = _run(d)
    assert result.returncode != 0
    assert "glossary_manifest_unreadable" in result.stdout


def test_script_detects_entry_count_mismatch(tmp_path: Path) -> None:
    d = _write_glossary(tmp_path, [_entry()])
    manifest_path = d / "MANIFEST.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["entry_count"] = 999
    manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    result = _run(d)
    assert result.returncode != 0
    assert "glossary_entry_count_mismatch" in result.stdout
