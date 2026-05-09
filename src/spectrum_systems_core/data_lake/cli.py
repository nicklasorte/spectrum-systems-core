"""Command-line entry point: process one meeting end-to-end.

Usage:

    spectrum-core process-meeting --lake <path> --meeting-id <meeting_id>

Default behavior: run all four supported workflows over the same raw
inputs in turn, write any promoted JSON artifacts via the existing
writer, and render Markdown views for the promoted artifacts plus a
per-meeting index. JSON remains the canonical artifact; Markdown is a
view.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .loader import load_meeting
from .markdown import (
    supported_artifact_types,
    write_artifact_markdown,
    write_index_markdown,
)
from .pipeline import PipelineResult, run_transcript_pipeline

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

    @property
    def promoted_workflows(self) -> list[str]:
        return [r.workflow_name for r in self.pipeline_results if r.promoted]

    @property
    def blocked_workflows(self) -> list[str]:
        return [r.workflow_name for r in self.pipeline_results if not r.promoted]


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

    for workflow_name in workflows:
        result = run_transcript_pipeline(
            lake_root=lake_root,
            transcript_input=transcript_input,
            workflow_name=workflow_name,
            write_outputs=True,
        )
        pipeline_results.append(result)

        if result.promoted and result.target.artifact_type in supported_artifact_types():
            md_path = write_artifact_markdown(
                lake_root,
                result.target,
                transcript_input=transcript_input,
            )
            markdown_paths.append(md_path)
            promoted_pairs.append((result.target.artifact_type, result.target))
        else:
            reason_codes = list(
                result.control_decision.payload.get("reason_codes", [])
            )
            blocked_entries.append(
                {
                    "artifact_type": workflow_name,
                    "reason_codes": reason_codes,
                }
            )

    index_path = write_index_markdown(
        lake_root,
        transcript_input=transcript_input,
        promoted=promoted_pairs,
        blocked=blocked_entries,
    )

    return ProcessMeetingResult(
        meeting_id=meeting_id,
        lake_root=lake_root,
        pipeline_results=pipeline_results,
        markdown_paths=markdown_paths,
        index_path=index_path,
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
            "reports, Markdown views, and a per-meeting Markdown index."
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
