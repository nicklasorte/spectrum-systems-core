"""Unit tests for the deterministic Haiku-vs-Opus comparison engine.

Gate (task spec): every test here must pass before System 2 work
begins. These defend the trust properties of the diff: symmetric
match, no double-counting, zero-division safety, provenance gating,
and append-only eval_history.
"""
from __future__ import annotations

import importlib.util
import json
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
    producer here (in a test, never in the script) proves the two maps
    are identical so they cannot drift.
    """
    import create_opus_reference_baselines as crb  # noqa: WPS433

    assert cmp._PRIMARY_TEXT_FIELD == crb._PRIMARY_TEXT_FIELD
    assert cmp._LEGACY_OBJECT_TEXT_FIELD == crb._LEGACY_OBJECT_TEXT_FIELD


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
