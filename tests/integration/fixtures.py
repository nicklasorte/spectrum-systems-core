"""Fixture factories for integration contract tests.

These factories produce artifacts in the EXACT format the live pipeline
writes them. They call the real writer (``ExtractionMerger.merge``)
instead of hand-rolling dicts so that if the writer changes its output
format, tests that depend on these fixtures change automatically and
catch the drift.

Rule (CLAUDE.md, Integration test requirement): every factory function
in this module MUST call the actual writer for the artifact it produces.
Hand-rolled dicts here defeat the entire point of the integration layer.
"""
from __future__ import annotations

import datetime
import uuid
from typing import Any

from spectrum_systems_core.extraction.extraction_merger import ExtractionMerger


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _default_decisions() -> list[dict[str, Any]]:
    """Minimal valid decisions covering all three target outcome types.

    Each decision carries every field the meeting_extraction schema
    requires on a decision item (``decision_text``, ``decision_type``,
    ``stakeholders``, ``rationale``, ``source_turn_ids``,
    ``source_turn_validation``, ``confidence``) plus the optional
    schema-declared fields ``decision_outcome``, ``speaker``, and
    ``grounding_verified`` that ``select_few_shot_examples.py`` reads.

    ``regulatory_verb`` and ``source_text`` are intentionally omitted:
    they are NOT declared on the meeting_extraction schema (the schema
    is closed via ``additionalProperties: false``) and the script
    reads them via ``.get(...)`` with fallback so their absence is
    safe.
    """
    return [
        {
            "decision_text": "NTIA approved the 7 GHz downlink threshold.",
            "decision_type": "approved",
            "decision_outcome": "approval",
            "stakeholders": ["NTIA"],
            "rationale": "Threshold consistent with prior comment cycle.",
            "speaker": "NTIA Lead",
            "confidence": 0.92,
            "grounding_verified": True,
            "source_turn_ids": ["real-turn-001"],
            "source_turn_validation": "verified",
        },
        {
            "decision_text": "Deferred aggregate interference methodology.",
            "decision_type": "deferred",
            "decision_outcome": "deferral",
            "stakeholders": ["Chair"],
            "rationale": None,
            "speaker": "Chair Smith",
            "confidence": 0.88,
            "grounding_verified": True,
            "source_turn_ids": ["real-turn-010"],
            "source_turn_validation": "verified",
        },
        {
            "decision_text": "DoD required to submit revised ERP values.",
            "decision_type": "action_required",
            "decision_outcome": "action_required",
            "stakeholders": ["DoD"],
            "rationale": "Outstanding requirement from prior session.",
            "speaker": "Chair Smith",
            "confidence": 0.85,
            "grounding_verified": True,
            "source_turn_ids": ["real-turn-020"],
            "source_turn_validation": "verified",
        },
    ]


def make_meeting_extraction_artifact(
    source_artifact_id: str,
    decisions: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    action_items: list[dict[str, Any]] | None = None,
    classifications: list[dict[str, Any]] | None = None,
    extraction_run_id: str | None = None,
) -> dict[str, Any]:
    """Produce a meeting_extraction artifact via the real merger.

    Calls ``ExtractionMerger.merge`` so the output is byte-shape
    identical to what the live typed_extraction_runner writes. If the
    merger ever adds, renames, or removes a top-level field, every test
    that depends on this factory rebuilds against the new shape on the
    next run — no per-test dict edits required.

    Args:
      source_artifact_id: the UUID the runner resolved from
        ``source_record.json``. Tests that wire ``source_record.json``
        on disk MUST pass the same id here or the script's resolver
        won't match.
      decisions / claims / action_items: optional lists. ``decisions``
        defaults to one item per target outcome (``approval`` /
        ``deferral`` / ``action_required``); claims and action_items
        default to empty so the artifact stays minimal.
      classifications / extraction_run_id: optional. Default empty
        list and fresh UUID, both safe for tests that don't care about
        routing metrics.
    """
    merger = ExtractionMerger()
    return merger.merge(
        source_artifact_id=source_artifact_id,
        extraction_run_id=extraction_run_id or str(uuid.uuid4()),
        classifications=classifications or [],
        decisions=decisions if decisions is not None else _default_decisions(),
        claims=claims or [],
        action_items=action_items or [],
    )


def make_source_record(
    source_id: str,
    artifact_id: str,
    *,
    raw_path: str | None = None,
) -> dict[str, Any]:
    """Produce a source_record.json in the format the pipeline writes.

    The runner's resolver reads ``artifact_id`` from this file and uses
    it as the ``source_artifact_id`` of every downstream artifact. Tests
    that seed extraction artifacts MUST pass the SAME ``artifact_id``
    to ``make_meeting_extraction_artifact`` or the script's slug ->
    UUID lookup won't match.

    Conforms to ``schemas/source_record.schema.json`` (additionalProperties
    is false, so we include only declared fields).

    ``raw_path`` (keyword-only, optional): when given, a ``payload``
    object is emitted carrying ``raw_path`` — mirroring the
    ``payload.raw_path`` the ingestion ``SourceLoader`` records as the
    authoritative pointer back to the processed transcript. The
    correction miner resolves the transcript from this field, so the
    integration contract test seeds it via this factory rather than a
    hand-rolled dict. When omitted the record is byte-identical to the
    historical shape (no ``payload`` key) so every existing caller is
    unaffected.
    """
    record: dict[str, Any] = {
        "artifact_type": "source_record",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "source_id": source_id,
        "created_at": _now_iso(),
    }
    if raw_path is not None:
        record["payload"] = {"raw_path": raw_path}
    return record


def make_ground_truth_pair_from_decision(
    *,
    source_id: str,
    source_artifact_id: str,
    minutes_artifact_id: str,
    decision_text: str,
    decision_outcome: str = "approval",
    meeting_date: str = "2025-12-18",
    meeting_name: str = "Phase X2 follow-up GT pair fixture",
) -> dict[str, Any]:
    """Produce a ``ground_truth_pair`` via the real writer.

    Calls ``scripts.generate_gt_pairs.build_pair`` so the output is
    byte-shape identical to what the live ``generate_gt_pairs.py``
    writes. If the writer ever renames a field, every test that
    depends on this factory rebuilds against the new shape on the
    next run — no per-test dict edits required.

    Importing the script as a module is safe: ``generate_gt_pairs.py``
    only adds ``scripts/`` to ``sys.path`` and pulls in
    ``_artifact_validator``; no I/O happens at import.
    """
    import sys as _sys

    scripts_dir = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
    )
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))

    import generate_gt_pairs  # type: ignore  # noqa: WPS433

    return generate_gt_pairs.build_pair(
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        minutes_artifact_id=minutes_artifact_id,
        meeting_date=meeting_date,
        meeting_name=meeting_name,
        decision={
            "decision_text": decision_text,
            "decision_outcome": decision_outcome,
        },
    )


def make_gt_pair_review(
    *,
    pair_id: str,
    reviewer_id: str = "fixture-reviewer",
    outcome_confirmed: bool = True,
    expected_decision_outcome: str | None = "approval",
    notes: str | None = None,
) -> dict[str, Any]:
    """Produce a ``gt_pair_review`` via the real script's writer.

    Calls ``scripts.review_gt_pairs.build_review`` so the output stays
    byte-shape identical to what the production review script writes.
    Used by integration tests that wire a GT pair review on disk to
    satisfy the Phase P1 eval gate.
    """
    import sys as _sys

    scripts_dir = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
    )
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))

    import review_gt_pairs  # type: ignore  # noqa: WPS433

    return review_gt_pairs.build_review(
        pair_id=pair_id,
        reviewer_id=reviewer_id,
        outcome_confirmed=outcome_confirmed,
        expected_decision_outcome=expected_decision_outcome,
        notes=notes,
    )


def make_human_minutes_gt_pair(
    *,
    source_id: str,
    source_artifact_id: str,
    ground_truth_text: str,
    extraction_type: str = "decision",
) -> dict[str, Any]:
    """Produce a human-authored ``ground_truth_pair`` via the real writer.

    Calls ``scripts.create_human_gt_pairs.build_pair`` so the output is
    byte-shape identical to what the live ``create_human_gt_pairs.py``
    writes. If the writer renames a field, every test that depends on
    this factory rebuilds against the new shape on the next run — no
    per-test dict edits required (CLAUDE.md integration-test rule).
    """
    import sys as _sys

    scripts_dir = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
    )
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))

    import create_human_gt_pairs  # type: ignore  # noqa: WPS433

    return create_human_gt_pairs.build_pair(
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        ground_truth_text=ground_truth_text,
        extraction_type=extraction_type,
    )


def make_promoted_meeting_minutes_artifact(
    *,
    lake_root: Any,
    source_id: str,
    decisions: list[str] | None = None,
    action_items: list[dict[str, Any]] | list[str] | None = None,
    open_questions: list[str] | None = None,
    transcript_text: str | None = None,
    model_id: str | None = None,
) -> Any:
    """Produce a promoted ``meeting_minutes`` artifact via the REAL path.

    Runs ``run_meeting_minutes_llm_workflow`` (the real governed loop +
    every LLM eval gate) with a deterministic stub transport, then
    writes it through the real ``write_promoted_artifact`` writer. If
    the workflow, the envelope, or the writer changes shape, every test
    that depends on this factory rebuilds against the new shape on the
    next run (CLAUDE.md integration-test rule — no hand-rolled dict).

    The stubbed model returns the passed items verbatim; the transcript
    is synthesized to contain every item as a literal substring so the
    within-source eval passes and the artifact actually promotes.

    ``model_id`` (optional) overrides ``payload.provenance.model_id`` on
    the promoted artifact BEFORE it is written. The real workflow
    resolves that field from ``ai/registry/model_registry.json``; the
    ONLY thing that differs between a Haiku run and a Sonnet run of this
    deterministic loop is which registry model_id is stamped into
    provenance, so overriding just that one field faithfully reproduces
    the on-disk shape the real Sonnet path writes (this mirrors the
    workflow's own established post-promotion provenance stamp;
    ``compare_opus_haiku`` never reads ``content_hash``). Used so a
    three-way contract test can place a real Haiku artifact and a real
    Sonnet artifact in the same directory.
    """
    import sys as _sys
    from pathlib import Path as _Path

    from spectrum_systems_core.data_lake.writer import (
        write_promoted_artifact,
    )
    from spectrum_systems_core.workflows.meeting_minutes_llm import (
        run_meeting_minutes_llm_workflow,
    )

    tests_dir = _Path(__file__).resolve().parents[1]
    if str(tests_dir) not in _sys.path:
        _sys.path.insert(0, str(tests_dir))
    from llm_stub import json_stub  # type: ignore  # noqa: WPS433

    decisions = decisions or [
        "The group approved the 7 GHz downlink threshold."
    ]
    action_items = action_items or [
        {"action": "DoD will submit revised ERP values before the next session."}
    ]
    open_questions = open_questions or [
        "What is the coordination distance for federal incumbents?"
    ]

    if transcript_text is None:
        lines = ["7 GHz Downlink TIG — kickoff"]
        lines.extend(decisions)
        for ai in action_items:
            lines.append(ai["action"] if isinstance(ai, dict) else ai)
        lines.extend(open_questions)
        transcript_text = "\n".join(lines) + "\n"

    client = json_stub(
        decisions=decisions,
        action_items=action_items,
        open_questions=open_questions,
    )
    result = run_meeting_minutes_llm_workflow(
        transcript_text,
        client=client,
        meeting_id=source_id,
        source_id=source_id,
        lake_root=lake_root,
    )
    if not result.promoted:
        eval_payloads = [r.payload for r in result.eval_results]
        raise AssertionError(
            "factory could not promote meeting_minutes; "
            f"decision={result.control_decision.payload} "
            f"evals={eval_payloads}"
        )
    if model_id is not None:
        provenance = result.meeting_minutes.payload.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
            result.meeting_minutes.payload["provenance"] = provenance
        provenance["model_id"] = model_id
    return write_promoted_artifact(
        _Path(lake_root), result.meeting_minutes, meeting_id=source_id
    )


def make_opus_reference_baseline(
    *,
    data_lake_root: Any,
    source_id: str,
    source_artifact_id: str,
    model: str,
    items_by_type: dict[str, list[Any]],
) -> Any:
    """Produce ``opus_reference_minutes.jsonl`` via the REAL builder.

    Calls ``create_opus_reference_baselines.build_records`` and the
    script's own ``_write_jsonl`` writer so the JSONL is byte-shape
    identical to a real Opus baseline run (CLAUDE.md rule). ``model``
    is stamped into every line as ``model_id`` exactly as the workflow
    resolves it from ``ai/registry/model_registry.json``.
    ``data_lake_root`` is the data-lake REPO root (the script appends
    ``store/processed/meetings/...`` itself).
    """
    import sys as _sys
    from pathlib import Path as _Path

    scripts_dir = _Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import create_opus_reference_baselines as crb  # type: ignore  # noqa: WPS433

    records = crb.build_records(
        parsed=items_by_type,
        types=crb.extraction_types(),
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        model=model,
        meeting_date="2025-12-18",
        created_at="1970-01-01T00:00:00+00:00",
        # Mirror the real workflow: resolve CHUNK_OVERLAP_TURNS once
        # via the SSOT helper the script also uses, so a test that
        # sets the env var produces a matched overlap=N baseline
        # through the factory exactly as the production script would.
        chunking_strategy_version_value=crb.chunking_strategy_version(),
    )
    out = crb._jsonl_path(_Path(data_lake_root), source_id)
    crb._write_jsonl(out, records)
    return out


def _ceiling_item(
    item_id: str,
    schema_type: str,
    turns: list[str],
    text: str,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "schema_type": schema_type,
        "source_turn_ids": turns,
        "source_text": text,
        "payload": {"text": text},
    }


def make_opus_ceiling_artifact(
    *,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    transcript_text: str = (
        "t0001 DECISION: the group approved the 7 GHz downlink "
        "threshold.\nt0002 ACTION: DoD will follow up with revised ERP."
    ),
    items: list[dict[str, Any]] | None = None,
) -> Any:
    """Produce an ``opus_ceiling`` via the REAL ``extract_ceiling``.

    The Opus call is injected (deterministic) so the artifact is
    byte-stable, but every other field — per_type_counts,
    transcript_keyword_hits, item normalisation/sorting — is produced
    by the real extractor, so a shape change there breaks this factory
    (CLAUDE.md integration-test rule: call the real writer).
    """
    from spectrum_systems_core.extraction.opus_ceiling_extractor import (
        extract_ceiling,
    )

    default_items = items if items is not None else [
        _ceiling_item("c-001", "decision", ["t0001"],
                      "the group approved the 7 GHz downlink threshold"),
        _ceiling_item("c-002", "action_item", ["t0002"],
                      "DoD will follow up with revised ERP"),
    ]
    return extract_ceiling(
        transcript_text,
        transcript_id,
        opus_call=lambda _t: list(default_items),
    )


def make_extraction_alignment_comparison_artifact(
    *,
    ceiling_items: list[dict[str, Any]] | None = None,
    haiku_items: list[dict[str, Any]] | None = None,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
) -> Any:
    """Produce an ``extraction_alignment_comparison`` via the REAL
    comparator over two real ``opus_ceiling`` artifacts."""
    from spectrum_systems_core.evals.extraction_comparison import (
        compare_extractions,
        contract_version,
    )

    ceiling = make_opus_ceiling_artifact(
        transcript_id=transcript_id,
        items=ceiling_items if ceiling_items is not None else [
            _ceiling_item("c-001", "decision", ["t1"], "approved the threshold"),
        ],
    )
    haiku = make_opus_ceiling_artifact(
        transcript_id=transcript_id,
        items=haiku_items if haiku_items is not None else [
            _ceiling_item("h-001", "decision", ["t1"], "approved the threshold"),
        ],
    )
    return compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )


def make_false_negative_set_artifact(
    *,
    ceiling_items: list[dict[str, Any]] | None = None,
    haiku_items: list[dict[str, Any]] | None = None,
) -> Any:
    """Produce a ``false_negative_set`` via the REAL builder over a
    REAL comparison artifact."""
    from spectrum_systems_core.extraction.false_negative_builder import (
        build_false_negative_set,
    )

    comparison = make_extraction_alignment_comparison_artifact(
        ceiling_items=ceiling_items if ceiling_items is not None else [
            _ceiling_item("c-001", "decision", ["t1"], "approved the threshold"),
            _ceiling_item("c-002", "decision", ["t9"], "deferred the methodology"),
        ],
        haiku_items=haiku_items if haiku_items is not None else [
            _ceiling_item("h-001", "decision", ["t1"], "approved the threshold"),
        ],
    )
    return build_false_negative_set(comparison)


def make_candidate_evaluation_artifact(
    *,
    candidate_id: str = "cand-fixture-001",
    target_transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    holdout_transcript_id: str = "m-2025-11-20-ntia-coordination-session",
    target_baseline_f1: float = 0.65,
    target_candidate_f1: float = 0.72,
    holdout_baseline_f1: float = 0.70,
    holdout_candidate_f1: float = 0.71,
) -> Any:
    """Produce a ``candidate_evaluation`` via the REAL
    ``evaluate_candidate`` writer.

    The ceiling/baseline/candidate artifacts are constructed so the
    real comparator yields the requested total F1 values, then the
    real evaluator computes deltas, regressions, and the eligibility
    stamp — no hand-rolled candidate_evaluation dict.
    """
    from spectrum_systems_core.evals.extraction_comparison import (
        contract_version,
    )
    from spectrum_systems_core.extraction.candidate_evaluator import (
        evaluate_candidate,
    )

    version = contract_version()

    def _ceiling(tid: str) -> Any:
        return make_opus_ceiling_artifact(
            transcript_id=tid,
            items=[
                _ceiling_item("c-1", "decision", ["t1"], "alpha decision text"),
                _ceiling_item("c-2", "decision", ["t2"], "beta decision text"),
            ],
        )

    # Two haiku shapes: one matching 0 of 2 (f1 0.0) and one matching
    # both (f1 1.0). The comparator is real; we pick which to return so
    # the per-transcript total F1 lands on the requested value set.
    def _haiku_for(f1: float, tid: str) -> Any:
        if f1 >= 0.99:
            items = [
                _ceiling_item("h-1", "decision", ["t1"], "alpha decision text"),
                _ceiling_item("h-2", "decision", ["t2"], "beta decision text"),
            ]
        elif f1 <= 0.01:
            items = [
                _ceiling_item("h-x", "decision", ["t99"], "unrelated content"),
            ]
        else:  # one of two -> f1 = 2*(1/1 * 1/2)? handled via 1 match
            items = [
                _ceiling_item("h-1", "decision", ["t1"], "alpha decision text"),
            ]
        return make_opus_ceiling_artifact(transcript_id=tid, items=items)

    plan = {
        target_transcript_id: (target_baseline_f1, target_candidate_f1),
        holdout_transcript_id: (holdout_baseline_f1, holdout_candidate_f1),
    }

    def baseline_loader(tid: str) -> Any:
        return _haiku_for(plan[tid][0], tid)

    def haiku_runner(tid: str, _prompt: str) -> Any:
        return _haiku_for(plan[tid][1], tid)

    return evaluate_candidate(
        candidate_id=candidate_id,
        candidate_prompt="add: extract deferred decisions explicitly",
        target_transcript_id=target_transcript_id,
        ceiling_loader=_ceiling,
        baseline_loader=baseline_loader,
        haiku_runner=haiku_runner,
        holdout_transcript_id=holdout_transcript_id,
        alignment_contract_version=version,
    )


def make_improvement_cycle_result_artifact(
    *,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    all_present: bool = True,
) -> Any:
    """Produce an ``improvement_cycle_result`` via the REAL harness."""
    from spectrum_systems_core.harness.improvement_cycle import (
        PHASES,
        run_improvement_cycle,
    )

    if all_present:
        funcs = {p: (lambda p=p: f"art-{p}") for p in PHASES}
    else:
        funcs = {p: (lambda p=p: f"art-{p}") for p in PHASES}
        funcs["Y_5"] = lambda: (_ for _ in ()).throw(RuntimeError("Y_5 boom"))
    return run_improvement_cycle(
        transcript_id=transcript_id,
        phase_funcs=funcs,
        open_pr_lookup=lambda _t: [],
        cycle_id="fixture-cycle-0001",
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )


def _scripts_on_path() -> None:
    import sys as _sys
    from pathlib import Path as _Path

    scripts_dir = _Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))


def _many_ceiling_items(
    n: int, schema_type: str = "decision"
) -> list[dict[str, Any]]:
    return [
        _ceiling_item(
            f"c-{i:04d}", schema_type, [f"t{i}"],
            f"governed item number {i} approved by the working group",
        )
        for i in range(n)
    ]


def make_dec18_run_report(
    *,
    ceiling_count: int = 60,
    aligned: int = 60,
) -> Any:
    """Produce a ``dec18_run_report`` via the REAL Z.1 orchestrator.

    ``run_dec18_loop`` is run with deterministic injected seams (the
    same injection seam Phase Y already uses for the Opus call): a real
    ``extract_ceiling`` over ``ceiling_count`` synthetic items and the
    real ``compare_extractions`` / ``decide_control`` /
    ``build_false_negative_set``. No hand-rolled dict — a shape change
    in the orchestrator or any Y.1..Y.4 primitive breaks this factory
    (CLAUDE.md integration-test rule)."""
    import tempfile
    from pathlib import Path as _Path

    _scripts_on_path()
    import run_dec18_loop as z1  # type: ignore  # noqa: WPS433

    ceiling_items = _many_ceiling_items(ceiling_count)
    haiku_items = ceiling_items[:aligned]

    def _ceiling_extractor(_txt: str) -> Any:
        return make_opus_ceiling_artifact(items=ceiling_items)

    def _haiku_loader(_store: Any) -> Any:
        return make_opus_ceiling_artifact(items=haiku_items)

    with tempfile.TemporaryDirectory() as td:
        store = _Path(td) / "store"
        art, _code = z1.run_dec18_loop(
            transcript_text="injected — not read (ceiling seam wins)",
            store=store,
            api_key_present=True,
            transcript_present=True,
            ceiling_extractor=_ceiling_extractor,
            haiku_loader=_haiku_loader,
            comparator=z1._default_comparator,
            open_pr_lookup=lambda _t: [],
        )
    return art


def make_transcript_ingest_result(
    *,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    well_formed: bool = True,
) -> Any:
    """Produce a ``transcript_ingest_result`` via the REAL Z.4 path.

    Writes a synthetic transcript into a temp data-lake and runs
    ``ingest_corpus.ingest_one`` (the real validator + chunker +
    artifact writer), then reads the written envelope back so the
    factory output is byte-shape identical to a real ingest run."""
    import tempfile
    from pathlib import Path as _Path

    _scripts_on_path()
    import ingest_corpus as z4  # type: ignore  # noqa: WPS433
    from _phase_z_lake import (  # type: ignore  # noqa: WPS433
        latest_instrument,
    )

    if well_formed:
        turns = "\n".join(
            f"SPEAKER {chr(ord('A') + i % 5)}: point {i} about the "
            f"7 GHz downlink threshold and aggregate interference."
            for i in range(14)
        )
        text = "MEETING: 7 GHz Downlink TIG\n" + turns + "\n"
    else:
        text = (
            "MEETING: short\n"
            "SPEAKER A: only one turn here, far too short.\n"
        )

    with tempfile.TemporaryDirectory() as td:
        store = _Path(td) / "store"
        raw = store.parent / "raw" / "transcripts" / "fixture.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(text, encoding="utf-8")
        z4.ingest_one(
            entry={
                "id": transcript_id,
                "raw_path": "{data_lake}/raw/transcripts/fixture.txt",
            },
            lake_dir=str(store.parent),
            store=store,
            log=[],
        )
        env = latest_instrument(
            store, transcript_id, "transcript_ingest_result"
        )
        assert env is not None, "ingest_one wrote no result"
    from spectrum_systems_core.artifacts import new_artifact

    return new_artifact(
        artifact_type="transcript_ingest_result",
        payload=env["payload"],
        trace_id=env.get("trace_id", "fixture"),
        status="draft",
    )


def make_corpus_ingest_summary(
    *,
    n_present: int = 2,
    n_blocked: int = 1,
) -> Any:
    """Produce a ``corpus_ingest_summary`` via the REAL Z.4
    ``run_corpus_ingest`` over a synthetic temp corpus (1 malformed
    transcript among well-formed ones)."""
    import tempfile
    from pathlib import Path as _Path

    _scripts_on_path()
    import ingest_corpus as z4  # type: ignore  # noqa: WPS433
    import yaml as _yaml
    from _phase_z_lake import (  # type: ignore  # noqa: WPS433
        latest_corpus_instrument,
    )

    good = "MEETING: ok\n" + "\n".join(
        f"SPEAKER {chr(ord('A') + i % 4)}: substantive remark number "
        f"{i} on the coexistence framework and ERP working session."
        for i in range(16)
    )
    bad = "MEETING: bad\nSPEAKER A: too short.\n"

    with tempfile.TemporaryDirectory() as td:
        store = _Path(td) / "store"
        raw_dir = store.parent / "raw" / "transcripts"
        raw_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for i in range(n_present):
            (raw_dir / f"good{i}.txt").write_text(good, encoding="utf-8")
            entries.append(
                {
                    "id": f"m-2026-01-0{i + 1}-good-{i}",
                    "raw_path": f"{{data_lake}}/raw/transcripts/good{i}.txt",
                }
            )
        for i in range(n_blocked):
            (raw_dir / f"bad{i}.txt").write_text(bad, encoding="utf-8")
            entries.append(
                {
                    "id": f"m-2026-02-0{i + 1}-bad-{i}",
                    "raw_path": f"{{data_lake}}/raw/transcripts/bad{i}.txt",
                }
            )
        manifest = store.parent / "corpus_manifest.yaml"
        manifest.write_text(
            _yaml.safe_dump({"transcripts": entries}), encoding="utf-8"
        )
        z4.run_corpus_ingest(
            manifest_path=manifest,
            lake_dir=str(store.parent),
            store=store,
            log=[],
        )
        env = latest_corpus_instrument(store, "corpus_ingest_summary")
        assert env is not None
    from spectrum_systems_core.artifacts import new_artifact

    return new_artifact(
        artifact_type="corpus_ingest_summary",
        payload=env["payload"],
        trace_id=env.get("trace_id", "fixture"),
        status="draft",
    )


def make_corpus_improvement_summary(
    *,
    transcript_ids: list[str] | None = None,
    blocked_one: bool = True,
) -> Any:
    """Produce a ``corpus_improvement_summary`` via the REAL Z.5
    ``run_corpus_improvement_cycle`` with deterministic per-transcript
    seams (2 promoted + 1 blocked by default)."""
    from spectrum_systems_core.harness.improvement_cycle import (
        run_corpus_improvement_cycle,
    )

    tids = transcript_ids or [
        "m-2025-12-18-7ghz-downlink-tig-kickoff",
        "m-2025-11-20-ntia-coordination-session",
        "m-2026-04-01-bands",
    ]

    def _runner(tid: str) -> dict[str, Any]:
        if blocked_one and tid == tids[-1]:
            return {
                "overall_status": "blocked",
                "total_f1": None,
                "false_negative_count": None,
                "correction_candidates_produced": None,
                "blocking_phase": "Y_3",
                "error_or_none": None,
            }
        return {
            "overall_status": "promoted",
            "total_f1": 0.80,
            "false_negative_count": 2,
            "correction_candidates_produced": 1,
            "blocking_phase": None,
            "error_or_none": None,
        }

    return run_corpus_improvement_cycle(
        transcript_ids=tids,
        corpus_ingest_summary_loader=lambda: {
            "artifact_type": "corpus_ingest_summary",
            "schema_version": "1.0.0",
            "produced_at": "1970-01-01T00:00:00+00:00",
            "total_transcripts": len(tids),
            "present": len(tids),
            "blocked": 0,
            "blocked_ids": [],
        },
        per_transcript_runner=_runner,
        open_pr_lookup=lambda _t: [],
        cycle_id="fixture-corpus-0001",
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )


def make_decision_few_shot_placeholder(
    extraction_type: str = "decision",
) -> dict[str, Any]:
    """Produce the Phase V placeholder few-shot artifact.

    Mirrors the artifact that ships in the repo at
    ``data-lake/store/artifacts/evals/few_shot/decision_examples_v1.json``
    with ``verified: false`` placeholders. Tests use this as the seed
    state that ``scripts/select_few_shot_examples.py`` must overwrite
    with real decisions.
    """
    return {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": extraction_type,
        "verified": False,
        "created_at": _now_iso(),
        "examples": [
            {
                "example_id": "phase-v-placeholder-approval",
                "source_meeting_id": "phase-v-placeholder",
                "input_text": "placeholder",
                "expected_output": {"decision_outcome": "approval"},
                "verified": False,
                "verified_by": None,
                "verified_at": None,
            },
        ],
    }


def _proposer_context(
    *,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    trial_id: str = "trial-fixture-0001",
    prior_trial_ids: list[str] | None = None,
) -> Any:
    from spectrum_systems_core.harness.proposer import ProposerContext

    summaries = [
        {"trial_id": t, "total_f1": 0.60 + 0.05 * i}
        for i, t in enumerate(prior_trial_ids or ["trial-a", "trial-b"])
    ]
    return ProposerContext(
        transcript_id=transcript_id,
        current_trial_id=trial_id,
        score_summaries=summaries,
        experience_rows=[],
        harness_snapshot_paths=[],
        current_harness_files={},
        false_negative_set={},
        pareto_frontier=[],
    )


def make_harness_code_candidate_artifact(
    *,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    proposed_diff: str | None = None,
    valid: bool = True,
) -> Any:
    """Produce a ``harness_code_candidate`` via the REAL proposer
    builder + the REAL allowlist validator (no hand-rolled dict).

    The diff is run through ``validate_diff`` exactly as the AA.7
    driver does, and the resulting validation dict is embedded — so a
    shape change in the proposer builder or the validator breaks this
    factory (CLAUDE.md integration-test rule)."""
    from spectrum_systems_core.harness.harness_mutation_validator import (
        validate_diff,
    )
    from spectrum_systems_core.harness.proposer import (
        ProposerProposal,
        build_harness_code_candidate,
    )

    if proposed_diff is None:
        proposed_diff = (
            "diff --git a/src/spectrum_systems_core/extraction/chunker.py "
            "b/src/spectrum_systems_core/extraction/chunker.py\n"
            "--- a/src/spectrum_systems_core/extraction/chunker.py\n"
            "+++ b/src/spectrum_systems_core/extraction/chunker.py\n"
            "@@ -1 +1 @@\n"
            "-# old\n+# new chunking heuristic\n"
        )
    proposal = ProposerProposal(
        candidate_type="B",
        trial_ids_read=["trial-a", "trial-b"],
        proposer_reasoning="prior trials missed deferred decisions in "
        "long chunks",
        hypothesis="splitting chunks at speaker turns recovers the FN set",
        predicted_improvement="target F1 +0.08, no holdout regression",
        proposed_diff=proposed_diff,
    )
    ctx = _proposer_context(transcript_id=transcript_id)
    result = validate_diff(proposed_diff)
    return build_harness_code_candidate(
        proposal,
        ctx,
        allowlist_validation_result={
            "valid": result.valid,
            "reason": result.reason,
            "rejected_paths": list(result.rejected_paths),
            "touched_paths": list(result.touched_paths),
        },
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )


def make_harness_code_candidate_evaluation_artifact(
    *,
    target_transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    holdout_transcript_id: str = "m-2025-11-20-ntia-coordination-session",
    eligible: bool = True,
) -> Any:
    """Produce a ``harness_code_candidate_evaluation`` via the REAL
    AA.5 evaluator over a REAL allowlisted diff, with deterministic
    ceiling/baseline/patched seams chosen so the real comparator yields
    an eligible (or ineligible) verdict."""
    from spectrum_systems_core.evals.extraction_comparison import (
        contract_version,
    )
    from spectrum_systems_core.harness.code_candidate_evaluator import (
        evaluate_code_candidate,
    )

    diff = (
        "diff --git a/src/spectrum_systems_core/extraction/chunker.py "
        "b/src/spectrum_systems_core/extraction/chunker.py\n"
        "--- a/src/spectrum_systems_core/extraction/chunker.py\n"
        "+++ b/src/spectrum_systems_core/extraction/chunker.py\n"
        "@@ -1 +1 @@\n-# old\n+# improved\n"
    )
    candidate = make_harness_code_candidate_artifact(
        transcript_id=target_transcript_id, proposed_diff=diff
    )

    def _ceiling(tid: str) -> Any:
        return make_opus_ceiling_artifact(
            transcript_id=tid,
            items=[
                _ceiling_item("c-1", "decision", ["t1"], "alpha decision"),
                _ceiling_item("c-2", "decision", ["t2"], "beta decision"),
            ],
        )

    def _haiku(f1: float, tid: str) -> Any:
        if f1 >= 0.99:
            items = [
                _ceiling_item("h-1", "decision", ["t1"], "alpha decision"),
                _ceiling_item("h-2", "decision", ["t2"], "beta decision"),
            ]
        else:
            items = [
                _ceiling_item("h-x", "decision", ["t9"], "unrelated"),
            ]
        return make_opus_ceiling_artifact(transcript_id=tid, items=items)

    # eligible: baseline 0.0, candidate 1.0 on both -> +1.0 deltas.
    # ineligible: baseline 1.0, candidate 0.0 -> holdout regression.
    base_f1 = 0.0 if eligible else 1.0
    cand_f1 = 1.0 if eligible else 0.0

    def _apply_diff(_diff_text: str, _dest: Any) -> None:
        return None  # real apply is exercised in the AA.5 contract test

    return evaluate_code_candidate(
        candidate=candidate,
        target_transcript_id=target_transcript_id,
        ceiling_loader=_ceiling,
        baseline_loader=lambda tid: _haiku(base_f1, tid),
        patched_runner=lambda tid, _d: _haiku(cand_f1, tid),
        holdout_transcript_id=holdout_transcript_id,
        alignment_contract_version=contract_version(),
        apply_diff=_apply_diff,
    )


def make_harness_search_result_artifact(
    *,
    transcript_id: str = "m-2025-12-18-7ghz-downlink-tig-kickoff",
    preflight_ok: bool = False,
) -> Any:
    """Produce a ``harness_search_result`` via the REAL AA.7 driver.

    Default is the clean pre-flight halt (the sandbox-expected result):
    a valid artifact with iterations_completed: 0 and halt_reason:
    preflight_failed."""
    from spectrum_systems_core.harness.harness_search import (
        run_harness_search,
    )

    def _preflight() -> tuple[bool, str | None]:
        if preflight_ok:
            return (True, None)
        return (False, "no_trace_data_available — run the governed loop first")

    def _unreached(*_a, **_k):
        raise AssertionError("post-preflight seam should not be reached")

    return run_harness_search(
        transcript_id=transcript_id,
        iterations=1,
        preflight=_preflight,
        propose=_unreached,
        context_for=_unreached,
        evaluate_code=_unreached,
        route_prompt=_unreached,
        trigger_pr=_unreached,
        update_frontier=_unreached,
        search_id="search-fixture-0001",
        clock=lambda: "1970-01-01T00:00:00+00:00",
    )
