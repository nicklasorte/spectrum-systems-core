"""Tests for AliasNormalizer (FINDING-E-002)."""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from spectrum_systems_core.agency.alias_normalizer import AliasNormalizer


class AliasNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fcc_variations_normalize_to_fcc(self) -> None:
        normalizer = AliasNormalizer()
        for name in [
            "FCC",
            "fcc",
            "Federal Communications Commission",
            "federal communications commission",
            "F.C.C.",
        ]:
            self.assertEqual(
                normalizer.normalize(name, str(self.repo_root)),
                "fcc",
                f"failed for {name}",
            )

    def test_unknown_agency_generates_slug(self) -> None:
        normalizer = AliasNormalizer()
        slug = normalizer.normalize(
            "Bureau of Frequency Allocation", str(self.repo_root)
        )
        self.assertEqual(slug, "bureau_of_frequency_allocation")

    def test_normalize_case_insensitive(self) -> None:
        normalizer = AliasNormalizer()
        self.assertEqual(
            normalizer.normalize("DOD", str(self.repo_root)), "dod"
        )
        self.assertEqual(
            normalizer.normalize("Department of Defense", str(self.repo_root)),
            "dod",
        )

    def test_would_duplicate_detects_existing_profile(self) -> None:
        # Create an existing profile under agency/foo/profile.json.
        slug = "foo"
        target = self.repo_root / "agency" / slug
        target.mkdir(parents=True, exist_ok=True)
        profile = {
            "profile_id": str(uuid.uuid4()),
            "agency_name": "Foo Agency",
            "agency_slug": slug,
            "aliases": ["foo", "f.a."],
            "jurisdiction": "test",
            "description": "",
            "active": True,
            "created_at": "2026-05-09T00:00:00+00:00",
            "updated_at": "2026-05-09T00:00:00+00:00",
            "total_comment_count": 0,
            "total_objection_count": 0,
            "provenance": {
                "produced_by": {"component": "test", "version": "1.0.0"},
                "execution_fingerprint_hash": "sha256:" + "0" * 64,
            },
        }
        (target / "profile.json").write_text(json.dumps(profile))

        normalizer = AliasNormalizer()
        self.assertTrue(
            normalizer.would_duplicate("Foo Agency", "anothername", str(self.repo_root))
        )
        self.assertTrue(
            normalizer.would_duplicate("F.A.", "anothername", str(self.repo_root))
        )
        self.assertTrue(
            normalizer.would_duplicate("nope", slug, str(self.repo_root))
        )
        self.assertFalse(
            normalizer.would_duplicate("Other Agency", "other", str(self.repo_root))
        )

    def test_jaccard_helper_matches_phase_d_registry(self) -> None:
        """Both Phase D and Phase E import the same jaccard implementation."""
        from spectrum_systems_core.paper.issue_registry import IssueRegistry
        from spectrum_systems_core.utils.text_similarity import jaccard

        registry = IssueRegistry()
        a = "Spectrum allocation methodology raises serious concerns"
        b = "Allocation methodology serious concerns about spectrum"
        self.assertAlmostEqual(
            registry._jaccard(a, b), jaccard(a, b, min_word_length=4)
        )


if __name__ == "__main__":
    unittest.main()
