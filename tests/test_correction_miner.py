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
    rows = [json.loads(ln) for ln in post.splitlines() if ln.strip()]
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


# --------------------------------------------------------------------------
# Transcript resolution (regression for the missing_transcript bug:
# the miner only looked under store/raw/transcripts/ and never in the
# processed-meeting dir where transcripts actually live).
# --------------------------------------------------------------------------
SID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"


def _processed_dir(dl: Path) -> Path:
    d = dl / "store" / "processed" / "meetings" / SID
    d.mkdir(parents=True, exist_ok=True)
    return d


def _raw_dir(dl: Path) -> Path:
    d = dl / "store" / "raw" / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_docx(path: Path, text: str) -> None:
    from docx import Document

    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))


def test_transcript_resolved_from_processed_meetings_dir(
    tmp_path: Path,
) -> None:
    """Case 1: the transcript lives in the processed-meeting dir
    alongside the JSON product artifacts — the resolver must find it
    there (this fails before the fix: only raw/ was searched)."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    # JSON product artifacts that share the directory must be ignored.
    (pdir / "meeting_minutes__abc123.json").write_text(
        "{}", encoding="utf-8"
    )
    (pdir / "source_record.json").write_text("{}", encoding="utf-8")
    (pdir / "transcript.txt").write_text(
        "7 GHz Downlink TIG\nThe group approved the threshold.\n",
        encoding="utf-8",
    )
    text = cm._load_transcript(dl, SID)
    assert "approved the threshold" in text


def test_transcript_fallback_to_raw_when_processed_empty(
    tmp_path: Path,
) -> None:
    """Case 2: nothing in processed -> fall back to raw. Both the
    legacy flat name AND the new <source_id>/ subdir form resolve."""
    # Legacy flat name (pre-existing behaviour, must still work).
    dl1 = tmp_path / "dl1"
    (_raw_dir(dl1) / f"{SID}.txt").write_text(
        "legacy flat raw transcript\n", encoding="utf-8"
    )
    assert "legacy flat" in cm._load_transcript(dl1, SID)

    # New <source_id>/ subdirectory form (raises before the fix).
    dl2 = tmp_path / "dl2"
    sub = _raw_dir(dl2) / SID
    sub.mkdir(parents=True)
    (sub / "transcript.txt").write_text(
        "raw subdir transcript\n", encoding="utf-8"
    )
    assert "raw subdir" in cm._load_transcript(dl2, SID)


def test_processed_takes_priority_over_raw(tmp_path: Path) -> None:
    """Case 3: transcript in BOTH locations -> processed wins."""
    dl = tmp_path / "dl"
    (_processed_dir(dl) / "transcript.txt").write_text(
        "PROCESSED transcript content\n", encoding="utf-8"
    )
    (_raw_dir(dl) / f"{SID}.txt").write_text(
        "RAW transcript content\n", encoding="utf-8"
    )
    text = cm._load_transcript(dl, SID)
    assert "PROCESSED" in text
    assert "RAW" not in text


def test_missing_transcript_lists_both_paths(tmp_path: Path) -> None:
    """Case 4: neither location -> missing_transcript naming BOTH the
    processed dir and the raw fallback dir in the error detail."""
    dl = tmp_path / "dl"
    _processed_dir(dl)  # exists but empty
    _raw_dir(dl)
    try:
        cm._load_transcript(dl, SID)
        raise AssertionError("expected missing_transcript")
    except cm.CorrectionMinerError as exc:
        assert exc.reason == "missing_transcript"
        assert "processed" in exc.detail and SID in exc.detail
        assert "raw" in exc.detail
        # Both concrete locations appear.
        assert str(
            dl / "store" / "processed" / "meetings" / SID
        ) in exc.detail
        assert str(
            dl / "store" / "raw" / "transcripts"
        ) in exc.detail


def test_transcript_path_override_bypasses_search(
    tmp_path: Path,
) -> None:
    """Case 5: an explicit --transcript-path is read verbatim and the
    search is skipped entirely (works even with NOTHING in either
    auto-detect location)."""
    dl = tmp_path / "dl"
    _processed_dir(dl)
    _raw_dir(dl)
    override = tmp_path / "anywhere" / "explicit.txt"
    override.parent.mkdir(parents=True)
    override.write_text("explicit override content\n", encoding="utf-8")
    text = cm._load_transcript(dl, SID, transcript_path=override)
    assert "explicit override" in text


def test_override_missing_fails_closed_immediately(
    tmp_path: Path,
) -> None:
    """Attack: --transcript-path to a non-existent file fails closed
    before any model work."""
    dl = tmp_path / "dl"
    # A valid auto-detect transcript also exists; the override must
    # still win and fail closed (it is not a fallback).
    (_processed_dir(dl) / "transcript.txt").write_text(
        "would-be-found\n", encoding="utf-8"
    )
    try:
        cm._load_transcript(
            dl, SID, transcript_path=tmp_path / "nope.txt"
        )
        raise AssertionError("expected missing_transcript")
    except cm.CorrectionMinerError as exc:
        assert exc.reason == "missing_transcript"
        assert "nope.txt" in exc.detail


def test_docx_in_processed_is_extracted(tmp_path: Path) -> None:
    """The transcript is a .docx — the resolver must extract clean
    plain text via the deterministic DocxExtractor (no LLM)."""
    dl = tmp_path / "dl"
    _make_docx(
        _processed_dir(dl) / "meeting-transcript.docx",
        "7 GHz Downlink TIG\nThe group approved the threshold.",
    )
    text = cm._load_transcript(dl, SID)
    assert "approved the threshold" in text


def test_corrupt_docx_in_processed_raises_with_path(
    tmp_path: Path,
) -> None:
    """Attack: a corrupt .docx in the processed dir raises
    missing_transcript with the actual path — it does NOT silently
    fall through to the raw location."""
    dl = tmp_path / "dl"
    bad = _processed_dir(dl) / "broken.docx"
    bad.write_bytes(b"not a real docx zip")
    # A perfectly good raw transcript exists; the corrupt processed
    # file must still fail closed (processed has priority).
    (_raw_dir(dl) / f"{SID}.txt").write_text(
        "raw is fine\n", encoding="utf-8"
    )
    try:
        cm._load_transcript(dl, SID)
        raise AssertionError("expected missing_transcript")
    except cm.CorrectionMinerError as exc:
        assert exc.reason == "missing_transcript"
        assert "broken.docx" in exc.detail


def test_multiple_transcripts_in_processed_first_alpha_no_fail(
    tmp_path: Path,
) -> None:
    """Attack: multiple transcript files in the processed dir -> pick
    the first alphabetically and warn; do not fail."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    (pdir / "b-second.txt").write_text("SECOND\n", encoding="utf-8")
    (pdir / "a-first.txt").write_text("FIRST\n", encoding="utf-8")
    text = cm._load_transcript(dl, SID)
    assert "FIRST" in text
    assert "SECOND" not in text


# --------------------------------------------------------------------------
# source_record.json authoritative-pointer resolution (the #187 glob
# failed because transcripts are NOT co-located with the processed
# artifacts; source_record.payload.raw_path is the canonical pointer).
# --------------------------------------------------------------------------
def _write_source_record(pdir: Path, raw_path: object) -> None:
    """Write a source_record.json shaped like SourceLoader writes it.

    ``raw_path`` is placed under ``payload.raw_path``. Pass the
    sentinel ``...`` to omit the field entirely (missing-field case)
    and ``None`` to write an empty ``payload`` object.
    """
    payload: dict = {}
    if raw_path is not ...:
        payload["raw_path"] = raw_path
    pdir.joinpath("source_record.json").write_text(
        json.dumps(
            {
                "artifact_type": "source_record",
                "schema_version": "1.0.0",
                "artifact_id": "aid-1",
                "source_id": SID,
                "created_at": "1970-01-01T00:00:00+00:00",
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )


def test_source_record_relative_raw_path_resolves_step2(
    tmp_path: Path,
) -> None:
    """Case 1: source_record.json with a valid relative raw_path ->
    resolved from step 2; the directory glob is NEVER consulted."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    tx = dl / "store" / "raw" / "meetings" / SID / "source.txt"
    tx.parent.mkdir(parents=True)
    tx.write_text(
        "7 GHz Downlink TIG\nThe group approved it.\n", encoding="utf-8"
    )
    _write_source_record(pdir, "raw/meetings/" + SID + "/source.txt")

    import correction_miner as _cm

    called = {"glob": False}
    orig = _cm._find_transcript_in_dir

    def _spy(directory):  # noqa: ANN001
        called["glob"] = True
        return orig(directory)

    _cm._find_transcript_in_dir = _spy
    try:
        text = cm._load_transcript(dl, SID)
    finally:
        _cm._find_transcript_in_dir = orig
    assert "approved it" in text
    assert called["glob"] is False, "step 3 glob ran despite step 2 hit"


def test_source_record_absolute_raw_path_resolves(
    tmp_path: Path,
) -> None:
    """An absolute raw_path (record written when paths were absolute)
    that exists on THIS machine is read as-is."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    tx = tmp_path / "abs" / "source.txt"
    tx.parent.mkdir(parents=True)
    tx.write_text("absolute path transcript\n", encoding="utf-8")
    _write_source_record(pdir, str(tx))
    assert "absolute path" in cm._load_transcript(dl, SID)


def test_source_record_cross_machine_absolute_rerooted(
    tmp_path: Path,
) -> None:
    """Attack: raw_path is absolute and from a DIFFERENT machine. The
    segment after the last ``store`` is re-rooted under this store/
    so an identically-laid-out lake still resolves."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    tx = dl / "store" / "raw" / "meetings" / SID / "source.txt"
    tx.parent.mkdir(parents=True)
    tx.write_text("re-rooted transcript\n", encoding="utf-8")
    foreign = (
        "/home/someone-else/data-lake/store/raw/meetings/"
        + SID
        + "/source.txt"
    )
    _write_source_record(pdir, foreign)
    assert "re-rooted" in cm._load_transcript(dl, SID)


def test_source_record_missing_field_warns_falls_through(
    tmp_path: Path, capsys
) -> None:
    """Case 2: source_record.json present but no raw_path field ->
    WARNING logged, resolution falls through to the glob (step 3)."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    _write_source_record(pdir, ...)  # payload present, raw_path absent
    (pdir / "transcript.txt").write_text(
        "glob-found transcript\n", encoding="utf-8"
    )
    text = cm._load_transcript(dl, SID)
    assert "glob-found" in text
    err = capsys.readouterr().err
    assert "no payload.raw_path" in err


def test_source_record_dangling_path_warns_falls_through(
    tmp_path: Path, capsys
) -> None:
    """Case 3: raw_path points at a file that does not exist ->
    WARNING, fall through to the glob (step 3)."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    _write_source_record(pdir, "raw/meetings/" + SID + "/gone.txt")
    (pdir / "transcript.txt").write_text(
        "fallback after dangling\n", encoding="utf-8"
    )
    text = cm._load_transcript(dl, SID)
    assert "fallback after dangling" in text
    err = capsys.readouterr().err
    assert "no such file exists" in err


def test_source_record_malformed_json_warns_falls_through(
    tmp_path: Path, capsys
) -> None:
    """Attack: source_record.json is not valid JSON -> never crash;
    WARNING + fall through to the glob (step 3)."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    (pdir / "source_record.json").write_text(
        "{ this is not json", encoding="utf-8"
    )
    (pdir / "transcript.txt").write_text(
        "survived bad record\n", encoding="utf-8"
    )
    text = cm._load_transcript(dl, SID)
    assert "survived bad record" in text
    err = capsys.readouterr().err
    assert "unreadable/not JSON" in err


def test_no_source_record_falls_through_to_raw(
    tmp_path: Path,
) -> None:
    """Case 4: no source_record.json at all -> glob (empty) then raw
    fallback (step 4) still resolves."""
    dl = tmp_path / "dl"
    _processed_dir(dl)  # exists, empty, no source_record.json
    (_raw_dir(dl) / f"{SID}.txt").write_text(
        "raw fallback only\n", encoding="utf-8"
    )
    assert "raw fallback only" in cm._load_transcript(dl, SID)


def test_override_bypasses_source_record(tmp_path: Path) -> None:
    """Case 5: --transcript-path override is read verbatim even when a
    perfectly good source_record.json points elsewhere."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    decoy = dl / "store" / "raw" / "meetings" / SID / "source.txt"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("SOURCE-RECORD decoy\n", encoding="utf-8")
    _write_source_record(pdir, "raw/meetings/" + SID + "/source.txt")
    override = tmp_path / "explicit.txt"
    override.write_text("OVERRIDE wins\n", encoding="utf-8")
    text = cm._load_transcript(dl, SID, transcript_path=override)
    assert "OVERRIDE wins" in text
    assert "decoy" not in text


def test_all_four_locations_empty_lists_all_four(
    tmp_path: Path,
) -> None:
    """Case 6: nothing anywhere -> missing_transcript whose detail
    names ALL FOUR checked locations (source_record, processed glob,
    raw flat, raw subdir)."""
    dl = tmp_path / "dl"
    _processed_dir(dl)
    _raw_dir(dl)
    try:
        cm._load_transcript(dl, SID)
        raise AssertionError("expected missing_transcript")
    except cm.CorrectionMinerError as exc:
        assert exc.reason == "missing_transcript"
        d = exc.detail
        assert "source_record.json" in d
        assert "processed dir" in d
        assert "raw flat" in d
        assert "raw subdir" in d
        assert SID in d


def test_source_record_corrupt_pointed_file_fails_closed(
    tmp_path: Path,
) -> None:
    """A source_record that points at an EXISTING but corrupt .docx is
    the authoritative input: it fails closed with the real path, it
    does NOT silently glob elsewhere (mirrors the processed-dir rule)."""
    dl = tmp_path / "dl"
    pdir = _processed_dir(dl)
    bad = dl / "store" / "raw" / "meetings" / SID / "source.docx"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a real docx zip")
    _write_source_record(pdir, "raw/meetings/" + SID + "/source.docx")
    # A perfectly good glob transcript also exists; it must NOT mask
    # the corrupt authoritative input.
    (pdir / "transcript.txt").write_text(
        "would-be-found\n", encoding="utf-8"
    )
    try:
        cm._load_transcript(dl, SID)
        raise AssertionError("expected missing_transcript")
    except cm.CorrectionMinerError as exc:
        assert exc.reason == "missing_transcript"
        assert "source.docx" in exc.detail


def test_cli_threads_transcript_path(monkeypatch, tmp_path: Path) -> None:
    """The --transcript-path CLI arg reaches run_correction_miner as a
    Path; absent, it is None."""
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "success",
            "dry_run": True,
            "source_id": kwargs["source_id"],
            "patterns": [],
            "candidates": [],
            "scores": [],
            "promotion": {"promoted": False, "reason": "stub"},
        }

    monkeypatch.setattr(cm, "run_correction_miner", fake_run)

    rc = cm.main(
        [
            "--data-lake", str(tmp_path),
            "--source-id", "x",
            "--dry-run",
            "--transcript-path", "/tmp/explicit.txt",
        ]
    )
    assert rc == 0
    assert captured["transcript_path"] == Path("/tmp/explicit.txt")

    captured.clear()
    rc = cm.main(
        ["--data-lake", str(tmp_path), "--source-id", "x", "--dry-run"]
    )
    assert rc == 0
    assert captured["transcript_path"] is None
