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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .eval_history import build_eval_records, write_eval_history
from .experience import build_experience_record, write_experience_history
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "process-meeting":
        workflows = tuple(args.workflows) if args.workflows else DEFAULT_WORKFLOWS
        result = process_meeting(
            lake_root=args.lake,
            meeting_id=args.meeting_id,
            workflows=workflows,
        )
        _print_result(result)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
