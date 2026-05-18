"""Steps 3-6 gates: hallucination defense, non-empty, within-source,
GT-coverage. Each gate has a happy path AND a rejection path that
proves it fails closed (eval fail -> reason code -> control block ->
unpromoted artifact).
"""
from __future__ import annotations

import json as _json

import pytest

from spectrum_systems_core.evals import (
    EXTRACTION_EMPTY_WITH_CONTENT,
    EXTRACTION_NOT_IN_SOURCE,
    GT_COVERAGE_EVAL_TYPE,
    NONEMPTY_EVAL_TYPE,
    SCHEMA_VIOLATION,
    STRICT_SCHEMA_EVAL_TYPE,
    WITHIN_SOURCE_EVAL_TYPE,
)
from spectrum_systems_core.workflows import run_meeting_minutes_llm_workflow
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    json_stub,
    load_fixture,
    text_stub,
)

DEC18 = load_fixture("dec18_transcript.txt")
PROCEDURAL = load_fixture("procedural_only.txt")


def _eval(result, eval_type):
    matches = [
        e for e in result.eval_results if e.payload.get("eval_type") == eval_type
    ]
    assert len(matches) == 1, f"expected exactly one {eval_type} eval_result"
    return matches[0].payload


def _decision(result) -> str:
    return result.control_decision.payload["decision"]


# ---- Step 3: hallucination defense (empty-content -> no invented items) --


def test_step3_procedural_only_yields_empty_decisions_and_promotes():
    result = run_meeting_minutes_llm_workflow(
        PROCEDURAL,
        client=json_stub(),  # model correctly returns all-empty
    )
    assert result.meeting_minutes.payload["decisions"] == []
    assert result.meeting_minutes.payload["action_items"] == []
    assert result.meeting_minutes.payload["open_questions"] == []
    # No content present -> empty is allowed -> promoted, nothing invented.
    assert result.promoted is True
    assert _decision(result) == "allow"


# ---- Step 4: non-empty extraction required when content present ---------


def test_step4_happy_content_with_items_passes():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    nonempty = _eval(result, NONEMPTY_EVAL_TYPE)
    assert nonempty["status"] == "pass"
    assert result.promoted is True


def test_step4_rejection_forced_empty_on_content_blocks():
    # Dec 18 has known content; a forced-empty extraction must block.
    result = run_meeting_minutes_llm_workflow(DEC18, client=json_stub())
    nonempty = _eval(result, NONEMPTY_EVAL_TYPE)
    assert nonempty["status"] == "fail"
    assert EXTRACTION_EMPTY_WITH_CONTENT in nonempty["reason_codes"]
    assert _decision(result) == "block"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"


# ---- Step 5: within-source attribution ---------------------------------


def test_step5_happy_all_items_in_source():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
    )
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert within["status"] == "pass"
    # 2 decisions + 1 action + 1 question (legacy) + 1 grounded
    # technical_parameters.value (Step 4 structured within-source).
    assert within["items_in_source"] == 5
    assert within["items_not_in_source"] == 0


def test_step5_rejection_injected_non_source_decision_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=[
                "The committee approved a brand new unrelated budget line."
            ],
        ),
    )
    within = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert within["status"] == "fail"
    assert within["items_not_in_source"] >= 1
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE)
        for rc in within["reason_codes"]
    )
    assert _decision(result) == "block"
    assert result.promoted is False


# ---- Step 6: coverage vs human GT pairs (observe-only) ------------------


def _seed_gt_pairs(lake_root, source_id, source_artifact_id, texts_types):
    from tests.integration.fixtures import make_human_minutes_gt_pair

    out = (
        lake_root
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "ground_truth"
        / "human_minutes_gt_pairs.jsonl"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    lines = []
    for text, etype in texts_types:
        pair = make_human_minutes_gt_pair(
            source_id=source_id,
            source_artifact_id=source_artifact_id,
            ground_truth_text=text,
            extraction_type=etype,
        )
        lines.append(_json.dumps(pair, sort_keys=True, separators=(",", ":")))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_step6_coverage_emits_numeric_float_and_threshold(tmp_path):
    source_id = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
    _seed_gt_pairs(
        tmp_path,
        source_id,
        "11111111-1111-4111-8111-111111111111",
        [
            (DEC18_DECISIONS[0], "decision"),
            ("an unrelated claim never extracted", "claim"),
        ],
    )
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
        source_id=source_id,
        lake_root=tmp_path,
    )
    cov = _eval(result, GT_COVERAGE_EVAL_TYPE)
    assert isinstance(cov["coverage_percent"], float)
    assert isinstance(cov["threshold"], float)
    assert cov["threshold"] == 0.0
    # one decision pair covered, one claim pair unmatched -> 0.5
    assert cov["coverage_percent"] == 0.5
    assert cov["status"] == "pass"  # observe-only never blocks
    # Threshold echoed into reason_codes for eval_history auditability.
    assert "coverage_threshold:0.0" in cov["reason_codes"]
    # Observe-only: the overall run is still allowed.
    assert _decision(result) == "allow"
    assert result.promoted is True


def test_step6_no_gt_file_still_numeric_and_passes():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            decisions=DEC18_DECISIONS,
            action_items=DEC18_ACTION_ITEMS,
            open_questions=DEC18_OPEN_QUESTIONS,
            technical_parameters=DEC18_TECHNICAL_PARAMETERS,
        ),
        source_id="missing",
        lake_root=None,
    )
    cov = _eval(result, GT_COVERAGE_EVAL_TYPE)
    assert isinstance(cov["coverage_percent"], float)
    assert cov["coverage_percent"] == 0.0
    assert cov["status"] == "pass"


# ---- Step 2 strict schema (rejection here; happy path in contract test) -


def test_step2_rejection_malformed_raw_string_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=text_stub("this is not json at all")
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert any(
        rc.startswith(SCHEMA_VIOLATION) for rc in strict["reason_codes"]
    )
    assert _decision(result) == "block"
    assert result.promoted is False
    assert result.meeting_minutes.status == "rejected"


def test_step2_rejection_json_string_not_object_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=text_stub('"a bare json string"')
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert _decision(result) == "block"


def test_step2_rejection_missing_one_array_blocks():
    # Valid object but missing open_questions -> schema_violation.
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=text_stub('{"decisions": [], "action_items": []}'),
    )
    strict = _eval(result, STRICT_SCHEMA_EVAL_TYPE)
    assert strict["status"] == "fail"
    assert any(
        "missing_array" in rc for rc in strict["reason_codes"]
    )
    assert _decision(result) == "block"
    assert result.promoted is False


# ---- Option C: 34-chunk regression — object decision, no verb ----------
#
# At full-transcript scale the model emits object-form decisions (to
# attach stakeholders / confidence, which the prompt encourages) and
# does not always supply a verb. Before the fix the regulatory_verb gate
# hard-blocked the whole run with verb_not_classified:__missing__ even
# though the IDENTICAL decision as a plain string promotes. These tests
# pin the fix AND its no-weakening boundary.

# Verbatim substring of dec18_transcript.txt with NO taxonomy verb.
_INSRC_NO_VERB = "DoD has a concern about the aggregate interference methodology"


def _base_kwargs(decisions):
    return dict(
        decisions=decisions,
        action_items=DEC18_ACTION_ITEMS,
        open_questions=DEC18_OPEN_QUESTIONS,
        technical_parameters=DEC18_TECHNICAL_PARAMETERS,
    )


def test_optionc_object_decision_without_verb_now_promotes():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([
                DEC18_DECISIONS[0],
                {"text": _INSRC_NO_VERB,
                 "stakeholders": ["DoD"], "confidence": 0.6},
            ])
        ),
    )
    assert result.promoted is True
    assert _decision(result) == "allow"
    # The indeterminate verb is recorded ON the artifact (auditable
    # field), not silently dropped.
    decisions = result.meeting_minutes.payload["decisions"]
    stamped = [
        d for d in decisions
        if isinstance(d, dict) and d.get("verb") == "unclassified"
    ]
    assert len(stamped) == 1
    verb = _eval(result, "regulatory_verb")
    assert verb["status"] == "pass"
    assert any(
        rc.startswith("verb_unclassified:") for rc in verb["reason_codes"]
    )


def test_optionc_no_weakening_claimed_garbage_verb_still_blocks():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([
                DEC18_DECISIONS[0],
                {"text": _INSRC_NO_VERB, "verb": "frobnicated"},
            ])
        ),
    )
    assert result.promoted is False
    assert _decision(result) == "block"
    verb = _eval(result, "regulatory_verb")
    assert verb["status"] == "fail"
    assert any(
        rc.startswith("verb_not_classified:frobnicated")
        for rc in verb["reason_codes"]
    )


def test_optionc_string_form_decisions_byte_identical_no_sentinel():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(**_base_kwargs([DEC18_DECISIONS[0], _INSRC_NO_VERB])),
    )
    assert result.promoted is True
    decisions = result.meeting_minutes.payload["decisions"]
    assert all(isinstance(d, str) for d in decisions)


def test_optionc_text_derived_verb_is_not_overridden_by_sentinel():
    # Object decision, no `verb` key, but text contains "approved" —
    # the existing text-derived classification must still apply; the
    # producer must NOT stamp the sentinel over a classifiable decision.
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([{"text": DEC18_DECISIONS[0]}])
        ),
    )
    assert result.promoted is True
    decision = result.meeting_minutes.payload["decisions"][0]
    assert "verb" not in decision  # untouched — text already classifies
    verb = _eval(result, "regulatory_verb")
    assert verb["status"] == "pass"
    assert verb["reason_codes"] == []


# ---- Prompt↔eval taxonomy-drift regression (BLOCK 1 real root cause) ----
#
# The verb sentinel (Option C above) only fires when NO verb is
# classifiable. The actual production block was different: the model,
# doing exactly what the prompt instructs, emits an object decision with
# a real canonical regulatory verb (``adopted`` / ``authorized`` /
# ``ratified`` / ...). The eval used a 6-verb ad-hoc subset, so those
# correctly-extracted decisions hard-blocked with
# ``verb_not_classified:<verb>``. The fix widens the eval's classified
# set to the canonical ``REGULATORY_VERBS`` taxonomy the prompt uses.


def test_canonical_regulatory_verb_adopted_promotes_through_workflow():
    """An object decision whose verb is a canonical regulatory verb the
    prompt explicitly sanctions (``adopted``) must promote — it is NOT
    the sentinel path (the verb is claimed and recognised)."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([
                {"text": DEC18_DECISIONS[0], "verb": "adopted"},
            ])
        ),
    )
    assert result.promoted is True
    assert _decision(result) == "allow"
    verb = _eval(result, "regulatory_verb")
    assert verb["status"] == "pass"
    # NOT routed through the indeterminate sentinel — a real verb is
    # classified, so there is no verb_unclassified surface note.
    assert verb["reason_codes"] == []
    decision = result.meeting_minutes.payload["decisions"][0]
    assert decision["verb"] == "adopted"  # claimed verb left intact


def test_canonical_drift_fix_does_not_weaken_garbage_verb_still_blocks():
    """No-weakening boundary: widening to the canonical taxonomy must
    NOT admit a verb the taxonomy does not contain. A hallucinated verb
    on a within-source-grounded decision still hard-blocks."""
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([
                {"text": DEC18_DECISIONS[0], "verb": "bamboozled"},
            ])
        ),
    )
    assert result.promoted is False
    assert _decision(result) == "block"
    verb = _eval(result, "regulatory_verb")
    assert verb["status"] == "fail"
    assert any(
        rc.startswith("verb_not_classified:bamboozled")
        for rc in verb["reason_codes"]
    )


# ---- Verbatim-extraction prompt regression (BLOCK 2) -------------------
#
# The within-source gate requires the extracted text to be a normalized
# substring of the transcript. The prompt previously licensed
# "near-verbatim" text; the fix removes that license for every
# within-source-checked field. The gate itself is unchanged — these
# tests pin that a paraphrased item still hard-blocks (gate not
# weakened) while a verbatim item promotes.


def test_block2_paraphrased_decision_text_still_blocks_within_source():
    paraphrase = "The group OK'd a downlink power limit of about -47 dBm."
    assert paraphrase not in DEC18  # genuinely not a verbatim span
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([{"text": paraphrase, "verb": "approved"}])
        ),
    )
    assert result.promoted is False
    assert _decision(result) == "block"
    ws = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert ws["status"] == "fail"
    assert any(
        rc.startswith(EXTRACTION_NOT_IN_SOURCE) for rc in ws["reason_codes"]
    )


def test_block2_verbatim_decision_text_passes_within_source():
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([{"text": DEC18_DECISIONS[0], "verb": "adopted"}])
        ),
    )
    assert result.promoted is True
    assert _decision(result) == "allow"
    ws = _eval(result, WITHIN_SOURCE_EVAL_TYPE)
    assert ws["status"] == "pass"
    assert ws["items_not_in_source"] == 0


# ---- _normalize: invisible-char hardening (binding match algorithm) ----
#
# Regression for chunk-67 (t0066): a transcription / copy-paste pipeline
# splices an invisible zero-width / soft-hyphen / BOM character next to
# the space between stutter-repeated words ("that <ZWSP>that",
# "our<SHY> our"). The extracted text is verbatim to a human reader but
# was not a normalized substring of the raw transcript, so
# extraction_within_source_required failed. _normalize now NFKC-folds
# compatibility forms and deletes invisible format chars. These tests
# pin the fix AND that it does not weaken the gate. Invisible / compat
# code points are spelled with \u escapes so the source stays ASCII and
# editor-safe.

ZWSP = "\u200b"  # zero width space
SHY = "\u00ad"  # soft hyphen
BOM = "\ufeff"  # zero width no-break space (BOM)
NBSP = "\u00a0"  # non-breaking space
LIGATURE_FI = "\ufb01"  # 'fi' compatibility ligature
FULLWIDTH_5 = "\uff15"  # full-width digit five


def test_normalize_strips_invisible_chars_so_verbatim_text_matches():
    from spectrum_systems_core.evals.llm_extraction import _normalize

    transcript = (
        "Kerry and I will be collaborating to make sure that "
        f"{ZWSP}that we have the data that we needed.{BOM} "
        f"And we will share our{SHY} our code for doing the analysis."
    )
    hay = _normalize(transcript)
    assert (
        _normalize(
            "Kerry and I will be collaborating to make sure that that "
            "we have the data that we needed"
        )
        in hay
    )
    assert (
        _normalize("we will share our our code for doing the analysis")
        in hay
    )


def test_normalize_folds_compatibility_forms():
    from spectrum_systems_core.evals.llm_extraction import _normalize

    # NBSP -> space, ligature 'fi' -> "fi", full-width '5' -> "5".
    assert _normalize(f"a{NBSP}b c") == "a b c"
    assert _normalize(f"{BOM}{LIGATURE_FI}le {FULLWIDTH_5}G") == "file 5g"


def test_normalize_does_not_weaken_gate_genuine_diffs_still_fail():
    from spectrum_systems_core.evals.llm_extraction import _normalize

    hay = _normalize(
        "we will share our our code for doing the analysis, so we have "
        "some consistent answers"
    )
    # Paraphrase, a dropped word, a dropped comma, and a swapped token
    # must all remain non-substrings: invisible-char folding must never
    # let a non-verbatim extraction through.
    paraphrase = "we will share our code so answers stay consistent"
    dropped_word = (
        "we will share our our code for the analysis, so we have some "
        "consistent answers"
    )
    dropped_comma = (
        "we will share our our code for doing the analysis so we have "
        "some consistent answers"
    )
    swapped_token = (
        "we will share our our script for doing the analysis, so we "
        "have some consistent answers"
    )
    for bad in (paraphrase, dropped_word, dropped_comma, swapped_token):
        assert _normalize(bad) not in hay


def test_normalize_is_idempotent_and_stable():
    from spectrum_systems_core.evals.llm_extraction import _normalize

    for s in [
        f"X{ZWSP}{BOM}{SHY}Y",
        f"{BOM}{LIGATURE_FI}le {FULLWIDTH_5}G",
        "Kerry and I will be collaborating",
        "",
    ]:
        assert _normalize(_normalize(s)) == _normalize(s)
        assert _normalize(s) == _normalize(s)

# ---- Full-transcript output-truncation regression ----------------------
#
# At full-transcript scale the schema_version 1.3.0 extraction (~24
# structured arrays) exceeds a too-small client token budget and is cut
# off mid-emit into invalid JSON. _parse_llm_payload returns None and
# the producer emits a base payload with NO decisions/action_items/
# open_questions, so required_meeting_minutes_fields + regulatory_verb +
# llm_extraction_strict_schema (+ nonempty) ALL fail together. This is
# the reported production block; the PR #144 verb sentinel cannot help
# because parsed is None. These two tests pin both the fail-closed
# block AND that an untruncated full extraction promotes.


def _truncated(client) -> str:
    """A full valid extraction severed mid-emit (what a token-cap cut
    does), so json.loads() raises -> _parse_llm_payload returns None."""
    full = client(system="s", user="\n".join(f"[t{i:04d}] x" for i in range(3)))
    cut = full[: len(full) - 120]
    with pytest.raises(_json.JSONDecodeError):
        _json.loads(cut)
    return cut


def test_full_scale_output_truncation_blocks_fail_closed():
    full_client = json_stub(
        **_base_kwargs([
            DEC18_DECISIONS[0],
            {"text": _INSRC_NO_VERB, "stakeholders": ["DoD"], "confidence": 0.6},
        ])
    )
    result = run_meeting_minutes_llm_workflow(
        DEC18, client=text_stub(_truncated(full_client))
    )
    assert result.promoted is False
    assert _decision(result) == "block"
    failed = {
        e.payload["eval_type"]
        for e in result.eval_results
        if e.payload.get("status") == "fail"
    }
    # The exact reported production reason codes, all together.
    assert {
        "required_meeting_minutes_fields",
        "regulatory_verb",
        STRICT_SCHEMA_EVAL_TYPE,
    }.issubset(failed)


def test_full_scale_untruncated_extraction_promotes():
    # Same full extraction, NOT truncated -> the governed loop promotes
    # it on merit; the PR #144 sentinel composes (object decision with
    # no verb gets the explicit unclassified marker).
    result = run_meeting_minutes_llm_workflow(
        DEC18,
        client=json_stub(
            **_base_kwargs([
                DEC18_DECISIONS[0],
                {"text": _INSRC_NO_VERB,
                 "stakeholders": ["DoD"], "confidence": 0.6},
            ])
        ),
    )
    assert result.promoted is True
    assert _decision(result) == "allow"
    stamped = [
        d for d in result.meeting_minutes.payload["decisions"]
        if isinstance(d, dict) and d.get("verb") == "unclassified"
    ]
    assert len(stamped) == 1
