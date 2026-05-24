"""Unit tests for the human-minutes comparison logic.

The comparison MUST be deterministic, MUST NOT call any LLM, and MUST
produce ``artifact_type == "human_minutes_comparison"`` (never the
deprecated ``artifact_kind`` field; ``artifact_type`` is the binding
constitution-mandated name).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

# Import the comparison script as a module. The script lives in
# ``scripts/`` so a direct ``import`` requires the path manipulation
# this importlib pattern handles.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "compare_haiku_vs_human_minutes",
    _SCRIPTS_DIR / "compare_haiku_vs_human_minutes.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

compare = _mod.compare
build_artifact = _mod.build_artifact
text_similarity = _mod.text_similarity
COMPARISON_ARTIFACT_TYPE = _mod.COMPARISON_ARTIFACT_TYPE


def _human(
    discussion: list[dict] | None = None,
    action: list[dict] | None = None,
    next_steps: list[str] | None = None,
) -> dict:
    return {
        "artifact_type": "human_minutes",
        "schema_version": "1.0.0",
        "source_id": "test",
        "meeting_name": "test",
        "meeting_date": "1/1/2026",
        "discussion_items": discussion or [],
        "action_items": action or [],
        "next_steps": next_steps or [],
    }


def _extraction(payload: dict) -> dict:
    return {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.5.0",
        "payload": payload,
    }


def test_comparison_perfect_match():
    """A human item with an identical extraction text yields TP=1, FP=0, FN=0."""
    human = _human(
        action=[
            {"text": "Submit revised ERP values before the next session.",
             "responsible_party": "DoD"}
        ]
    )
    extraction = _extraction({
        "action_items": [
            {"action": "Submit revised ERP values before the next session."}
        ],
    })
    metrics = compare(extraction_artifact=extraction, human_minutes=human)
    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 0
    assert metrics["false_negatives"] == 0
    assert metrics["precision_vs_human"] == 1.0
    assert metrics["recall_vs_human"] == 1.0
    assert metrics["f1_vs_human"] == 1.0


def test_comparison_no_match():
    """Extraction items of wrong type cannot cover a human discussion item."""
    human = _human(
        discussion=[
            {
                "item_number": 1,
                "category": "Scope",
                "question_topic": "Why does the study cover only the US&P?",
                "asked_by": "Agency",
                "response": "Answered.",
                "follow_up": None,
            }
        ]
    )
    # Extraction puts the matching text in ``attendees`` — an array the
    # human-type mapping does not consult for ``discussion`` items.
    extraction = _extraction({
        "attendees": [
            {"name": "Why does the study cover only the US&P?"}
        ],
    })
    metrics = compare(extraction_artifact=extraction, human_minutes=human)
    assert metrics["false_negatives"] == 1
    assert metrics["true_positives"] == 0


def test_comparison_over_extraction_ratio():
    """5 human items, 263 extraction items -> ratio ≈ 52.6."""
    human = _human(
        discussion=[{
            "item_number": 1,
            "category": "X",
            "question_topic": "Q",
            "asked_by": "A",
            "response": "R",
            "follow_up": None,
        }],
        action=[
            {"text": "Action one item.", "responsible_party": "X"},
            {"text": "Action two item.", "responsible_party": "X"},
            {"text": "Action three item.", "responsible_party": "X"},
        ],
        next_steps=["Next step one item."],
    )
    extraction = _extraction({
        "decisions": [{"text": f"decision {i}"} for i in range(100)],
        "action_items": [{"action": f"action {i}"} for i in range(100)],
        "open_questions": [{"text": f"oq {i}"} for i in range(63)],
    })
    metrics = compare(extraction_artifact=extraction, human_minutes=human)
    assert metrics["total_human_items"] == 5
    assert metrics["extraction_total_items"] == 263
    assert metrics["over_extraction_ratio"] == pytest.approx(52.6, abs=0.1)


def test_comparison_f1_calculation():
    """Known TP/FP/FN exercises the F1 = 2PR/(P+R) calculation."""
    # 2 action items in human; one matches an extraction action, one does not.
    # Extraction has 1 matching action + 1 unrelated decision.
    human = _human(
        action=[
            {"text": "Submit the revised ERP values to NTIA.",
             "responsible_party": "DoD"},
            {"text": "An unrelated human action no extraction covers.",
             "responsible_party": "X"},
        ]
    )
    extraction = _extraction({
        "action_items": [
            {"action": "Submit the revised ERP values to NTIA."},
        ],
        "decisions": [
            {"text": "Some unrelated decision."},
        ],
    })
    metrics = compare(extraction_artifact=extraction, human_minutes=human)
    assert metrics["true_positives"] == 1
    assert metrics["false_negatives"] == 1
    assert metrics["false_positives"] == 1
    # precision = 1/2 = 0.5; recall = 1/2 = 0.5; F1 = 0.5
    assert metrics["precision_vs_human"] == 0.5
    assert metrics["recall_vs_human"] == 0.5
    assert metrics["f1_vs_human"] == 0.5


def test_comparison_threshold_sensitivity():
    """A lower threshold admits more matches."""
    human = _human(
        action=[
            {"text": "Submit a quarterly status report.",
             "responsible_party": "X"}
        ]
    )
    extraction = _extraction({
        "action_items": [
            # Same words but not a substring; SequenceMatcher ratio
            # depends on length similarity too.
            {"action": "Quarterly status report submission."},
        ],
    })
    low = compare(
        extraction_artifact=extraction, human_minutes=human, match_threshold=0.3
    )
    high = compare(
        extraction_artifact=extraction, human_minutes=human, match_threshold=0.85
    )
    # Lower threshold should never yield FEWER matches.
    assert low["true_positives"] >= high["true_positives"]


def test_comparison_artifact_type_correct():
    """Built artifact uses ``artifact_type`` (never ``artifact_kind``)."""
    metrics = compare(
        extraction_artifact=_extraction({"action_items": []}),
        human_minutes=_human(),
    )
    artifact = build_artifact(
        source_id="test",
        extraction_artifact_path=Path("/tmp/meeting_minutes__test.json"),
        human_minutes_path=Path("/tmp/human_minutes__test.json"),
        metrics=metrics,
    )
    assert artifact["artifact_type"] == "human_minutes_comparison"
    assert "artifact_kind" not in artifact  # constitution uses artifact_type


def test_comparison_zero_human_items_no_division_error():
    """Zero human items should not crash the F1 calculation."""
    metrics = compare(
        extraction_artifact=_extraction({"action_items": []}),
        human_minutes=_human(),
    )
    assert metrics["total_human_items"] == 0
    assert metrics["over_extraction_ratio"] is None
    assert metrics["f1_vs_human"] == 0.0


def test_comparison_is_deterministic():
    """Two runs over the same artifacts produce equal metrics."""
    human = _human(
        action=[
            {"text": "Submit the revised ERP values.",
             "responsible_party": "DoD"}
        ]
    )
    extraction = _extraction({
        "action_items": [{"action": "Submit the revised ERP values."}],
    })
    m1 = compare(extraction_artifact=extraction, human_minutes=human)
    m2 = compare(extraction_artifact=extraction, human_minutes=human)
    assert m1 == m2


def test_comparison_no_llm_imports():
    """Static scan: the comparison script must not import an LLM SDK."""
    src = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "compare_haiku_vs_human_minutes.py"
    ).read_text(encoding="utf-8")
    forbidden = ("anthropic", "from llm_client", "import llm_client", "openai")
    for token in forbidden:
        assert re.search(re.escape(token), src, re.IGNORECASE) is None, (
            f"forbidden token {token!r} appears in compare_haiku_vs_human_minutes.py"
        )


def test_comparison_extreme_over_extraction_low_precision():
    """An extraction with ~50x the human-item count produces very low precision."""
    human = _human(
        discussion=[{
            "item_number": 1,
            "category": "X",
            "question_topic": "A specific question only one extraction covers.",
            "asked_by": "A",
            "response": "R",
            "follow_up": None,
        }],
        action=[
            {"text": "A specific human action item one.",
             "responsible_party": "X"},
            {"text": "A specific human action item two.",
             "responsible_party": "X"},
            {"text": "A specific human action item three.",
             "responsible_party": "X"},
        ],
        next_steps=["A specific next step from the human minutes."],
    )
    # 250+ extraction items, none of which match human items.
    extraction = _extraction({
        "decisions": [
            {"text": f"unrelated decision number {i}"} for i in range(100)
        ],
        "action_items": [
            {"action": f"unrelated action {i}"} for i in range(100)
        ],
        "open_questions": [
            {"text": f"unrelated question {i}"} for i in range(63)
        ],
    })
    metrics = compare(
        extraction_artifact=extraction, human_minutes=human
    )
    # Over-extraction is extreme.
    assert metrics["over_extraction_ratio"] >= 50
    # Precision must be very low — most extraction items cover nothing.
    assert metrics["precision_vs_human"] < 0.20
