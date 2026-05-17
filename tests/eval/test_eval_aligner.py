"""Tests for EvalAligner (Phase M.4)."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from spectrum_systems_core.evals.m4 import EvalAligner

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "eval"


def _load_schema(name: str) -> dict:
    return json.loads(
        (CONTRACT_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    )


def test_coverage_alignment_finds_matching_item() -> None:
    """A minutes item with strong semantic + lexical overlap matches."""
    aligner = EvalAligner()
    minutes_text = (
        "DECISION: Group agreed to apply ITU two-point criteria for FSS "
        "protection analysis."
    )
    extracted = [
        {
            "id": "ex-1",
            "text": (
                "Decision: the working group agreed to apply ITU two-point "
                "criteria for FSS protection analysis going forward."
            ),
        }
    ]
    result = aligner.align(
        extracted_items=extracted,
        minutes_text=minutes_text,
        source_id="src-1",
        minutes_artifact_id="min-1",
    )
    cov = result["coverage_alignments"]
    assert len(cov) == 1
    assert cov[0]["alignment_status"] == "matched"
    assert cov[0]["semantic_similarity"] >= EvalAligner.SEMANTIC_THRESHOLD
    assert cov[0]["content_word_overlap"] >= EvalAligner.MIN_CONTENT_WORD_OVERLAP


def test_short_item_requires_exact_lexical_match() -> None:
    """Short items reject matches that disagree on the anchor (owner)."""
    aligner = EvalAligner()
    minutes_text = "ACTION: DoW to provide FSS template by Friday."
    extracted = [
        {
            "id": "ex-1",
            "text": "Action: NTIA to provide FSS template by Friday.",
        }
    ]
    result = aligner.align(
        extracted_items=extracted,
        minutes_text=minutes_text,
        source_id="src-2",
        minutes_artifact_id="min-2",
    )
    cov = result["coverage_alignments"]
    assert cov[0]["alignment_status"] == "unmatched", (
        "Owner mismatch (DoW vs NTIA) on a short item must NOT match -- "
        "domain-term anchors disagree."
    )


def test_unmatched_extracted_item_gets_requires_review_status() -> None:
    """Extracted items without a minutes match land in the review queue."""
    aligner = EvalAligner()
    minutes_text = "DECISION: Approve the ITU criteria."
    extracted = [
        {
            "id": "ex-only-stuff",
            "text": (
                "Side conversation about meeting logistics with no decision."
            ),
        }
    ]
    result = aligner.align(
        extracted_items=extracted,
        minutes_text=minutes_text,
        source_id="src-3",
        minutes_artifact_id="min-3",
    )
    reviews = result["review_alignments"]
    assert len(reviews) == 1
    assert reviews[0]["alignment_status"] == "requires_review"
    # Critical naming: must NOT use hallucination / spurious_add.
    assert reviews[0]["alignment_status"] != "hallucination"


def test_semantic_threshold_enforced_on_long_items() -> None:
    """A long item below 0.7 cosine does not match even with some overlap."""
    aligner = EvalAligner()
    # Two long sentences that share a few terms but have very different meaning.
    minutes_text = (
        "DECISION: The committee voted to defer the entire EPFD methodology "
        "discussion to the spring working group session in March because "
        "additional propagation modelling data was requested by member states."
    )
    extracted = [
        {
            "id": "ex-mismatch",
            "text": (
                "The committee briefly discussed lunch arrangements for the "
                "spring working group session in March, with several attendees "
                "expressing food preferences and the secretariat noting "
                "catering deadlines."
            ),
        }
    ]
    result = aligner.align(
        extracted_items=extracted,
        minutes_text=minutes_text,
        source_id="src-4",
        minutes_artifact_id="min-4",
    )
    cov = result["coverage_alignments"]
    assert cov[0]["alignment_status"] == "unmatched"
    # Sanity: the test only proves what we want if the similarity really
    # is below the threshold. If similarity creeps above 0.7 the test is
    # no longer exercising the threshold and needs updating.
    assert cov[0]["semantic_similarity"] < EvalAligner.SEMANTIC_THRESHOLD


def test_alignment_result_schema_validates() -> None:
    """Aligner output validates against the alignment_result schema."""
    schema = _load_schema("alignment_result")
    aligner = EvalAligner()
    result = aligner.align(
        extracted_items=[
            {"id": "ex-1", "text": "Decision: approve the test plan."}
        ],
        minutes_text="DECISION: Approve the test plan for this quarter.",
        source_id="src-5",
        minutes_artifact_id="min-5",
        chunking_strategy="speaker_turn",
    )
    jsonschema.Draft202012Validator(schema).validate(result)
    assert result["artifact_type"] == "alignment_result"
    assert result["chunking_strategy"] == "speaker_turn"


def test_empty_minutes_text_yields_zero_coverage_not_vacuous_match() -> None:
    """Empty minutes_text means coverage is 0 across zero items.

    Critical safety property: an empty paired minutes document must
    never produce a vacuously perfect coverage (which would let any
    pair pass the gate by submitting an empty minutes file).
    """
    aligner = EvalAligner()
    result = aligner.align(
        extracted_items=[
            {"id": "ex-1", "text": "Decision: deploy version 2."}
        ],
        minutes_text="",
        source_id="src-6",
        minutes_artifact_id="min-6",
    )
    assert result["coverage_alignments"] == []
    # The review side still records every extracted item as requires_review.
    assert all(
        r["alignment_status"] == "requires_review"
        for r in result["review_alignments"]
    )


def test_aligner_never_raises_on_garbage_input() -> None:
    """Aligner returns a partial result even on garbage extracted_items."""
    aligner = EvalAligner()
    result = aligner.align(
        extracted_items=[None, {}, {"id": "x"}, {"text": ""}, {"text": "  "}],  # type: ignore[list-item]
        minutes_text="DECISION: Some decision.",
        source_id="src-7",
        minutes_artifact_id="min-7",
    )
    assert result["artifact_type"] == "alignment_result"
    # All extracted_items are invalid -> review_alignments empty.
    assert result["review_alignments"] == []
    # Coverage side still records the minutes item as unmatched.
    assert len(result["coverage_alignments"]) == 1
    assert result["coverage_alignments"][0]["alignment_status"] == "unmatched"
