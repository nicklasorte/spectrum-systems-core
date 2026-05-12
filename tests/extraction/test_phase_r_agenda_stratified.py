"""Phase R.4: agenda-stratified eval metrics tests."""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from spectrum_systems_core.evals.m4.agenda_stratified import (
    UNCLASSIFIED_SECTION,
    build_per_agenda_section_metrics,
    diff_against_baseline,
)


class BuildPerAgendaSectionMetricsTests(unittest.TestCase):
    def test_unclassified_fallback_when_no_agenda_metrics(self) -> None:
        # AgendaDetector not deployed -> empty per_agenda_item_metrics ->
        # result is a single "unclassified" section.
        out = build_per_agenda_section_metrics(
            per_agenda_item_metrics={},
            agenda_items=[],
            chunks=[{"chunk_id": "c-1"}, {"chunk_id": "c-2"}],
        )
        self.assertIn(UNCLASSIFIED_SECTION, out)
        self.assertEqual(out[UNCLASSIFIED_SECTION]["pairs"], 2)

    def test_section_per_agenda_item(self) -> None:
        per_agenda = {
            "coverage_by_agenda_item": {"a-1": 0.8, "a-2": 0.6},
            "precision_by_agenda_item": {"a-1": 0.9, "a-2": 0.7},
        }
        agenda_items = [
            {"agenda_item_id": "a-1", "agenda_item_label": "Roll Call"},
            {"agenda_item_id": "a-2", "agenda_item_label": "Study Plan"},
        ]
        chunks = [
            {"chunk_id": "c-1", "agenda_item_id": "a-1"},
            {"chunk_id": "c-2", "agenda_item_id": "a-1"},
            {"chunk_id": "c-3", "agenda_item_id": "a-1"},
            {"chunk_id": "c-4", "agenda_item_id": "a-2"},
            {"chunk_id": "c-5", "agenda_item_id": "a-2"},
        ]
        out = build_per_agenda_section_metrics(
            per_agenda, agenda_items, chunks=chunks,
        )
        self.assertIn("agenda_item_Roll Call", out)
        self.assertIn("agenda_item_Study Plan", out)
        self.assertEqual(out["agenda_item_Roll Call"]["coverage"], 0.8)
        self.assertEqual(out["agenda_item_Roll Call"]["pairs"], 3)
        self.assertEqual(out["agenda_item_Study Plan"]["pairs"], 2)

    def test_excluded_small_passes_through(self) -> None:
        per_agenda = {
            "coverage_by_agenda_item": {"a-1": "excluded_small"},
            "precision_by_agenda_item": {"a-1": "excluded_small"},
        }
        out = build_per_agenda_section_metrics(
            per_agenda, agenda_items=[{"agenda_item_id": "a-1"}], chunks=[],
        )
        self.assertEqual(
            list(out.values())[0]["coverage"], "excluded_small",
        )

    def test_chunks_without_agenda_pool_into_unclassified(self) -> None:
        per_agenda = {
            "coverage_by_agenda_item": {"a-1": 0.8},
            "precision_by_agenda_item": {"a-1": 0.9},
        }
        chunks = [
            {"chunk_id": "c-1", "agenda_item_id": "a-1"},
            {"chunk_id": "c-2"},  # no agenda
            {"chunk_id": "c-3"},  # no agenda
        ]
        out = build_per_agenda_section_metrics(
            per_agenda,
            agenda_items=[{"agenda_item_id": "a-1", "agenda_item_label": "Roll"}],
            chunks=chunks,
        )
        self.assertIn(UNCLASSIFIED_SECTION, out)
        self.assertEqual(out[UNCLASSIFIED_SECTION]["pairs"], 2)


class DiffAgainstBaselineTests(unittest.TestCase):
    def test_new_section_in_current_recorded(self) -> None:
        baseline = {
            "agenda_item_Roll": {"coverage": 0.8, "precision": 0.9, "pairs": 3},
        }
        current = {
            "agenda_item_Roll": {"coverage": 0.7, "precision": 0.9, "pairs": 3},
            "agenda_item_New": {"coverage": 0.5, "precision": 0.6, "pairs": 2},
        }
        out = diff_against_baseline(current, baseline)
        self.assertIn("agenda_item_New", out["new_sections_discovered"])

    def test_missing_section_in_current_recorded(self) -> None:
        baseline = {
            "agenda_item_Roll": {"coverage": 0.8, "precision": 0.9, "pairs": 3},
            "agenda_item_Gone": {"coverage": 0.6, "precision": 0.7, "pairs": 2},
        }
        current = {
            "agenda_item_Roll": {"coverage": 0.8, "precision": 0.9, "pairs": 3},
        }
        out = diff_against_baseline(current, baseline)
        self.assertIn("agenda_item_Gone", out["missing_sections_in_current"])

    def test_missing_baseline_section_does_not_error(self) -> None:
        # RegressionGate uses .get() per spec: missing baseline sections
        # do NOT raise -- they show up as new_sections_discovered.
        current = {
            "agenda_item_Roll": {"coverage": 0.8, "precision": 0.9, "pairs": 3},
        }
        out = diff_against_baseline(current, baseline={})
        self.assertEqual(
            out["new_sections_discovered"], ["agenda_item_Roll"],
        )
        self.assertEqual(out["diffs"], {})

    def test_delta_computed_for_common_sections(self) -> None:
        baseline = {
            "agenda_item_Roll": {"coverage": 0.8, "precision": 0.9, "pairs": 3},
        }
        current = {
            "agenda_item_Roll": {"coverage": 0.6, "precision": 0.85, "pairs": 3},
        }
        out = diff_against_baseline(current, baseline)
        self.assertAlmostEqual(
            out["diffs"]["agenda_item_Roll"]["coverage_delta"], -0.2,
        )
        self.assertAlmostEqual(
            out["diffs"]["agenda_item_Roll"]["precision_delta"], -0.05,
        )

    def test_excluded_small_skipped_in_diff(self) -> None:
        # When baseline says excluded_small, no numeric delta can be
        # computed: the diff dict for that section is empty.
        baseline = {
            "agenda_item_Roll": {
                "coverage": "excluded_small",
                "precision": "excluded_small",
                "pairs": 1,
            },
        }
        current = {
            "agenda_item_Roll": {"coverage": 0.8, "precision": 0.9, "pairs": 3},
        }
        out = diff_against_baseline(current, baseline)
        self.assertNotIn("agenda_item_Roll", out["diffs"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
