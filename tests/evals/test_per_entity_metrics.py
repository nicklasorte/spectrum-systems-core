"""Phase AC.1 — per-entity F1 + three-bucket LCS classification tests.

Boundary tests construct inputs whose ``difflib`` ratio is the EXACT
threshold value (asserted with ``_ratio`` before classification), not
an approximation — red-team Pass 2 item 1.
"""
from __future__ import annotations

import difflib
import json

import pytest

from spectrum_systems_core.config.taxonomy import (
    MATCH_LCS_THRESHOLD,
    PARTIAL_LCS_THRESHOLD,
)
from spectrum_systems_core.evals.extraction_gap import (
    _classify_extraction,
    compute_per_entity_metrics,
)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _pair(match_chars: int) -> tuple[str, str]:
    """Two 100-char strings sharing ``match_chars`` leading chars.
    ``difflib`` ratio == 2*match/200, so ``_pair(70)`` is EXACTLY 0.7.
    autojunk does not trigger (each string is 100 < 200 chars)."""
    rest = 100 - match_chars
    return "m" * match_chars + "a" * rest, "m" * match_chars + "b" * rest


def test_match_thresholds_pinned():
    assert MATCH_LCS_THRESHOLD == 0.7
    assert PARTIAL_LCS_THRESHOLD == 0.4
    assert PARTIAL_LCS_THRESHOLD < MATCH_LCS_THRESHOLD


# ---------------------------------------------------------------- happy

def test_all_categories_full_match_precision_recall_one():
    extracted = {
        "decisions": [{"text": "approved the framework"}],
        "actions": [{"text": "carol drafts the response"}],
        "questions": [{"text": "do we need a separate eval"}],
    }
    gold = {
        "decisions": [{"text": "approved the framework"}],
        "actions": [{"text": "carol drafts the response"}],
        "questions": [{"text": "do we need a separate eval"}],
    }
    res = compute_per_entity_metrics(extracted, gold)
    for cat in ("decisions", "actions", "questions"):
        assert res[cat]["precision"] == 1.0
        assert res[cat]["recall"] == 1.0
        assert res[cat]["f1"] == 1.0
        assert res[cat]["tp"] == 1
        assert res[cat]["fp"] == 0
        assert res[cat]["fn"] == 0
        assert res[cat]["partial_match_count"] == 0
        assert res[cat]["findings"] == []


def test_mixed_decisions_perfect_actions_empty_questions_one_spurious():
    extracted = {
        "decisions": [{"text": "approved the framework"}],
        "actions": [],
        "questions": [
            {"text": "do we need a separate eval"},
            {"text": "zzz totally unrelated noise string"},
        ],
    }
    gold = {
        "decisions": [{"text": "approved the framework"}],
        "actions": [{"text": "carol drafts the response"}],
        "questions": [{"text": "do we need a separate eval"}],
    }
    res = compute_per_entity_metrics(extracted, gold)

    # decisions: perfect.
    assert res["decisions"]["f1"] == 1.0

    # actions: nothing extracted → precision 0.0 (NOT NaN) + finding;
    # recall 0.0 (gold exists, nothing recovered).
    assert res["actions"]["precision"] == 0.0
    assert res["actions"]["recall"] == 0.0
    assert any(
        f.startswith("no_data_for_metric:precision")
        for f in res["actions"]["findings"]
    )

    # questions: 1 TP + 1 spurious → precision 0.5, recall 1.0.
    assert res["questions"]["tp"] == 1
    assert res["questions"]["fp"] == 1
    assert res["questions"]["spurious_count"] == 1
    assert res["questions"]["precision"] == 0.5
    assert res["questions"]["recall"] == 1.0


def test_partial_match_is_fp_not_tp():
    a, b = _pair(50)  # ratio exactly 0.5 → partial (0.4 <= 0.5 < 0.7)
    assert _ratio(a, b) == pytest.approx(0.5, abs=1e-9)
    res = compute_per_entity_metrics(
        {"decisions": [{"text": a}], "actions": [], "questions": []},
        {"decisions": [{"text": b}], "actions": [], "questions": []},
    )
    d = res["decisions"]
    assert d["tp"] == 0  # partial is NOT a true positive
    assert d["partial_match_count"] == 1
    assert d["fp"] == 1  # counted as a false positive for precision
    assert d["precision"] == 0.0  # lowered, not inflated
    assert d["fn"] == 1  # the gold item was never consumed by a TP
    # The diagnostic list itself (not just the count) is present.
    assert len(d["partial_items"]) == 1
    pi = d["partial_items"][0]
    assert pi["extracted_text"] == a
    assert pi["best_gold_text"] == b
    assert pi["lcs"] == pytest.approx(0.5, abs=1e-4)


# ---------------------------------------------------------------- edges

def test_zero_extracted_precision_zero_not_nan():
    res = compute_per_entity_metrics(
        {"decisions": [], "actions": [], "questions": []},
        {"decisions": [{"text": "a decision"}], "actions": [],
         "questions": []},
    )
    d = res["decisions"]
    assert d["precision"] == 0.0
    assert d["precision"] == d["precision"]  # not NaN
    assert any(
        f.startswith("no_data_for_metric:precision") for f in d["findings"]
    )


def test_zero_gold_recall_zero_not_nan():
    res = compute_per_entity_metrics(
        {"decisions": [{"text": "an extracted decision"}], "actions": [],
         "questions": []},
        {"decisions": [], "actions": [], "questions": []},
    )
    d = res["decisions"]
    assert d["recall"] == 0.0
    assert d["recall"] == d["recall"]  # not NaN
    assert any(
        f.startswith("no_data_for_metric:recall") for f in d["findings"]
    )


def test_lcs_exactly_match_threshold_is_tp_inclusive():
    a, b = _pair(70)  # exactly 0.7
    assert _ratio(a, b) == pytest.approx(0.7, abs=1e-9)
    c = _classify_extraction([{"text": a}], [{"text": b}])
    assert c["true_positive"] == 1
    assert c["partial_match"] == 0
    assert c["spurious"] == 0
    assert c["false_negative"] == 0


def test_lcs_exactly_partial_threshold_is_partial_inclusive_lower():
    a, b = _pair(40)  # exactly 0.4
    assert _ratio(a, b) == pytest.approx(0.4, abs=1e-9)
    c = _classify_extraction([{"text": a}], [{"text": b}])
    assert c["partial_match"] == 1
    assert c["true_positive"] == 0
    assert c["spurious"] == 0
    assert len(c["partial_items"]) == 1


def test_lcs_just_below_partial_threshold_is_spurious():
    a, b = _pair(39)  # exactly 0.39 (< 0.4, exclusive lower bound)
    assert _ratio(a, b) == pytest.approx(0.39, abs=1e-9)
    c = _classify_extraction([{"text": a}], [{"text": b}])
    assert c["spurious"] == 1
    assert c["partial_match"] == 0
    assert c["true_positive"] == 0


def test_duplicate_extraction_cannot_inflate_recall():
    gold = {"decisions": [{"text": "approved the sharing framework"}],
            "actions": [], "questions": []}
    extracted = {
        "decisions": [
            {"text": "approved the sharing framework"},
            {"text": "approved the sharing framework"},
        ],
        "actions": [],
        "questions": [],
    }
    d = compute_per_entity_metrics(extracted, gold)["decisions"]
    assert d["tp"] == 1  # gold consumed at most once
    assert d["fp"] == 1  # the duplicate is a false positive
    assert d["fn"] == 0


# ---------------------------------------------------------- determinism

def test_determinism_byte_identical_three_runs():
    extracted = {
        "decisions": [{"text": "approved x"}, {"text": "noisy zzz"}],
        "actions": [{"text": "carol drafts y"}],
        "questions": [{"text": "do we need z"}],
    }
    gold = {
        "decisions": [{"text": "approved x"}],
        "actions": [{"text": "carol drafts y"}],
        "questions": [{"text": "do we need z"}],
    }
    runs = [
        json.dumps(compute_per_entity_metrics(extracted, gold), sort_keys=True)
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]
