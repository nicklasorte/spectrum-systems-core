"""Phase P3-A T-3: smoke tests for scripts/update_glossary.py.

Drives the operator script with a temp data-lake to verify the
bump-only, --glossary-file, and --target-version flag combinations
produce a schema-valid versioned glossary artifact on disk.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_glossary(path: Path, version: int, term_count: int = 1) -> None:
    artifact = {
        "artifact_type": "spectrum_glossary",
        "schema_version": "1.0.0",
        "glossary_version": str(version),
        "term_count": term_count,
        "content_hash": "sha256:" + "a" * 64,
        "created_at": "1970-01-01T00:00:00+00:00",
        "terms": [
            {
                "term_id": f"t-{i}",
                "term": f"term_{i}",
                "abbreviation": None,
                "definition": f"def {i}",
                "short_definition": f"short {i}",
                "authoritative_source": "FCC",
                "domain_scope": "spectrum",
                "related_term_ids": [],
            }
            for i in range(term_count)
        ],
    }
    path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "update_glossary.py"


def _run(args, env=None):
    """Run the script in a subprocess and return (returncode, stdout, stderr)."""
    import os
    cmd = [sys.executable, str(SCRIPT)] + args
    e = dict(os.environ)
    # Start clean: never inherit a real API key from the test runner.
    e.pop("ANTHROPIC_API_KEY", None)
    if env:
        e.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=e)
    return proc.returncode, proc.stdout, proc.stderr


def test_bump_only_writes_next_version(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    gloss = dl / "store" / "artifacts" / "glossary"
    gloss.mkdir(parents=True)
    _write_glossary(gloss / "spectrum_glossary_v1.json", 1, term_count=3)

    rc, stdout, stderr = _run(["--data-lake", str(dl), "--bump-only"])
    assert rc == 0, f"rc={rc} stderr={stderr}"
    assert (gloss / "spectrum_glossary_v2.json").is_file()
    artifact = json.loads(
        (gloss / "spectrum_glossary_v2.json").read_text(encoding="utf-8")
    )
    assert artifact["glossary_version"] == "2"
    # bump-only copies the term list verbatim.
    assert artifact["term_count"] == 3


def test_bump_only_refuses_when_no_existing_glossary(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    (dl / "store" / "artifacts" / "glossary").mkdir(parents=True)
    rc, _, stderr = _run(["--data-lake", str(dl), "--bump-only"])
    assert rc != 0
    assert "requires an existing versioned glossary" in stderr


def test_refuses_to_overwrite_existing_version(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    gloss = dl / "store" / "artifacts" / "glossary"
    gloss.mkdir(parents=True)
    _write_glossary(gloss / "spectrum_glossary_v1.json", 1)
    _write_glossary(gloss / "spectrum_glossary_v2.json", 2)

    rc, _, stderr = _run([
        "--data-lake", str(dl), "--bump-only", "--target-version", "1",
    ])
    assert rc != 0
    # Either overwrite refusal or target-version<=max refusal.
    assert ("not above" in stderr) or ("refusing to overwrite" in stderr)


def test_glossary_file_input(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    gloss = dl / "store" / "artifacts" / "glossary"
    gloss.mkdir(parents=True)
    _write_glossary(gloss / "spectrum_glossary_v1.json", 1)

    new_terms_file = tmp_path / "new_terms.json"
    new_terms_file.write_text(json.dumps([
        {
            "term_id": "abc",
            "term": "FOO",
            "abbreviation": None,
            "definition": "Foo Bar",
            "short_definition": "Foo Bar",
            "authoritative_source": "test",
            "domain_scope": "test",
            "related_term_ids": [],
        },
    ]))

    rc, stdout, stderr = _run([
        "--data-lake", str(dl),
        "--glossary-file", str(new_terms_file),
    ])
    assert rc == 0, f"rc={rc} stderr={stderr}"
    v2 = json.loads((gloss / "spectrum_glossary_v2.json").read_text())
    assert v2["term_count"] == 1
    assert v2["terms"][0]["term"] == "FOO"


def test_refuses_when_anthropic_api_key_set(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    (dl / "store" / "artifacts" / "glossary").mkdir(parents=True)
    rc, _, stderr = _run(
        ["--data-lake", str(dl), "--bump-only"],
        env={"ANTHROPIC_API_KEY": "dummy"},
    )
    assert rc != 0
    assert "ANTHROPIC_API_KEY" in stderr


def test_force_flag_overrides_api_key_guard(tmp_path: Path) -> None:
    dl = tmp_path / "data-lake"
    gloss = dl / "store" / "artifacts" / "glossary"
    gloss.mkdir(parents=True)
    _write_glossary(gloss / "spectrum_glossary_v1.json", 1)
    rc, _, stderr = _run(
        ["--data-lake", str(dl), "--bump-only", "--force"],
        env={"ANTHROPIC_API_KEY": "dummy"},
    )
    assert rc == 0, f"rc={rc} stderr={stderr}"
