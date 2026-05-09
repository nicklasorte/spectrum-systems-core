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


    # ---------- Phase C projections (story / knowledge / connections) ----------

    def write_story_projection(
        self,
        source_id: str,
        candidates: List[Dict[str, Any]],
        repo_root: str | Path,
        label: str = "",
    ) -> str:
        """Write processed/<family>/<source_id>/markdown/stories.md.

        VIEW ONLY. Regenerated each call. ``label`` records which step in
        the Phase C pipeline produced this projection (FINDING-C-004 fix).
        Blocked candidates are listed by ID + reason but their excerpts
        are NOT shown — they may be hallucinated (RT2-004).
        """
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, source_id)
        markdown_dir = processed_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)

        target_path = markdown_dir / "stories.md"
        target_path.write_text(
            self._render_story_projection(source_id, candidates, label),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_story_projection(
        self,
        source_id: str,
        candidates: List[Dict[str, Any]],
        label: str,
    ) -> str:
        generated_at = _now_iso()
        non_blocked = [c for c in candidates if c.get("status") != "blocked"]
        blocked = [c for c in candidates if c.get("status") == "blocked"]
        grounded = [c for c in non_blocked if c.get("grounded")]

        lines = [
            "---",
            f"source_id: {source_id}",
            f"generated_at: {generated_at}",
            f"step_label: {label}",
            f"total_candidates: {len(candidates)}",
            f"grounded_candidates: {len(grounded)}",
            f"blocked_candidates: {len(blocked)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Story Bank — {source_id}",
            "",
            f"> Step: {label} | Generated: {generated_at} | VIEW ONLY",
            "> This projection is regenerated on every run. Do not edit.",
            "",
            f"## Candidates ({len(non_blocked)})",
            "",
        ]
        if not non_blocked:
            lines.append("_No non-blocked candidates._")
            lines.append("")
        else:
            for candidate in non_blocked:
                tier = candidate.get("tier_guess", "")
                verdict = candidate.get("storyworthy_verdict", "")
                story_id = candidate.get("story_id", "")
                theme = candidate.get("possible_theme", "")
                page_numbers = candidate.get("page_numbers", [])
                status = candidate.get("status", "")
                grounded_flag = candidate.get("grounded", False)
                excerpt = candidate.get("source_excerpt", "")
                summary = candidate.get("story_summary", "")
                why = candidate.get("why_it_might_work", "")
                risks = ", ".join(candidate.get("risk_flags", []) or []) or "—"
                lines.extend(
                    [
                        f"### {story_id} | {tier} | {verdict}",
                        f"**Theme:** {theme}",
                        f"**Pages:** {page_numbers}",
                        f"**Status:** {status}",
                        f"**Grounded:** {grounded_flag}",
                        "",
                        "> " + excerpt.replace("\n", "\n> "),
                        "",
                        f"**Summary:** {summary}",
                        f"**Why it works:** {why}",
                        f"**Risks:** {risks}",
                        "",
                        "---",
                        "",
                    ]
                )

        lines.extend(
            [
                f"## Blocked ({len(blocked)})",
                "",
                "> Blocked candidate excerpts are not shown — they may be ungrounded.",
                "",
            ]
        )
        if not blocked:
            lines.append("_No blocked candidates._")
            lines.append("")
        else:
            for candidate in blocked:
                story_id = candidate.get("story_id", "")
                reason = candidate.get("block_reason", "unknown")
                lines.append(f"- `{story_id}` — {reason}")
            lines.append("")

        return "\n".join(lines)

    def write_knowledge_projection(
        self,
        source_id: str,
        repo_root: str | Path,
        label: str = "",
    ) -> str:
        """Write processed/<family>/<source_id>/markdown/knowledge.md.

        VIEW ONLY. Regenerated each call. Lists concept, theme, and analogy
        candidates produced by KnowledgeSynthesizer.
        """
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, source_id)
        markdown_dir = processed_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)

        knowledge_dir = processed_dir / "knowledge"
        concepts = _load_jsonl(knowledge_dir / "concepts.jsonl")
        themes = _load_jsonl(knowledge_dir / "themes.jsonl")
        analogies = _load_jsonl(knowledge_dir / "analogies.jsonl")

        target_path = markdown_dir / "knowledge.md"
        target_path.write_text(
            self._render_knowledge_projection(
                source_id, label, concepts, themes, analogies
            ),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_knowledge_projection(
        self,
        source_id: str,
        label: str,
        concepts: List[Dict[str, Any]],
        themes: List[Dict[str, Any]],
        analogies: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"source_id: {source_id}",
            f"generated_at: {generated_at}",
            f"step_label: {label}",
            f"concept_count: {len(concepts)}",
            f"theme_count: {len(themes)}",
            f"analogy_count: {len(analogies)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Knowledge Index — {source_id}",
            "",
            f"> Step: {label} | Generated: {generated_at} | VIEW ONLY",
            "> Do not edit. Knowledge artifacts are candidate status until a "
            "human runs `promote-knowledge`.",
            "",
        ]

        def _block(title: str, items: List[Dict[str, Any]], name_key: str,
                   id_key: str) -> List[str]:
            out = [f"## {title} ({len(items)})", ""]
            if not items:
                out.append(f"_No {title.lower()} candidates._")
                out.append("")
                return out
            for item in items:
                name = item.get(name_key, "")
                aid = item.get(id_key, "")
                status = item.get("status", "")
                story_ids = item.get("source_story_ids", []) or []
                excerpts = item.get("supporting_excerpts", []) or []
                first_excerpt = ""
                if excerpts:
                    first_excerpt = (
                        excerpts[0].get("excerpt", "")
                        if isinstance(excerpts[0], dict)
                        else ""
                    )
                out.extend(
                    [
                        f"### {name}",
                        f"- ID: `{aid}`",
                        f"- Status: `{status}`",
                        f"- Source stories: {len(story_ids)}",
                    ]
                )
                if first_excerpt:
                    out.append("> " + first_excerpt.replace("\n", "\n> "))
                out.append("")
            return out

        lines.extend(_block("Concepts", concepts, "concept_name", "concept_id"))
        lines.extend(_block("Themes", themes, "theme_name", "theme_id"))
        lines.extend(_block("Analogies", analogies, "analogy_name", "analogy_id"))
        return "\n".join(lines)

    def write_connection_projection(
        self,
        source_id: str,
        repo_root: str | Path,
        label: str = "",
    ) -> str:
        """Write processed/<family>/<source_id>/markdown/connections.md.

        VIEW ONLY. Regenerated each call.
        """
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, source_id)
        markdown_dir = processed_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)

        knowledge_dir = processed_dir / "knowledge"
        connections = _load_jsonl(knowledge_dir / "connections.jsonl")

        target_path = markdown_dir / "connections.md"
        target_path.write_text(
            self._render_connection_projection(source_id, label, connections),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_connection_projection(
        self,
        source_id: str,
        label: str,
        connections: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"source_id: {source_id}",
            f"generated_at: {generated_at}",
            f"step_label: {label}",
            f"connection_count: {len(connections)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Connection Map — {source_id}",
            "",
            f"> Step: {label} | Generated: {generated_at} | VIEW ONLY",
            "> Do not edit. Regenerated on every run.",
            "",
        ]
        if not connections:
            lines.append("_No connections found yet._")
            lines.append("")
            return "\n".join(lines)

        for conn in connections:
            cid = conn.get("connection_id", "")
            sa = conn.get("source_id_a", "")
            sb = conn.get("source_id_b", "")
            ctype = conn.get("connection_type", "")
            strength = conn.get("strength", "")
            matching = conn.get("matching_fields", []) or []
            lines.extend(
                [
                    f"### {sa} ↔ {sb}",
                    f"- ID: `{cid}`",
                    f"- Type: `{ctype}`",
                    f"- Strength: `{strength}`",
                    "- Matching fields:",
                ]
            )
            for field in matching:
                name = field.get("field_name", "")
                va = field.get("value_a", "")
                vb = field.get("value_b", "")
                lines.append(f"  - **{name}**: `{va}` ↔ `{vb}`")
            lines.append("")

        return "\n".join(lines)


    # ---------- Phase D projections (claims / issues / revisions) ----------

    def write_paper_claims_projection(
        self,
        source_id: str,
        claims: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        """Write processed/<family>/<source_id>/paper/markdown/claims.md.

        VIEW ONLY. Regenerated each call. Never read back as authority.
        """
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, source_id)
        markdown_dir = processed_dir / "paper" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "claims.md"
        target_path.write_text(
            self._render_claims_projection(source_id, claims),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_claims_projection(
        self,
        source_id: str,
        claims: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"source_id: {source_id}",
            f"generated_at: {generated_at}",
            f"claim_count: {len(claims)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Claim Map - {source_id}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Regenerated on every run. Do not edit. Never authoritative.",
            "",
            "| claim_id | claim_type | materiality | claim_text |",
            "| -------- | ---------- | ----------- | ---------- |",
        ]
        for c in claims:
            cid = c.get("claim_id", "")
            ctype = c.get("claim_type", "")
            mat = c.get("materiality", "")
            text = (c.get("claim_text", "") or "").replace("\n", " ").replace(
                "|", "\\|"
            )
            if len(text) > 80:
                text = text[:80] + "..."
            lines.append(f"| `{cid}` | {ctype} | {mat} | {text} |")
        if not claims:
            lines.append("| — | — | — | _no claims_ |")
        lines.append("")
        return "\n".join(lines)

    def write_paper_issues_projection(
        self,
        source_id: str,
        issues: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        """Write processed/<family>/<source_id>/paper/markdown/issues.md."""
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, source_id)
        markdown_dir = processed_dir / "paper" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "issues.md"
        target_path.write_text(
            self._render_issues_projection(source_id, issues),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_issues_projection(
        self,
        source_id: str,
        issues: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"source_id: {source_id}",
            f"generated_at: {generated_at}",
            f"issue_count: {len(issues)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Issue Registry - {source_id}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Regenerated on every run. Do not edit. Never authoritative.",
            "",
            "| issue_id | issue_type | severity | status | description |",
            "| -------- | ---------- | -------- | ------ | ----------- |",
        ]
        for issue in issues:
            iid = issue.get("issue_id", "")
            itype = issue.get("issue_type", "")
            sev = issue.get("severity", "")
            stat = issue.get("status", "")
            desc = (issue.get("description", "") or "").replace(
                "\n", " "
            ).replace("|", "\\|")
            if len(desc) > 60:
                desc = desc[:60] + "..."
            lines.append(f"| `{iid}` | {itype} | {sev} | {stat} | {desc} |")
        if not issues:
            lines.append("| — | — | — | — | _no issues_ |")
        lines.append("")

        for issue in issues:
            similar = issue.get("similar_issue_ids") or []
            if similar:
                lines.append(
                    f"- `{issue.get('issue_id', '')}` similar_issue_ids: "
                    + ", ".join(f"`{s}`" for s in similar)
                )
        if any(issue.get("similar_issue_ids") for issue in issues):
            lines.append("")
        return "\n".join(lines)

    def write_paper_revisions_projection(
        self,
        source_id: str,
        instructions: List[Dict[str, Any]],
        diffs: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        """Write processed/<family>/<source_id>/paper/markdown/revisions.md.

        Reflects blocked revisions (RT5-006) — for blocked diffs we surface
        the failure_reason and the dropped claim ids; we do NOT include the
        revised_text because there isn't one for a blocked revision.
        """
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, source_id)
        markdown_dir = processed_dir / "paper" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "revisions.md"
        target_path.write_text(
            self._render_revisions_projection(source_id, instructions, diffs),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_revisions_projection(
        self,
        source_id: str,
        instructions: List[Dict[str, Any]],
        diffs: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        diffs_by_inst = {d.get("instruction_id"): d for d in diffs}
        lines = [
            "---",
            f"source_id: {source_id}",
            f"generated_at: {generated_at}",
            f"instruction_count: {len(instructions)}",
            f"diff_count: {len(diffs)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Revision Log - {source_id}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Regenerated on every run. Do not edit. Never authoritative.",
            "",
            "## Instructions",
            "",
            "| instruction_id | type | priority | status | target_section |",
            "| -------------- | ---- | -------- | ------ | -------------- |",
        ]
        for inst in instructions:
            iid = inst.get("instruction_id", "")
            itype = inst.get("instruction_type", "")
            prio = inst.get("priority", "")
            stat = inst.get("status", "")
            target = (inst.get("target_section", "") or "").replace(
                "\n", " "
            ).replace("|", "\\|")
            if len(target) > 60:
                target = target[:60] + "..."
            lines.append(f"| `{iid}` | {itype} | {prio} | {stat} | {target} |")
        if not instructions:
            lines.append("| — | — | — | — | _no instructions_ |")
        lines.append("")

        lines.extend(["## Diffs", ""])
        if not diffs:
            lines.append("_No revision diffs recorded yet._")
            lines.append("")
        for diff in diffs:
            iid = diff.get("instruction_id", "")
            dstatus = diff.get("status", "")
            section = diff.get("source_section", "")
            lines.append(f"### `{iid}` — {dstatus}")
            lines.append(f"- Section: `{section}`")
            lines.append(
                f"- Original chars: {diff.get('original_char_count', 0)} "
                f"-> revised: {diff.get('revised_char_count', 0)}"
            )
            lines.append(
                f"- Claims before: {diff.get('claims_before_count', 0)} "
                f"-> after: {diff.get('claims_after_count', 0)}"
            )
            dropped = diff.get("high_materiality_claims_dropped") or []
            if dropped:
                lines.append(
                    "- Dropped high-materiality claims: "
                    + ", ".join(f"`{c}`" for c in dropped)
                )
            if dstatus != "success":
                lines.append(
                    f"- Failure reason: `{diff.get('failure_reason', '') or 'n/a'}`"
                )
            lines.append("")
        return "\n".join(lines)

    # ---------- Phase E projections (agency / objections / mitigations / patterns) ----------

    def write_agency_profile_projection(
        self,
        profile: Dict[str, Any],
        active_positions: List[Dict[str, Any]],
        all_positions: List[Dict[str, Any]],
        recent_history: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        """Write agency/<slug>/markdown/profile.md.

        VIEW ONLY. Regenerated each call. Never read back as authority.
        """
        repo_root_path = Path(repo_root).resolve()
        agency_slug = str(profile.get("agency_slug") or "unknown")
        markdown_dir = repo_root_path / "agency" / agency_slug / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "profile.md"
        target_path.write_text(
            self._render_agency_profile_projection(
                profile, active_positions, all_positions, recent_history
            ),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_agency_profile_projection(
        self,
        profile: Dict[str, Any],
        active_positions: List[Dict[str, Any]],
        all_positions: List[Dict[str, Any]],
        recent_history: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        slug = profile.get("agency_slug", "?")
        agency_name = profile.get("agency_name", "?")
        active_ids = {p.get("position_id") for p in active_positions}
        # Detect stale primary positions (newest per topic with valid_until in past).
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).date()
        by_topic: Dict[str, List[Dict[str, Any]]] = {}
        for pos in all_positions:
            topic = str(pos.get("topic") or "").strip().lower()
            if not topic:
                continue
            by_topic.setdefault(topic, []).append(pos)
        stale_topics: set = set()
        stale_position_ids: set = set()
        for topic, plist in by_topic.items():
            primary = sorted(
                plist, key=lambda p: str(p.get("valid_from") or ""), reverse=True
            )[0]
            valid_until = primary.get("valid_until")
            if valid_until is None:
                continue
            try:
                vu = _dt.date.fromisoformat(str(valid_until))
            except ValueError:
                continue
            if vu < today:
                stale_topics.add(topic)
                stale_position_ids.add(primary.get("position_id"))

        lines = [
            "---",
            f"agency_slug: {slug}",
            f"agency_name: {agency_name}",
            f"generated_at: {generated_at}",
            f"active_position_count: {len(active_positions)}",
            f"total_position_count: {len(all_positions)}",
            f"recent_history_count: {len(recent_history)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Agency Profile - {agency_name}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Do not edit. Regenerated on every run. Never authoritative.",
            "",
            f"**Slug:** `{slug}`",
            f"**Jurisdiction:** {profile.get('jurisdiction', '') or '_n/a_'}",
            f"**Active:** {profile.get('active', False)}",
            f"**Total comments:** {profile.get('total_comment_count', 0)}",
            f"**Total objections:** {profile.get('total_objection_count', 0)}",
            f"**Aliases:** {', '.join(profile.get('aliases') or []) or '_none_'}",
            "",
            "## Active Positions",
            "",
            "| topic | position_type | valid_from | valid_until | stale | position_id |",
            "| ----- | ------------- | ---------- | ----------- | ----- | ----------- |",
        ]
        for pos in active_positions:
            topic = str(pos.get("topic") or "").replace("|", "\\|")
            stale_flag = "STALE" if pos.get("position_id") in stale_position_ids else ""
            lines.append(
                f"| {topic} | {pos.get('position_type', '')} | "
                f"{pos.get('valid_from', '')} | "
                f"{pos.get('valid_until') or 'null'} | {stale_flag} | "
                f"`{pos.get('position_id', '')}` |"
            )
        if not active_positions:
            lines.append("| — | — | — | — | — | _no active positions_ |")
        lines.append("")

        if stale_position_ids:
            lines.append("> ⚠ Stale primary positions detected:")
            for pid in sorted(p for p in stale_position_ids if p):
                lines.append(f"> - `{pid}`")
            lines.append("")

        lines.extend([
            "## Recent Objection History (top 10)",
            "",
            "| objection_type | raised_at | paper_source_id | resolved | entry_id |",
            "| -------------- | --------- | --------------- | -------- | -------- |",
        ])
        for entry in recent_history:
            lines.append(
                f"| {entry.get('objection_type', '')} | "
                f"{entry.get('raised_at', '')} | "
                f"{entry.get('paper_source_id', '')} | "
                f"{entry.get('resolved', False)} | "
                f"`{entry.get('entry_id', '')}` |"
            )
        if not recent_history:
            lines.append("| — | — | — | — | _no history_ |")
        lines.append("")
        return "\n".join(lines)

    def write_objections_projection(
        self,
        paper_source_id: str,
        predictions: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        """Write processed/<family>/<source_id>/paper/markdown/objections.md."""
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, paper_source_id)
        markdown_dir = processed_dir / "paper" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "objections.md"
        target_path.write_text(
            self._render_objections_projection(paper_source_id, predictions),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_objections_projection(
        self,
        paper_source_id: str,
        predictions: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"source_id: {paper_source_id}",
            f"generated_at: {generated_at}",
            f"prediction_count: {len(predictions)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Objection Predictions - {paper_source_id}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Predictions are advisory only. Humans decide what to revise.",
            "> Do not edit. Regenerated on every run. Never authoritative.",
            "",
            "| prediction_id | agency_slug | confidence | objection_type | "
            "evidence_basis | flag |",
            "| ------------- | ----------- | ---------- | -------------- | "
            "-------------- | ---- |",
        ]
        for pred in predictions:
            ev = pred.get("evidence_basis") or []
            flag = "no_evidence_basis" if pred.get("no_evidence_basis_flag") else ""
            lines.append(
                f"| `{pred.get('prediction_id', '')}` | "
                f"{pred.get('agency_slug', '')} | "
                f"{pred.get('confidence', '')} | "
                f"{pred.get('objection_type', '')} | "
                f"{len(ev)} | {flag} |"
            )
        if not predictions:
            lines.append("| — | — | — | — | — | _no predictions_ |")
        lines.append("")
        for pred in predictions:
            lines.append(f"### `{pred.get('prediction_id', '')}`")
            lines.append("")
            lines.append(
                f"**Predicted:** {pred.get('predicted_objection_text', '')}"
            )
            lines.append("")
            lines.append(f"**Rationale:** {pred.get('rationale', '')}")
            lines.append("")
        return "\n".join(lines)

    def write_mitigations_projection(
        self,
        paper_source_id: str,
        mitigations: List[Dict[str, Any]],
        blocked_reasons: List[str],
        repo_root: str | Path,
    ) -> str:
        """Write processed/<family>/<source_id>/paper/markdown/mitigations.md."""
        repo_root_path = Path(repo_root).resolve()
        processed_dir = _resolve_phase_c_dir(repo_root_path, paper_source_id)
        markdown_dir = processed_dir / "paper" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "mitigations.md"
        target_path.write_text(
            self._render_mitigations_projection(
                paper_source_id, mitigations, blocked_reasons
            ),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_mitigations_projection(
        self,
        paper_source_id: str,
        mitigations: List[Dict[str, Any]],
        blocked_reasons: List[str],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"source_id: {paper_source_id}",
            f"generated_at: {generated_at}",
            f"mitigation_count: {len(mitigations)}",
            f"blocked_count: {len(blocked_reasons)}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Mitigation Suggestions - {paper_source_id}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Mitigations are advisory only. Humans decide what to apply.",
            "> Do not edit. Regenerated on every run. Never authoritative.",
            "",
            "| mitigation_id | mitigation_type | effectiveness | search_terms | prediction_id |",
            "| ------------- | --------------- | ------------- | ------------ | ------------- |",
        ]
        for mit in mitigations:
            terms = mit.get("evidence_search_terms") or []
            terms_text = ", ".join(terms) if terms else "_n/a_"
            lines.append(
                f"| `{mit.get('mitigation_id', '')}` | "
                f"{mit.get('mitigation_type', '')} | "
                f"{mit.get('expected_effectiveness', '')} | "
                f"{terms_text} | `{mit.get('prediction_id', '')}` |"
            )
        if not mitigations:
            lines.append("| — | — | — | — | _no actionable mitigations_ |")
        lines.append("")

        if blocked_reasons:
            lines.append("## Blocked Mitigations")
            lines.append("")
            lines.append(
                "> The following mitigations were blocked at generation and "
                "are NOT actionable suggestions."
            )
            lines.append("")
            for reason in blocked_reasons:
                lines.append(f"- {reason}")
            lines.append("")
        return "\n".join(lines)

    def write_patterns_projection(
        self,
        patterns: List[Dict[str, Any]],
        repo_root: str | Path,
    ) -> str:
        """Write agency/markdown/patterns.md (cross-agency pattern index)."""
        repo_root_path = Path(repo_root).resolve()
        markdown_dir = repo_root_path / "agency" / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "patterns.md"
        target_path.write_text(
            self._render_patterns_projection(patterns),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_patterns_projection(
        self,
        patterns: List[Dict[str, Any]],
    ) -> str:
        generated_at = _now_iso()
        lines = [
            "---",
            f"generated_at: {generated_at}",
            f"pattern_count: {len(patterns)}",
            "similarity_method: jaccard_word",
            "vault_note_status: projection",
            "---",
            "",
            "# Recurring Patterns",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Similarity method: `jaccard_word` (deterministic word-set overlap).",
            "> No semantic, vector, or AI-based similarity is used.",
            "> Do not edit. Regenerated on every run. Never authoritative.",
            "",
            "| pattern_id | pattern_type | jaccard | agency_slugs | topic_keywords |",
            "| ---------- | ------------ | ------- | ------------ | -------------- |",
        ]
        for pattern in patterns:
            slugs = ", ".join(pattern.get("agency_slugs") or [])
            keywords = ", ".join(pattern.get("topic_keywords") or [])
            lines.append(
                f"| `{pattern.get('pattern_id', '')}` | "
                f"{pattern.get('pattern_type', '')} | "
                f"{pattern.get('jaccard_similarity', '')} | "
                f"{slugs} | {keywords} |"
            )
        if not patterns:
            lines.append("| — | — | — | — | _no patterns found_ |")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Phase F: synthesis projections (report.md, keynote.md, review_summary.md)
    # ------------------------------------------------------------------

    def write_report_projection(
        self,
        report_draft: Dict[str, Any],
        repo_root: str | Path,
    ) -> str:
        """Write synthesis/<run_id>/markdown/report.md. VIEW ONLY."""
        repo_root_path = Path(repo_root).resolve()
        run_id = report_draft.get("run_id", "")
        run_dir = repo_root_path / "synthesis" / run_id
        markdown_dir = run_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "report.md"
        target_path.write_text(
            self._render_report_projection(report_draft),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_report_projection(
        self,
        report_draft: Dict[str, Any],
    ) -> str:
        generated_at = _now_iso()
        sections = report_draft.get("sections", []) or []
        grounded = sum(1 for s in sections if s.get("grounded"))
        lines = [
            "---",
            f"draft_id: {report_draft.get('draft_id', '')}",
            f"run_id: {report_draft.get('run_id', '')}",
            f"bundle_id: {report_draft.get('bundle_id', '')}",
            f"bundle_hash: {report_draft.get('bundle_hash', '')}",
            f"audience: {report_draft.get('audience', '')}",
            f"status: {report_draft.get('status', '')}",
            f"section_count: {len(sections)}",
            f"grounded_section_count: {grounded}",
            f"generated_at: {generated_at}",
            "vault_note_status: projection",
            "---",
            "",
            f"# {report_draft.get('title', 'Report')}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Regenerated on every run. Never authoritative. Do not edit.",
            "",
        ]
        for section in sections:
            lines.append(
                f"## {section.get('section_title', '?')} "
                f"(`{section.get('section_type', '?')}`)"
            )
            lines.append("")
            lines.append(
                f"_grounded_: **{bool(section.get('grounded'))}** | "
                f"citations: {len(section.get('inline_citations', []))} | "
                f"unverified: {len(section.get('unverified_citations', []))}"
            )
            lines.append("")
            content = section.get("content") or "_(empty)_"
            lines.append(content)
            lines.append("")
        return "\n".join(lines)

    def write_keynote_projection(
        self,
        keynote_scaffold: Dict[str, Any],
        repo_root: str | Path,
    ) -> str:
        """Write synthesis/<run_id>/markdown/keynote.md. VIEW ONLY."""
        repo_root_path = Path(repo_root).resolve()
        run_id = keynote_scaffold.get("run_id", "")
        run_dir = repo_root_path / "synthesis" / run_id
        markdown_dir = run_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "keynote.md"
        target_path.write_text(
            self._render_keynote_projection(keynote_scaffold),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_keynote_projection(
        self,
        keynote_scaffold: Dict[str, Any],
    ) -> str:
        generated_at = _now_iso()
        arc = keynote_scaffold.get("arc", []) or []
        opener = keynote_scaffold.get("opener", {}) or {}
        lines = [
            "---",
            f"scaffold_id: {keynote_scaffold.get('scaffold_id', '')}",
            f"run_id: {keynote_scaffold.get('run_id', '')}",
            f"bundle_id: {keynote_scaffold.get('bundle_id', '')}",
            f"bundle_hash: {keynote_scaffold.get('bundle_hash', '')}",
            f"audience: {keynote_scaffold.get('audience', '')}",
            f"status: {keynote_scaffold.get('status', '')}",
            f"beat_count: {len(arc)}",
            f"generated_at: {generated_at}",
            "vault_note_status: projection",
            "---",
            "",
            f"# {keynote_scaffold.get('title', 'Keynote')}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Regenerated on every run. Never authoritative. Do not edit.",
            "",
            "## Opener",
            "",
            f"- **story_id:** `{opener.get('story_id', '')}`",
            f"- **hook:** {opener.get('hook_text', '')}",
            f"- **why this story:** {opener.get('why_this_story', '')}",
            "",
            "## Central Tension",
            "",
            keynote_scaffold.get("central_tension", "") or "_(empty)_",
            "",
            "## Arc",
            "",
        ]
        for idx, beat in enumerate(arc, start=1):
            lines.append(
                f"### Beat {idx}: {beat.get('beat_type', '?')}"
            )
            lines.append("")
            lines.append(beat.get("content", "") or "_(empty)_")
            sid = beat.get("story_id")
            if sid:
                lines.append(f"- story_id: `{sid}`")
            for cid in beat.get("claim_ids") or []:
                lines.append(f"- claim_id: `{cid}`")
            lines.append("")
        lines.append("## Closing Call to Action")
        lines.append("")
        lines.append(
            keynote_scaffold.get("closing_call_to_action", "") or "_(empty)_"
        )
        lines.append("")
        return "\n".join(lines)

    def write_review_summary_projection(
        self,
        run_id: str,
        repo_root: str | Path,
    ) -> str:
        """Write synthesis/<run_id>/markdown/review_summary.md. VIEW ONLY."""
        repo_root_path = Path(repo_root).resolve()
        run_dir = repo_root_path / "synthesis" / run_id
        markdown_dir = run_dir / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        target_path = markdown_dir / "review_summary.md"
        target_path.write_text(
            self._render_review_summary_projection(run_id, run_dir),
            encoding="utf-8",
        )
        return str(target_path)

    def _render_review_summary_projection(
        self,
        run_id: str,
        run_dir: Path,
    ) -> str:
        generated_at = _now_iso()
        manifest_path = run_dir / "run_manifest.json"
        manifest: Dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}

        cost_path = run_dir / "cost.jsonl"
        cost_lines: List[Dict[str, Any]] = []
        if cost_path.is_file():
            try:
                with cost_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            cost_lines.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass

        lines = [
            "---",
            f"run_id: {run_id}",
            f"audience: {manifest.get('audience', '')}",
            f"purpose: {manifest.get('purpose', '')}",
            f"generated_at: {generated_at}",
            "vault_note_status: projection",
            "---",
            "",
            f"# Synthesis Run Review Summary — {run_id}",
            "",
            f"> Generated: {generated_at} | VIEW ONLY",
            "> Regenerated on every run. Never authoritative. Do not edit.",
            "",
            "## Run Manifest",
            "",
            f"- audience: `{manifest.get('audience', '')}`",
            f"- purpose: `{manifest.get('purpose', '')}`",
            f"- started_at: `{manifest.get('started_at', '')}`",
            f"- completed_at: `{manifest.get('completed_at', '')}`",
            f"- source_ids: {len(manifest.get('source_ids_included', []))}",
            f"- story_ids: {len(manifest.get('story_ids_included', []))}",
            f"- claim_ids: {len(manifest.get('claim_ids_included', []))}",
            f"- theme_ids: {len(manifest.get('theme_ids_included', []))}",
            f"- total_input_tokens: {manifest.get('total_input_tokens', 0)}",
            f"- total_output_tokens: {manifest.get('total_output_tokens', 0)}",
            (
                "- **total_estimated_cost_usd:** "
                f"**${float(manifest.get('total_estimated_cost_usd', 0.0)):.4f}**"
            ),
            "",
            "## Per-call cost breakdown",
            "",
        ]
        if not cost_lines:
            lines.append("_(no cost records)_")
        else:
            lines.append(
                "| call_purpose | input_tokens | output_tokens | cost_usd | model |"
            )
            lines.append(
                "| ------------ | ------------ | ------------- | -------- | ----- |"
            )
            for rec in cost_lines:
                lines.append(
                    f"| {rec.get('call_purpose', '?')} | "
                    f"{rec.get('input_tokens', 0)} | "
                    f"{rec.get('output_tokens', 0)} | "
                    f"${float(rec.get('estimated_cost_usd', 0.0)):.6f} | "
                    f"{rec.get('model', '?')} |"
                )
        lines.append("")
        lines.append("## Files in this run directory")
        lines.append("")
        for child in sorted(run_dir.iterdir()):
            if child.name == "markdown":
                continue
            lines.append(f"- `{child.name}`")
        lines.append("")
        return "\n".join(lines)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _resolve_phase_c_dir(repo_root: Path, source_id: str) -> Path:
    """Locate processed/<family>/<source_id>/ for Phase C projections."""
    from ..ingestion.source_loader import SOURCE_FAMILIES

    for family in SOURCE_FAMILIES:
        candidate = repo_root / "processed" / family / source_id
        if candidate.is_dir():
            return candidate
    # Fall back to notes/ if the directory hasn't been created yet (test setups).
    fallback = repo_root / "processed" / "notes" / source_id
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


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
