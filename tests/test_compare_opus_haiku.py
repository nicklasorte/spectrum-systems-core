"""Unit tests for the deterministic Haiku-vs-Opus comparison engine.

Gate (task spec): every test here must pass before System 2 work
begins. These defend the trust properties of the diff: symmetric
match, no double-counting, zero-division safety, provenance gating,
and append-only eval_history.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compare_opus_haiku as cmp  # noqa: E402


# --------------------------------------------------------------------------
# Zero-LLM static guarantee.
# --------------------------------------------------------------------------
def test_source_has_zero_llm_calls() -> None:
    """Static scan: the comparison script never touches a model."""
    src = (SCRIPTS / "compare_opus_haiku.py").read_text(encoding="utf-8")
    forbidden = [
        "import anthropic",
        "anthropic.Anthropic",
        "llm_client",
        "AnthropicJSONClient",
        "messages.create",
        ".complete(",
    ]
    for token in forbidden:
        assert token not in src, f"LLM token {token!r} present in script"


def test_primary_text_map_matches_baseline_producer() -> None:
    """The Haiku text reader must mirror the Opus baseline producer.

    An asymmetric reader would make the diff lie. Importing the baseline
    producer here (in a test, never in the script) proves the two
    readers are identical so they cannot drift. The behavioral check
    below is the real trust property: the vestigial per-type maps stayed
    byte-equal while ``_item_text`` had silently drifted from the
    producer (it dropped object-form ``decisions`` as ``''`` because the
    producer stopped consulting ``_LEGACY_OBJECT_TEXT_FIELD`` and started
    using the tolerant priority list) — which read 0 Haiku items off a
    real, fully-grounded artifact.
    """
    import create_opus_reference_baselines as crb  # noqa: WPS433

    # Vestigial cross-script mirror — still defined identically.
    assert cmp._PRIMARY_TEXT_FIELD == crb._PRIMARY_TEXT_FIELD
    assert cmp._LEGACY_OBJECT_TEXT_FIELD == crb._LEGACY_OBJECT_TEXT_FIELD
    # The resolver the script actually uses must be byte-identical to
    # the producer's, and its OUTPUT must match for every item shape the
    # canonical extraction prompt can return.
    assert cmp._GROUND_TRUTH_TEXT_FIELDS == crb._GROUND_TRUTH_TEXT_FIELDS
    representative = [
        ("decisions", "a verbatim decision string"),
        ("decisions", {"text": "obj decision", "verb": "approved"}),
        ("decisions", {"text": "d", "verb": "x", "stakeholders": ["A"],
                       "confidence": 0.9, "rationale": "because"}),
        ("action_items", {"action": "submit ERP", "status": "open"}),
        ("open_questions",
         {"question_id": "q1", "question_text": "what distance?"}),
        ("commitments",
         {"commitment_id": "c1", "owner": "X",
          "commitment_text": "I will file", "source_speaker": "X"}),
        ("risks", {"risk_id": "r1", "risk_text": "interference",
                   "raised_by": "Y"}),
        ("technical_parameters",
         {"param_id": "p1", "parameter_name": "ERP", "value": "30 dBW"}),
        ("dissent_or_objection",
         {"dissent_id": "d1", "objector": "Z", "agency": "A",
          "objection_text": "object to scope", "objection_topic": "scope"}),
        ("topics", {"topic_id": "t1", "title": "interference"}),
    ]
    for etype, item in representative:
        assert cmp._item_text(etype, item) == crb.extract_ground_truth_text(
            item, etype
        ), (etype, item)


def test_object_form_decisions_are_read_not_dropped() -> None:
    """Regression: object-form ``decisions`` must produce items > 0.

    The real Haiku/LLM artifact returns object-form decisions (the
    prompt encourages the object form and the workflow stamps a ``verb``
    onto every object decision). Before the fix ``_item_text`` returned
    ``''`` for them, so ``compute_comparison`` reported 0 Haiku items and
    0.0 recall against an Opus baseline built from the SAME items — a
    lying diff on a real, promoted, fully-grounded artifact.
    """
    obj_decision = {
        "text": "The group approved the 7 GHz downlink threshold.",
        "verb": "approved",
        "stakeholders": ["DoD"],
        "confidence": 0.9,
    }
    assert cmp._item_text("decisions", obj_decision) == obj_decision["text"]

    import create_opus_reference_baselines as crb  # noqa: WPS433

    baseline = [
        {
            "extraction_type": "decisions",
            "ground_truth_text": crb.extract_ground_truth_text(
                obj_decision, "decisions"
            ),
            "model_id": "claude-opus-4-6",
        }
    ]
    haiku_payload = {
        "decisions": [obj_decision],
        "action_items": [],
        "open_questions": [],
        # A populated grounding array is the operator-visible signal the
        # artifact really did extract content (the real run had 178).
        "grounding": [
            {
                "kind": "decision",
                "text": obj_decision["text"],
                "source_turns": ["t0001"],
            }
        ],
    }
    s = cmp.compute_comparison(
        baseline_rows=baseline,
        haiku_payload=haiku_payload,
        gt_pairs=None,
        types=cmp.extraction_types(),
    )["summary"]
    assert s["total_haiku_items"] == 1, s
    assert s["true_positives"] == 1, s
    assert s["haiku_recall_vs_opus"] == 1.0, s


# --------------------------------------------------------------------------
# text_match.
# --------------------------------------------------------------------------
def test_text_match_exact() -> None:
    assert cmp.text_match("hello world", "hello world") is True


def test_text_match_substring() -> None:
    assert cmp.text_match("hello", "well hello there") is True


def test_text_match_no_match() -> None:
    assert cmp.text_match("alpha", "beta gamma") is False


def test_text_match_case_insensitive() -> None:
    assert cmp.text_match("HeLLo WORLD", "hello world") is True


def test_text_match_whitespace_normalized() -> None:
    assert cmp.text_match("hello   world", "hello world") is True
    assert cmp.text_match("  hello\tworld\n", "say hello world now") is True


def test_text_match_symmetric() -> None:
    pairs = [
        ("hello", "well hello there"),
        ("alpha", "beta"),
        ("Same Text", "same text"),
        ("a b   c", "x a b c y"),
        ("", "nonempty"),
    ]
    for a, b in pairs:
        assert cmp.text_match(a, b) == cmp.text_match(b, a)


def test_text_match_empty_is_false_both_ways() -> None:
    assert cmp.text_match("", "") is False
    assert cmp.text_match("", "x") is False
    assert cmp.text_match("x", "") is False


# --------------------------------------------------------------------------
# Metric computation.
# --------------------------------------------------------------------------
def _baseline(rows):
    return [
        {
            "extraction_type": et,
            "ground_truth_text": t,
            "model_id": "claude-opus-4-6",
        }
        for et, t in rows
    ]


def _payload(**kw):
    return dict(kw)


TYPES = ["decisions", "action_items", "claims"]


def test_identical_sets_recall_precision_f1_all_one() -> None:
    base = _baseline(
        [("decisions", "approved threshold"), ("action_items", "submit erp")]
    )
    payload = _payload(
        decisions=["approved threshold"], action_items=["submit erp"]
    )
    m = cmp.compute_comparison(
        baseline_rows=base,
        haiku_payload=payload,
        gt_pairs=None,
        types=TYPES,
    )["summary"]
    assert m["haiku_recall_vs_opus"] == 1.0
    assert m["haiku_precision_vs_opus"] == 1.0
    assert m["haiku_f1_vs_opus"] == 1.0
    assert m["true_positives"] == 2
    assert m["false_negatives"] == 0
    assert m["haiku_only"] == 0


def test_opus_four_haiku_zero() -> None:
    base = _baseline([("decisions", f"d{i}") for i in range(4)])
    m = cmp.compute_comparison(
        baseline_rows=base,
        haiku_payload=_payload(decisions=[]),
        gt_pairs=None,
        types=TYPES,
    )["summary"]
    assert m["haiku_recall_vs_opus"] == 0.0
    assert m["false_negatives"] == 4
    assert m["haiku_only"] == 0
    assert m["haiku_precision_vs_opus"] == 0.0  # zero-division safe


def test_opus_zero_haiku_four() -> None:
    m = cmp.compute_comparison(
        baseline_rows=_baseline([]),
        haiku_payload=_payload(decisions=[f"h{i}" for i in range(4)]),
        gt_pairs=None,
        types=TYPES,
    )["summary"]
    assert m["haiku_precision_vs_opus"] == 0.0  # zero-division safe
    assert m["false_negatives"] == 0
    assert m["haiku_only"] == 4
    assert m["haiku_recall_vs_opus"] == 0.0


def test_mixed_counts() -> None:
    base = _baseline(
        [
            ("decisions", "approved the threshold"),
            ("decisions", "deferred the methodology"),
            ("action_items", "submit revised erp"),
        ]
    )
    payload = _payload(
        decisions=["the group approved the threshold today"],
        action_items=["nothing relevant here"],
    )
    res = cmp.compute_comparison(
        baseline_rows=base,
        haiku_payload=payload,
        gt_pairs=None,
        types=TYPES,
    )
    m = res["summary"]
    assert m["true_positives"] == 1
    assert m["false_negatives"] == 2  # one decision + one action_item
    assert m["haiku_only"] == 1  # the irrelevant action_item


def test_no_double_counting_one_opus_many_haiku() -> None:
    """One Opus item, three Haiku items that all contain it: TP must be
    1, not 3. The other two Haiku items become haiku_only."""
    base = _baseline([("decisions", "approved")])
    payload = _payload(
        decisions=[
            "the board approved item one",
            "approved",
            "we approved that motion",
        ]
    )
    res = cmp.compute_comparison(
        baseline_rows=base,
        haiku_payload=payload,
        gt_pairs=None,
        types=TYPES,
    )
    m = res["summary"]
    assert m["true_positives"] == 1
    assert m["haiku_only"] == 2
    assert m["false_negatives"] == 0


def test_no_double_counting_many_opus_one_haiku() -> None:
    """Three Opus items all substrings of one Haiku item: only one
    Opus may consume that Haiku item, the other two are FN."""
    base = _baseline(
        [("decisions", "approved"), ("decisions", "approved"),
         ("decisions", "approved")]
    )
    payload = _payload(decisions=["the committee approved the plan"])
    m = cmp.compute_comparison(
        baseline_rows=base,
        haiku_payload=payload,
        gt_pairs=None,
        types=TYPES,
    )["summary"]
    assert m["true_positives"] == 1
    assert m["false_negatives"] == 2
    assert m["haiku_only"] == 0


def test_gt_metrics_present() -> None:
    base = _baseline([("decisions", "approved threshold")])
    payload = _payload(decisions=["approved threshold"])
    gt = [
        {"ground_truth_text": "approved threshold", "extraction_type": "decision"},
        {"ground_truth_text": "totally missing item", "extraction_type": "decision"},
    ]
    res = cmp.compute_comparison(
        baseline_rows=base,
        haiku_payload=payload,
        gt_pairs=gt,
        types=TYPES,
    )
    m = res["summary"]
    assert m["gt_covered_by_haiku"] == 1
    assert m["gt_missed_by_haiku"] == 1
    assert m["gt_recall_haiku"] == 0.5
    assert m["gt_covered_by_opus"] == 1
    assert res["gt_pairs_present"] is True
    assert len(res["gt_missed"]) == 1


def test_gt_absent_is_zero_not_crash() -> None:
    res = cmp.compute_comparison(
        baseline_rows=_baseline([("decisions", "x")]),
        haiku_payload=_payload(decisions=["x"]),
        gt_pairs=None,
        types=TYPES,
    )
    assert res["summary"]["gt_recall_haiku"] == 0.0
    assert res["gt_pairs_present"] is False


# --------------------------------------------------------------------------
# Loader / provenance gating via the public functions.
# --------------------------------------------------------------------------
def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _seed_baseline(dl: Path, sid: str) -> None:
    p = (
        dl / "store" / "processed" / "meetings" / sid
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": "approved the threshold",
                "model_id": "claude-opus-4-6",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _minutes_artifact(produced_by: str) -> dict:
    return {
        "artifact_id": "art-1",
        "artifact_type": "meeting_minutes",
        "schema_version": "1.0.0",
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "trace-haiku-1",
        "input_refs": [],
        "content_hash": "deadbeef",
        "payload": {
            "schema_version": "1.0.0",
            "title": "7 GHz Downlink TIG",
            "summary": "kickoff",
            "decisions": ["approved the threshold"],
            "action_items": [],
            "open_questions": [],
            "meeting_id": "src",
            "provenance": {"produced_by": produced_by},
        },
    }


def test_missing_opus_baseline_halts(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    dl.mkdir()
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id="src", dry_run=True)
    assert ei.value.reason == "missing_opus_baseline"


def test_wrong_provenance_regex_halts(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__regex-1.json",
        _minutes_artifact("meeting_minutes"),  # regex extractor
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei.value.reason == "missing_haiku_llm_output"
    assert "regex-extractor" in ei.value.detail


def test_llm_provenance_accepted_and_artifact_validates(
    tmp_path: Path,
) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _minutes_artifact("meeting_minutes_llm"),
    )
    res = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=False)
    assert res["status"] == "success"
    art = json.loads(
        Path(res["comparison_artifact_path"]).read_text(encoding="utf-8")
    )
    assert art["artifact_type"] == "comparison_result"
    assert "artifact_kind" not in json.dumps(art)
    assert art["schema_version"] == "1.0.0"
    assert art["summary"]["haiku_recall_vs_opus"] == 1.0


def test_selector_returns_newest_by_mtime_not_filename(
    tmp_path: Path,
) -> None:
    """Regression: two LLM ``meeting_minutes`` artifacts for one source.

    ``find_haiku_artifact`` must return the NEWEST artifact, not the
    first in filename-sorted order. The on-disk filename is
    ``meeting_minutes__<artifact_id>.json`` and ``artifact_id`` is a
    content hash, so a stale all-empty earlier run can sort BEFORE the
    current real one — the exact bug (``...67ccaa13dda9.json`` shadowed
    ``...eecbe9e2de04.json``, halting at ``haiku_item_count == 0``).
    Here the stale all-empty artifact's filename is forced to sort
    FIRST and its mtime is forced OLDER; the selector must still return
    the newer, populated artifact.
    """
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid

    # OLD: every extraction array empty; filename sorts FIRST so the
    # pre-fix ``sorted(...)[first-match]`` logic would pick it.
    old = _minutes_artifact("meeting_minutes_llm")
    old["artifact_id"] = "0" * 12
    old["payload"]["decisions"] = []
    old_path = mdir / f"meeting_minutes__{'0' * 12}.json"
    _write(old_path, old)

    # NEW: real extraction; filename sorts AFTER the stale one.
    new = _minutes_artifact("meeting_minutes_llm")
    new["artifact_id"] = "f" * 12
    new["payload"]["decisions"] = ["approved the threshold"]
    new_path = mdir / f"meeting_minutes__{'f' * 12}.json"
    _write(new_path, new)

    # Force OLD strictly older than NEW so mtime ordering is
    # unambiguous regardless of write/filesystem-granularity timing.
    os.utime(old_path, (1_000_000, 1_000_000))
    os.utime(new_path, (2_000_000, 2_000_000))

    artifact, path = cmp.find_haiku_artifact(dl, sid)
    assert path == new_path, path
    assert artifact["payload"]["decisions"] == ["approved the threshold"]

    # End-to-end through run_comparison: the comparison must read the
    # NEW artifact's items, not halt at 0 like it did on the stale one.
    res = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert res["haiku_artifact_path"] == str(new_path)
    assert res["summary"]["total_haiku_items"] == 1
    assert res["summary"]["true_positives"] == 1


def _empty_minutes(artifact_id: str) -> dict:
    """An LLM ``meeting_minutes`` envelope with EVERY extraction array
    empty — the exact shape of the stale ``...67ccaa13dda9.json`` file
    that shadowed the real run in the data-lake. Valid envelope, valid
    schema (empty arrays validate), zero extracted content."""
    art = _minutes_artifact("meeting_minutes_llm")
    art["artifact_id"] = artifact_id
    art["payload"]["decisions"] = []
    art["payload"]["action_items"] = []
    art["payload"]["open_questions"] = []
    return art


def _populated_minutes(artifact_id: str) -> dict:
    art = _minutes_artifact("meeting_minutes_llm")
    art["artifact_id"] = artifact_id
    art["payload"]["decisions"] = ["approved the threshold"]
    return art


def test_selector_skips_empty_when_mtimes_collide_after_git_clone(
    tmp_path: Path,
) -> None:
    """Regression for the PR #183 follow-up bug.

    #183 selected by ``(st_mtime, filename)`` so a stale all-empty run
    could not shadow the real one. But the workflow reaches the selector
    only via ``clone-data-lake`` (``git clone``), and git stamps EVERY
    checked-out file's mtime with the single clone time. With mtimes
    EQUAL the tuple ties and ``max()`` falls back to the content-blind
    filename (``artifact_id`` is a content hash). The real data-lake
    held two files for the Dec-18 source: an all-empty
    ``...67ccaa13dda9.json`` and the real 467-item
    ``...4138e10ad104.json``. ``67cc...`` sorts AFTER ``4138...``
    (``'6' > '4'``) so the pre-fix ``max()`` deterministically picked
    the EMPTY one — comparison produced no output, push step skipped.

    This test reproduces that exactly: identical mtimes, the empty
    file's name sorting larger. It FAILS pre-fix (selector returns the
    ``67cc`` empty file) and PASSES post-fix (content check skips it).
    """
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid

    empty_path = mdir / "meeting_minutes__67ccaa13dda9.json"
    _write(empty_path, _empty_minutes("67ccaa13dda9"))
    populated_path = mdir / "meeting_minutes__4138e10ad104.json"
    _write(populated_path, _populated_minutes("4138e10ad104"))

    # The empty file's name sorts strictly AFTER the populated one, so
    # the pre-fix max((mtime, name)) deterministically picks the empty
    # file once the mtimes tie.
    assert empty_path.name > populated_path.name

    # Simulate git clone: BOTH files get the identical clone timestamp.
    clone_ts = 1_700_000_000
    os.utime(empty_path, (clone_ts, clone_ts))
    os.utime(populated_path, (clone_ts, clone_ts))
    assert (
        empty_path.stat().st_mtime == populated_path.stat().st_mtime
    )

    artifact, path = cmp.find_haiku_artifact(dl, sid)
    assert path == populated_path, (
        f"selector returned {path.name}, expected "
        f"{populated_path.name} (empty file must not shadow it)"
    )
    assert artifact["payload"]["decisions"] == ["approved the threshold"]

    # End-to-end: the comparison must read the populated artifact's
    # item, not halt/lie at 0 like it did on the stale empty one.
    res = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert res["haiku_artifact_path"] == str(populated_path)
    assert res["summary"]["total_haiku_items"] == 1
    assert res["summary"]["true_positives"] == 1


def test_selector_fail_closed_when_all_artifacts_empty(
    tmp_path: Path,
) -> None:
    """Attack: every LLM artifact for the source is all-empty.

    The selector must HALT ``empty_haiku_artifact`` — never silently
    return an empty artifact (that would emit a lying 0.0-recall diff
    and overwrite eval_history with a false signal)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid

    a = mdir / "meeting_minutes__aaaaaaaaaaaa.json"
    b = mdir / "meeting_minutes__bbbbbbbbbbbb.json"
    _write(a, _empty_minutes("aaaaaaaaaaaa"))
    _write(b, _empty_minutes("bbbbbbbbbbbb"))
    clone_ts = 1_700_000_000
    os.utime(a, (clone_ts, clone_ts))
    os.utime(b, (clone_ts, clone_ts))

    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.find_haiku_artifact(dl, sid)
    assert ei.value.reason == "empty_haiku_artifact"
    # Fail-closed end to end too: run_comparison must propagate the halt
    # and write nothing.
    with pytest.raises(cmp.ComparisonError) as ei2:
        cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert ei2.value.reason == "empty_haiku_artifact"


def test_selector_fail_closed_when_only_artifact_is_empty(
    tmp_path: Path,
) -> None:
    """Attack: only one file exists and it is empty → HALT, not a
    silent 0-item diff."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid
    only = mdir / "meeting_minutes__deadbeefdead.json"
    _write(only, _empty_minutes("deadbeefdead"))

    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.find_haiku_artifact(dl, sid)
    assert ei.value.reason == "empty_haiku_artifact"


def test_selector_no_files_still_missing_haiku_llm_output(
    tmp_path: Path,
) -> None:
    """Attack: the glob finds no ``meeting_minutes__*.json`` at all.

    Behavior must be UNCHANGED from before the fix:
    ``missing_haiku_llm_output`` (not the new empty halt)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    (dl / "store" / "processed" / "meetings" / sid).mkdir(
        parents=True, exist_ok=True
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.find_haiku_artifact(dl, sid)
    assert ei.value.reason == "missing_haiku_llm_output"


def test_eval_history_append_is_additive(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _minutes_artifact("meeting_minutes_llm"),
    )
    eh = (
        dl / "store" / "processed" / "meetings" / sid
        / "eval_history.jsonl"
    )
    eh.parent.mkdir(parents=True, exist_ok=True)
    pre_existing = (
        json.dumps({"eval_type": "pre_existing_row", "keep": True})
        + "\n"
    )
    eh.write_text(pre_existing, encoding="utf-8")

    cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=False)

    lines = eh.read_text(encoding="utf-8").splitlines()
    assert lines[0] == pre_existing.strip()  # untouched, byte-identical
    assert len(lines) == 2
    new_row = json.loads(lines[1])
    assert new_row["eval_type"] == "haiku_vs_opus_comparison"
    assert new_row["haiku_f1_vs_opus"] == 1.0


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _minutes_artifact("meeting_minutes_llm"),
    )
    res = cmp.run_comparison(data_lake=dl, source_id=sid, dry_run=True)
    assert not Path(res["comparison_artifact_path"]).exists()
    assert not (
        dl / "store" / "processed" / "meetings" / sid
        / "eval_history.jsonl"
    ).exists()


# --------------------------------------------------------------------------
# Debug flags are observe-only: they must not change the result, the
# metrics, or what is written — only emit extra lines to STDERR.
# --------------------------------------------------------------------------
def _seed_llm(dl: Path, sid: str) -> None:
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__llm-1.json",
        _minutes_artifact("meeting_minutes_llm"),
    )


def test_print_flags_do_not_change_result(
    tmp_path: Path, capsys
) -> None:
    base = tmp_path / "base"
    dbg = tmp_path / "dbg"
    _seed_llm(base, "src")
    _seed_llm(dbg, "src")

    plain = cmp.run_comparison(
        data_lake=base, source_id="src", dry_run=True
    )
    capsys.readouterr()
    debug = cmp.run_comparison(
        data_lake=dbg,
        source_id="src",
        dry_run=True,
        print_inputs=True,
        print_scores=True,
    )
    captured = capsys.readouterr()

    # Identical summary + metrics regardless of the debug flags.
    assert plain["summary"] == debug["summary"]
    assert plain["status"] == debug["status"] == "success"
    # Debug output is STDERR-only; STDOUT is never touched here
    # (run_comparison itself prints nothing to STDOUT).
    assert captured.out == ""
    assert "=== print_inputs ===" in captured.err
    assert "opus item count:" in captured.err
    assert "haiku item count:" in captured.err
    assert "=== print_scores ===" in captured.err
    assert '"haiku_f1_vs_opus"' in captured.err
    assert "DRY RUN — artifact not written" in captured.err


def test_print_inputs_then_normal_write_still_happens(
    tmp_path: Path, capsys
) -> None:
    dl = tmp_path / "dl"
    _seed_llm(dl, "src")
    res = cmp.run_comparison(
        data_lake=dl,
        source_id="src",
        dry_run=False,
        print_inputs=True,
        print_scores=True,
    )
    err = capsys.readouterr().err
    assert "=== print_inputs ===" in err
    assert "DRY RUN" not in err  # not a dry run — artifact IS written
    art_path = Path(res["comparison_artifact_path"])
    assert art_path.is_file()
    art = json.loads(art_path.read_text(encoding="utf-8"))
    assert art["artifact_type"] == "comparison_result"
    assert art["summary"]["haiku_recall_vs_opus"] == 1.0


# --------------------------------------------------------------------------
# Three-way Opus / Haiku / Sonnet extension.
#
# Every test below fails before the fix (find_candidate_artifact /
# build_three_way_comparison_artifact / include_sonnet do not exist) and
# passes after. They defend: model-string selection, fail-closed missing
# / empty Sonnet, the three-way schema, two-way byte-stability, and the
# preserved find_haiku_artifact contract.
# --------------------------------------------------------------------------
def _minutes_with_model(model_id: str, *, decisions, artifact_id="art-x"):
    art = _minutes_artifact("meeting_minutes_llm")
    art["artifact_id"] = artifact_id
    art["payload"]["provenance"]["model_id"] = model_id
    art["payload"]["decisions"] = decisions
    art["payload"]["action_items"] = []
    art["payload"]["open_questions"] = []
    return art


def test_find_candidate_selects_populated_sonnet_over_empty_collision(
    tmp_path: Path,
) -> None:
    """#185 regression, mirrored for the Sonnet selector.

    Two Sonnet-model LLM artifacts with IDENTICAL mtimes (git-clone
    collision); the empty one's filename sorts AFTER the populated one
    so the content-blind ``max((mtime, name))`` would pick the empty
    file. ``find_candidate_artifact(..., "sonnet")`` must content-skip
    the empty one and return the populated Sonnet artifact."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid

    populated = _minutes_with_model(
        "test-sonnet-model",
        decisions=["approved the threshold"],
        artifact_id="1111aaaa",
    )
    empty = _minutes_with_model(
        "test-sonnet-model", decisions=[], artifact_id="9999zzzz"
    )
    pop_path = mdir / "meeting_minutes__1111aaaa.json"
    empty_path = mdir / "meeting_minutes__9999zzzz.json"
    _write(pop_path, populated)
    _write(empty_path, empty)
    assert empty_path.name > pop_path.name  # empty sorts AFTER
    clone_ts = 1_700_000_000
    os.utime(pop_path, (clone_ts, clone_ts))
    os.utime(empty_path, (clone_ts, clone_ts))

    artifact, path = cmp.find_candidate_artifact(dl, sid, "sonnet")
    assert path == pop_path, path
    assert artifact["payload"]["decisions"] == ["approved the threshold"]


def test_find_candidate_missing_sonnet_is_fail_closed(
    tmp_path: Path,
) -> None:
    """Only a Haiku artifact present → asking for Sonnet must HALT
    ``missing_candidate_artifact``, never silently return the Haiku
    one."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__h1.json",
        _minutes_with_model(
            "claude-haiku-test",
            decisions=["approved the threshold"],
            artifact_id="h1",
        ),
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.find_candidate_artifact(dl, sid, "sonnet")
    assert ei.value.reason == "missing_candidate_artifact"


def test_find_candidate_empty_sonnet_is_fail_closed(
    tmp_path: Path,
) -> None:
    """A Sonnet artifact exists but every array is empty → HALT
    ``empty_candidate_artifact`` (no lying 0-item diff)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__s0.json",
        _minutes_with_model(
            "test-sonnet-model", decisions=[], artifact_id="s0"
        ),
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.find_candidate_artifact(dl, sid, "sonnet")
    assert ei.value.reason == "empty_candidate_artifact"


def test_find_candidate_discriminates_haiku_and_sonnet(
    tmp_path: Path,
) -> None:
    """Step 5.3 selector verification: with BOTH a Haiku-model and a
    Sonnet-model artifact in the same directory,
    ``find_candidate_artifact("sonnet")`` returns the Sonnet one and
    ``find_candidate_artifact("haiku")`` returns the Haiku one."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid
    _write(
        mdir / "meeting_minutes__h1.json",
        _minutes_with_model(
            "claude-haiku-test", decisions=["haiku decision"],
            artifact_id="h1",
        ),
    )
    _write(
        mdir / "meeting_minutes__s1.json",
        _minutes_with_model(
            "test-sonnet-model", decisions=["sonnet decision"],
            artifact_id="s1",
        ),
    )

    h_art, h_path = cmp.find_candidate_artifact(dl, sid, "haiku")
    s_art, s_path = cmp.find_candidate_artifact(dl, sid, "sonnet")
    assert h_path.name == "meeting_minutes__h1.json", h_path
    assert s_path.name == "meeting_minutes__s1.json", s_path
    assert h_art["payload"]["decisions"] == ["haiku decision"]
    assert s_art["payload"]["decisions"] == ["sonnet decision"]


def test_find_haiku_artifact_preserved_after_refactor(
    tmp_path: Path,
) -> None:
    """The refactor must not change find_haiku_artifact behaviour.

    A legacy ``meeting_minutes_llm`` artifact with NO stamped
    provenance.model_id is still selected (the default-token clause),
    and a Sonnet-model artifact in the same directory is NOT returned
    by find_haiku_artifact."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    mdir = dl / "store" / "processed" / "meetings" / sid
    # Legacy LLM artifact: provenance has produced_by only, no model_id.
    legacy = _minutes_artifact("meeting_minutes_llm")
    legacy["artifact_id"] = "legacy1"
    legacy["payload"]["decisions"] = ["approved the threshold"]
    _write(mdir / "meeting_minutes__legacy1.json", legacy)
    # A Sonnet artifact alongside it.
    _write(
        mdir / "meeting_minutes__s9.json",
        _minutes_with_model(
            "test-sonnet-model", decisions=["sonnet only"],
            artifact_id="s9",
        ),
    )

    artifact, path = cmp.find_haiku_artifact(dl, sid)
    assert path.name == "meeting_minutes__legacy1.json", path
    assert artifact["payload"]["decisions"] == ["approved the threshold"]


def _seed_three_way(dl: Path, sid: str) -> None:
    """Opus baseline (2 items) + Haiku artifact (1/2 → recall 0.5) +
    Sonnet artifact (2/2 → recall 1.0), all distinct content so the
    on-disk filenames differ."""
    base = (
        dl / "store" / "processed" / "meetings" / sid
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": "approved the threshold",
                "model_id": "claude-opus-test",
            }
        )
        + "\n"
        + json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": "deferred the methodology",
                "model_id": "claude-opus-test",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mdir = dl / "store" / "processed" / "meetings" / sid
    _write(
        mdir / "meeting_minutes__h.json",
        _minutes_with_model(
            "claude-haiku-test",
            decisions=["approved the threshold"],
            artifact_id="h",
        ),
    )
    _write(
        mdir / "meeting_minutes__s.json",
        _minutes_with_model(
            "test-sonnet-model",
            decisions=[
                "approved the threshold",
                "deferred the methodology",
            ],
            artifact_id="s",
        ),
    )


def test_three_way_merge_schema(tmp_path: Path) -> None:
    """Three-way merge produces the required schema and validates.

    comparison_mode == 'three_way'; haiku_summary AND sonnet_summary
    present; sonnet_run_id present; by_type carries the three-way
    per-type keys; the two-way-only top-level keys are ABSENT."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_three_way(dl, sid)
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=False, include_sonnet=True
    )
    assert res["comparison_mode"] == "three_way"
    art_path = Path(res["comparison_artifact_path"])
    assert art_path.name.startswith("three_way_"), art_path
    art = json.loads(art_path.read_text(encoding="utf-8"))

    assert art["artifact_type"] == "comparison_result"
    assert art["comparison_mode"] == "three_way"
    assert "artifact_kind" not in json.dumps(art)
    for key in (
        "haiku_summary",
        "sonnet_summary",
        "haiku_run_id",
        "sonnet_run_id",
        "opus_model_id",
        "compared_at",
        "by_type",
    ):
        assert key in art, key
    # The two-way-only top-level keys must NOT appear.
    for absent in (
        "summary",
        "false_negatives",
        "haiku_only_items",
        "gt_missed",
    ):
        assert absent not in art, absent
    # Per-type three-way shape.
    d = art["by_type"]["decisions"]
    for k in (
        "opus_count",
        "haiku_count",
        "haiku_tp",
        "haiku_fn",
        "haiku_only",
        "sonnet_count",
        "sonnet_tp",
        "sonnet_fn",
        "sonnet_only",
    ):
        assert k in d, k
    assert d["opus_count"] == 2
    assert d["haiku_count"] == 1 and d["haiku_tp"] == 1
    assert d["sonnet_count"] == 2 and d["sonnet_tp"] == 2
    # Metrics: Haiku 1/2 = 0.5, Sonnet 2/2 = 1.0.
    assert art["haiku_summary"]["haiku_recall_vs_opus"] == 0.5
    assert art["sonnet_summary"]["haiku_recall_vs_opus"] == 1.0
    # Self-validation already ran inside run_comparison; assert it
    # explicitly too so a schema regression is caught here.
    cmp.validate_artifact(art, "comparison_result")

    # eval_history carries an additive three_way_comparison row.
    eh = (
        dl / "store" / "processed" / "meetings" / sid
        / "eval_history.jsonl"
    )
    rows = [
        json.loads(ln)
        for ln in eh.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    tw = [r for r in rows if r.get("eval_type") == "three_way_comparison"]
    assert len(tw) == 1
    assert tw[0]["haiku_f1_vs_opus"] == pytest.approx(2 / 3)
    assert tw[0]["sonnet_f1_vs_opus"] == 1.0


def test_two_way_default_schema_unchanged_and_sonnet_ignored(
    tmp_path: Path,
) -> None:
    """include_sonnet defaults False: output is the legacy two-way
    shape and a Sonnet artifact in the data-lake is COMPLETELY
    ignored."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_three_way(dl, sid)  # Haiku AND Sonnet artifacts both present
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=False
    )
    assert "comparison_mode" not in res
    assert "sonnet_summary" not in res
    art_path = Path(res["comparison_artifact_path"])
    assert art_path.name.startswith("haiku_vs_opus_"), art_path
    art = json.loads(art_path.read_text(encoding="utf-8"))
    assert "comparison_mode" not in art
    assert "sonnet_summary" not in art
    assert "sonnet_run_id" not in art
    for key in (
        "summary",
        "by_type",
        "false_negatives",
        "haiku_only_items",
        "gt_missed",
    ):
        assert key in art, key
    # The two-way diff read the HAIKU artifact (1/2), not the Sonnet
    # one (2/2) — proof the Sonnet artifact was ignored.
    assert art["summary"]["haiku_recall_vs_opus"] == 0.5
    bt = art["by_type"]["decisions"]
    assert set(bt.keys()) == {
        "opus_count",
        "haiku_count",
        "true_positives",
        "false_negatives",
        "haiku_only",
    }
    # No three_way_*.json was written.
    comp_dir = (
        dl / "store" / "processed" / "meetings" / sid / "comparisons"
    )
    assert not list(comp_dir.glob("three_way_*.json"))
    cmp.validate_artifact(art, "comparison_result")


def test_three_way_missing_sonnet_halts_via_run_comparison(
    tmp_path: Path,
) -> None:
    """include_sonnet=True but no Sonnet artifact → run_comparison
    HALTS missing_candidate_artifact and writes nothing (never a
    two-way result mislabelled three-way)."""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    _write(
        dl / "store" / "processed" / "meetings" / sid
        / "meeting_minutes__h.json",
        _minutes_with_model(
            "claude-haiku-test",
            decisions=["approved the threshold"],
            artifact_id="h",
        ),
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(
            data_lake=dl,
            source_id=sid,
            dry_run=True,
            include_sonnet=True,
        )
    assert ei.value.reason == "missing_candidate_artifact"


def test_three_way_both_missing_gives_haiku_error_first(
    tmp_path: Path,
) -> None:
    """Attack: both candidates missing. Haiku is resolved first, so its
    own clear ``missing_haiku_llm_output`` halt surfaces — not a single
    confusing combined failure. (The Sonnet halt would surface on a
    separate run once Haiku exists.)"""
    dl = tmp_path / "dl"
    sid = "src"
    _seed_baseline(dl, sid)
    (dl / "store" / "processed" / "meetings" / sid).mkdir(
        parents=True, exist_ok=True
    )
    with pytest.raises(cmp.ComparisonError) as ei:
        cmp.run_comparison(
            data_lake=dl,
            source_id=sid,
            dry_run=True,
            include_sonnet=True,
        )
    assert ei.value.reason == "missing_haiku_llm_output"


def test_three_way_dry_run_writes_nothing(tmp_path: Path) -> None:
    dl = tmp_path / "dl"
    sid = "src"
    _seed_three_way(dl, sid)
    res = cmp.run_comparison(
        data_lake=dl, source_id=sid, dry_run=True, include_sonnet=True
    )
    assert res["comparison_mode"] == "three_way"
    assert not Path(res["comparison_artifact_path"]).exists()
    assert not (
        dl / "store" / "processed" / "meetings" / sid
        / "eval_history.jsonl"
    ).exists()
