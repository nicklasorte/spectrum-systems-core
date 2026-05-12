"""Phase V.6 tests: scope overgeneralization detector."""
from __future__ import annotations

import pytest

from spectrum_systems_core.config.taxonomy import OVERGENERALIZATION_MARKERS
from spectrum_systems_core.extraction.generalization_checker import (
    BAND_PATTERN,
    check_generalization_bias,
    find_band_refs,
    find_overgeneralization_markers,
    scan_items,
)


def test_specific_band_specific_extraction_no_finding() -> None:
    finding = check_generalization_bias(
        source_text="discussion about the 7 GHz band",
        extracted_text="approved the 7 GHz band plan",
        item_id="i1",
    )
    assert finding is None


def test_specific_band_broad_extraction_fires() -> None:
    finding = check_generalization_bias(
        source_text="the 7 GHz band protection criterion",
        extracted_text="approved for all spectrum bands",
        item_id="i2",
    )
    assert finding is not None
    assert finding.finding_code == "scope_overgeneralization"
    assert finding.severity == "warn"
    assert "7 GHz" in " ".join(finding.context["source_band_ref"])
    assert "all spectrum" in finding.context["triggered_markers"]


def test_mhz_source_broad_extraction_fires() -> None:
    finding = check_generalization_bias(
        source_text="threshold at 6525 MHz",
        extracted_text="applicable to all frequencies",
        item_id="i3",
    )
    assert finding is not None
    assert any("6525" in b for b in finding.context["source_band_ref"])


def test_no_band_in_source_no_finding() -> None:
    """The detector requires a specific band reference in source."""
    finding = check_generalization_bias(
        source_text="spectrum policy is complex",
        extracted_text="all spectrum should be regulated",
        item_id="i4",
    )
    assert finding is None


def test_related_bands_phrase_not_a_marker() -> None:
    """Legitimate "narrower band + related bands" phrasing should
    not fire because "related bands" is not in
    OVERGENERALIZATION_MARKERS."""
    finding = check_generalization_bias(
        source_text="7 GHz band study",
        extracted_text="affects 7 GHz and related bands",
        item_id="i5",
    )
    assert finding is None


def test_entire_spectrum_marker_fires() -> None:
    finding = check_generalization_bias(
        source_text="6525 MHz to 6875 MHz",
        extracted_text="impacts the entire spectrum",
        item_id="i6",
    )
    assert finding is not None
    assert "entire spectrum" in finding.context["triggered_markers"]


def test_finding_context_has_required_structure() -> None:
    finding = check_generalization_bias(
        source_text="discussion of 12.7 GHz allocation",
        extracted_text="affects all bands",
        item_id="i7",
    )
    assert finding is not None
    assert "source_band_ref" in finding.context
    assert "triggered_markers" in finding.context
    assert isinstance(finding.context["source_band_ref"], list)
    assert isinstance(finding.context["triggered_markers"], list)
    assert finding.context["item_id"] == "i7"


def test_overgeneralization_markers_non_empty_guard() -> None:
    """If OVERGENERALIZATION_MARKERS were accidentally emptied, the
    detector becomes a no-op. Assert it can't happen silently."""
    assert len(OVERGENERALIZATION_MARKERS) > 0


def test_band_pattern_matches_common_forms() -> None:
    for sample in ("6525 MHz", "7 GHz", "12.7 GHz", "30 kHz", "6525MHz"):
        assert BAND_PATTERN.search(sample), sample


def test_band_pattern_rejects_non_band_numbers() -> None:
    for sample in ("7 meeting items", "item 7", "page 12", "version 1.0"):
        assert not BAND_PATTERN.search(sample), sample


def test_null_extracted_text_returns_none() -> None:
    """A failed extraction must not crash the checker."""
    assert check_generalization_bias("7 GHz", None, "i") is None
    assert check_generalization_bias("7 GHz", "", "i") is None


def test_null_source_text_returns_none() -> None:
    assert check_generalization_bias(None, "all spectrum", "i") is None
    assert check_generalization_bias("", "all spectrum", "i") is None


def test_env_disable_suppresses_all_findings(monkeypatch) -> None:
    monkeypatch.setenv("GENERALIZATION_CHECK_ENABLED", "false")
    finding = check_generalization_bias(
        source_text="7 GHz band",
        extracted_text="affects all spectrum bands",
        item_id="i",
    )
    assert finding is None


def test_find_band_refs_lists_all_occurrences() -> None:
    refs = find_band_refs("between 6525 MHz and 6875 MHz at 7 GHz")
    assert len(refs) == 3


def test_find_overgeneralization_markers_case_insensitive() -> None:
    markers = find_overgeneralization_markers("Applies to ALL SPECTRUM users")
    assert "all spectrum" in markers


def test_scan_items_returns_findings_list() -> None:
    items = [
        {
            "id": "a",
            "source_text": "7 GHz band",
            "extracted_text": "approved 7 GHz band plan",
        },
        {
            "id": "b",
            "source_text": "6525 MHz",
            "extracted_text": "approved for all frequencies",
        },
    ]
    findings = scan_items(
        items, source_text_key="source_text",
        extracted_text_key="extracted_text",
    )
    assert len(findings) == 1
    assert findings[0].context["item_id"] == "b"
