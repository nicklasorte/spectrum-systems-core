"""Integration contract test for ``scripts/generate_gt_pairs.py``.

The script reads a real ``meeting_extraction`` artifact off disk and
writes ``ground_truth_pair`` artifacts the ``annotate_rubric.py``
script must be able to consume. This test:

  1. Uses ``tests.integration.fixtures`` factories that call the
     REAL writers (``ExtractionMerger.merge`` for the extraction,
     ``generate_gt_pairs.build_pair`` for the GT pair shape assertion).
  2. Writes the seed artifacts to a real temp ``data-lake/`` directory.
  3. Calls ``generate_gt_pairs.py`` via ``subprocess.run`` against the
     temp directory.
  4. Asserts:
       a. The script exits 0.
       b. ``ground_truth/*.json`` files appear on disk.
       c. Each pair validates against the ``ground_truth_pair`` schema.
       d. ``annotate_rubric.list_candidates`` finds the pairs when
          filtered by the human-readable ``source_id`` slug — this is
          the exact code path the annotate-gt-rubric mobile workflow
          hits.

Without this test the field-name contract between the generator
(``source_id`` top-level) and the consumer (``annotate_rubric``'s
``_SOURCE_ID_FIELDS`` filter) is invisible until production runs.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import jsonschema

from tests.integration.fixtures import (
    make_meeting_extraction_artifact,
    make_source_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
GT_SCHEMA_PATH = (
    REPO_ROOT / "contracts" / "schemas" / "ingestion"
    / "ground_truth_pair.schema.json"
)


def _seed_data_lake(tmp_path: Path) -> tuple[Path, str]:
    dl = tmp_path / "data-lake"
    artifact_id = str(uuid.uuid4())

    proc_dir = dl / "store" / "processed" / "meetings" / SOURCE_ID
    proc_dir.mkdir(parents=True)
    (proc_dir / "source_record.json").write_text(
        json.dumps(make_source_record(SOURCE_ID, artifact_id))
    )

    ext_dir = dl / "store" / "artifacts" / "extractions"
    ext_dir.mkdir(parents=True)
    extraction = make_meeting_extraction_artifact(artifact_id)
    (ext_dir / f"{artifact_id}_meeting_extraction.json").write_text(
        json.dumps(extraction)
    )

    return dl, artifact_id


def _run_script(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "generate_gt_pairs.py"),
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_generate_gt_pairs_writes_schema_valid_pairs(tmp_path: Path) -> None:
    """Each decision in the seeded extraction becomes a schema-valid
    GT pair on disk under ground_truth/."""
    data_lake, artifact_id = _seed_data_lake(tmp_path)

    result = _run_script(
        ["--source-id", SOURCE_ID, "--data-lake", str(data_lake)]
    )
    assert result.returncode == 0, (
        f"Script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    gt_dir = data_lake / "store" / "artifacts" / "ground_truth"
    pair_files = sorted(gt_dir.glob("*.json"))
    # The default fixture decisions cover 3 outcomes; expect 3 pairs.
    assert len(pair_files) == 3, (
        f"Expected 3 GT pairs, found {len(pair_files)}: "
        f"{[p.name for p in pair_files]}"
    )

    schema = json.loads(GT_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for path in pair_files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(doc), key=lambda e: e.path)
        assert not errors, (
            f"{path.name} failed schema: "
            f"{[e.message for e in errors]}"
        )
        # Contract assertions on the writer's output shape.
        assert doc["source_id"] == SOURCE_ID
        assert doc["source_artifact_id"] == artifact_id
        assert doc["target_type"] == "decision"
        assert doc["provenance"]["produced_by"] == "GenerateGTPairs"
        assert doc["ground_truth_text"], "ground_truth_text must be populated"


def test_generated_pairs_are_idempotent(tmp_path: Path) -> None:
    """Re-running the script must produce the SAME files (same
    pair_ids) — the deterministic uuid5 path. A non-idempotent
    writer would duplicate pairs and break the downstream gate."""
    data_lake, _ = _seed_data_lake(tmp_path)
    _run_script(["--source-id", SOURCE_ID, "--data-lake", str(data_lake)])
    gt_dir = data_lake / "store" / "artifacts" / "ground_truth"
    first = {p.name for p in gt_dir.glob("*.json")}

    _run_script(["--source-id", SOURCE_ID, "--data-lake", str(data_lake)])
    second = {p.name for p in gt_dir.glob("*.json")}

    assert first == second, (
        f"Re-run produced different pair_ids — idempotency broken.\n"
        f"first run: {first}\nsecond run: {second}"
    )


def test_annotate_rubric_finds_generated_pairs(tmp_path: Path) -> None:
    """The whole point of this script: ``annotate_rubric`` must be
    able to filter the generated pairs by the human-readable slug
    (the same form passed to the mobile annotate-gt-rubric workflow).

    This is the exact failure case the fix targets: previously the
    annotate workflow exited with "source_id matched 0 pairs"
    because no GT pairs existed for the slug. With the generator
    wired in, ``list_candidates`` must return them.
    """
    data_lake, _ = _seed_data_lake(tmp_path)
    result = _run_script(
        ["--source-id", SOURCE_ID, "--data-lake", str(data_lake)]
    )
    assert result.returncode == 0, result.stderr

    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import annotate_rubric  # type: ignore  # noqa: WPS433

    sdl_root = data_lake / "store" / "artifacts"
    pairs = annotate_rubric.list_candidates(
        sdl_root, source_id=SOURCE_ID, limit=20,
    )
    assert pairs, (
        "annotate_rubric.list_candidates must find the generated "
        "pairs by the human-readable source_id slug. The whole fix "
        "is that the annotate workflow stops failing with "
        "'source_id matched 0 pairs'."
    )
    assert len(pairs) == 3
    # Each candidate must carry decision-derived text for the
    # rubric reviewer to inspect.
    for p in pairs:
        assert (p.get("ground_truth_text") or "").strip(), (
            f"GT pair missing ground_truth_text: {p}"
        )


def test_script_fails_loudly_when_no_extraction(tmp_path: Path) -> None:
    """Without a seeded extraction the script must exit non-zero with
    a descriptive reason, not silently write zero pairs and exit 0."""
    dl = tmp_path / "data-lake"
    (dl / "store" / "artifacts").mkdir(parents=True)

    result = _run_script(["--source-id", SOURCE_ID, "--data-lake", str(dl)])
    assert result.returncode != 0, (
        "Script must exit non-zero when no extraction is on disk"
    )
    assert "no_meeting_extraction_for_source_id" in result.stdout, (
        f"Expected diagnostic reason in stdout; got: {result.stdout}"
    )
