"""Markdown views over promoted artifacts.

The JSON written by `writer.write_promoted_artifact` is the canonical
artifact and the source of truth. The Markdown produced here is a
human-readable view of a promoted artifact and of the per-meeting run.
It exists so a person can read a meeting's outputs in Obsidian or any
plain text editor without parsing JSON.

Rules:
- Markdown is never used as input to the loop.
- Markdown is regenerated from the canonical JSON. Editing it does not
  change any artifact.
- Markdown lives under `processed/meetings/<meeting_id>/markdown/` so it
  is co-located with the JSON it views, but in its own subdirectory and
  not subject to the `<artifact_type>__<slug>.json` convention.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..artifacts import Artifact
from .loader import TranscriptInput
from .paths import processed_meeting_dir

MARKDOWN_SUBDIR = "markdown"
INDEX_FILENAME = "index.md"


def markdown_dir(lake_root: Path | str, meeting_id: str) -> Path:
    return processed_meeting_dir(lake_root, meeting_id) / MARKDOWN_SUBDIR


def artifact_markdown_filename(artifact_type: str) -> str:
    return f"{artifact_type}.md"


def _yaml_escape(value: str) -> str:
    """Quote a YAML scalar conservatively. We only need string values."""
    text = str(value)
    if any(ch in text for ch in ('"', "\\", "\n")):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if text == "" or text.startswith(" ") or text.endswith(" "):
        return f'"{text}"'
    return text


def _frontmatter(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key in ("artifact_type", "meeting_id", "date", "title", "status", "trace_id"):
        lines.append(f"{key}: {_yaml_escape(fields.get(key, ''))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _bullet_list(items: Iterable[Any]) -> str:
    rendered = [f"- {str(x).strip()}" for x in items if str(x).strip()]
    return "\n".join(rendered) if rendered else "_(none)_"


def _grounding_excerpts(payload: dict[str, Any]) -> str:
    grounding = payload.get("grounding") or []
    if not grounding:
        return "_(no source excerpts)_"
    lines: list[str] = []
    for entry in grounding:
        kind = entry.get("kind", "span")
        excerpt = (entry.get("source_excerpt") or "").strip()
        start = entry.get("start_line")
        suffix = f" (line {start})" if isinstance(start, int) else ""
        lines.append(f"- **{kind}**{suffix}: {excerpt}")
    return "\n".join(lines)


def _render_meeting_minutes(payload: dict[str, Any]) -> str:
    return (
        f"# {payload.get('title', '').strip() or 'Untitled meeting'}\n\n"
        f"## Summary\n\n{payload.get('summary', '').strip() or '_(no summary)_'}\n\n"
        f"## Decisions\n\n{_bullet_list(payload.get('decisions') or [])}\n\n"
        f"## Action items\n\n{_bullet_list(payload.get('action_items') or [])}\n\n"
        f"## Open questions\n\n{_bullet_list(payload.get('open_questions') or [])}\n\n"
        f"## Source excerpts\n\n{_grounding_excerpts(payload)}\n"
    )


def _render_decision_brief(payload: dict[str, Any]) -> str:
    return (
        f"# {payload.get('title', '').strip() or 'Untitled brief'}\n\n"
        f"## Context\n\n{payload.get('context', '').strip() or '_(no context)_'}\n\n"
        f"## Options\n\n{_bullet_list(payload.get('options') or [])}\n\n"
        f"## Recommendation\n\n{payload.get('recommendation', '').strip() or '_(none)_'}\n\n"
        f"## Rationale\n\n{payload.get('rationale', '').strip() or '_(none)_'}\n\n"
        f"## Source excerpts\n\n{_grounding_excerpts(payload)}\n"
    )


def _render_agency_question_summary(payload: dict[str, Any]) -> str:
    return (
        f"# {payload.get('title', '').strip() or 'Untitled inquiry'}\n\n"
        f"- **Agency:** {payload.get('agency', '').strip() or '_(unspecified)_'}\n\n"
        f"## Question\n\n{payload.get('question', '').strip() or '_(none)_'}\n\n"
        f"## Summary\n\n{payload.get('summary', '').strip() or '_(none)_'}\n\n"
        f"## Citations\n\n{_bullet_list(payload.get('citations') or [])}\n\n"
        f"## Source excerpts\n\n{_grounding_excerpts(payload)}\n"
    )


def _render_meeting_action_log(payload: dict[str, Any]) -> str:
    open_count = payload.get("open_count")
    open_count_text = str(open_count) if isinstance(open_count, int) else "0"
    return (
        f"# {payload.get('title', '').strip() or 'Untitled action log'}\n\n"
        f"- **Meeting ref:** {payload.get('meeting_ref', '').strip() or '_(unspecified)_'}\n"
        f"- **Open count:** {open_count_text}\n\n"
        f"## Actions\n\n{_bullet_list(payload.get('actions') or [])}\n\n"
        f"## Source excerpts\n\n{_grounding_excerpts(payload)}\n"
    )


_RENDERERS = {
    "meeting_minutes": _render_meeting_minutes,
    "decision_brief": _render_decision_brief,
    "agency_question_summary": _render_agency_question_summary,
    "meeting_action_log": _render_meeting_action_log,
}


def supported_artifact_types() -> tuple[str, ...]:
    return tuple(sorted(_RENDERERS))


def render_artifact_markdown(
    artifact: Artifact, *, transcript_input: TranscriptInput
) -> str:
    """Render one promoted artifact to a Markdown string with frontmatter."""
    if artifact.artifact_type not in _RENDERERS:
        raise ValueError(
            f"no markdown renderer for artifact_type {artifact.artifact_type!r}"
        )
    fields = {
        "artifact_type": artifact.artifact_type,
        "meeting_id": transcript_input.meeting_id,
        "date": transcript_input.date,
        "title": artifact.payload.get("title") or transcript_input.title,
        "status": artifact.status,
        "trace_id": artifact.trace_id,
    }
    body = _RENDERERS[artifact.artifact_type](artifact.payload)
    return _frontmatter(fields) + "\n" + body


def write_artifact_markdown(
    lake_root: Path | str,
    artifact: Artifact,
    *,
    transcript_input: TranscriptInput,
) -> Path:
    """Write the Markdown view for one promoted artifact. Returns the path."""
    out_dir = markdown_dir(lake_root, transcript_input.meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / artifact_markdown_filename(artifact.artifact_type)
    path.write_text(render_artifact_markdown(artifact, transcript_input=transcript_input),
                    encoding="utf-8")
    return path


_BLOCK_EXPLANATIONS: dict[str, str] = {
    "failed:transcript_evidence": (
        "no signal for this artifact type in the transcript"
    ),
    "failed:source_grounding": "grounding could not be verified against the transcript",
    "failed:non_empty_payload": "extractor produced an empty payload",
    "failed:content_signal": "non-transcript source had no content for this type",
    "missing_required_evals": "no eval results were produced for this artifact",
}


def _explain_reason(reason_code: str) -> str:
    if reason_code in _BLOCK_EXPLANATIONS:
        return _BLOCK_EXPLANATIONS[reason_code]
    if reason_code.startswith("empty_required_field:"):
        field = reason_code.split(":", 1)[1]
        return f"required field '{field}' was empty"
    if reason_code.startswith("missing_field:"):
        field = reason_code.split(":", 1)[1]
        return f"required field '{field}' was missing"
    return reason_code


def _index_trace_id(meeting_id: str) -> str:
    """Stable identifier for the meeting index frontmatter.

    The index spans multiple workflows, each with its own trace_id, so a
    single trace_id field would be misleading. We expose a deterministic
    `meeting-<meeting_id>` token so the field is meaningful when filtered
    in a vault.
    """
    return f"meeting-{meeting_id}"


def render_index_markdown(
    *,
    transcript_input: TranscriptInput,
    promoted: list[tuple[str, Artifact]],
    blocked: list[dict[str, Any]],
) -> str:
    """Render the per-meeting index Markdown.

    `promoted` is a list of (artifact_type, artifact) tuples for workflows
    whose artifact promoted. `blocked` is a list of
    `{"artifact_type", "reason_codes"}` dicts for workflows that did not
    promote.
    """
    fields = {
        "artifact_type": "meeting_index",
        "meeting_id": transcript_input.meeting_id,
        "date": transcript_input.date,
        "title": transcript_input.title,
        "status": "promoted" if promoted else "rejected",
        "trace_id": _index_trace_id(transcript_input.meeting_id),
    }
    front = _frontmatter(fields)

    body_lines = [
        f"# {transcript_input.title}",
        "",
        f"- **Meeting:** `{transcript_input.meeting_id}`",
        f"- **Date:** {transcript_input.date}",
        f"- **Source type:** {transcript_input.source_type}",
        "",
        "## Promoted artifacts",
        "",
    ]
    if promoted:
        for artifact_type, artifact in sorted(promoted, key=lambda p: p[0]):
            link = artifact_markdown_filename(artifact_type)
            body_lines.append(
                f"- [{artifact_type}]({link}) — trace `{artifact.trace_id}`"
            )
    else:
        body_lines.append("_(none)_")

    body_lines.append("")
    body_lines.append("## Blocked workflows")
    body_lines.append("")
    if blocked:
        for entry in sorted(blocked, key=lambda d: d["artifact_type"]):
            codes = entry.get("reason_codes") or []
            if codes:
                reason_str = ", ".join(codes)
                explanation = "; ".join(_explain_reason(c) for c in codes)
                body_lines.append(
                    f"- **{entry['artifact_type']}**: {reason_str} "
                    f"({explanation})"
                )
            else:
                body_lines.append(f"- **{entry['artifact_type']}**: blocked")
    else:
        body_lines.append("_(none)_")

    body_lines.append("")
    return front + "\n" + "\n".join(body_lines) + "\n"


def write_index_markdown(
    lake_root: Path | str,
    *,
    transcript_input: TranscriptInput,
    promoted: list[tuple[str, Artifact]],
    blocked: list[dict[str, Any]],
) -> Path:
    out_dir = markdown_dir(lake_root, transcript_input.meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / INDEX_FILENAME
    path.write_text(
        render_index_markdown(
            transcript_input=transcript_input,
            promoted=promoted,
            blocked=blocked,
        ),
        encoding="utf-8",
    )
    return path
