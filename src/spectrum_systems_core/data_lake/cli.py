"""Command-line entry point: process one meeting end-to-end.

Usage:

    spectrum-core process-meeting --lake <path> --meeting-id <meeting_id>

Default behavior: run all four supported workflows over the same raw
inputs in turn, write any promoted JSON artifacts via the existing
writer, and render Markdown views for the promoted artifacts plus a
per-meeting index, agency / topic notes, and run notes. JSON remains the
canonical artifact; Markdown is a regenerated view.

Harness memory:
- run_history.jsonl, experience_history.jsonl, eval_history.jsonl are
  append-style projections of the run records into easy-to-scan files.
  They are memory, not authority. The control function and promotion
  gate are unchanged.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import LLMConfigError, llm_extraction_enabled
from .eval_history import build_eval_records, write_eval_history
from .experience import build_experience_record, write_experience_history
from .index import write_artifact_index
from .loader import load_meeting
from .markdown import (
    supported_artifact_types,
    write_agency_markdown,
    write_artifact_markdown,
    write_index_markdown,
    write_topic_markdown,
)
from .pipeline import PipelineResult, run_transcript_pipeline
from .run_history import (
    build_run_record,
    run_note_markdown_relpath,
    write_run_history,
    write_run_note_markdown,
)
from .writer import write_promoted_artifact

# artifact_type token of the live-LLM arm's eval_history rows. The arm
# still produces a "meeting_minutes" envelope; this names the producer
# so an eval_history reader can tell the LLM rows from the regex rows.
LLM_WORKFLOW_NAME = "meeting_minutes_llm"

DEFAULT_WORKFLOWS: tuple[str, ...] = (
    "meeting_minutes",
    "meeting_action_log",
    "agency_question_summary",
    "decision_brief",
)


@dataclass
class ProcessMeetingResult:
    meeting_id: str
    lake_root: Path
    pipeline_results: list[PipelineResult]
    markdown_paths: list[Path]
    index_path: Path
    agency_paths: list[Path] = field(default_factory=list)
    topic_paths: list[Path] = field(default_factory=list)
    run_history_path: Path | None = None
    experience_history_path: Path | None = None
    eval_history_path: Path | None = None
    run_note_paths: list[Path] = field(default_factory=list)

    @property
    def promoted_workflows(self) -> list[str]:
        return [r.workflow_name for r in self.pipeline_results if r.promoted]

    @property
    def blocked_workflows(self) -> list[str]:
        return [r.workflow_name for r in self.pipeline_results if not r.promoted]


def _collect_agencies(
    transcript_input,
    pipeline_results: list[PipelineResult],
) -> dict[str, list[str]]:
    """Return {agency_value -> [artifact_types referencing it]}.

    Only promoted artifacts contribute artifact_types so the per-agency
    note never advertises blocked workflows as references. The metadata
    `agency` field also seeds the dict so the note exists even when the
    transcript itself didn't mention the agency.
    """
    agencies: dict[str, list[str]] = {}
    meta_agency = transcript_input.metadata.get("agency")
    if isinstance(meta_agency, str) and meta_agency.strip():
        agencies.setdefault(meta_agency.strip(), [])

    for r in pipeline_results:
        if not r.promoted:
            continue
        payload_agency = r.target.payload.get("agency")
        if isinstance(payload_agency, str) and payload_agency.strip():
            agencies.setdefault(payload_agency.strip(), []).append(
                r.target.artifact_type
            )
    return agencies


def _collect_topics(
    transcript_input,
    pipeline_results: list[PipelineResult],
) -> dict[str, list[str]]:
    topics: dict[str, list[str]] = {}
    meta_topic = transcript_input.metadata.get("topic")
    if isinstance(meta_topic, str) and meta_topic.strip():
        topics.setdefault(meta_topic.strip(), [])
    for r in pipeline_results:
        if not r.promoted:
            continue
        payload_topic = r.target.payload.get("topic")
        if isinstance(payload_topic, str) and payload_topic.strip():
            topics.setdefault(payload_topic.strip(), []).append(
                r.target.artifact_type
            )
    return topics


def process_meeting(
    *,
    lake_root: Path | str,
    meeting_id: str,
    workflows: Sequence[str] = DEFAULT_WORKFLOWS,
) -> ProcessMeetingResult:
    """Run the configured workflows on one meeting; write JSON + Markdown.

    Returns a small result object summarizing what was promoted, what was
    blocked, and where outputs landed. JSON artifacts are written by the
    existing pipeline writer; Markdown is a separate, regenerated view.
    """
    lake_root = Path(lake_root)
    transcript_input = load_meeting(lake_root, meeting_id)

    pipeline_results: list[PipelineResult] = []
    promoted_pairs: list[tuple[str, Any]] = []
    blocked_entries: list[dict[str, Any]] = []
    markdown_paths: list[Path] = []
    canonical_json_paths: dict[str, str] = {}

    for workflow_name in workflows:
        result = run_transcript_pipeline(
            lake_root=lake_root,
            transcript_input=transcript_input,
            workflow_name=workflow_name,
            write_outputs=True,
        )
        pipeline_results.append(result)

        if result.promoted and result.target.artifact_type in supported_artifact_types():
            json_path = result.written_paths[0] if result.written_paths else None
            md_path = write_artifact_markdown(
                lake_root,
                result.target,
                transcript_input=transcript_input,
                canonical_json_path=json_path,
            )
            markdown_paths.append(md_path)
            promoted_pairs.append((result.target.artifact_type, result.target))
            if json_path:
                canonical_json_paths[result.target.artifact_type] = (
                    Path(json_path).name
                )
        else:
            reason_codes = list(
                result.control_decision.payload.get("reason_codes", [])
            )
            for ev in result.eval_results:
                if ev.payload.get("status") == "fail":
                    for rc in ev.payload.get("reason_codes", []):
                        if rc not in reason_codes:
                            reason_codes.append(rc)
            blocked_entries.append(
                {
                    "artifact_type": workflow_name,
                    "reason_codes": reason_codes,
                }
            )

    # Harness memory: build records first, then write each JSONL once.
    run_records: list[dict[str, Any]] = []
    experience_records: list[dict[str, Any]] = []
    eval_records: list[dict[str, Any]] = []
    run_note_paths: list[Path] = []
    for r in pipeline_results:
        run_md_relpath = run_note_markdown_relpath(r.run_id)
        record = build_run_record(r, run_markdown_path=run_md_relpath)
        run_records.append(record)
        run_note_paths.append(
            write_run_note_markdown(
                lake_root,
                meeting_id=meeting_id,
                record=record,
            )
        )
        experience_records.append(build_experience_record(r))
        eval_records.extend(build_eval_records(r))

    run_history_path_out = write_run_history(
        lake_root, meeting_id=meeting_id, records=run_records
    )
    experience_history_path_out = write_experience_history(
        lake_root, meeting_id=meeting_id, records=experience_records
    )
    eval_history_path_out = write_eval_history(
        lake_root, meeting_id=meeting_id, records=eval_records
    )

    # Agency / topic notes (per-meeting, view-only).
    agency_paths: list[Path] = []
    for agency, artifact_types in _collect_agencies(
        transcript_input, pipeline_results
    ).items():
        agency_paths.append(
            write_agency_markdown(
                lake_root,
                transcript_input=transcript_input,
                agency=agency,
                referenced_artifact_types=artifact_types,
            )
        )

    topic_paths: list[Path] = []
    for topic, artifact_types in _collect_topics(
        transcript_input, pipeline_results
    ).items():
        topic_paths.append(
            write_topic_markdown(
                lake_root,
                transcript_input=transcript_input,
                topic=topic,
                referenced_artifact_types=artifact_types,
            )
        )

    index_path = write_index_markdown(
        lake_root,
        transcript_input=transcript_input,
        promoted=promoted_pairs,
        blocked=blocked_entries,
        canonical_json_paths=canonical_json_paths,
        run_records=run_records,
    )

    return ProcessMeetingResult(
        meeting_id=meeting_id,
        lake_root=lake_root,
        pipeline_results=pipeline_results,
        markdown_paths=markdown_paths,
        index_path=index_path,
        agency_paths=agency_paths,
        topic_paths=topic_paths,
        run_history_path=run_history_path_out,
        experience_history_path=experience_history_path_out,
        eval_history_path=eval_history_path_out,
        run_note_paths=run_note_paths,
    )


def _print_result(result: ProcessMeetingResult, stream=None) -> None:
    out = stream if stream is not None else sys.stdout
    print(f"meeting_id: {result.meeting_id}", file=out)
    print(f"lake_root: {result.lake_root}", file=out)
    print("", file=out)
    print("workflows:", file=out)
    for r in result.pipeline_results:
        status = "promoted" if r.promoted else "blocked"
        reasons = ""
        if not r.promoted:
            codes = r.control_decision.payload.get("reason_codes") or []
            if codes:
                reasons = f"  ({', '.join(codes)})"
        print(f"  - {r.workflow_name}: {status}{reasons}", file=out)
    print("", file=out)
    if result.markdown_paths:
        print("markdown:", file=out)
        for p in result.markdown_paths:
            print(f"  - {p}", file=out)
    print(f"index: {result.index_path}", file=out)
    if result.run_history_path:
        print(f"run_history: {result.run_history_path}", file=out)
    if result.experience_history_path:
        print(f"experience_history: {result.experience_history_path}", file=out)
    if result.eval_history_path:
        print(f"eval_history: {result.eval_history_path}", file=out)


def _print_llm_result(
    result: Any,
    *,
    meeting_id: str,
    source_id: str,
    produced_by: Any,
    written_path: Path | None,
    eval_history_path: Path | None,
    stream=None,
) -> None:
    out = stream if stream is not None else sys.stdout
    payload = result.meeting_minutes.payload
    payload = payload if isinstance(payload, dict) else {}

    print(f"meeting_id: {meeting_id}", file=out)
    print(f"source_id: {source_id}", file=out)
    print(f"provenance.produced_by: {produced_by}", file=out)
    decision = result.control_decision.payload.get("decision")
    decision_reasons = result.control_decision.payload.get("reason_codes") or []
    print(
        f"control decision: {decision}"
        + (f"  ({', '.join(decision_reasons)})" if decision_reasons else ""),
        file=out,
    )
    print(f"promoted: {result.promoted}", file=out)
    if written_path is not None:
        print(f"promoted_artifact: {written_path}", file=out)
    if eval_history_path is not None:
        print(f"eval_history: {eval_history_path}", file=out)
    print("", file=out)

    counts: dict[str, int] = {}
    for key in ("decisions", "action_items", "open_questions"):
        items = payload.get(key)
        items = items if isinstance(items, list) else []
        counts[key] = len(items)
        print(f"{key} ({len(items)}):", file=out)
        for item in items:
            print(f"  - {item}", file=out)
    print("", file=out)

    print("evals:", file=out)
    cov_percent = None
    cov_scored = None
    cov_matched = None
    in_source = None
    not_in_source = None
    for ev in result.eval_results:
        ep = ev.payload
        line = f"  - {ep.get('eval_type')}: {ep.get('status')}"
        rc = ep.get("reason_codes") or []
        if rc:
            line += f"  ({', '.join(str(c) for c in rc)})"
        print(line, file=out)
        if "items_in_source" in ep:
            in_source = ep.get("items_in_source")
            not_in_source = ep.get("items_not_in_source")
            print(
                f"      items_in_source={in_source} "
                f"items_not_in_source={not_in_source}",
                file=out,
            )
        if "coverage_percent" in ep:
            cov_percent = ep.get("coverage_percent")
            cov_scored = ep.get("gt_pairs_scored")
            cov_matched = ep.get("gt_pairs_matched")
            print(
                f"      coverage_percent={cov_percent} "
                f"threshold={ep.get('threshold')} "
                f"gt_pairs_scored={cov_scored} "
                f"gt_pairs_matched={cov_matched}",
                file=out,
            )
    print("", file=out)

    # Honest reporting: in_source and coverage_percent are AGGREGATES
    # (over all extracted items / all scored GT pairs), not per-type
    # numbers. The table carries per-type Count only; the aggregates are
    # printed once, clearly labelled, so nothing is presented as a
    # per-type metric the evals never computed.
    print("summary:", file=out)
    print("  Extraction type | Count", file=out)
    print(f"  decisions       | {counts['decisions']}", file=out)
    print(f"  action_items    | {counts['action_items']}", file=out)
    print(f"  open_questions  | {counts['open_questions']}", file=out)
    if in_source is not None:
        print(
            f"  within-source (aggregate): items_in_source={in_source} "
            f"items_not_in_source={not_in_source}",
            file=out,
        )
    if cov_percent is not None:
        print(
            "  coverage vs human GT pairs (aggregate over "
            f"decision+action_item+claim): coverage_percent={cov_percent} "
            f"({cov_matched}/{cov_scored} pairs matched)",
            file=out,
        )


def process_meeting_llm(
    *,
    lake_root: Path | str,
    meeting_id: str,
    source_id: str | None = None,
    client: Any = None,
    env: Mapping[str, str] | None = None,
    stream=None,
) -> int:
    """Run the live-LLM ``meeting_minutes`` arm for one meeting.

    Reached from :func:`main` only when ``llm_extraction_enabled``
    resolves True (the ``--llm`` in-process override or the governed
    data-lake flag artifact). The deterministic regex
    :func:`process_meeting` path is intentionally left byte-for-byte
    unchanged — this is a separate entry so the golden /
    validate-and-baseline signals are untouched.

    Fail-closed: a missing ``ANTHROPIC_API_KEY`` (or any dispatch
    pre-run guard) prints a machine-readable ``reason_code=...`` line
    and returns a non-zero exit BEFORE any artifact is produced. It
    never silently falls back to the regex extractor — mutual exclusion
    holds even on the error path.

    ``client`` / ``env`` are injection seams for the contract test
    (a deterministic stub + an explicit env) so the success path is
    exercised in CI with no API key and no network. Production passes
    neither: the real Anthropic client is constructed only after the
    fail-closed pre-run gate passes.
    """
    out = stream if stream is not None else sys.stdout
    lake_root = Path(lake_root)
    source_id = source_id or meeting_id

    transcript_input = load_meeting(lake_root, meeting_id)

    # Lazy import: data_lake/__init__ imports this module last; importing
    # ..workflows at module top would risk an import cycle through that
    # partially-initialised package. Inside the function it is safe and
    # mirrors the lazy-SDK pattern in workflows/llm_client.py.
    from ..workflows import WorkflowDispatchError, run_meeting_minutes_dispatch
    from ..workflows.llm_eval_history import (
        build_eval_records as build_llm_eval_records,
    )
    from ..workflows.llm_eval_history import (
        write_eval_history as write_llm_eval_history,
    )

    try:
        result = run_meeting_minutes_dispatch(
            transcript_input.transcript_text,
            llm_enabled=True,
            client=client,
            meeting_id=meeting_id,
            source_id=source_id,
            lake_root=lake_root,
            env=env if env is not None else os.environ,
        )
    except LLMConfigError as exc:
        print(
            f"llm_extraction halted pre-run: reason_code={exc.reason_code} "
            f"-- {exc}",
            file=out,
        )
        return 2
    except WorkflowDispatchError as exc:
        print(
            "llm_extraction halted pre-run: reason_code=dispatch_error "
            f"-- {exc}",
            file=out,
        )
        return 2

    mm = result.meeting_minutes
    mm_payload = mm.payload if isinstance(mm.payload, dict) else {}
    produced_by = (mm_payload.get("provenance") or {}).get("produced_by")

    written_path: Path | None = None
    eval_history_path: Path | None = None
    if result.promoted:
        # Persist exactly as the LLM integration contract does: promoted
        # JSON + rebuilt index + eval_history projection. The LLM arm
        # deliberately does NOT run the deterministic pipeline, so the
        # markdown / run_history / experience machinery (which is
        # PipelineResult-shaped) is not invoked here.
        written_path = write_promoted_artifact(lake_root, mm)
        write_artifact_index(lake_root)
        records = build_llm_eval_records(
            result, meeting_id=source_id, workflow_name=LLM_WORKFLOW_NAME
        )
        eval_history_path = write_llm_eval_history(
            lake_root, source_id=source_id, records=records
        )

    _print_llm_result(
        result,
        meeting_id=meeting_id,
        source_id=source_id,
        produced_by=produced_by,
        written_path=written_path,
        eval_history_path=eval_history_path,
        stream=out,
    )
    # Non-zero on a blocked run so an operator / CI sees the gate held;
    # the artifact was not promoted and nothing was persisted.
    return 0 if result.promoted else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectrum-core",
        description=(
            "Spectrum Systems Core CLI. Runs the governed Produce -> "
            "Evaluate -> Decide -> Promote loop over a meeting in the "
            "data lake and renders Markdown views for promoted artifacts."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pm = sub.add_parser(
        "process-meeting",
        help="Process one meeting from the data lake.",
        description=(
            "Run all default workflows on a single meeting in the data "
            "lake. Writes promoted JSON artifacts, run manifests, debug "
            "reports, Markdown views, a per-meeting Markdown index, "
            "agency / topic notes, run notes, and harness-memory JSONL "
            "files (run_history, experience_history, eval_history)."
        ),
    )
    pm.add_argument(
        "--lake",
        required=True,
        help="Path to the data lake root.",
    )
    pm.add_argument(
        "--meeting-id",
        required=True,
        help="Meeting id (the directory name under raw/meetings/).",
    )
    pm.add_argument(
        "--workflow",
        action="append",
        dest="workflows",
        choices=DEFAULT_WORKFLOWS,
        help=(
            "Restrict to a specific workflow. Repeatable. Defaults to all "
            "four supported workflows."
        ),
    )
    pm.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Route the meeting_minutes workflow through the live-LLM "
            "extractor. Resolves the llm_extraction flag via the "
            "in-process override (the code default stays False; this "
            "does not edit it). Fail-closed: if ANTHROPIC_API_KEY is "
            "unset the run halts pre-run with reason_code=config_error "
            "and never falls back to the regex extractor."
        ),
    )
    pm.add_argument(
        "--source-id",
        default=None,
        help=(
            "Source id for the human-GT coverage eval and eval_history "
            "(defaults to --meeting-id). Only consulted on the --llm path."
        ),
    )

    ce = sub.add_parser(
        "compare-extraction",
        help="Phase AB: three-point extraction comparison instrument.",
        description=(
            "Run the regex / Haiku / Opus extractors over one meeting's "
            "transcript and write extraction_comparison, "
            "extraction_telemetry, and (on Opus success) "
            "extraction_unconstrained instrument artifacts plus a "
            "Markdown report. Provide exactly one of --meeting-id "
            "(reads the lake; source_record must already exist) or "
            "--transcript-file (reads a flat file; meeting_id is "
            "derived from the slugified filename stem). Fail-closed: a "
            "missing/empty ANTHROPIC_API_KEY, an invalid source "
            "selector, or a missing source_record halts the run before "
            "any API call and writes NO artifact. Exit 1 on any "
            "pre-flight or extractor failure (comparison status "
            "'rejected')."
        ),
    )
    ce.add_argument("--lake", required=True, help="Path to the data lake root.")
    ce.add_argument(
        "--meeting-id",
        help="Meeting id (directory under raw/meetings/; source_record "
        "must already exist under processed/meetings/). Mutually "
        "exclusive with --transcript-file; provide exactly one.",
    )
    ce.add_argument(
        "--transcript-file",
        help="Path to a flat transcript file. meeting_id is derived "
        "from the slugified filename stem; no source_record is "
        "required. Mutually exclusive with --meeting-id; provide "
        "exactly one.",
    )

    cc = sub.add_parser(
        "compare-corpus",
        help="Phase AC: corpus-wide per-entity extraction comparison.",
        description=(
            "Run the regex / Haiku / Opus extractors over EVERY .txt "
            "transcript under --transcripts and write one "
            "corpus_comparison instrument artifact (per-meeting + "
            "aggregate per-entity F1 for decisions / actions / "
            "questions) plus a Markdown projection. Per-entity F1 is "
            "computed only for a meeting that has a sibling "
            "independent_gold.json (the Phase AB.4 comparison_gold "
            "layout); meetings without gold are recorded with "
            "per_entity_f1=null and excluded from the aggregate mean. "
            "Fail-closed: a missing/empty ANTHROPIC_API_KEY or a "
            "transcripts dir with no .txt files halts before any API "
            "call and writes NO artifact. A per-transcript failure is "
            "recorded and the run continues; corpus_status is "
            "complete / degraded / rejected (rejected exits 1)."
        ),
    )
    cc.add_argument(
        "--lake", required=True, help="Path to the data lake root."
    )
    cc.add_argument(
        "--transcripts",
        required=True,
        help="Directory of .txt transcripts (searched recursively). "
        "Non-.txt files are skipped with a finding; a sibling "
        "independent_gold.json next to a transcript enables its "
        "per-entity F1.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "process-meeting":
        # Resolution order (config.feature_flags.llm_extraction_enabled):
        # 1. --llm  -> in-process override=True (wins outright).
        # 2. else    -> the governed data-lake flag artifact at
        #    <lake>/store/artifacts/config/llm_extraction_enabled.json.
        # 3. else    -> False (the unchanged regex path).
        # The code-level default is never edited here.
        enabled = llm_extraction_enabled(
            override=True if args.llm else None,
            data_lake_path=args.lake,
        )
        if enabled:
            return process_meeting_llm(
                lake_root=args.lake,
                meeting_id=args.meeting_id,
                source_id=args.source_id,
            )
        workflows = tuple(args.workflows) if args.workflows else DEFAULT_WORKFLOWS
        result = process_meeting(
            lake_root=args.lake,
            meeting_id=args.meeting_id,
            workflows=workflows,
        )
        _print_result(result)
        return 0

    if args.command == "compare-extraction":
        # Lazy import: keeps the anthropic-backed adapters out of the
        # import path for the common process-meeting command.
        from ..extraction.comparison_runner import run_compare_extraction

        return run_compare_extraction(
            lake_root=args.lake,
            meeting_id=args.meeting_id,
            transcript_file=args.transcript_file,
        )

    if args.command == "compare-corpus":
        # Lazy import for the same reason as compare-extraction: keeps
        # the anthropic-backed adapters off the common command path.
        from ..extraction.corpus_runner import run_compare_corpus

        return run_compare_corpus(
            lake_root=args.lake,
            transcripts_dir=args.transcripts,
        )

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
