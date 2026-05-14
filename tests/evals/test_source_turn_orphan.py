"""Phase P3-A T-1: source turn orphan and diversity metric tests."""
from __future__ import annotations

from spectrum_systems_core.evals.source_turn_orphan import (
    aggregate_source_turn_reports,
    compute_source_turn_report,
)


def _decision(text: str, source_turn_ids: list) -> dict:
    return {
        "decision_text": text,
        "decision_type": "approved",
        "source_turn_ids": source_turn_ids,
        "source_turn_validation": "verified",
        "confidence": 0.9,
    }


def test_orphan_rate_is_zero_when_all_turns_resolve() -> None:
    valid = {"c-1", "c-2", "c-3"}
    items = [
        _decision("first", ["c-1"]),
        _decision("second", ["c-2", "c-3"]),
    ]
    report = compute_source_turn_report(items, valid, item_type="decision")
    assert report.orphan_count == 0
    assert report.orphan_rate == 0.0


def test_orphan_rate_when_one_item_cites_fake_turn() -> None:
    valid = {"c-1", "c-2", "c-3"}
    items = [
        _decision("first", ["c-1"]),
        _decision("orphaned", ["FAKE-TURN-999"]),
    ]
    report = compute_source_turn_report(items, valid, item_type="decision")
    assert report.orphan_count == 1
    assert report.orphan_rate == 0.5
    # Identifier should land in the orphaned_item_ids report so the
    # operator can find the bad item without scrolling through every
    # extracted decision.
    assert any(
        "orphaned" in oid for oid in report.orphaned_item_ids
    )


def test_diversity_rate_low_when_all_decisions_cite_same_turn() -> None:
    valid = {"c-1", "c-2", "c-3", "c-4", "c-5"}
    items = [_decision(f"d{i}", ["c-1"]) for i in range(5)]
    report = compute_source_turn_report(items, valid, item_type="decision")
    # 1 distinct turn out of 5 available -> 0.2.
    assert report.distinct_turns_cited == 1
    assert report.diversity_rate == 0.2


def test_diversity_rate_full_when_every_turn_cited() -> None:
    valid = {"c-1", "c-2", "c-3"}
    items = [
        _decision("d1", ["c-1"]),
        _decision("d2", ["c-2"]),
        _decision("d3", ["c-3"]),
    ]
    report = compute_source_turn_report(items, valid, item_type="decision")
    assert report.diversity_rate == 1.0


def test_diversity_rate_caps_at_one_when_extra_turns_cited() -> None:
    # An item cites a turn id not in the valid set; the gate still
    # counts it in distinct_turns_cited, but diversity_rate is
    # capped at 1.0 so a regression on the cap is visible.
    valid = {"c-1"}
    items = [
        _decision("d1", ["c-1", "FAKE"]),
    ]
    report = compute_source_turn_report(items, valid, item_type="decision")
    assert report.diversity_rate == 1.0


def test_empty_items_returns_zero_rates() -> None:
    report = compute_source_turn_report(
        [], {"c-1"}, item_type="decision",
    )
    assert report.total == 0
    assert report.orphan_rate == 0.0
    assert report.diversity_rate == 0.0


def test_items_without_source_turn_ids_are_not_orphans() -> None:
    valid = {"c-1"}
    # Item with no source_turn_ids is reported via the schema's
    # source_turn_validation field, not the orphan rate. Including
    # it as an orphan would conflate "no provenance recorded" with
    # "cited a non-existent chunk".
    items = [
        {"decision_text": "d1", "source_turn_ids": []},
        {"decision_text": "d2", "source_turn_ids": None},
        {"decision_text": "d3"},  # field absent entirely
    ]
    report = compute_source_turn_report(items, valid, item_type="decision")
    assert report.orphan_count == 0
    assert report.total == 3


def test_aggregate_across_types_sums_correctly() -> None:
    valid = {"c-1", "c-2"}
    reports = [
        compute_source_turn_report(
            [_decision("d1", ["c-1"]), _decision("orphan", ["FAKE"])],
            valid, item_type="decision",
        ),
        compute_source_turn_report(
            [_decision("c1", ["c-2"])], valid, item_type="claim",
        ),
        compute_source_turn_report(
            [], valid, item_type="action_item",
        ),
    ]
    summary = aggregate_source_turn_reports(reports)
    assert summary["total_items"] == 3
    assert summary["orphan_count"] == 1
    assert summary["orphan_rate"] == 1 / 3
    assert summary["by_type"]["decision"]["orphan_rate"] == 0.5
    assert summary["by_type"]["claim"]["orphan_rate"] == 0.0
