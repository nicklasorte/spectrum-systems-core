"""Obsidian projection: regenerated Markdown index for a source_record.

View only. Never read back as authority. Regenerated on every run.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


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

    def write_book_extraction_index(
        self,
        source_id: str,
        metadata: Dict[str, Any],
        extraction_report: Dict[str, Any],
        repo_root: str | Path,
    ) -> str:
        """Render a Markdown projection of a Phase B PDF extraction.

        VIEW ONLY. Regenerated on every run. Never authoritative.

        Returns absolute path to the written file.
        """
        repo_root_path = Path(repo_root).resolve()
        target_dir = (
            repo_root_path / "processed" / "books" / source_id / "markdown"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "index.md"
        pages: List[Dict[str, Any]] = _load_pages(
            repo_root_path / "raw" / "books" / source_id / "pages.jsonl"
        )
        target_path.write_text(
            self._render_book_extraction(
                source_id, metadata, extraction_report, pages
            ),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_book_extraction(
        self,
        source_id: str,
        metadata: Dict[str, Any],
        extraction_report: Dict[str, Any],
        pages: List[Dict[str, Any]],
    ) -> str:
        title = str(metadata.get("title", source_id))
        date = str(metadata.get("date", ""))
        page_count = extraction_report.get("page_count", 0)
        total_chars = extraction_report.get("total_char_count", 0)
        lib = extraction_report.get("extraction_library", "")
        lib_version = extraction_report.get("extraction_library_version", "")
        extracted_at = extraction_report.get("extracted_at", "")
        scanned = extraction_report.get("scanned_pdf_suspected", False)
        status = extraction_report.get("status", "")
        failure_reason = extraction_report.get("failure_reason", "")

        frontmatter = [
            "---",
            f"source_id: {source_id}",
            "source_family: books",
            f"title: {title}",
            f"date: {date}",
            f"page_count: {page_count}",
            f"total_char_count: {total_chars}",
            f"extraction_library: {lib}",
            f"extraction_library_version: {lib_version}",
            f"extracted_at: {extracted_at}",
            f"scanned_pdf_suspected: {str(scanned).lower()}",
            f"status: {status}",
            "vault_note_status: projection",
            "---",
        ]

        body = [
            "",
            f"# {title}",
            "",
            f"**Source ID:** {source_id}",
            f"**Pages:** {page_count}",
            f"**Characters extracted:** {total_chars}",
            f"**Extraction library:** {lib} {lib_version}",
            f"**Extracted:** {extracted_at}",
            "",
            "> ⚠️ PRIVATE USE ONLY. This source must not be distributed.",
            "> This is a read-only projection. Do not edit.",
            "> Changes will be overwritten on next run.",
            "",
            "## Extraction Status",
            "",
            f"**Status:** {status}",
        ]
        if failure_reason:
            body.append(f"**Failure reason:** {failure_reason}")
        if scanned:
            body.extend(
                [
                    "",
                    "⚠️ Scanned PDF suspected. Character count below threshold.",
                    "Text extraction may be incomplete. Do not use for "
                    "downstream processing.",
                ]
            )

        body.extend(["", "## Page Summary", ""])
        if not pages:
            body.append("_No pages available._")
        else:
            for entry in pages[:10]:
                page_num = entry.get("page_number")
                text = str(entry.get("text", ""))
                preview = text.replace("\n", " ").replace("\r", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                char_count = entry.get("char_count", 0)
                body.append(f"### Page {page_num}")
                body.append(preview)
                body.append(f"({char_count} characters)")
                body.append("")

        body.extend(["## Next Step", ""])
        if status == "success":
            body.append("If status is success, run:")
            body.append("")
            body.append("```")
            body.append(
                "python -m spectrum_systems_core.cli process-source "
                f"--source-id {source_id}"
            )
            body.append("```")
        else:
            body.append(
                "Extraction failed. Resolve the failure reason and re-run "
                "`prepare-pdf` before continuing."
            )
        body.append("")

        return "\n".join(frontmatter + body)


def _load_pages(pages_path: Path) -> List[Dict[str, Any]]:
    if not pages_path.is_file():
        return []
    pages: List[Dict[str, Any]] = []
    try:
        with pages_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    pages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return pages
