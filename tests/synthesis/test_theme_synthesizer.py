"""Tests for ThemeSynthesizer (deterministic cross-source theme synthesis)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.theme_synthesizer import ThemeSynthesizer
from spectrum_systems_core.utils.text_similarity import jaccard

from ._fixtures import write_evidenced_claim, write_promoted_theme


class ThemeSynthesizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read_records(self, run_id: str) -> list:
        path = self.repo_root / "synthesis" / run_id / "themes.jsonl"
        if not path.is_file():
            return []
        out: list = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out

    def test_cross_source_theme_created(self) -> None:
        write_promoted_theme(
            self.repo_root,
            source_id="src-A",
            theme_name="adjacent channel interference modelling concerns",
        )
        write_promoted_theme(
            self.repo_root,
            source_id="src-B",
            theme_name="adjacent channel interference modelling concerns",
        )
        run_id = str(uuid.uuid4())
        result = ThemeSynthesizer().synthesize(run_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        records = self._read_records(run_id)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["cross_source"])
        self.assertEqual(
            sorted(records[0]["contributing_source_ids"]), ["src-A", "src-B"]
        )

    def test_single_source_theme_not_cross_source(self) -> None:
        write_promoted_theme(
            self.repo_root,
            source_id="src-A",
            theme_name="standalone topic of interest detail",
        )
        run_id = str(uuid.uuid4())
        ThemeSynthesizer().synthesize(run_id, str(self.repo_root))
        records = self._read_records(run_id)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["cross_source"])

    def test_no_themes_writes_empty_jsonl(self) -> None:
        run_id = str(uuid.uuid4())
        result = ThemeSynthesizer().synthesize(run_id, str(self.repo_root))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["theme_count"], 0)
        path = self.repo_root / "synthesis" / run_id / "themes.jsonl"
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_only_promoted_themes_included(self) -> None:
        # Write a theme manually with status=candidate -> must be excluded.
        target = (
            self.repo_root / "processed" / "books" / "src-A"
            / "knowledge" / "promoted"
        )
        target.mkdir(parents=True, exist_ok=True)
        bogus = {
            "theme_id": str(uuid.uuid4()),
            "theme_name": "candidate-only theme should be skipped",
            "description": "twenty character description here.",
            "source_story_ids": [str(uuid.uuid4())],
            "source_ids": ["src-A"],
            "supporting_excerpts": [
                {
                    "unit_id": str(uuid.uuid4()),
                    "excerpt": "x",
                    "source_id": "src-A",
                }
            ],
            "status": "candidate",
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        (target / f"{bogus['theme_id']}.json").write_text(
            json.dumps(bogus, sort_keys=True), encoding="utf-8"
        )
        run_id = str(uuid.uuid4())
        ThemeSynthesizer().synthesize(run_id, str(self.repo_root))
        records = self._read_records(run_id)
        self.assertEqual(records, [])

    def test_jaccard_from_shared_utility(self) -> None:
        # Two themes that overlap by Jaccard >= 0.6 should group together.
        a = "adjacent channel interference modelling concerns"
        b = "adjacent channel interference modelling specifications"
        self.assertGreaterEqual(jaccard(a, b), 0.6)
        write_promoted_theme(self.repo_root, source_id="src-A", theme_name=a)
        write_promoted_theme(self.repo_root, source_id="src-B", theme_name=b)
        run_id = str(uuid.uuid4())
        ThemeSynthesizer().synthesize(run_id, str(self.repo_root))
        records = self._read_records(run_id)
        self.assertEqual(len(records), 1, records)


if __name__ == "__main__":
    unittest.main()
