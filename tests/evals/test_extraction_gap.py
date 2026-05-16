"""Phase AB.4 — extraction gap metric tests."""
from __future__ import annotations

import difflib
import json

import pytest

from spectrum_systems_core.config.taxonomy import EXTRACTION_GAP_MIN_LCS
from spectrum_systems_core.evals.extraction_gap import (
    EmptyGoldSetError,
    _match_against_gold,
    compute_gap_metrics,
    parse_opus_output,
)
from spectrum_systems_core.evals.extraction_precision import LCS_THRESHOLD


def test_lcs_threshold_pinned():
    assert EXTRACTION_GAP_MIN_LCS == 0.7
    # Must equal the Phase Z precision eval's threshold so the gap
    # instrument and the precision eval use one paraphrase boundary.
    assert EXTRACTION_GAP_MIN_LCS == LCS_THRESHOLD


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def test_adversarial_069_ratio_is_not_a_true_positive():
    gold_text = "m" * 69 + "a" * 31
    extracted_text = "m" * 69 + "b" * 31
    # Prove the values genuinely produce a sub-threshold ratio (not
    # just lengths that "suggest" it) — red-team Pass 2 item 2.
    assert _ratio(extracted_text, gold_text) == pytest.approx(0.69, abs=1e-9)
    tp, fp, fn = _match_against_gold(
        [{"text": extracted_text}], [{"text": gold_text}]
    )
    assert (tp, fp, fn) == (0, 1, 1)


def test_adversarial_071_ratio_is_a_true_positive():
    gold_text = "m" * 71 + "a" * 29
    extracted_text = "m" * 71 + "b" * 29
    assert _ratio(extracted_text, gold_text) == pytest.approx(0.71, abs=1e-9)
    tp, fp, fn = _match_against_gold(
        [{"text": extracted_text}], [{"text": gold_text}]
    )
    assert (tp, fp, fn) == (1, 0, 0)


def test_each_gold_item_consumed_at_most_once():
    """Two identical extracted items cannot both match one gold item
    (no recall inflation by duplication)."""
    gold = [{"text": "approved the sharing framework"}]
    extracted = [
        {"text": "approved the sharing framework"},
        {"text": "approved the sharing framework"},
    ]
    tp, fp, fn = _match_against_gold(extracted, gold)
    assert (tp, fp, fn) == (1, 1, 0)


def _write_gold(tmp_path, gold: dict):
    p = tmp_path / "independent_gold.json"
    p.write_text(json.dumps(gold), encoding="utf-8")
    return p


def test_empty_gold_set_fails_loud(tmp_path):
    gold_path = _write_gold(
        tmp_path, {"decisions": [], "actions": [], "questions": []}
    )
    comparison = {
        "payload": {
            "regex_output": {"decisions": [], "actions": [], "questions": []},
            "haiku_output": {"decisions": [], "actions": [], "questions": []},
        }
    }
    with pytest.raises(EmptyGoldSetError, match="empty_gold_set"):
        compute_gap_metrics(comparison, gold_path)


def test_known_metrics_and_gaps(tmp_path):
    gold = {
        "decisions": [
            {"text": "approved the sharing framework"},
            {"text": "deferred the timeline review"},
        ],
        "actions": [{"text": "carol drafts the response"}],
        "questions": [{"text": "do we need a separate eval"}],
        "rubric": "test rubric",
    }
    gold_path = _write_gold(tmp_path, gold)

    # regex: catches only the one labelled-style decision.
    # haiku: catches both decisions + the action (3/4 gold).
    # opus: structured text covering all four gold items.
    comparison = {
        "payload": {
            "regex_output": {
                "decisions": [{"text": "approved the sharing framework"}],
                "actions": [],
                "questions": [],
            },
            "haiku_output": {
                "decisions": [
                    {"text": "approved the sharing framework"},
                    {"text": "deferred the timeline review"},
                ],
                "actions": [{"text": "carol drafts the response"}],
                "questions": [],
            },
        }
    }
    opus_raw = (
        "Decisions:\n"
        "- approved the sharing framework\n"
        "- deferred the timeline review\n"
        "Action Items:\n"
        "- carol drafts the response\n"
        "Open Questions:\n"
        "- do we need a separate eval\n"
    )

    res = compute_gap_metrics(comparison, gold_path, opus_raw_output=opus_raw)

    # regex: tp=1, fp=0, fn=3 → recall .25, precision 1.0
    assert res["regex"]["tp"] == 1
    assert res["regex"]["recall"] == pytest.approx(0.25, abs=1e-3)
    assert res["regex"]["precision"] == pytest.approx(1.0)

    # haiku: tp=3, fn=1 → recall .75, precision 1.0
    assert res["haiku"]["tp"] == 3
    assert res["haiku"]["recall"] == pytest.approx(0.75, abs=1e-3)

    # opus: all 4 → f1 1.0
    assert res["opus"]["tp"] == 4
    assert res["opus"]["f1"] == pytest.approx(1.0)

    assert res["gap_1_to_2_f1"] == pytest.approx(
        res["haiku"]["f1"] - res["regex"]["f1"], abs=1e-6
    )
    assert res["gap_2_to_3_f1"] == pytest.approx(
        res["opus"]["f1"] - res["haiku"]["f1"], abs=1e-6
    )
    assert res["gap_1_to_2_f1"] > 0  # Haiku beats regex
    assert res["gold_item_count"] == 4
    assert res["gold_rubric"] == "test rubric"
    assert res["rubric"]["lcs_threshold"] == 0.7


def test_opus_no_structure_emits_warning_not_silent_zero():
    parsed, warnings = parse_opus_output(
        "The meeting covered several items but with no headings at all."
    )
    assert "opus_no_structure_detected" in warnings
    # Falls back to a single item — never a silent zero.
    total = sum(len(parsed[c]) for c in parsed)
    assert total == 1


def test_opus_section_prose_fallback_warns():
    raw = "Decisions:\nThe board approved the plan after long debate.\n"
    parsed, warnings = parse_opus_output(raw)
    assert "opus_section_prose_fallback:decisions" in warnings
    assert len(parsed["decisions"]) == 1


def test_opus_empty_output_warns():
    parsed, warnings = parse_opus_output("   ")
    assert "opus_output_empty" in warnings
    assert all(len(parsed[c]) == 0 for c in parsed)


def test_opus_not_supplied_warns_but_still_computes(tmp_path):
    gold_path = _write_gold(
        tmp_path,
        {"decisions": [{"text": "a decision"}], "actions": [],
         "questions": []},
    )
    comparison = {
        "payload": {
            "regex_output": {"decisions": [{"text": "a decision"}]},
            "haiku_output": {"decisions": [{"text": "a decision"}]},
        }
    }
    res = compute_gap_metrics(comparison, gold_path)  # 2-arg pinned form
    assert "opus_raw_output_not_supplied" in res["opus_parser_warnings"]
    assert res["opus"]["tp"] == 0
    assert res["regex"]["tp"] == 1
