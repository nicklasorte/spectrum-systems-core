"""Obsidian projection: regenerated Markdown index for a source_record.

View only. Never read back as authority. Regenerated on every run.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_processed_path(
    payload: Dict[str, Any], repo_root: Path
) -> Path:
    processed_path = payload.get("processed_path", "")
    p = Path(processed_path)
    if p.is_absolute():
        return p
    return repo_root / processed_path


def _truncate(text: str, limit: int = 300) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


class ObsidianProjection:
    """Render a Markdown index for one source. View only — never authoritative."""

    def write_source_index(
        self,
        source_record: Dict[str, Any],
        text_units: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        repo_root_path = Path(repo_root).resolve()
        payload = source_record["payload"]
        processed_dir = _resolve_processed_path(payload, repo_root_path)
        markdown_dir = processed_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)

        index_path = markdown_dir / "index.md"
        index_path.write_text(
            self._render(source_record, text_units),
            encoding="utf-8",
        )
        return str(index_path)

    def _render(
        self,
        source_record: Dict[str, Any],
        text_units: List[Dict[str, Any]],
    ) -> str:
        payload = source_record["payload"]
        metadata = payload.get("metadata", {})
        generated_at = _now_iso()

        frontmatter_lines = [
            "---",
            f"source_id: {payload['source_id']}",
            f"source_family: {payload['source_family']}",
            f"source_type: {payload['source_type']}",
            f"title: {payload['title']}",
            f"date: {metadata.get('date', '')}",
            f"artifact_id: {source_record['artifact_id']}",
            f"raw_hash: {payload['raw_hash']}",
            f"text_unit_count: {payload['text_unit_count']}",
            f"generated_at: {generated_at}",
            "vault_note_status: projection",
            "---",
        ]

        body_lines = [
            "",
            f"# {payload['title']}",
            "",
            f"**Source ID:** {payload['source_id']}",
            f"**Family:** {payload['source_family']}",
            f"**Type:** {payload['source_type']}",
            f"**Date:** {metadata.get('date', '')}",
            f"**Text Units:** {payload['text_unit_count']}",
            "",
            "> This is a read-only projection generated from source_record.json.",
            "> Do not edit. Changes will be overwritten on next run.",
            "",
            "## Text Units Preview",
            "",
        ]

        preview_units = text_units[:5]
        if not preview_units:
            body_lines.append("_No text units available._")
            body_lines.append("")
        else:
            for unit in preview_units:
                body_lines.append(
                    f"### Unit {unit['ordinal']} ({unit['unit_type']})"
                )
                body_lines.append(_truncate(unit.get("text", "")))
                body_lines.append("")

        body_lines.extend(
            [
                "## Provenance",
                "",
                f"- Artifact ID: `{source_record['artifact_id']}`",
                f"- Raw hash: `{payload['raw_hash']}`",
                "- Produced by: `source_loader v1.0.0`",
                f"- Generated at: {generated_at}",
                "",
            ]
        )

        return "\n".join(frontmatter_lines + body_lines)
