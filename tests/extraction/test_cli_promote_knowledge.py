"""Tests for the `promote-knowledge` CLI (Step 16 + RT4-004)."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.cli import promote_knowledge


def _write_concept(repo_root: Path, source_id: str, concept_id: str) -> None:
    target = repo_root / "processed" / "notes" / source_id / "knowledge"
    target.mkdir(parents=True, exist_ok=True)
    record = {
        "concept_id": concept_id,
        "concept_name": "candidate concept",
        "definition": "A definition that is over twenty characters long.",
        "source_story_ids": [str(uuid.uuid4())],
        "source_ids": [source_id],
        "supporting_excerpts": [
            {"unit_id": "u-1", "excerpt": "an excerpt", "source_id": source_id}
        ],
        "related_concepts": [],
        "status": "candidate",
        "created_at": "2026-05-09T00:00:00+00:00",
    }
    with (target / "concepts.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


class PromoteKnowledgeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_promotes_concept_writes_promoted_file(self) -> None:
        sid = "src-promote-001"
        cid = str(uuid.uuid4())
        _write_concept(self.repo_root, sid, cid)
        out = io.StringIO()
        rc = promote_knowledge(
            artifact_id=cid,
            source_id=sid,
            artifact_type="concept",
            repo_root=self.repo_root,
            out_stream=out,
        )
        self.assertEqual(rc, 0)
        promoted = (
            self.repo_root / "processed" / "notes" / sid
            / "knowledge" / "promoted" / f"{cid}.json"
        )
        self.assertTrue(promoted.is_file())
        record = json.loads(promoted.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "promoted")

    def test_double_promote_blocks(self) -> None:
        """RT4-004: promote-knowledge twice must fail with already_promoted."""
        sid = "src-promote-002"
        cid = str(uuid.uuid4())
        _write_concept(self.repo_root, sid, cid)
        rc1 = promote_knowledge(
            artifact_id=cid, source_id=sid, artifact_type="concept",
            repo_root=self.repo_root, out_stream=io.StringIO(),
        )
        self.assertEqual(rc1, 0)
        out = io.StringIO()
        rc2 = promote_knowledge(
            artifact_id=cid, source_id=sid, artifact_type="concept",
            repo_root=self.repo_root, out_stream=out,
        )
        self.assertEqual(rc2, 1)
        self.assertIn("already_promoted", out.getvalue())


if __name__ == "__main__":
    unittest.main()
