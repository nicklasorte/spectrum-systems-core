"""Tests for the Phase Z.2 regulatory verb classification eval."""
from __future__ import annotations

import pytest

from spectrum_systems_core.artifacts import new_artifact
from spectrum_systems_core.control import decide_control
from spectrum_systems_core.config.taxonomy import UNCLASSIFIED_DECISION_VERB
from spectrum_systems_core.evals import (
    DECISIONS_FIELD_MISSING,
    REGULATORY_VERB_EVAL_TYPE,
    VERB_AMBIGUOUS_PREFIX,
    VERB_NOT_CLASSIFIED_PREFIX,
    VERB_UNCLASSIFIED_PREFIX,
    resolve_decision_verb,
    run_regulatory_verb_eval,
    run_required_evals,
)


def _meeting_minutes(decisions: list | None, *, include_field: bool = True):
    payload: dict = {
        "title": "x",
        "summary": "x",
        "action_items": [],
        "open_questions": [],
        "schema_version": "1.1.0",
    }
    if include_field:
        payload["decisions"] = decisions if decisions is not None else []
    return new_artifact(
        artifact_type="meeting_minutes",
        payload=payload,
        trace_id="trace-test",
        status="draft",
    )


# ---- happy paths ---------------------------------------------------------


def test_canonical_verb_approved_passes():
    artifact = _meeting_minutes(
        [{"text": "The FCC approved the framework.", "verb": "approved"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["eval_type"] == REGULATORY_VERB_EVAL_TYPE
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_canonical_verb_deferred_passes():
    artifact = _meeting_minutes(
        [{"text": "NTIA deferred the review.", "verb": "deferred"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_canonical_regulatory_verb_adopted_passes():
    """Regression for the prompt↔eval taxonomy-drift block: a verb the
    extraction prompt explicitly instructs the model to emit (``adopted``
    is in the canonical ``REGULATORY_VERBS``) must classify as a real
    decision verb, not hard-block with ``verb_not_classified``."""
    artifact = _meeting_minutes(
        [{"text": "The committee adopted the band plan.", "verb": "adopted"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_every_canonical_regulatory_verb_passes():
    """Every verb the canonical taxonomy (and the prompt) sanctions must
    pass the gate. Pins the no-drift property: the eval's classified set
    is the canonical taxonomy, not an ad-hoc subset."""
    from spectrum_systems_core.config.taxonomy import REGULATORY_VERBS

    for verb in REGULATORY_VERBS:
        artifact = _meeting_minutes(
            [{"text": f"The committee {verb} the band plan.", "verb": verb}]
        )
        result = run_regulatory_verb_eval(artifact)
        # ``recommended`` is also an AMBIGUOUS verb → it passes WITH a
        # warn; every other canonical verb passes cleanly. Neither
        # blocks.
        assert result.payload["status"] == "pass", verb
        assert not any(
            r.startswith(VERB_NOT_CLASSIFIED_PREFIX)
            for r in result.payload["reason_codes"]
        ), verb


def test_recommended_is_both_regulatory_and_ambiguous_still_warns():
    """No-weakening of the operator signal: ``recommended`` is in
    REGULATORY_VERBS ∩ AMBIGUOUS_VERBS. Widening the classified set must
    NOT make it pass silently — the ambiguous-warn is checked first so
    the operator still sees the informal-language signal."""
    artifact = _meeting_minutes(
        [{"text": "The chair recommended the change.", "verb": "recommended"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert any(
        r.startswith(VERB_AMBIGUOUS_PREFIX)
        for r in result.payload["reason_codes"]
    )


def test_ambiguous_verb_discussed_warns_but_passes():
    artifact = _meeting_minutes(
        [{"text": "The committee discussed the amendment.",
          "verb": "discussed"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    # The reason_codes carry the warn for operator visibility.
    assert any(
        r.startswith(VERB_AMBIGUOUS_PREFIX)
        for r in result.payload["reason_codes"]
    )


def test_non_decision_artifact_type_passes_immediately():
    artifact = new_artifact(
        artifact_type="agency_question_summary",
        payload={"question": "x"},
        trace_id="trace-test",
        status="draft",
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert result.payload["reason_codes"] == []


def test_verb_extracted_from_text_when_no_verb_field():
    # The eval falls back to scanning text for a taxonomy verb when the
    # ``verb`` field is absent. "approved" appears, so the eval passes.
    artifact = _meeting_minutes(
        [{"text": "The FCC approved the framework."}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"


def test_empty_decisions_list_passes():
    # Zero decisions is a pass for THIS eval. The required-field eval
    # owns the "decisions must exist" claim.
    artifact = _meeting_minutes([])
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"


# ---- Option C: explicit indeterminate-verb sentinel ----------------------


def test_unclassified_sentinel_does_not_block_and_is_surfaced():
    """The explicit producer sentinel is non-blocking (the identical
    decision in plain-string form already promotes) but the gap stays
    auditable via a distinct reason code."""
    artifact = _meeting_minutes(
        [{"text": "DoD has a concern about the methodology.",
          "verb": UNCLASSIFIED_DECISION_VERB}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass"
    assert any(
        r.startswith(VERB_UNCLASSIFIED_PREFIX)
        for r in result.payload["reason_codes"]
    )


def test_unclassified_sentinel_mixed_with_garbage_verb_still_blocks():
    """No-weakening: the sentinel never rescues a CLAIMED, unrecognised
    verb on another decision — that still hard-blocks the whole eval."""
    artifact = _meeting_minutes(
        [
            {"text": "Indeterminate item.",
             "verb": UNCLASSIFIED_DECISION_VERB},
            {"text": "Chair grumbled.", "verb": "grumbled"},
        ]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}grumbled")
        for r in result.payload["reason_codes"]
    )
    # The sentinel note is still surfaced alongside the block.
    assert any(
        r.startswith(VERB_UNCLASSIFIED_PREFIX)
        for r in result.payload["reason_codes"]
    )


def test_unclassified_sentinel_routes_to_allow_through_decide_control():
    artifact = _meeting_minutes(
        [{"text": "x", "verb": UNCLASSIFIED_DECISION_VERB}]
    )
    verb_eval = run_regulatory_verb_eval(artifact)
    decision = decide_control(artifact, [verb_eval])
    assert decision.payload["decision"] == "allow"


def test_resolve_decision_verb_is_the_shared_resolution_function():
    """The producer imports this exact function so it cannot drift from
    the gate. ``None`` is precisely the __missing__ block condition."""
    assert resolve_decision_verb({"text": "no verb here at all"}) is None
    assert resolve_decision_verb(
        {"text": "x", "verb": "approved"}
    ) == "approved"
    assert resolve_decision_verb(
        {"text": "The FCC approved the plan."}
    ) == "approved"


# ---- rejection paths -----------------------------------------------------


def test_unrecognized_verb_blocks_with_verb_not_classified():
    artifact = _meeting_minutes(
        [{"text": "Someone mumbled at the meeting.", "verb": "mumbled"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}mumbled")
        for r in result.payload["reason_codes"]
    )


def test_unclassified_verb_routes_to_block_through_decide_control():
    """Fail-closed: a fail status must translate to a ``block``
    decision via the control function, not a silent pass."""
    artifact = _meeting_minutes(
        [{"text": "x", "verb": "wibbled"}]
    )
    verb_eval = run_regulatory_verb_eval(artifact)
    decision = decide_control(artifact, [verb_eval])
    assert decision.payload["decision"] == "block"
    assert any(
        f"failed:{REGULATORY_VERB_EVAL_TYPE}" in r
        for r in decision.payload["reason_codes"]
    )


def test_decision_with_no_verb_and_no_taxonomy_word_in_text_blocks():
    artifact = _meeting_minutes(
        [{"text": "Nothing actionable was said about chocolate."}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}__missing__")
        for r in result.payload["reason_codes"]
    )


def test_mixed_verbs_one_canonical_one_unclassified_blocks_on_unclassified():
    artifact = _meeting_minutes(
        [
            {"text": "FCC approved the framework.", "verb": "approved"},
            {"text": "Chair grumbled.", "verb": "grumbled"},
        ]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        r.startswith(f"{VERB_NOT_CLASSIFIED_PREFIX}grumbled")
        for r in result.payload["reason_codes"]
    )


def test_missing_decisions_field_blocks_with_specific_reason_code():
    artifact = _meeting_minutes(decisions=None, include_field=False)
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert DECISIONS_FIELD_MISSING in result.payload["reason_codes"]


def test_non_list_decisions_field_blocks():
    artifact = new_artifact(
        artifact_type="meeting_minutes",
        payload={
            "title": "x", "summary": "x",
            "decisions": "not a list",
            "action_items": [], "open_questions": [],
            "schema_version": "1.1.0",
        },
        trace_id="trace-test",
        status="draft",
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"


# ---- integration with run_required_evals -------------------------------


def test_run_required_evals_includes_regulatory_verb_for_meeting_minutes():
    artifact = _meeting_minutes(
        [{"text": "FCC approved.", "verb": "approved"}]
    )
    results = run_required_evals(artifact)
    eval_types = [r.payload["eval_type"] for r in results]
    assert REGULATORY_VERB_EVAL_TYPE in eval_types


def test_run_required_evals_includes_regulatory_verb_for_decision_brief():
    artifact = new_artifact(
        artifact_type="decision_brief",
        payload={
            "title": "x", "context": "x", "options": [],
            "recommendation": "x", "rationale": "x",
            "decisions": [
                {"text": "FCC approved framework.", "verb": "approved"}
            ],
            "schema_version": "1.1.0",
        },
        trace_id="trace-test",
        status="draft",
    )
    results = run_required_evals(artifact)
    eval_types = [r.payload["eval_type"] for r in results]
    assert REGULATORY_VERB_EVAL_TYPE in eval_types


def test_run_required_evals_skips_regulatory_verb_for_non_decision_types():
    artifact = new_artifact(
        artifact_type="agency_question_summary",
        payload={
            "title": "x", "agency": "FCC", "question": "?",
            "summary": "x", "citations": [],
            "schema_version": "1.1.0",
        },
        trace_id="trace-test",
        status="draft",
    )
    results = run_required_evals(artifact)
    eval_types = [r.payload["eval_type"] for r in results]
    assert REGULATORY_VERB_EVAL_TYPE not in eval_types


# ---- prompt↔eval taxonomy alignment (the persistent block, fixed) -------
#
# Root cause of the 6-PR meeting_minutes_llm full-transcript block: the
# extraction prompt's own decision definition sanctions "agreed" /
# "decided" (and direct decision synonyms) and instructs the model to
# emit the governing verb actually used in the transcript, but the
# regulatory_verb eval's classified-pass set omitted them — so a
# correctly-extracted OBJECT-form decision the model faithfully
# labelled "agreed" hard-blocked while the IDENTICAL decision in
# plain-STRING form promoted (string decisions are never verb-checked).
# These pin the alignment AND the no-weakening invariants.

import pathlib  # noqa: E402
import re  # noqa: E402

from spectrum_systems_core.config.taxonomy import (  # noqa: E402
    DECISION_SYNONYM_VERBS,
)
from spectrum_systems_core.evals import (  # noqa: E402
    CLASSIFIED_DECISION_VERBS,
)

_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)


@pytest.mark.parametrize("verb", sorted(DECISION_SYNONYM_VERBS))
def test_prompt_sanctioned_decision_synonym_promotes_object_form(verb):
    """Every DECISION_SYNONYM_VERBS member, on an object-form decision,
    passes the gate (no block) — the fix for the persistent
    regulatory_verb hard-block."""
    artifact = _meeting_minutes(
        [{"text": "The group agreed on the threshold.", "verb": verb}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "pass", (verb, result.payload)
    assert not any(
        c.startswith(VERB_NOT_CLASSIFIED_PREFIX)
        for c in result.payload["reason_codes"]
    ), (verb, result.payload["reason_codes"])


def test_object_agreed_decision_no_longer_blocks_control():
    """End-to-end: an object decision verb='agreed' -> allow (was the
    persistent ``failed:regulatory_verb`` block)."""
    artifact = _meeting_minutes(
        [{"text": "The group approved the 7 GHz threshold.",
          "verb": "agreed"}]
    )
    results = run_required_evals(artifact)
    decision = decide_control(artifact, results)
    assert decision.payload["decision"] == "allow", decision.payload


def test_prompt_recognized_decision_verbs_match_eval_no_drift():
    """CLAUDE.md anti-drift: every decision verb the prompt enumerates
    as 'recognized' MUST be in the eval's classified-pass set. The
    meeting_minutes_llm prompt is a static markdown file (it cannot
    import the taxonomy), so this test is the structural pin that the
    prompt and the eval cannot silently drift apart again."""
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"recognized decision verbs are:\s*(.+?)\.\s", text, re.DOTALL
    )
    assert m, "prompt no longer enumerates the recognized decision verbs"
    verbs = [
        v.strip().lower()
        for v in m.group(1).replace("\n", " ").split(",")
        if v.strip()
    ]
    assert len(verbs) >= 19, verbs
    missing = [v for v in verbs if v not in CLASSIFIED_DECISION_VERBS]
    assert not missing, (
        f"prompt sanctions decision verbs the eval would BLOCK "
        f"(prompt↔eval drift): {missing}"
    )
    # And the curated synonym set is fully reflected in the prompt.
    assert DECISION_SYNONYM_VERBS.issubset(set(verbs)), (
        DECISION_SYNONYM_VERBS - set(verbs)
    )


def test_no_weakening_garbage_verb_still_blocks_after_alignment():
    """A hallucinated / garbage verb absent from the curated taxonomy
    still falls through to the fail-closed block — widening the
    classified set with prompt-sanctioned verbs did NOT weaken the
    hallucination defence."""
    artifact = _meeting_minutes(
        [{"text": "The group frobnicated the band plan.",
          "verb": "frobnicated"}]
    )
    result = run_regulatory_verb_eval(artifact)
    assert result.payload["status"] == "fail"
    assert any(
        c.startswith(VERB_NOT_CLASSIFIED_PREFIX)
        for c in result.payload["reason_codes"]
    )
