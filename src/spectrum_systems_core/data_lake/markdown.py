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
- Layout (binding for SSC-025):
    markdown/index.md
    markdown/artifacts/<artifact_type>.md
    markdown/agencies/<slug>.md      (when an agency value is known)
    markdown/topics/<slug>.md        (when a topic value is known)
    markdown/runs/<run_id>.md        (run history; SSC-031)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..artifacts import Artifact
from .loader import TranscriptInput
from .paths import processed_meeting_dir
from .serialize import slugify

MARKDOWN_SUBDIR = "markdown"
ARTIFACTS_SUBDIR = "artifacts"
AGENCIES_SUBDIR = "agencies"
TOPICS_SUBDIR = "topics"
RUNS_SUBDIR = "runs"
INDEX_FILENAME = "index.md"


def markdown_dir(lake_root: Path | str, meeting_id: str) -> Path:
    return processed_meeting_dir(lake_root, meeting_id) / MARKDOWN_SUBDIR


def artifacts_markdown_dir(lake_root: Path | str, meeting_id: str) -> Path:
    return markdown_dir(lake_root, meeting_id) / ARTIFACTS_SUBDIR


def agencies_markdown_dir(lake_root: Path | str, meeting_id: str) -> Path:
    return markdown_dir(lake_root, meeting_id) / AGENCIES_SUBDIR


def topics_markdown_dir(lake_root: Path | str, meeting_id: str) -> Path:
    return markdown_dir(lake_root, meeting_id) / TOPICS_SUBDIR


def runs_markdown_dir(lake_root: Path | str, meeting_id: str) -> Path:
    return markdown_dir(lake_root, meeting_id) / RUNS_SUBDIR


def artifact_markdown_filename(artifact_type: str) -> str:
    return f"{artifact_type}.md"


def artifact_markdown_path(
    lake_root: Path | str, meeting_id: str, artifact_type: str
) -> Path:
    return (
        artifacts_markdown_dir(lake_root, meeting_id)
        / artifact_markdown_filename(artifact_type)
    )


def agency_markdown_filename(agency: str) -> str:
    return f"{slugify(agency)}.md"


def topic_markdown_filename(topic: str) -> str:
    return f"{slugify(topic)}.md"


def run_markdown_filename(run_id: str) -> str:
    return f"{run_id}.md"


def _yaml_escape(value: str) -> str:
    """Quote a YAML scalar conservatively. We only need string values."""
    text = str(value)
    if any(ch in text for ch in ('"', "\\", "\n")):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if text == "" or text.startswith(" ") or text.endswith(" "):
        return f'"{text}"'
    return text


def _yaml_list(values: Iterable[str]) -> str:
    inner = ", ".join(_yaml_escape(str(v)) for v in values)
    return f"[{inner}]"


def _emit_frontmatter(ordered_keys: list[str], fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key in ordered_keys:
        if key not in fields:
            continue
        value = fields[key]
        if isinstance(value, list):
            lines.append(f"{key}: {_yaml_list(value)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {_yaml_escape(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


_ARTIFACT_FRONTMATTER_KEYS: list[str] = [
    "artifact_type",
    "artifact_id",
    "meeting_id",
    "date",
    "title",
    "status",
    "trace_id",
    "content_hash",
    "canonical_json_path",
]


_INDEX_FRONTMATTER_KEYS: list[str] = [
    "artifact_type",
    "meeting_id",
    "date",
    "title",
    "status",
    "trace_id",
    "canonical",
]


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


_BACKLINK_NOTE = (
    "> JSON is the canonical source of truth for this artifact. "
    "This Markdown is a regenerated view: editing it does not change "
    "any artifact, and core never reads it back."
)

# Artifact Markdown lives at .../markdown/artifacts/<type>.md, so the
# index it backlinks is one level up. If the layout depth changes,
# update this constant and the relative paths it controls.
_ARTIFACT_MD_TO_INDEX_RELPATH = "../index.md"
_CANONICAL_JSON_PATH_UNWRITTEN = "(unwritten)"


def _backlinks_block(
    *,
    canonical_json_relpath: str,
    agencies: list[str],
    topics: list[str],
) -> str:
    lines: list[str] = ["## Links", ""]
    lines.append(f"- Index: [meeting index]({_ARTIFACT_MD_TO_INDEX_RELPATH})")
    if canonical_json_relpath == _CANONICAL_JSON_PATH_UNWRITTEN:
        lines.append(
            "- Canonical JSON: _(not written — the artifact has not been "
            "promoted to disk)_"
        )
    else:
        lines.append(
            f"- Canonical JSON: [{canonical_json_relpath}]({canonical_json_relpath})"
        )
    lines.append(f"- Meeting wikilink: [[Meeting/{{meeting_id}}]]")
    if agencies:
        for agency in agencies:
            lines.append(
                f"- Agency: [{agency}](../{AGENCIES_SUBDIR}/{agency_markdown_filename(agency)}) "
                f"— [[Agency/{agency}]]"
            )
    if topics:
        for topic in topics:
            lines.append(
                f"- Topic: [{topic}](../{TOPICS_SUBDIR}/{topic_markdown_filename(topic)}) "
                f"— [[Topic/{topic}]]"
            )
    return "\n".join(lines) + "\n"


def _resolve_metadata_agencies(
    transcript_input: TranscriptInput, payload: dict[str, Any]
) -> list[str]:
    agencies: list[str] = []
    meta_agency = transcript_input.metadata.get("agency")
    if isinstance(meta_agency, str) and meta_agency.strip():
        agencies.append(meta_agency.strip())
    payload_agency = payload.get("agency")
    if isinstance(payload_agency, str) and payload_agency.strip():
        if payload_agency.strip() not in agencies:
            agencies.append(payload_agency.strip())
    return agencies


def _resolve_metadata_topics(
    transcript_input: TranscriptInput,
) -> list[str]:
    topics: list[str] = []
    meta_topic = transcript_input.metadata.get("topic")
    if isinstance(meta_topic, str) and meta_topic.strip():
        topics.append(meta_topic.strip())
    return topics


def _canonical_json_relpath_from_artifact_md(json_path: Path | str | None) -> str:
    """Path the artifact Markdown file uses to point at the canonical JSON.

    Artifact Markdown lives at .../markdown/artifacts/<type>.md.
    JSON lives at .../<type>__<slug>.json, two levels up.

    When `json_path is None` we return a sentinel string rather than the
    empty string so a frontmatter reader can tell "not written" from
    "missing field". Fix M1 from `ssc_next_memory_redteam_1.md`.
    """
    if json_path is None:
        return _CANONICAL_JSON_PATH_UNWRITTEN
    return f"../../{Path(json_path).name}"


def render_artifact_markdown(
    artifact: Artifact,
    *,
    transcript_input: TranscriptInput,
    canonical_json_path: Path | str | None = None,
) -> str:
    """Render one promoted artifact to a Markdown string with frontmatter."""
    if artifact.artifact_type not in _RENDERERS:
        raise ValueError(
            f"no markdown renderer for artifact_type {artifact.artifact_type!r}"
        )
    json_relpath = _canonical_json_relpath_from_artifact_md(canonical_json_path)
    fields: dict[str, Any] = {
        "artifact_type": artifact.artifact_type,
        "artifact_id": artifact.artifact_id,
        "meeting_id": transcript_input.meeting_id,
        "date": transcript_input.date,
        "title": artifact.payload.get("title") or transcript_input.title,
        "status": artifact.status,
        "trace_id": artifact.trace_id,
        "content_hash": artifact.content_hash,
        "canonical_json_path": json_relpath,
    }
    front = _emit_frontmatter(_ARTIFACT_FRONTMATTER_KEYS, fields)
    body = _RENDERERS[artifact.artifact_type](artifact.payload)

    agencies = _resolve_metadata_agencies(transcript_input, artifact.payload)
    topics = _resolve_metadata_topics(transcript_input)

    backlinks = _backlinks_block(
        canonical_json_relpath=json_relpath or "(unwritten)",
        agencies=agencies,
        topics=topics,
    ).replace("{meeting_id}", transcript_input.meeting_id)

    return front + "\n" + body + "\n" + _BACKLINK_NOTE + "\n\n" + backlinks


def write_artifact_markdown(
    lake_root: Path | str,
    artifact: Artifact,
    *,
    transcript_input: TranscriptInput,
    canonical_json_path: Path | str | None = None,
) -> Path:
    """Write the Markdown view for one promoted artifact. Returns the path."""
    out_dir = artifacts_markdown_dir(lake_root, transcript_input.meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / artifact_markdown_filename(artifact.artifact_type)
    path.write_text(
        render_artifact_markdown(
            artifact,
            transcript_input=transcript_input,
            canonical_json_path=canonical_json_path,
        ),
        encoding="utf-8",
    )
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


def explain_reason_codes(reason_codes: Iterable[str]) -> str:
    """Public helper: render reason codes as a single plain-English sentence."""
    return "; ".join(_explain_reason(c) for c in reason_codes)


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
    canonical_json_paths: dict[str, str] | None = None,
    run_records: list[dict[str, Any]] | None = None,
) -> str:
    """Render the per-meeting index Markdown.

    `promoted` is a list of (artifact_type, artifact) tuples for workflows
    whose artifact promoted. `blocked` is a list of
    `{"artifact_type", "reason_codes"}` dicts for workflows that did not
    promote. `canonical_json_paths` maps `artifact_type -> json filename`
    so links point to the byte source of truth. `run_records` is an
    optional list of run-level records (one per workflow run) used to
    surface manifest/debug paths and reason codes in the index.
    """
    canonical_json_paths = canonical_json_paths or {}
    run_records = list(run_records or [])

    fields: dict[str, Any] = {
        "artifact_type": "meeting_index",
        "meeting_id": transcript_input.meeting_id,
        "date": transcript_input.date,
        "title": transcript_input.title,
        "status": "view",
        "trace_id": _index_trace_id(transcript_input.meeting_id),
        "canonical": False,
    }
    front = _emit_frontmatter(_INDEX_FRONTMATTER_KEYS, fields)

    body_lines = [
        f"# {transcript_input.title}",
        "",
        f"- **Meeting:** `{transcript_input.meeting_id}`",
        f"- **Date:** {transcript_input.date}",
        f"- **Source type:** {transcript_input.source_type}",
        f"- **Source transcript:** `{transcript_input.transcript_path}`",
        f"- **Source metadata:** `{transcript_input.metadata_path}`",
    ]

    meta_agency = transcript_input.metadata.get("agency")
    if isinstance(meta_agency, str) and meta_agency.strip():
        link = f"{AGENCIES_SUBDIR}/{agency_markdown_filename(meta_agency)}"
        body_lines.append(
            f"- **Agency:** [{meta_agency}]({link}) — [[Agency/{meta_agency}]]"
        )
    meta_topic = transcript_input.metadata.get("topic")
    if isinstance(meta_topic, str) and meta_topic.strip():
        link = f"{TOPICS_SUBDIR}/{topic_markdown_filename(meta_topic)}"
        body_lines.append(
            f"- **Topic:** [{meta_topic}]({link}) — [[Topic/{meta_topic}]]"
        )

    body_lines.append("")
    body_lines.append(
        "> JSON is canonical. This index and every other Markdown file "
        "in this folder are regenerated views: editing them does not "
        "change any artifact, and core never reads them back."
    )
    body_lines.append("")
    body_lines.append("## Promoted artifacts")
    body_lines.append("")
    if promoted:
        for artifact_type, artifact in sorted(promoted, key=lambda p: p[0]):
            md_link = f"{ARTIFACTS_SUBDIR}/{artifact_markdown_filename(artifact_type)}"
            body_lines.append(
                f"- **{artifact_type}** — [view markdown]({md_link})"
                f" · trace `{artifact.trace_id}`"
            )
            json_name = canonical_json_paths.get(artifact_type)
            if json_name:
                body_lines.append(
                    f"  - canonical JSON: [{json_name}]({json_name})"
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
                explanation = explain_reason_codes(codes)
                body_lines.append(
                    f"- **{entry['artifact_type']}**: {reason_str} "
                    f"({explanation})"
                )
            else:
                body_lines.append(
                    f"- **{entry['artifact_type']}**: blocked"
                )
    else:
        body_lines.append("_(none)_")

    body_lines.append("")
    body_lines.append("## Run records")
    body_lines.append("")
    if run_records:
        for record in sorted(
            run_records,
            key=lambda r: (r.get("workflow_name", ""), r.get("run_id", "")),
        ):
            wf = record.get("workflow_name", "?")
            run_id = record.get("run_id", "")
            decision = record.get("decision", "?")
            body_lines.append(
                f"- **{wf}** — `{run_id}` decision `{decision}`"
            )
            manifest_path = record.get("manifest_path")
            if manifest_path:
                manifest_name = Path(manifest_path).name
                body_lines.append(
                    f"  - manifest: [{manifest_name}]({manifest_name})"
                )
            debug_path = record.get("debug_path")
            if debug_path:
                debug_name = Path(debug_path).name
                body_lines.append(
                    f"  - debug: [{debug_name}]({debug_name})"
                )
            run_md = record.get("run_markdown_path")
            if run_md:
                run_md_name = Path(run_md).name
                body_lines.append(
                    f"  - run note: [{run_md_name}]({RUNS_SUBDIR}/{run_md_name})"
                )
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
    canonical_json_paths: dict[str, str] | None = None,
    run_records: list[dict[str, Any]] | None = None,
) -> Path:
    out_dir = markdown_dir(lake_root, transcript_input.meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / INDEX_FILENAME
    path.write_text(
        render_index_markdown(
            transcript_input=transcript_input,
            promoted=promoted,
            blocked=blocked,
            canonical_json_paths=canonical_json_paths,
            run_records=run_records,
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Agency / topic notes (per-meeting)
# ---------------------------------------------------------------------------


_AGENCY_FRONTMATTER_KEYS: list[str] = [
    "artifact_type",
    "meeting_id",
    "date",
    "title",
    "agency",
    "status",
    "canonical",
]

_TOPIC_FRONTMATTER_KEYS: list[str] = [
    "artifact_type",
    "meeting_id",
    "date",
    "title",
    "topic",
    "status",
    "canonical",
]


def render_agency_markdown(
    *,
    transcript_input: TranscriptInput,
    agency: str,
    referenced_artifact_types: list[str],
) -> str:
    fields: dict[str, Any] = {
        "artifact_type": "agency_note",
        "meeting_id": transcript_input.meeting_id,
        "date": transcript_input.date,
        "title": agency,
        "agency": agency,
        "status": "view",
        "canonical": False,
    }
    front = _emit_frontmatter(_AGENCY_FRONTMATTER_KEYS, fields)
    lines: list[str] = [f"# Agency: {agency}", ""]
    lines.append(f"- Original agency string: `{agency}`")
    lines.append(
        f"- Meeting: [meeting index](../{INDEX_FILENAME}) "
        f"— [[Meeting/{transcript_input.meeting_id}]]"
    )
    lines.append("")
    lines.append(
        "> This page is a regenerated view. JSON is canonical."
    )
    lines.append("")
    lines.append("## Promoted artifacts referencing this agency")
    lines.append("")
    if referenced_artifact_types:
        for at in sorted(referenced_artifact_types):
            link = f"../{ARTIFACTS_SUBDIR}/{artifact_markdown_filename(at)}"
            lines.append(f"- [{at}]({link})")
    else:
        lines.append("_(none promoted)_")
    lines.append("")
    return front + "\n" + "\n".join(lines) + "\n"


def render_topic_markdown(
    *,
    transcript_input: TranscriptInput,
    topic: str,
    referenced_artifact_types: list[str],
) -> str:
    fields: dict[str, Any] = {
        "artifact_type": "topic_note",
        "meeting_id": transcript_input.meeting_id,
        "date": transcript_input.date,
        "title": topic,
        "topic": topic,
        "status": "view",
        "canonical": False,
    }
    front = _emit_frontmatter(_TOPIC_FRONTMATTER_KEYS, fields)
    lines: list[str] = [f"# Topic: {topic}", ""]
    lines.append(f"- Original topic string: `{topic}`")
    lines.append(
        f"- Meeting: [meeting index](../{INDEX_FILENAME}) "
        f"— [[Meeting/{transcript_input.meeting_id}]]"
    )
    lines.append("")
    lines.append(
        "> This page is a regenerated view. JSON is canonical."
    )
    lines.append("")
    lines.append("## Promoted artifacts on this topic")
    lines.append("")
    if referenced_artifact_types:
        for at in sorted(referenced_artifact_types):
            link = f"../{ARTIFACTS_SUBDIR}/{artifact_markdown_filename(at)}"
            lines.append(f"- [{at}]({link})")
    else:
        lines.append("_(none promoted)_")
    lines.append("")
    return front + "\n" + "\n".join(lines) + "\n"


def write_agency_markdown(
    lake_root: Path | str,
    *,
    transcript_input: TranscriptInput,
    agency: str,
    referenced_artifact_types: list[str],
) -> Path:
    out_dir = agencies_markdown_dir(lake_root, transcript_input.meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / agency_markdown_filename(agency)
    path.write_text(
        render_agency_markdown(
            transcript_input=transcript_input,
            agency=agency,
            referenced_artifact_types=referenced_artifact_types,
        ),
        encoding="utf-8",
    )
    return path


def write_topic_markdown(
    lake_root: Path | str,
    *,
    transcript_input: TranscriptInput,
    topic: str,
    referenced_artifact_types: list[str],
) -> Path:
    out_dir = topics_markdown_dir(lake_root, transcript_input.meeting_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / topic_markdown_filename(topic)
    path.write_text(
        render_topic_markdown(
            transcript_input=transcript_input,
            topic=topic,
            referenced_artifact_types=referenced_artifact_types,
        ),
        encoding="utf-8",
    )
    return path
