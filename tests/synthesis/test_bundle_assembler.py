"""Tests for BundleAssembler (FINDING-F-001, FINDING-F-002, FINDING-F-003)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.synthesis.bundle_assembler import (
    BundleAssembler,
    MAX_BUNDLE_TOKENS,
)
from spectrum_systems_core.synthesis._paths import synthesis_schema_path

from ._fixtures import (
    write_evidenced_claim,
    write_promoted_story,
    write_promoted_theme,
)


class BundleAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_min_eligible(self) -> None:
        write_promoted_story(self.repo_root)
        write_evidenced_claim(self.repo_root)
        write_promoted_theme(self.repo_root)

    def test_promoted_only_items_in_bundle(self) -> None:
        self._seed_min_eligible()
        result = BundleAssembler().assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        for item in result["bundle"]["items"]:
            self.assertIn(item["promoted_status"], {"promoted", "evidenced"})

    def test_candidate_item_excluded(self) -> None:
        # A candidate-status story (not promoted) should not enter the bundle.
        write_promoted_story(self.repo_root, status="candidate")
        write_promoted_story(self.repo_root, source_id="src-B", status="promoted")
        write_evidenced_claim(self.repo_root)
        write_promoted_theme(self.repo_root, source_id="src-B")
        result = BundleAssembler().assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        story_items = [
            it for it in result["bundle"]["items"]
            if it["artifact_type"] == "story_candidate"
        ]
        self.assertGreaterEqual(len(story_items), 1)
        for it in story_items:
            self.assertEqual(it["promoted_status"], "promoted")

    def test_token_budget_not_exceeded(self) -> None:
        for i in range(20):
            write_promoted_story(
                self.repo_root,
                source_id=f"src-{i}",
                summary=("X" * 1000) + f" story {i}",
            )
        write_evidenced_claim(self.repo_root)
        write_promoted_theme(self.repo_root)
        result = BundleAssembler().assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        self.assertLessEqual(
            result["bundle"]["total_token_estimate"], MAX_BUNDLE_TOKENS
        )

    def test_token_budget_exceeded_blocks(self) -> None:
        # Force a tiny budget by using a recipe with a small max_total_tokens
        # — done by patching the registry.
        from spectrum_systems_core.synthesis import retrieval_registry

        original = dict(retrieval_registry.BUILT_IN_RECIPES["default_report_v1"])
        try:
            patched = dict(original)
            patched["max_total_tokens"] = 5  # impossibly small
            retrieval_registry.BUILT_IN_RECIPES["default_report_v1"] = patched
            self._seed_min_eligible()
            result = BundleAssembler().assemble(
                run_id=str(uuid.uuid4()),
                recipe_id="default_report_v1",
                audience="technical",
                purpose="report",
                repo_root=str(self.repo_root),
            )
            # With a tiny budget no items fit — assembler reports
            # no_eligible_artifacts (a real failure mode).
            self.assertEqual(result["status"], "failure")
            self.assertIn("no_eligible_artifacts", result["reason"])
        finally:
            retrieval_registry.BUILT_IN_RECIPES["default_report_v1"] = original

    def test_no_eligible_artifacts_fails(self) -> None:
        result = BundleAssembler().assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("no_eligible_artifacts", result["reason"])
        self.assertFalse(
            (self.repo_root / "synthesis").exists()
            and any((self.repo_root / "synthesis").iterdir())
        )

    def test_bundle_hash_deterministic(self) -> None:
        self._seed_min_eligible()
        run_id = str(uuid.uuid4())
        a = BundleAssembler().assemble(
            run_id=run_id,
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        b = BundleAssembler().assemble(
            run_id=run_id,
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(a["status"], "success")
        self.assertEqual(b["status"], "success")
        self.assertEqual(a["bundle"]["bundle_hash"], b["bundle"]["bundle_hash"])

    def test_invalid_audience_fails(self) -> None:
        self._seed_min_eligible()
        result = BundleAssembler().assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="investor",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "failure")
        self.assertIn("invalid_audience", result["reason"])

    def test_schema_valid_on_output(self) -> None:
        import jsonschema

        self._seed_min_eligible()
        result = BundleAssembler().assemble(
            run_id=str(uuid.uuid4()),
            recipe_id="default_report_v1",
            audience="technical",
            purpose="report",
            repo_root=str(self.repo_root),
        )
        self.assertEqual(result["status"], "success", result.get("reason"))
        schema = json.loads(
            synthesis_schema_path("context_bundle").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(result["bundle"])


if __name__ == "__main__":
    unittest.main()
