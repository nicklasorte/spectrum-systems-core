"""Unit tests for the correction miner (System 2).

These defend the governance invariants: heuristic-only classifier,
Opus-for-generation / Haiku-for-evaluation, additive-only prompt
edits, backup-before-modification, strictly-greater-than-0.05
promotion gate, PR-always, and append-only eval_history.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compare_opus_haiku as cmp  # noqa: E402
import correction_miner as cm  # noqa: E402

FAKE_REGISTRY = {
    "artifact_type": "model_registry",
    "schema_version": "1.0.0",
    "models": {
        "extraction": "HAIKU-FROM-REGISTRY",
        "generation": "SONNET-FROM-REGISTRY",
        "complex_reasoning": "OPUS-FROM-REGISTRY",
        "opus_reference_baseline": "OPUS-REF-FROM-REGISTRY",
    },
}


# --------------------------------------------------------------------------
# Static: no LLM in the classifier.
# --------------------------------------------------------------------------
def test_classifier_uses_no_llm() -> None:
    """analyze_failure_patterns + classify must be pure heuristic."""
    import inspect

    for fn in (cm.classify_false_negative, cm.analyze_failure_patterns):
        fn_src = inspect.getsource(fn)
        for token in (
            "client",
            "anthropic",
            "messages.create",
            "AnthropicJSONClient",
        ):
            assert token not in fn_src, (
                f"{fn.__name__} references {token!r} — heuristic only"
            )


def test_comparison_is_imported_not_duplicated() -> None:
    """The miner must REUSE compare_opus_haiku, not reimplement F1."""
    assert cm.cmp is cmp
    src = (SCRIPTS / "correction_miner.py").read_text(encoding="utf-8")
    # No private metric reimplementation in the miner.
    assert "def _f1(" not in src
    assert "def compute_comparison(" not in src
    assert "cmp.compute_comparison" in src


# --------------------------------------------------------------------------
# Step 2-A — pattern analyzer.
# --------------------------------------------------------------------------
def _comp(fns):
    return {
        "artifact_type": "comparison_result",
        "false_negatives": fns,
    }


def test_analyze_empty_no_patterns() -> None:
    assert cm.analyze_failure_patterns([]) == []
    assert cm.analyze_failure_patterns([_comp([])]) == []


def test_analyze_all_decisions_top_is_implicit_or_deferred() -> None:
    fns = [
        {"text_preview": "the group landed on the minus 47 number",
         "extraction_type": "decisions"},
        {"text_preview": "consensus to move forward with option B",
         "extraction_type": "decisions"},
        {"text_preview": "we will revisit the methodology later",
         "extraction_type": "decisions"},
    ]
    patterns = cm.analyze_failure_patterns([_comp(fns)])
    assert patterns
    assert patterns[0].pattern_type in (
        "implicit_decision",
        "deferred_item",
        "technical_detail",
    )
    # All three classified, frequencies sum to 3.
    assert sum(p.frequency for p in patterns) == 3


def test_analyze_mixed_frequency_counts() -> None:
    fns = [
        {"text_preview": "deferred the aggregate methodology",
         "extraction_type": "decisions"},
        {"text_preview": "postpone the ERP discussion",
         "extraction_type": "decisions"},
        {"text_preview": "DoD will submit revised values next week",
         "extraction_type": "action_items"},
        {"text_preview": "the threshold is -47 dBm/MHz",
         "extraction_type": "technical_parameters"},
    ]
    patterns = cm.analyze_failure_patterns([_comp(fns)])
    by = {p.pattern_type: p.frequency for p in patterns}
    assert by.get("deferred_item") == 2
    assert by.get("procedural_commitment") == 1
    assert by.get("technical_detail") == 1
    assert abs(sum(p.percentage_of_fns for p in patterns) - 1.0) < 1e-9
    # Sorted by frequency desc.
    assert patterns[0].frequency >= patterns[-1].frequency


# --------------------------------------------------------------------------
# Step 2-B — candidate generator.
# --------------------------------------------------------------------------
class SpyClient:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def __call__(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self.response


def test_generate_candidates_uses_stub_no_network(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("ORIGINAL PROMPT BODY\n", encoding="utf-8")
    spy = SpyClient("ADDED: capture deferrals as decisions.")
    patterns = [
        cm.FailurePattern("deferred_item", 5, 0.5, []),
        cm.FailurePattern("technical_detail", 3, 0.3, []),
    ]
    cands = cm.generate_candidates(
        patterns, prompt, FAKE_REGISTRY, client=spy, max_candidates=3
    )
    assert len(cands) == 2  # one per pattern, no network
    assert spy.calls  # the stub, not anthropic, was called
    for c in cands:
        # ADDITIVE: original text is a prefix; nothing above changed.
        assert c.full_prompt.startswith("ORIGINAL PROMPT BODY")
        assert "ADDED: capture deferrals" in c.prompt_addition
        assert c.generated_by == "OPUS-FROM-REGISTRY"


def test_generate_candidates_caps_at_max(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("BODY\n", encoding="utf-8")
    spy = SpyClient("x")
    patterns = [
        cm.FailurePattern(p, 9 - i, 0.1, [])
        for i, p in enumerate(cm.PATTERN_TYPES)
    ]
    cands = cm.generate_candidates(
        patterns, prompt, FAKE_REGISTRY, client=spy, max_candidates=2
    )
    assert len(cands) == 2
    # Cap is hard at 3 even if asked for more.
    cands = cm.generate_candidates(
        patterns, prompt, FAKE_REGISTRY, client=spy, max_candidates=99
    )
    assert len(cands) == 3


# --------------------------------------------------------------------------
# Step 2-C — candidate evaluator.
# --------------------------------------------------------------------------
def test_evaluate_uses_haiku_from_registry_not_opus(
    monkeypatch, tmp_path: Path
) -> None:
    """The evaluator must build its default client from the registry
    HAIKU (extraction) string — never Opus."""
    captured = {}

    def fake_haiku_client(model: str):
        captured["model"] = model

        def _c(*, system: str, user: str) -> str:
            return json.dumps(
                {
                    "decisions": ["approved the threshold"],
                    "action_items": [],
                    "open_questions": [],
                }
            )

        return _c

    monkeypatch.setattr(cm, "cmp_haiku_client", fake_haiku_client)

    dl = _seed_eval_lake(tmp_path)
    cand = cm.PromptCandidate(
        candidate_id="cand-1",
        pattern_addressed="implicit_decision",
        pattern_frequency=1,
        prompt_addition="add",
        full_prompt="FULL PROMPT",
        generated_by="OPUS-FROM-REGISTRY",
        generated_at="t",
    )
    score = cm.evaluate_candidate(
        cand, "src", dl, FAKE_REGISTRY, baseline_f1=0.0
    )
    assert captured["model"] == "HAIKU-FROM-REGISTRY"
    assert captured["model"] != "OPUS-FROM-REGISTRY"
    assert isinstance(score.f1_vs_opus, float)
    assert score.candidate_id == "cand-1"


def test_evaluate_calls_imported_compute_comparison(
    monkeypatch, tmp_path: Path
) -> None:
    """Proves the metric is the imported one, not a local copy."""
    sentinel = {
        "haiku_f1_vs_opus": 0.42,
        "haiku_recall_vs_opus": 0.4,
        "haiku_precision_vs_opus": 0.44,
        "gt_recall_haiku": 0.5,
    }
    called = {}

    def fake_compute(**kwargs):
        called["yes"] = True
        return {"summary": sentinel}

    monkeypatch.setattr(cm.cmp, "compute_comparison", fake_compute)

    def fake_haiku_client(model: str):
        return lambda *, system, user: json.dumps(
            {"decisions": [], "action_items": [], "open_questions": []}
        )

    monkeypatch.setattr(cm, "cmp_haiku_client", fake_haiku_client)
    dl = _seed_eval_lake(tmp_path)
    cand = cm.PromptCandidate(
        "c", "implicit_decision", 1, "a", "FULL", "OPUS", "t"
    )
    score = cm.evaluate_candidate(
        cand, "src", dl, FAKE_REGISTRY, baseline_f1=0.30
    )
    assert called.get("yes") is True
    assert score.f1_vs_opus == 0.42
    assert abs(score.delta_f1 - 0.12) < 1e-9
    assert score.better_than_baseline is True


# --------------------------------------------------------------------------
# Step 2-D — promotion gate.
# --------------------------------------------------------------------------
def _score(cid, f1, baseline):
    return cm.CandidateScore(
        candidate_id=cid,
        f1_vs_opus=f1,
        recall_vs_opus=f1,
        precision_vs_opus=f1,
        gt_recall=0.0,
        baseline_f1=baseline,
        delta_f1=f1 - baseline,
        better_than_baseline=(f1 - baseline) > 0.05,
    )


def _prompt_obj(cid):
    return cm.PromptCandidate(
        candidate_id=cid,
        pattern_addressed="implicit_decision",
        pattern_frequency=4,
        prompt_addition="capture implicit decisions",
        full_prompt="ORIGINAL\n\nADDITION BLOCK\n",
        generated_by="OPUS-FROM-REGISTRY",
        generated_at="t",
    )


class PROpenerSpy:
    def __init__(self):
        self.called = False
        self.kwargs = None

    def __call__(self, **kwargs):
        self.called = True
        self.kwargs = kwargs
        return {"returncode": 0, "stdout": "https://pr", "branch": kwargs["branch"]}


def test_delta_exactly_005_does_not_promote(tmp_path: Path) -> None:
    prompt = tmp_path / "meeting_minutes_llm.md"
    prompt.write_text("ORIGINAL\n", encoding="utf-8")
    spy = PROpenerSpy()
    scores = [_score("c1", 0.55, 0.50)]  # delta exactly 0.05
    assert abs(scores[0].delta_f1 - 0.05) < 1e-12
    res = cm.promote_best_candidate(
        scores, [_prompt_obj("c1")], prompt, tmp_path, pr_opener=spy
    )
    assert res["promoted"] is False
    assert "no promotion" in res["reason"]
    assert spy.called is False
    assert prompt.read_text(encoding="utf-8") == "ORIGINAL\n"  # untouched


def test_delta_006_promotes_backup_prompt_pr(tmp_path: Path) -> None:
    prompt = tmp_path / "meeting_minutes_llm.md"
    prompt.write_text("ORIGINAL PROMPT\n", encoding="utf-8")
    spy = PROpenerSpy()
    order = {}

    def on_backup(backup_path: Path):
        # Ordering proof: backup exists with the ORIGINAL content and
        # the live prompt has NOT been modified yet.
        order["backup_exists"] = backup_path.is_file()
        order["backup_content"] = backup_path.read_text(encoding="utf-8")
        order["prompt_still_original"] = (
            prompt.read_text(encoding="utf-8") == "ORIGINAL PROMPT\n"
        )

    scores = [_score("c1", 0.56, 0.50)]  # delta 0.06 > 0.05
    res = cm.promote_best_candidate(
        scores,
        [_prompt_obj("c1")],
        prompt,
        tmp_path,
        pr_opener=spy,
        on_backup=on_backup,
    )
    assert res["promoted"] is True
    assert order["backup_exists"] is True
    assert order["backup_content"] == "ORIGINAL PROMPT\n"
    assert order["prompt_still_original"] is True  # backup BEFORE write
    # After: prompt rewritten with the additive full_prompt.
    assert prompt.read_text(encoding="utf-8") == "ORIGINAL\n\nADDITION BLOCK\n"
    backups = list(tmp_path.glob("meeting_minutes_llm_backup_*.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "ORIGINAL PROMPT\n"
    assert spy.called is True
    assert spy.kwargs["title"].startswith("prompt(correction):")
    assert "cp " in spy.kwargs["body"]  # rollback instructions


def test_all_negative_delta_no_promotion(tmp_path: Path) -> None:
    prompt = tmp_path / "meeting_minutes_llm.md"
    prompt.write_text("ORIGINAL\n", encoding="utf-8")
    spy = PROpenerSpy()
    scores = [
        _score("c1", 0.40, 0.50),
        _score("c2", 0.30, 0.50),
    ]
    res = cm.promote_best_candidate(
        scores, [_prompt_obj("c1"), _prompt_obj("c2")],
        prompt, tmp_path, pr_opener=spy,
    )
    assert res["promoted"] is False
    assert spy.called is False
    assert prompt.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_promotion_eval_history_is_append_only(
    monkeypatch, tmp_path: Path
) -> None:
    """run_correction_miner appends the promotion row; existing rows
    must survive byte-identical."""
    dl = _seed_eval_lake(tmp_path)
    # Test isolation: NEVER let run_correction_miner promote against
    # the real canonical prompt. Point the module at a temp prompt.
    temp_prompt = tmp_path / "meeting_minutes_llm.md"
    temp_prompt.write_text("CANONICAL PROMPT BODY\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_PROMPT_PATH", temp_prompt)
    eh = (
        dl / "store" / "processed" / "meetings" / "src"
        / "eval_history.jsonl"
    )
    pre = eh.read_text(encoding="utf-8")  # has the baseline row
    assert pre.strip()

    # Seed one comparison_result with FNs so patterns/candidates exist.
    comp = cmp.build_comparison_artifact(
        source_id="src",
        haiku_artifact={"trace_id": "t", "payload": {}},
        baseline_rows=[{"model_id": "OPUS-REF-FROM-REGISTRY"}],
        metrics={
            "summary": {
                "total_opus_items": 1, "total_haiku_items": 0,
                "true_positives": 0, "false_negatives": 1,
                "haiku_only": 0, "gt_covered_by_haiku": 0,
                "gt_missed_by_haiku": 0, "gt_covered_by_opus": 0,
                "haiku_recall_vs_opus": 0.0,
                "haiku_precision_vs_opus": 0.0,
                "haiku_f1_vs_opus": 0.0,
                "gt_recall_haiku": 0.0, "gt_recall_opus": 0.0,
            },
            "by_type": {},
            "false_negatives": [
                {"text_preview": "the group landed on minus 47",
                 "extraction_type": "decisions"}
            ],
            "haiku_only_items": [],
            "gt_missed": [],
            "gt_pairs_present": False,
        },
        compared_at="2026-05-16T00:00:00+00:00",
    )
    comp_dir = (
        dl / "store" / "processed" / "meetings" / "src" / "comparisons"
    )
    comp_dir.mkdir(parents=True, exist_ok=True)
    (comp_dir / "haiku_vs_opus_t.json").write_text(
        json.dumps(comp), encoding="utf-8"
    )

    # Stub Opus generation + Haiku evaluation; force a winning score.
    monkeypatch.setattr(
        cm,
        "_opus_client",
        lambda m: (lambda *, system, user: "ADD: capture implicit decisions"),
    )

    def fake_haiku_client(model: str):
        return lambda *, system, user: json.dumps(
            {"decisions": [], "action_items": [], "open_questions": []}
        )

    monkeypatch.setattr(cm, "cmp_haiku_client", fake_haiku_client)
    monkeypatch.setattr(
        cm.cmp,
        "compute_comparison",
        lambda **k: {
            "summary": {
                "haiku_f1_vs_opus": 0.99,
                "haiku_recall_vs_opus": 0.99,
                "haiku_precision_vs_opus": 0.99,
                "gt_recall_haiku": 0.0,
            }
        },
    )
    spy = PROpenerSpy()
    res = cm.run_correction_miner(
        data_lake=dl,
        source_id="src",
        dry_run=False,
        max_candidates=1,
        registry_path=_fake_registry_file(tmp_path),
        pr_opener=spy,
    )
    assert res["promotion"]["promoted"] is True
    post = eh.read_text(encoding="utf-8")
    assert post.startswith(pre)  # every prior byte intact
    rows = [json.loads(l) for l in post.splitlines() if l.strip()]
    assert any(
        r.get("eval_type") == "correction_miner_promotion" for r in rows
    )
    # Isolation proof: the temp prompt was modified additively and the
    # backup landed next to the TEMP prompt — the real canonical
    # prompt dir was never touched.
    assert temp_prompt.read_text(encoding="utf-8").startswith(
        "CANONICAL PROMPT BODY"
    )
    temp_backups = list(
        tmp_path.glob("meeting_minutes_llm_backup_*.md")
    )
    assert len(temp_backups) == 1
    real_prompt_dir = cm._REPO_ROOT / (
        "src/spectrum_systems_core/workflows/prompts"
    )
    assert not list(
        real_prompt_dir.glob("meeting_minutes_llm_backup_*.md")
    ), "promotion leaked a backup into the real source tree"


def test_dry_run_no_eval_no_pr(monkeypatch, tmp_path: Path) -> None:
    dl = _seed_eval_lake(tmp_path)
    temp_prompt = tmp_path / "meeting_minutes_llm.md"
    temp_prompt.write_text("CANONICAL PROMPT BODY\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_PROMPT_PATH", temp_prompt)
    comp_dir = (
        dl / "store" / "processed" / "meetings" / "src" / "comparisons"
    )
    comp_dir.mkdir(parents=True, exist_ok=True)
    (comp_dir / "haiku_vs_opus_t.json").write_text(
        json.dumps(
            {
                "artifact_type": "comparison_result",
                "schema_version": "1.0.0",
                "source_id": "src",
                "haiku_run_id": "t",
                "opus_model_id": "o",
                "compared_at": "t",
                "summary": {
                    "total_opus_items": 1, "total_haiku_items": 0,
                    "true_positives": 0, "false_negatives": 1,
                    "haiku_only": 0, "gt_covered_by_haiku": 0,
                    "gt_missed_by_haiku": 0, "gt_covered_by_opus": 0,
                    "haiku_recall_vs_opus": 0.0,
                    "haiku_precision_vs_opus": 0.0,
                    "haiku_f1_vs_opus": 0.0,
                    "gt_recall_haiku": 0.0, "gt_recall_opus": 0.0,
                },
                "by_type": {},
                "false_negatives": [
                    {"text_preview": "deferred the methodology",
                     "extraction_type": "decisions"}
                ],
                "haiku_only_items": [],
                "gt_missed": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cm,
        "_opus_client",
        lambda m: (lambda *, system, user: "ADD BLOCK"),
    )
    res = cm.run_correction_miner(
        data_lake=dl,
        source_id="src",
        dry_run=True,
        max_candidates=3,
        registry_path=_fake_registry_file(tmp_path),
    )
    assert res["dry_run"] is True
    assert res["patterns"]
    assert res["scores"] == []
    assert res["promotion"]["promoted"] is False


# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------
def _fake_registry_file(tmp_path: Path) -> Path:
    p = tmp_path / "model_registry.json"
    p.write_text(json.dumps(FAKE_REGISTRY), encoding="utf-8")
    return p


def _seed_eval_lake(tmp_path: Path) -> Path:
    """A data-lake with an Opus baseline, a transcript, and an
    eval_history baseline row — the minimum evaluate_candidate needs."""
    dl = tmp_path / "dl"
    sid_dir = dl / "store" / "processed" / "meetings" / "src"
    sid_dir.mkdir(parents=True)
    (sid_dir / "reference_baselines").mkdir()
    (
        sid_dir / "reference_baselines" / "opus_reference_minutes.jsonl"
    ).write_text(
        json.dumps(
            {
                "extraction_type": "decisions",
                "ground_truth_text": "approved the threshold",
                "model_id": "OPUS-REF-FROM-REGISTRY",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (sid_dir / "eval_history.jsonl").write_text(
        json.dumps(
            {
                "eval_type": "haiku_vs_opus_comparison",
                "haiku_f1_vs_opus": 0.30,
                "timestamp": "2026-05-15T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tdir = dl / "store" / "raw" / "transcripts"
    tdir.mkdir(parents=True)
    (tdir / "src.txt").write_text(
        "7 GHz Downlink TIG\nThe group approved the threshold.\n",
        encoding="utf-8",
    )
    return dl
