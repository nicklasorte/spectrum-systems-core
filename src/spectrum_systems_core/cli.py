"""Top-level CLI entry point for spectrum_systems_core.

Currently exposes the `process-source` command that ingests one raw source
under `raw/<family>/<source_id>/` end-to-end:

    raw source -> source_record -> text_units.jsonl
    -> SourceEval -> Promoter (SDL_ROOT) -> Obsidian projection

Replaces the vault-note-tag trigger from PR #10. Markdown is view only.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

import yaml

from .extraction import (
    Chunker,
    StoryEval,
    StoryExtractor,
    StoryReviewGateway,
    StoryworthyFilter,
)
from .ingestion import (
    GroundingHelper,
    ObsidianProjection,
    PDFExtractor,
    Promoter,
    SourceEval,
    SourceLoader,
)
from .paper import (
    AssumptionExtractor,
    ClaimEval,
    ClaimExtractor,
    CommentProcessor,
    ContradictionDetector,
    EvidenceBuilder,
    EvidenceEval,
    IssueEval,
    IssueRegistry,
    RevisionEval,
    RevisionGenerator,
    RevisionWorkflow,
)


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slugify(name: str) -> str:
    lowered = name.lower().strip()
    lowered = lowered.replace(" ", "-")
    return _SLUG_RE.sub("-", lowered).strip("-_") or "note"


def _split_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Return (frontmatter_dict, body) — empty dict if no frontmatter."""
    pattern = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
    match = pattern.match(text)
    if not match:
        return {}, text
    raw_yaml, body = match.group(1), match.group(2)
    try:
        loaded = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(loaded, dict):
        return {}, text
    return loaded, body


def _ingest_vault_note(
    vault_root: Path,
    note_relpath: str,
    repo_root: Path,
) -> str:
    """Copy a vault note into raw/notes/<slug>/ and return the source_id."""
    note_path = (vault_root / note_relpath).resolve()
    if not note_path.is_file():
        raise FileNotFoundError(f"vault note not found: {note_path}")
    raw_text = note_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw_text)

    slug = _slugify(note_path.stem)
    explicit_id = frontmatter.get("source_id") if isinstance(frontmatter, dict) else None
    source_id = explicit_id if isinstance(explicit_id, str) and explicit_id.strip() else slug

    target_dir = repo_root / "raw" / "notes" / source_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "source.txt").write_text(
        body if frontmatter else raw_text, encoding="utf-8"
    )

    fm = frontmatter if isinstance(frontmatter, dict) else {}
    metadata = {
        "source_id": source_id,
        "source_family": str(fm.get("source_family", "notes")),
        "source_type": str(fm.get("source_type", "field_note")),
        "title": str(fm.get("title", note_path.stem)),
        "description": str(fm.get("description", "")),
        "date": str(fm.get("date", "1970-01-01")),
        "author": str(fm.get("author", "")),
        "tags": (
            list(fm.get("tags", []))
            if isinstance(fm.get("tags"), list)
            else []
        ),
        "raw_format": "txt",
        "private_use_only": bool(fm.get("private_use_only", False)),
    }
    metadata["tags"] = [str(t) for t in metadata["tags"]]

    (target_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return source_id


def _copy_projection_to_vault(
    projection_path: Path,
    vault_root: Path,
    source_family: str,
    source_id: str,
) -> Path:
    target = vault_root / "Sources" / source_family / f"{source_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(projection_path, target)
    return target


def process_source(
    *,
    source_id: str | None,
    vault: str | None,
    note: str | None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    if note:
        if not vault:
            print("error: --note requires --vault", file=out)
            return 1
        try:
            source_id = _ingest_vault_note(Path(vault).resolve(), note, repo_root_path)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=out)
            return 1

    if not source_id:
        print("error: must provide --source-id or --vault + --note", file=out)
        return 1

    loader_result = SourceLoader().load(source_id, str(repo_root_path))
    if loader_result["status"] != "success":
        print(f"error: load failed: {loader_result['reason']}", file=out)
        return 1

    source_record = loader_result["source_record"]
    text_units = loader_result["text_units"]

    eval_result = SourceEval().run(
        source_record, text_units, repo_root=str(repo_root_path)
    )
    if eval_result["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(eval_result["reason_codes"]),
            file=out,
        )
        return 1

    promote_result = Promoter().promote(source_record)
    if promote_result["status"] != "success":
        print(f"error: promotion failed: {promote_result['reason']}", file=out)
        return 1

    projection_path = ObsidianProjection().write_source_index(
        source_record, text_units, str(repo_root_path)
    )

    if vault:
        try:
            _copy_projection_to_vault(
                Path(projection_path),
                Path(vault).resolve(),
                source_record["payload"]["source_family"],
                source_record["payload"]["source_id"],
            )
        except OSError as exc:
            print(f"warning: vault copy failed: {exc}", file=out)

    payload = source_record["payload"]
    print(f"✓ source_id: {payload['source_id']}", file=out)
    print(f"✓ artifact_id: {source_record['artifact_id']}", file=out)
    print(f"✓ text_units: {payload['text_unit_count']}", file=out)
    print(f"✓ sdl_ref: {promote_result['sdl_ref']}", file=out)
    print(f"✓ projection: {projection_path}", file=out)
    return 0


def prepare_pdf(
    *,
    source_id: str,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase B: validate + extract a book PDF into raw/books/<id>/.

    Writes source.txt, pages.jsonl, extraction_report.json, and a Markdown
    projection under processed/books/<id>/markdown/index.md. Does NOT call
    process-source — Phase A and Phase B are deliberately separate steps.
    """
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    if not source_id:
        print("error: must provide --source-id", file=out)
        return 1

    extractor_result = PDFExtractor().extract(source_id, str(repo_root_path))
    if extractor_result["status"] != "success":
        print(f"error: {extractor_result['reason']}", file=out)
        return 1

    extraction_report = extractor_result["extraction_report"]

    metadata_path = (
        repo_root_path / "raw" / "books" / source_id / "metadata.json"
    )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: failed to read metadata.json: {exc}", file=out)
        return 1

    projection_path = ObsidianProjection().write_book_extraction_index(
        source_id, metadata, extraction_report, str(repo_root_path)
    )

    print(f"✓ source_id: {source_id}", file=out)
    print(f"✓ pages extracted: {extraction_report['page_count']}", file=out)
    print(f"✓ characters: {extraction_report['total_char_count']}", file=out)
    print(
        f"✓ extracted_text_hash: {extraction_report['extracted_text_hash']}",
        file=out,
    )
    print(
        "✓ pdfminer.six version: "
        f"{extraction_report['extraction_library_version']}",
        file=out,
    )
    print(f"✓ projection: {projection_path}", file=out)
    print("", file=out)
    print("Next step:", file=out)
    print(
        "  python -m spectrum_systems_core.cli process-source "
        f"--source-id {source_id}",
        file=out,
    )
    return 0


def extract_stories(
    *,
    source_id: str,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase C: chunk → extract → eval → score → review-form.

    Reads processed/<family>/<source_id>/text_units.jsonl. Writes:
      stories/chunks.jsonl
      stories/candidates.jsonl
      markdown/stories.md (post-eval projection)
    Emits a review form for each tier_1 admit candidate (no auto-promotion).
    """
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    if not source_id:
        print("error: must provide --source-id", file=out)
        return 1

    chunk_result = Chunker().chunk(source_id, str(repo_root_path))
    if chunk_result["status"] != "success":
        print(f"error: chunker failed: {chunk_result['reason']}", file=out)
        return 1
    chunks = chunk_result["chunks"]

    extractor_result = StoryExtractor().extract_from_source(
        source_id, str(repo_root_path)
    )
    if extractor_result["status"] != "success":
        print(
            f"error: extractor failed: {extractor_result['reason']}",
            file=out,
        )
        return 1
    all_records = extractor_result.get("all_records", [])

    StoryEval().run(all_records, source_id, str(repo_root_path))
    StoryworthyFilter().run_on_source(source_id, str(repo_root_path))

    # Reload candidates after filter rewrites.
    candidates = _load_candidates(repo_root_path, source_id)

    ObsidianProjection().write_story_projection(
        source_id, candidates, str(repo_root_path), label="post-eval"
    )

    sent_for_review = 0
    if vault:
        gateway = StoryReviewGateway()
        for candidate in candidates:
            if (
                candidate.get("status") == "candidate"
                and candidate.get("storyworthy_verdict") == "admit"
                and candidate.get("tier_guess") == "tier_1"
            ):
                gateway.emit_review_form(
                    candidate["story_id"], candidate, vault
                )
                sent_for_review += 1

    grounded = sum(1 for c in candidates if c.get("grounded"))
    blocked = sum(1 for c in candidates if c.get("status") == "blocked")
    print(f"✓ source_id: {source_id}", file=out)
    print(f"✓ chunks: {len(chunks)}", file=out)
    print(f"✓ candidates: {len(candidates)}", file=out)
    print(f"✓ grounded: {grounded}", file=out)
    print(f"✓ blocked: {blocked}", file=out)
    print(f"✓ sent for review: {sent_for_review}", file=out)
    return 0


def promote_knowledge(
    *,
    artifact_id: str,
    source_id: str,
    artifact_type: str,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase C: explicit human promotion of a knowledge artifact.

    Reads knowledge/<type>s.jsonl. Validates status == 'candidate'. Writes
    knowledge/promoted/<artifact_id>.json. Updates source jsonl entry to
    status='promoted'. (FINDING-C-003 fix: no auto-promotion path exists.)
    """
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    type_to_id_field = {
        "concept": ("concepts.jsonl", "concept_id"),
        "theme": ("themes.jsonl", "theme_id"),
        "analogy": ("analogies.jsonl", "analogy_id"),
        "connection": ("connections.jsonl", "connection_id"),
    }
    if artifact_type not in type_to_id_field:
        print(
            "error: --artifact-type must be one of "
            "concept|theme|analogy|connection",
            file=out,
        )
        return 1
    filename, id_field = type_to_id_field[artifact_type]

    from .extraction._paths import find_processed_dir
    processed_dir, _ = find_processed_dir(repo_root_path, source_id)
    if processed_dir is None:
        print(f"error: source_id not found: {source_id}", file=out)
        return 1
    knowledge_dir = processed_dir / "knowledge"
    jsonl_path = knowledge_dir / filename
    if not jsonl_path.is_file():
        print(f"error: {jsonl_path} not found", file=out)
        return 1

    records: list[Dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    target = None
    for record in records:
        if record.get(id_field) == artifact_id:
            target = record
            break
    if target is None:
        print(
            f"error: {artifact_type} {artifact_id} not found in {jsonl_path}",
            file=out,
        )
        return 1
    if target.get("status") == "promoted":
        print(
            f"error: already_promoted: {artifact_type} {artifact_id}",
            file=out,
        )
        return 1
    if target.get("status") != "candidate":
        print(
            f"error: cannot promote — status={target.get('status')!r}",
            file=out,
        )
        return 1

    target["status"] = "promoted"
    promoted_dir = knowledge_dir / "promoted"
    promoted_dir.mkdir(parents=True, exist_ok=True)
    (promoted_dir / f"{artifact_id}.json").write_text(
        json.dumps(target, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )

    print(f"✓ Promoted {artifact_type} {artifact_id}", file=out)
    return 0


def extract_claims(
    *,
    source_id: str,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase D: extract claims + assumptions, build evidence, detect contradictions.

    Reads processed/<family>/<source_id>/text_units.jsonl. Writes
    paper/claims.jsonl, paper/assumptions.jsonl, paper/evidence.jsonl,
    paper/contradiction_summary.json. Runs ClaimEval and EvidenceEval.
    """
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    if not source_id:
        print("error: must provide --source-id", file=out)
        return 1

    claim_result = ClaimExtractor().extract_from_source(
        source_id, str(repo_root_path)
    )
    if claim_result["status"] != "success":
        print(f"error: claim extraction failed: {claim_result['reason']}", file=out)
        return 1
    claims = claim_result["claims"]

    assumption_result = AssumptionExtractor().extract_from_source(
        source_id, str(repo_root_path)
    )
    if assumption_result["status"] != "success":
        print(
            f"error: assumption extraction failed: {assumption_result['reason']}",
            file=out,
        )
        return 1
    assumptions = assumption_result["assumptions"]

    claim_eval = ClaimEval().run(
        claims, assumptions, source_id, str(repo_root_path)
    )
    if claim_eval["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(claim_eval["reason_codes"]),
            file=out,
        )
        return 1

    evidence_result = EvidenceBuilder().build_for_source(
        source_id, str(repo_root_path)
    )
    if evidence_result["status"] != "success":
        print(
            f"error: evidence build failed: {evidence_result['reason']}",
            file=out,
        )
        return 1

    contradiction_result = ContradictionDetector().run_on_source(
        source_id, str(repo_root_path)
    )
    if contradiction_result["status"] != "success":
        print(
            f"error: contradiction detection failed: "
            f"{contradiction_result['reason']}",
            file=out,
        )
        return 1

    # Reload claims and evidence after the build/detector mutations.
    claims = _load_paper_jsonl(repo_root_path, source_id, "claims.jsonl")
    evidence = _load_paper_jsonl(repo_root_path, source_id, "evidence.jsonl")

    evidence_eval = EvidenceEval().run(
        claims, evidence, source_id, str(repo_root_path)
    )
    if evidence_eval["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(evidence_eval["reason_codes"]),
            file=out,
        )
        return 1

    warnings = [
        e for e in evidence_eval.get("eval_results", [])
        if e.get("status") == "warn"
    ]

    print(f"✓ source_id: {source_id}", file=out)
    print(f"✓ claims extracted: {len(claims)}", file=out)
    print(f"✓ assumptions extracted: {len(assumptions)}", file=out)
    print(f"✓ evidence records: {len(evidence)}", file=out)
    print(
        f"✓ contradiction pairs: {contradiction_result['contradiction_count']}",
        file=out,
    )
    if warnings:
        for w in warnings:
            print(f"⚠ warn: {w['name']}: {w.get('reason', '')}", file=out)
    return 0


def process_comments(
    *,
    comment_source_id: str,
    paper_source_id: str,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase D: process agency comments into issues + revision instructions."""
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    comment_result = CommentProcessor().process_source(
        comment_source_id, paper_source_id, str(repo_root_path)
    )
    if comment_result["status"] != "success":
        print(
            f"error: comment processing failed: {comment_result['reason']}",
            file=out,
        )
        return 1

    registry = IssueRegistry()
    all_issues = registry.get_all(paper_source_id, str(repo_root_path))
    issue_eval = IssueEval().run(
        all_issues,
        working_paper_source_id=paper_source_id,
        repo_root=str(repo_root_path),
    )
    if issue_eval["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(issue_eval["reason_codes"]),
            file=out,
        )
        return 1

    registry.write_issues_projection(paper_source_id, str(repo_root_path))

    rev_result = RevisionGenerator().generate_for_source(
        paper_source_id, str(repo_root_path)
    )
    if rev_result["status"] != "success":
        print(
            f"error: revision generation failed: {rev_result['reason']}",
            file=out,
        )
        return 1

    instructions = _load_paper_jsonl(
        repo_root_path, paper_source_id, "revision_instructions.jsonl"
    )
    claims = _load_paper_jsonl(repo_root_path, paper_source_id, "claims.jsonl")
    rev_eval = RevisionEval().run(instructions, claims)
    if rev_eval["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(rev_eval["reason_codes"]),
            file=out,
        )
        return 1

    print(f"✓ comment_source_id: {comment_source_id}", file=out)
    print(f"✓ paper_source_id: {paper_source_id}", file=out)
    print(f"✓ issues created: {comment_result['issues_created']}", file=out)
    print(f"✓ unstructured warnings: {comment_result['warnings']}", file=out)
    print(
        f"✓ revision instructions generated: {rev_result['instruction_count']}",
        file=out,
    )
    print(
        f"✓ instructions blocked at generation: {rev_result['blocked_count']}",
        file=out,
    )
    print("", file=out)
    print(
        "Review revision instructions in paper/revision_instructions.jsonl",
        file=out,
    )
    print(
        f"Run: approve-revisions --source-id {paper_source_id}", file=out
    )
    return 0


def approve_revisions(
    *,
    source_id: str,
    instruction_ids: str | None = None,
    all_pending: bool = False,
    vault: str | None = None,
    poll: bool = False,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase D: human-gated application of revision instructions.

    Writes one review form per selected instruction to
    vault/Reviews/Revisions/Pending/. Polls (or exits with awaiting status)
    until the human marks the form as submitted with decision==approve, then
    runs RevisionWorkflow.apply_all_approved.
    """
    out = out_stream if out_stream is not None else sys.stdout
    repo_root_path = (repo_root or Path.cwd()).resolve()

    if not source_id:
        print("error: must provide --source-id", file=out)
        return 1

    instructions = _load_paper_jsonl(
        repo_root_path, source_id, "revision_instructions.jsonl"
    )

    selected: list[Dict[str, Any]]
    if all_pending:
        selected = [i for i in instructions if i.get("status") == "pending"]
    elif instruction_ids:
        wanted = {x.strip() for x in instruction_ids.split(",") if x.strip()}
        selected = [
            i for i in instructions if i.get("instruction_id") in wanted
        ]
    else:
        print(
            "error: must provide --all-pending or --instruction-ids",
            file=out,
        )
        return 1

    if not selected:
        print("error: no instructions matched the selection", file=out)
        return 1

    if vault:
        vault_root = Path(vault).resolve()
        pending_dir = vault_root / "Reviews" / "Revisions" / "Pending"
        completed_dir = vault_root / "Reviews" / "Revisions" / "Completed"
        pending_dir.mkdir(parents=True, exist_ok=True)
        completed_dir.mkdir(parents=True, exist_ok=True)
        for inst in selected:
            iid = inst["instruction_id"]
            form_path = pending_dir / f"{iid}_review.md"
            form_path.write_text(
                _render_revision_review_form(inst), encoding="utf-8"
            )
        print(
            f"✓ wrote {len(selected)} review form(s) to "
            + str(pending_dir),
            file=out,
        )
        if not poll:
            print(
                "Set review_status: submitted and decision: approve in each "
                "form, then re-run with --poll to apply.",
                file=out,
            )
            return 0

        # Poll the forms in a single non-blocking sweep — we honour the
        # constitutional rule: no auto-application. Only forms explicitly
        # marked submitted with decision==approve are applied.
        approved_ids: list[str] = []
        for inst in selected:
            iid = inst["instruction_id"]
            form_path = pending_dir / f"{iid}_review.md"
            decision = _read_revision_decision(form_path)
            if decision == "approve":
                inst["status"] = "approved"
                approved_ids.append(iid)
                # Move form to Completed.
                try:
                    shutil.move(
                        str(form_path),
                        str(completed_dir / form_path.name),
                    )
                except OSError:
                    pass
        # Persist updated instruction statuses.
        _write_paper_jsonl(
            repo_root_path,
            source_id,
            "revision_instructions.jsonl",
            instructions,
        )

        if not approved_ids:
            print("no approved instructions found in vault forms", file=out)
            return 0
    else:
        # No vault — operator must have already set status==approved manually.
        approved_ids = [
            i["instruction_id"]
            for i in selected
            if i.get("status") == "approved"
        ]
        if not approved_ids:
            print(
                "error: no instructions with status=approved selected. "
                "Use --vault to emit human review forms first.",
                file=out,
            )
            return 1

    workflow_result = RevisionWorkflow().apply_all_approved(
        source_id, approved_ids, str(repo_root_path)
    )
    if workflow_result["status"] != "success":
        print(
            f"error: revision workflow failed: {workflow_result['reason']}",
            file=out,
        )
        return 1
    if workflow_result["blocked"] > 0:
        print(
            f"error: {workflow_result['blocked']} instruction(s) blocked. "
            "See paper/revision_diff.jsonl for reasons.",
            file=out,
        )
        return 1

    paper_dir = repo_root_path / "processed"
    print(f"✓ source_id: {source_id}", file=out)
    print(f"✓ applied: {workflow_result['applied']}", file=out)
    print(f"✓ blocked: {workflow_result['blocked']}", file=out)
    print(
        f"✓ revised_draft: {paper_dir}/<family>/{source_id}/paper/revised_draft.json",
        file=out,
    )
    return 0


_REVISION_REVIEW_FORM_TEMPLATE = """---
instruction_id: "{instruction_id}"
issue_id: "{issue_id}"
review_status: pending
reviewer_id: ""
decision: ""
reviewed_at: ""
notes: ""
---

# Revision Review: {instruction_id}

**Target section:** {target_section}
**Instruction type:** {instruction_type}
**Priority:** {priority}

## Instruction

{instruction_text}

## Expected outcome

{expected_outcome}

---

Set `decision` to: approve | reject | defer
Set `reviewer_id` to your reviewer identifier.
Set `review_status` to: submitted

Approval is required before the revision is applied.
"""


def _render_revision_review_form(instruction: Dict[str, Any]) -> str:
    return _REVISION_REVIEW_FORM_TEMPLATE.format(
        instruction_id=instruction.get("instruction_id", ""),
        issue_id=instruction.get("issue_id", ""),
        target_section=instruction.get("target_section", ""),
        instruction_type=instruction.get("instruction_type", ""),
        priority=instruction.get("priority", ""),
        instruction_text=instruction.get("instruction_text", ""),
        expected_outcome=instruction.get("expected_outcome", ""),
    )


def _read_revision_decision(form_path: Path) -> str:
    if not form_path.is_file():
        return ""
    try:
        raw = form_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    fm, _ = _split_frontmatter(raw)
    if not isinstance(fm, dict):
        return ""
    if str(fm.get("review_status") or "").strip() != "submitted":
        return ""
    if not str(fm.get("reviewer_id") or "").strip():
        return ""
    return str(fm.get("decision") or "").strip()


def _load_paper_jsonl(
    repo_root: Path, source_id: str, filename: str
) -> list[Dict[str, Any]]:
    from .extraction._paths import find_processed_dir

    processed_dir, _ = find_processed_dir(repo_root, source_id)
    if processed_dir is None:
        return []
    path = processed_dir / "paper" / filename
    if not path.is_file():
        return []
    out: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _write_paper_jsonl(
    repo_root: Path,
    source_id: str,
    filename: str,
    records: list[Dict[str, Any]],
) -> None:
    from .extraction._paths import find_processed_dir

    processed_dir, _ = find_processed_dir(repo_root, source_id)
    if processed_dir is None:
        return
    paper_dir = processed_dir / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    path = paper_dir / filename
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            )


def _load_candidates(repo_root: Path, source_id: str) -> list[Dict[str, Any]]:
    from .extraction._paths import find_processed_dir
    processed_dir, _ = find_processed_dir(repo_root, source_id)
    if processed_dir is None:
        return []
    path = processed_dir / "stories" / "candidates.jsonl"
    if not path.is_file():
        return []
    out: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m spectrum_systems_core.cli",
        description=(
            "spectrum_systems_core CLI. process-source ingests a raw source "
            "into a source_record + text_units.jsonl, runs eval + control, "
            "promotes to the data lake, and regenerates the Obsidian "
            "projection."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ps = sub.add_parser(
        "process-source",
        help="Ingest one raw source end-to-end.",
        description=(
            "Run the Phase A ingestion pipeline on a single source. "
            "Reads raw/<family>/<source_id>/source.{txt,md} + metadata.json. "
            "Writes processed/<family>/<source_id>/source_record.json + "
            "text_units.jsonl + markdown/index.md. Promotes to the data lake."
        ),
    )
    ps.add_argument("--source-id", help="Direct source ID under raw/<family>/.")
    ps.add_argument("--vault", help="Path to Obsidian vault root.")
    ps.add_argument(
        "--note",
        help=(
            "Relative path of a vault note to ingest as raw/notes/<slug>/. "
            "Requires --vault."
        ),
    )

    pp = sub.add_parser(
        "prepare-pdf",
        help="Phase B: extract text from a book PDF (run before process-source).",
        description=(
            "Phase B PDF preparation. Validates raw/books/<source_id>/ and "
            "extracts source.pdf into source.txt + pages.jsonl + "
            "extraction_report.json. Writes a view-only Markdown projection "
            "under processed/books/<source_id>/markdown/index.md. "
            "This command does NOT call process-source — Phase A and Phase "
            "B are deliberately separate steps. After prepare-pdf succeeds, "
            "run process-source with the same --source-id."
        ),
    )
    pp.add_argument(
        "--source-id",
        required=True,
        help="The book source_id (must match raw/books/<source_id>/ directory).",
    )

    es = sub.add_parser(
        "extract-stories",
        help="Phase C: chunk a source, extract story candidates, score, gate.",
        description=(
            "Phase C story extraction pipeline. Reads "
            "processed/<family>/<source_id>/text_units.jsonl. Writes "
            "stories/chunks.jsonl, stories/candidates.jsonl, and "
            "markdown/stories.md. Emits Tier-1 review forms to the vault. "
            "No auto-promotion: human review required for promotion."
        ),
    )
    es.add_argument(
        "--source-id",
        required=True,
        help="Source identifier (must exist under processed/<family>/).",
    )
    es.add_argument(
        "--vault",
        help=(
            "Path to Obsidian vault root. If provided, review forms for "
            "Tier-1 admit candidates are written under "
            "Reviews/Stories/Pending/."
        ),
    )

    pk = sub.add_parser(
        "promote-knowledge",
        help="Phase C: human promotion of a concept/theme/analogy/connection.",
        description=(
            "Promote a knowledge artifact from candidate to promoted status. "
            "FINDING-C-003 fix: synthesis artifacts never auto-promote — a "
            "human must run this command per artifact."
        ),
    )
    pk.add_argument("--artifact-id", required=True)
    pk.add_argument("--source-id", required=True)
    pk.add_argument(
        "--artifact-type",
        required=True,
        choices=["concept", "theme", "analogy", "connection"],
    )

    ec = sub.add_parser(
        "extract-claims",
        help="Phase D: extract claims + assumptions, build evidence.",
        description=(
            "Phase D claim/assumption extraction pipeline. Reads "
            "processed/<family>/<source_id>/text_units.jsonl. Writes "
            "paper/claims.jsonl, paper/assumptions.jsonl, paper/evidence.jsonl, "
            "and paper/contradiction_summary.json. Runs ClaimEval and "
            "EvidenceEval; blocks the pipeline on any failed required eval."
        ),
    )
    ec.add_argument("--source-id", required=True)

    pc = sub.add_parser(
        "process-comments",
        help="Phase D: process agency comments into issues + revision instructions.",
        description=(
            "Phase D agency comment workflow. Reads text units from "
            "raw/comments/<comment_source_id>/ and writes issues + revision "
            "instructions under processed/<family>/<paper_source_id>/paper/. "
            "Unstructured comments produce unstructured_comment_warning "
            "artifacts (FINDING-D-003)."
        ),
    )
    pc.add_argument("--comment-source-id", required=True)
    pc.add_argument("--paper-source-id", required=True)

    ar = sub.add_parser(
        "approve-revisions",
        help="Phase D: human-gated application of revision instructions.",
        description=(
            "Phase D revision gate. Emits a review form per instruction to "
            "vault/Reviews/Revisions/Pending/. Only instructions explicitly "
            "approved (review_status=submitted, decision=approve) are "
            "applied via Sonnet. No auto-application. FINDING-D-001 fix: "
            "post-revision claim drop check blocks any revision that "
            "removes a high-materiality claim."
        ),
    )
    ar.add_argument("--source-id", required=True)
    ar.add_argument(
        "--instruction-ids",
        help="Comma-separated list of instruction_ids to review.",
    )
    ar.add_argument(
        "--all-pending",
        action="store_true",
        help="Select all instructions with status=pending.",
    )
    ar.add_argument("--vault", help="Path to Obsidian vault root (review forms).")
    ar.add_argument(
        "--poll",
        action="store_true",
        help=(
            "After emitting review forms, poll for submitted approvals and "
            "apply them. Without --poll, exits after writing forms."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "process-source":
        return process_source(
            source_id=args.source_id,
            vault=args.vault,
            note=args.note,
        )
    if args.command == "prepare-pdf":
        return prepare_pdf(source_id=args.source_id)
    if args.command == "extract-stories":
        return extract_stories(
            source_id=args.source_id, vault=args.vault
        )
    if args.command == "promote-knowledge":
        return promote_knowledge(
            artifact_id=args.artifact_id,
            source_id=args.source_id,
            artifact_type=args.artifact_type,
        )
    if args.command == "extract-claims":
        return extract_claims(source_id=args.source_id)
    if args.command == "process-comments":
        return process_comments(
            comment_source_id=args.comment_source_id,
            paper_source_id=args.paper_source_id,
        )
    if args.command == "approve-revisions":
        return approve_revisions(
            source_id=args.source_id,
            instruction_ids=args.instruction_ids,
            all_pending=args.all_pending,
            vault=args.vault,
            poll=args.poll,
        )
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
