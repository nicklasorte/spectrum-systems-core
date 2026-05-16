"""Phase AC.2 — compare-corpus integration contract.

CLAUDE.md integration-test rule: a consumer that reads a pipeline
artifact gets an integration test that (1) produces the artifact via
the REAL writer (not a hand-rolled dict), (2) writes to a real temp
directory, (3) invokes the consumer via ``subprocess.run``, (4)
asserts the on-disk output (not just the return code).

The artifact the corpus runner reads back is the
``extraction_comparison`` (+ ``extraction_unconstrained``) that
``comparison_runner.run_compare_extraction`` writes for each
transcript — the REAL Phase AB writer, invoked inside the corpus
runner. The independent gold set is the AB.4 ``comparison_gold``
fixture (a real fixture, not a hand-rolled dict). Subprocess +
``COMPARE_EXTRACTION_STUB=1`` keeps it API-free in CI.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

COMPARISON_GOLD_DIR = (
    Path(__file__).parent.parent / "fixtures" / "comparison_gold"
)


def test_compare_corpus_subprocess_over_comparison_gold(tmp_path):
    lake = tmp_path / "lake"

    env = dict(os.environ)
    env["COMPARE_EXTRACTION_STUB"] = "1"
    env.pop("ANTHROPIC_API_KEY", None)
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "spectrum_systems_core.data_lake.cli",
            "compare-corpus",
            "--lake",
            str(lake),
            "--transcripts",
            str(COMPARISON_GOLD_DIR),
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    corpus_files = glob.glob(
        str(lake / "processed" / "corpus" / "*" / "corpus_comparison__*.json")
    )
    assert len(corpus_files) == 1, corpus_files
    corpus = json.loads(Path(corpus_files[0]).read_text(encoding="utf-8"))

    assert corpus["artifact_type"] == "corpus_comparison"
    assert corpus["schema_version"] == 1  # envelope int (constitution §6)
    payload = corpus["payload"]
    assert payload["schema_version"] == "1.0.0"  # payload semver marker
    assert payload["corpus_status"] == "complete"
    # comparison_gold has exactly one meeting dir → exactly one meeting.
    assert len(payload["meeting_ids"]) >= 1
    mid = payload["meeting_ids"][0]
    entry = payload["per_meeting"][mid]
    assert entry["gold_present"] is True
    # The sibling independent_gold.json enabled per-entity F1.
    assert entry["per_entity_f1"] is not None
    for cat in ("decisions", "actions", "questions"):
        assert "haiku" in entry["per_entity_f1"][cat]
        assert "opus" in entry["per_entity_f1"][cat]
    # The per-entity diagnostic (partial_items) is persisted in JSON,
    # not just a count (red-team Pass 1).
    assert entry["per_entity_metrics"] is not None
    assert "partial_items" in (
        entry["per_entity_metrics"]["haiku"]["decisions"]
    )

    # The extraction_comparison the corpus runner read back was written
    # by the REAL comparison_runner writer for this meeting.
    comp = glob.glob(
        str(lake / "processed" / "meetings" / mid
            / "extraction_comparison__*.json")
    )
    assert len(comp) == 1
    comp_art = json.loads(Path(comp[0]).read_text(encoding="utf-8"))
    assert comp_art["artifact_type"] == "extraction_comparison"
    assert comp_art["payload"]["schema_version"] == "1.1.0"
    # The corpus record's comparison_artifact_id is the ENVELOPE id of
    # the extraction_comparison written by the real writer (a resolvable
    # cross-artifact reference, not the transcript hash).
    assert entry["comparison_artifact_id"] == comp_art["artifact_id"]
    assert (
        entry["comparison_artifact_id"]
        != comp_art["payload"]["transcript_artifact_id"]
    )

    # Markdown projection exists and carries the corpus header.
    md = glob.glob(
        str(lake / "processed" / "corpus" / "*" / "markdown"
            / "corpus_comparison.md")
    )
    assert len(md) == 1
    body = Path(md[0]).read_text(encoding="utf-8")
    assert body.startswith("# Corpus Comparison — ")
    assert "## Aggregate per-entity F1" in body
    assert "## Per-meeting breakdown" in body
