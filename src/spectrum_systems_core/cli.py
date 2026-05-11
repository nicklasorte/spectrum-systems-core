"""Top-level CLI entry point for spectrum_systems_core.

Currently exposes the `process-source` command that ingests one raw source
under `raw/<family>/<source_id>/` end-to-end:

    raw source -> source_record -> text_units.jsonl
    -> SourceEval -> Promoter (SDL_ROOT) -> Obsidian projection

Replaces the vault-note-tag trigger from PR #10. Markdown is view only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import yaml

from .extraction import (
    Chunker,
    StoryEval,
    StoryExtractor,
    StoryReviewGateway,
    StoryworthyFilter,
)
from .ingestion import (
    DocxExtractor,
    GroundingHelper,
    GroundTruthLinker,
    MinutesProcessor,
    ObsidianProjection,
    PDFExtractor,
    Promoter,
    SourceEval,
    SourceLoader,
    deduplicate_minutes,
)
from .agency import (
    AgencyEval,
    AgencyProfileStore,
    MitigationEval,
    MitigationOutcomeTracker,
    MitigationSuggester,
    ObjectionEval,
    ObjectionPredictor,
    PatternIndexer,
    ProfileBuilder,
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
from .synthesis import (
    BundleAssembler,
    BundleEval,
    GroundingEval,
    KeynoteEval,
    KeynoteGenerator,
    ReportGenerator,
    RunManifest,
    StoryMatrix,
    SynthesisReviewGateway,
    ThemeSynthesizer,
    VALID_AUDIENCES,
    VALID_PURPOSES,
    total_cost_usd,
)
from .harness import (
    EntropyAuditor,
    EvalScoreHistory,
    FailurePatternIndex,
    OutcomeMemoryStore,
    OverrideStore,
    RunHistoryStore,
    WorkflowComparator,
)
from .ai import AIAdapter, PromptRegistry
from .governance import GovernanceDashboard
from .governance.apply_compression import apply_compression as _apply_compression
from .orchestration import PipelineOrchestrator


_AI_ADVISORY_BANNER = "⚠️ AI output is advisory only. Review before acting."


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _require_data_lake_store(out_stream=None) -> Optional[Path]:
    """Return DATA_LAKE_PATH/store, or print an error and return None."""
    out = out_stream if out_stream is not None else sys.stdout
    env = os.environ.get("DATA_LAKE_PATH", "")
    if not env or not Path(env).exists():
        print(
            "error: DATA_LAKE_PATH not set or does not exist",
            file=out,
        )
        return None
    store = Path(env) / "store"
    store.mkdir(parents=True, exist_ok=True)
    return store


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
    """Copy a vault note into DATA_LAKE_PATH/store/raw/notes/<slug>/ and return the source_id."""
    note_path = (vault_root / note_relpath).resolve()
    if not note_path.is_file():
        raise FileNotFoundError(f"vault note not found: {note_path}")
    raw_text = note_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw_text)

    slug = _slugify(note_path.stem)
    explicit_id = frontmatter.get("source_id") if isinstance(frontmatter, dict) else None
    source_id = explicit_id if isinstance(explicit_id, str) and explicit_id.strip() else slug

    env = os.environ.get("DATA_LAKE_PATH", "")
    if not env or not Path(env).exists():
        raise OSError("DATA_LAKE_PATH not set or does not exist")
    store_root = Path(env) / "store"
    target_dir = store_root / "raw" / "notes" / source_id
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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1

    if note:
        if not vault:
            print("error: --note requires --vault", file=out)
            return 1
        try:
            source_id = _ingest_vault_note(Path(vault).resolve(), note, store_root)
        except (FileNotFoundError, OSError) as exc:
            print(f"error: {exc}", file=out)
            return 1

    if not source_id:
        print("error: must provide --source-id or --vault + --note", file=out)
        return 1

    loader_result = SourceLoader().load(source_id, str(store_root))
    if loader_result["status"] not in ("success",):
        print(f"error: load failed: {loader_result['reason']}", file=out)
        return 1

    source_record = loader_result["source_record"]
    text_units = loader_result["text_units"]

    eval_result = SourceEval().run(
        source_record, text_units, repo_root=str(store_root)
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
        source_record, text_units, str(store_root)
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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1

    if not source_id:
        print("error: must provide --source-id", file=out)
        return 1

    extractor_result = PDFExtractor().extract(source_id, str(store_root))
    if extractor_result["status"] not in ("success",):
        print(f"error: {extractor_result['reason']}", file=out)
        return 1

    extraction_report = extractor_result["extraction_report"]

    metadata_path = store_root / "raw" / "books" / source_id / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: failed to read metadata.json: {exc}", file=out)
        return 1

    projection_path = ObsidianProjection().write_book_extraction_index(
        source_id, metadata, extraction_report, str(store_root)
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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1

    if not source_id:
        print("error: must provide --source-id", file=out)
        return 1

    chunk_result = Chunker().chunk(source_id, str(store_root))
    if chunk_result["status"] != "success":
        print(f"error: chunker failed: {chunk_result['reason']}", file=out)
        return 1
    chunks = chunk_result["chunks"]

    extractor_result = StoryExtractor().extract_from_source(
        source_id, str(store_root)
    )
    if extractor_result["status"] != "success":
        print(
            f"error: extractor failed: {extractor_result['reason']}",
            file=out,
        )
        return 1
    all_records = extractor_result.get("all_records", [])

    StoryEval().run(all_records, source_id, str(store_root))
    StoryworthyFilter().run_on_source(source_id, str(store_root))

    # Reload candidates after filter rewrites.
    candidates = _load_candidates(store_root, source_id)

    ObsidianProjection().write_story_projection(
        source_id, candidates, str(store_root), label="post-eval"
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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

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
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

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


def format_paper(
    *,
    revised_draft_id: str,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase J: format an approved revised_draft into a publication artifact."""
    from .paper import PublicationFormatter

    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    if not revised_draft_id:
        print("error: must provide --revised-draft-id", file=out)
        return 1

    result = PublicationFormatter().format(
        revised_draft_id=revised_draft_id,
        repo_root=str(repo_root_path),
        vault_root=vault,
    )

    if result["status"] != "success":
        print(f"error: {result['status']}: {result.get('reason', '')}", file=out)
        return 1

    artifact = result["artifact"]
    artifact_path = result.get("artifact_path", "")
    citation_count = len(artifact.get("citations") or [])
    content_hash = artifact.get("content_hash", "")

    projection_path: str | None = None
    if vault:
        try:
            projection_path = (
                ObsidianProjection().write_formatted_paper_projection(
                    artifact, vault
                )
            )
        except OSError as exc:
            print(f"error: projection_failed: {exc}", file=out)
            return 1

    print(f"artifact: {artifact_path}", file=out)
    print(f"citations: {citation_count}", file=out)
    print(f"content_hash: {content_hash}", file=out)
    if projection_path is not None:
        print(f"projection: {projection_path}", file=out)

    return 0


def certify_paper(
    *,
    paper_id: str,
    run_id: str,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase K (GOV-10): run the 7-check certification on a formatted paper."""
    from .governance import GOV10CertificationStep

    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    if not paper_id:
        print("error: must provide --paper-id", file=out)
        return 1
    if not run_id:
        print("error: must provide --run-id", file=out)
        return 1

    result = GOV10CertificationStep().certify(
        paper_id=paper_id,
        run_id=run_id,
        repo_root=str(repo_root_path),
        vault_root=vault,
    )

    record = result.get("record") or {}
    certification_id = result.get("certification_id", "")
    total_cost = float(record.get("total_pipeline_cost_usd") or 0.0)
    release_artifact = result.get("release_artifact")

    if result.get("status") == "PASSED":
        release_path = ""
        if isinstance(release_artifact, dict):
            release_path = release_artifact.get("release_path", "")
        print(f"certification_id: {certification_id}", file=out)
        print(f"status: PASSED", file=out)
        print(f"release: {release_path}", file=out)
        print(f"total_pipeline_cost_usd: {total_cost:.4f}", file=out)
        return 0

    print(f"certification_id: {certification_id}", file=out)
    print("status: FAILED", file=out)
    for reason in record.get("failure_reasons") or [result.get("reason", "")]:
        if reason:
            print(f"  - {reason}", file=out)
    return 1


def build_agency_profile(
    *,
    paper_source_id: str,
    agency_name: str,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase E: ingest agency_comment issues from a paper into the agency profile."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    if not paper_source_id:
        print("error: must provide --paper-source-id", file=out)
        return 1
    if not agency_name:
        print("error: must provide --agency-name", file=out)
        return 1

    builder_result = ProfileBuilder().ingest_issues_into_profile(
        paper_source_id, agency_name, str(repo_root_path)
    )
    if builder_result["status"] != "success":
        print(
            f"error: profile build failed: {builder_result.get('reason', '')}",
            file=out,
        )
        return 1

    agency_slug = builder_result.get("agency_slug") or ""
    if not agency_slug:
        print("error: profile build returned no agency_slug", file=out)
        return 1

    eval_result = AgencyEval().run(agency_slug, str(repo_root_path))
    if eval_result["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(eval_result["reason_codes"]),
            file=out,
        )
        return 1

    warnings = [
        e for e in eval_result.get("eval_results", []) if e.get("status") == "warn"
    ]
    if warnings:
        for w in warnings:
            print(f"⚠ warn: {w['name']}: {w.get('reason', '')}", file=out)

    profile_path = (
        repo_root_path / "agency" / agency_slug / "profile.json"
    )
    print(f"✓ paper_source_id: {paper_source_id}", file=out)
    print(f"✓ agency_slug: {agency_slug}", file=out)
    print(f"✓ positions added: {builder_result['positions_added']}", file=out)
    print(f"✓ history entries added: {builder_result['history_added']}", file=out)
    print(f"✓ build warnings: {builder_result['warnings']}", file=out)
    print(f"✓ profile: {profile_path}", file=out)
    return 0


def predict_objections(
    *,
    paper_source_id: str,
    agency_slug: str,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase E: predict objections, suggest mitigations, build pattern index."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    if not paper_source_id or not agency_slug:
        print("error: must provide --paper-source-id and --agency-slug", file=out)
        return 1

    pred_result = ObjectionPredictor().predict_for_paper(
        paper_source_id, agency_slug, str(repo_root_path)
    )
    if pred_result["status"] == "insufficient_history":
        print(
            f"insufficient history: {pred_result.get('reason', '')}", file=out
        )
        return 0
    if pred_result["status"] != "success":
        print(
            f"error: prediction failed: {pred_result.get('reason', '')}",
            file=out,
        )
        return 1

    predictions = pred_result["predictions"]
    obj_eval = ObjectionEval().run(predictions)
    if obj_eval["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(obj_eval["reason_codes"]),
            file=out,
        )
        return 1

    mit_result = MitigationSuggester().suggest_for_predictions(
        paper_source_id, str(repo_root_path)
    )
    if mit_result["status"] != "success":
        print(
            f"error: mitigation suggestion failed: "
            f"{mit_result.get('reason', '')}",
            file=out,
        )
        return 1

    # Reload mitigations.jsonl now that the suggester appended.
    from .extraction._paths import find_processed_dir as _fpd

    processed_dir, _ = _fpd(repo_root_path, paper_source_id)
    mitigations: list[Dict[str, Any]] = []
    if processed_dir is not None:
        m_path = processed_dir / "paper" / "objections" / "mitigations.jsonl"
        if m_path.is_file():
            with m_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    mitigations.append(json.loads(line))

    mit_eval = MitigationEval().run(mitigations, predictions)
    if mit_eval["decision"] == "block":
        print(
            "error: blocked: " + ", ".join(mit_eval["reason_codes"]),
            file=out,
        )
        return 1

    pattern_result = PatternIndexer().build_patterns(str(repo_root_path))
    if pattern_result["status"] != "success":
        print(
            f"error: pattern indexer failed: "
            f"{pattern_result.get('reason', '')}",
            file=out,
        )
        return 1

    print(f"✓ paper_source_id: {paper_source_id}", file=out)
    print(f"✓ agency_slug: {agency_slug}", file=out)
    print(f"✓ predictions: {len(predictions)}", file=out)
    print(f"✓ mitigations: {mit_result['mitigations']}", file=out)
    print(f"✓ blocked at generation: {mit_result['blocked']}", file=out)
    print(f"✓ recurring patterns: {pattern_result['pattern_count']}", file=out)
    print("", file=out)
    print(
        "Review predictions in paper/objections/predictions.jsonl",
        file=out,
    )
    return 0


def track_outcome(
    *,
    mitigation_id: str,
    agency_slug: str,
    paper_source_id: str,
    outcome: str,
    secondary_source_id: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase E: record the outcome of an applied mitigation (FINDING-E-004)."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    if not mitigation_id or not agency_slug or not paper_source_id or not outcome:
        print(
            "error: must provide --mitigation-id, --agency-slug, "
            "--paper-source-id and --outcome",
            file=out,
        )
        return 1

    result = MitigationOutcomeTracker().record_outcome(
        mitigation_id=mitigation_id,
        agency_slug=agency_slug,
        paper_source_id=paper_source_id,
        human_marked_outcome=outcome,
        secondary_check_source_id=secondary_source_id,
        repo_root=str(repo_root_path),
    )
    if result["status"] != "success":
        print(
            f"error: outcome tracking failed: {result.get('reason', '')}",
            file=out,
        )
        return 1

    print(f"✓ mitigation_id: {mitigation_id}", file=out)
    print(f"✓ agency_slug: {agency_slug}", file=out)
    print(f"✓ human_marked_outcome: {outcome}", file=out)
    print(f"✓ final_outcome: {result['final_outcome']}", file=out)
    print(f"✓ auto_downgraded: {result['auto_downgraded']}", file=out)
    return 0


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


def synthesize(
    *,
    audience: str,
    purpose: str,
    recipe_id: str | None = None,
    vault: str | None = None,
) -> int:
    """Run a Phase F synthesis end-to-end.

    Loop: structured retrieval -> context bundle -> Sonnet generation ->
    grounding eval -> human review.
    """
    import uuid

    if audience not in VALID_AUDIENCES:
        print(f"error: invalid audience: {audience}", file=sys.stderr)
        return 2
    if purpose not in VALID_PURPOSES:
        print(f"error: invalid purpose: {purpose}", file=sys.stderr)
        return 2

    repo_root = _require_data_lake_store(sys.stderr)
    if repo_root is None:
        return 1
    run_id = str(uuid.uuid4())

    if recipe_id is None:
        recipe_id = (
            "default_keynote_v1" if purpose == "keynote" else "default_report_v1"
        )

    RunManifest().open_run(run_id, audience, purpose, str(repo_root))

    bundle_result = BundleAssembler().assemble(
        run_id, recipe_id, audience, purpose, str(repo_root)
    )
    if bundle_result["status"] != "success":
        print(
            f"error: bundle assembly {bundle_result['status']}: "
            f"{bundle_result['reason']}",
            file=sys.stderr,
        )
        return 3
    bundle = bundle_result["bundle"]

    bundle_eval = BundleEval().run(bundle)
    if bundle_eval["decision"] != "allow":
        print(
            "error: bundle blocked: " + ", ".join(bundle_eval["reason_codes"]),
            file=sys.stderr,
        )
        return 3

    theme_result = ThemeSynthesizer().synthesize(run_id, str(repo_root))
    themes_path = repo_root / "synthesis" / run_id / "themes.jsonl"
    themes: list = []
    if themes_path.is_file():
        for line in themes_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            themes.append(json.loads(line))

    matrix_result = StoryMatrix().build(run_id, audience, themes, str(repo_root))

    report_summary: Dict[str, Any] = {}
    keynote_summary: Dict[str, Any] = {}
    report_draft: Dict[str, Any] = {}
    keynote_scaffold: Dict[str, Any] = {}

    if purpose in ("report", "both"):
        report_result = ReportGenerator().generate(
            run_id, bundle, audience, str(repo_root)
        )
        if report_result["status"] != "success":
            print(
                f"error: report generation: {report_result['reason']}",
                file=sys.stderr,
            )
            return 4
        report_path = repo_root / "synthesis" / run_id / "report_draft.json"
        report_draft = json.loads(report_path.read_text(encoding="utf-8"))
        grounding = GroundingEval().run(report_draft, str(repo_root))
        report_draft = json.loads(report_path.read_text(encoding="utf-8"))
        if grounding["decision"] == "block":
            print(
                "error: grounding eval blocked: "
                + ", ".join(grounding["reason_codes"]),
                file=sys.stderr,
            )
            return 4
        report_summary = {
            "section_count": len(report_draft.get("sections", [])),
            "grounded_count": sum(
                1 for s in report_draft.get("sections", []) if s.get("grounded")
            ),
        }

    if purpose in ("keynote", "both"):
        keynote_result = KeynoteGenerator().generate(
            run_id, bundle, audience, matrix_result, str(repo_root)
        )
        if keynote_result["status"] != "success":
            print(
                f"error: keynote generation: {keynote_result['reason']}",
                file=sys.stderr,
            )
            return 5
        scaffold_path = repo_root / "synthesis" / run_id / "keynote_scaffold.json"
        keynote_scaffold = json.loads(scaffold_path.read_text(encoding="utf-8"))
        key_eval = KeynoteEval().run(
            keynote_scaffold, bundle, repo_root=str(repo_root)
        )
        if key_eval["decision"] == "block":
            print(
                "error: keynote eval blocked: " + ", ".join(key_eval["reason_codes"]),
                file=sys.stderr,
            )
            return 5
        keynote_summary = {"beat_count": len(keynote_scaffold.get("arc", []))}

    close_result = RunManifest().close_run(run_id, str(repo_root))
    cost_total = total_cost_usd(run_id, str(repo_root))

    _record_synthesis_run_in_harness(run_id, repo_root, vault)

    review_form_path = ""
    if vault:
        review_form_path = SynthesisReviewGateway().emit_review_form(
            run_id=run_id,
            audience=audience,
            purpose=purpose,
            report_draft=report_draft or None,
            keynote_scaffold=keynote_scaffold or None,
            cost_total=cost_total,
            vault_root=vault,
            repo_root=str(repo_root),
        )

    print(f"✓ run_id: {run_id}")
    print(
        f"✓ bundle: {len(bundle.get('items', []))} items, "
        f"~{bundle.get('total_token_estimate', 0)} tokens"
    )
    print(f"✓ themes: {theme_result.get('theme_count', 0)}")
    print(f"✓ stories in matrix: {matrix_result.get('matrix_entries', 0)}")
    if report_summary:
        print(
            f"✓ report sections: {report_summary['section_count']} "
            f"({report_summary['grounded_count']} grounded)"
        )
    if keynote_summary:
        print(f"✓ keynote arc: {keynote_summary['beat_count']} beats")
    print(f"✓ estimated cost: ${cost_total:.4f}")
    if review_form_path:
        print(f"✓ review form: {review_form_path}")
    print("")
    print(f"Next: review synthesis/{run_id}/ and submit review form.")
    return 0


def _record_synthesis_run_in_harness(
    run_id: str,
    repo_root: Path,
    vault: str | None,
) -> None:
    """Best-effort harness recording. NEVER raises (FINDING-G-001 / RT5-002)."""
    try:
        manifest_path = repo_root / "synthesis" / run_id / "run_manifest.json"
        if not manifest_path.is_file():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    try:
        result = RunHistoryStore().record_run(manifest, str(repo_root))
        if result.get("status") != "success":
            print(
                f"warning: harness record_run failed: {result.get('reason', '')}",
                file=sys.stderr,
            )
            return

        # Record eval results from report_draft + keynote_scaffold sections.
        report_path = repo_root / "synthesis" / run_id / "report_draft.json"
        keynote_path = repo_root / "synthesis" / run_id / "keynote_scaffold.json"
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                eval_results = [
                    {
                        "name": "section_grounding",
                        "status": "pass" if s.get("grounded") else "fail",
                        "score": None,
                    }
                    for s in (report.get("sections") or [])
                ]
                EvalScoreHistory().record_eval_results(
                    run_id, eval_results, "report_draft", str(repo_root)
                )
            except (OSError, json.JSONDecodeError):
                pass
        if keynote_path.is_file():
            try:
                scaffold = json.loads(keynote_path.read_text(encoding="utf-8"))
                eval_results = [
                    {
                        "name": "keynote_status",
                        "status": (
                            "pass"
                            if scaffold.get("status") not in {"blocked", "rejected"}
                            else "fail"
                        ),
                        "score": None,
                    }
                ]
                EvalScoreHistory().record_eval_results(
                    run_id, eval_results, "keynote_scaffold", str(repo_root)
                )
            except (OSError, json.JSONDecodeError):
                pass

        # Ingest failures (ungrounded sections).
        failures: list[Dict[str, Any]] = []
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                for section in report.get("sections", []) or []:
                    if not section.get("grounded"):
                        failures.append(
                            {
                                "reason_code": "ungrounded_section",
                                "failure_detail": (
                                    f"section {section.get('section_title', '?')} "
                                    f"({section.get('section_type', '?')}) "
                                    f"had {len(section.get('unverified_citations', []) or [])} "
                                    "unverified citations"
                                ),
                            }
                        )
            except (OSError, json.JSONDecodeError):
                pass
        if failures:
            FailurePatternIndex().ingest_failures(
                run_id, failures, str(repo_root)
            )
            patterns = FailurePatternIndex().get_top_patterns(
                str(repo_root), n=50
            )
            for pattern in patterns:
                if int(pattern.get("occurrence_count", 0)) >= 3 and not pattern.get(
                    "eval_candidate_id"
                ):
                    FailurePatternIndex().propose_eval_candidate(
                        pattern, str(repo_root)
                    )

        try:
            RunHistoryStore().write_run_history_projection(
                str(repo_root), vault
            )
        except Exception as exc:  # pragma: no cover
            print(
                f"warning: harness projection failed: {exc}", file=sys.stderr
            )
    except Exception as exc:  # pragma: no cover
        print(f"warning: harness memory recording failed: {exc}", file=sys.stderr)


def record_run(
    *,
    run_id: str,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase G: record a completed synthesis run into harness memory."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root
    if not run_id:
        print("error: must provide --run-id", file=out)
        return 1
    manifest_path = repo_root_path / "synthesis" / run_id / "run_manifest.json"
    if not manifest_path.is_file():
        print(f"error: run_manifest not found: {manifest_path}", file=out)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: failed to read run_manifest: {exc}", file=out)
        return 1

    rh_result = RunHistoryStore().record_run(manifest, str(repo_root_path))
    if rh_result["status"] != "success":
        print(f"warning: record_run: {rh_result.get('reason', '')}", file=out)

    eval_count = 0
    pattern_count = 0
    candidate_count = 0
    report_path = repo_root_path / "synthesis" / run_id / "report_draft.json"
    keynote_path = repo_root_path / "synthesis" / run_id / "keynote_scaffold.json"

    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            results = [
                {
                    "name": "section_grounding",
                    "status": "pass" if s.get("grounded") else "fail",
                    "score": None,
                }
                for s in (report.get("sections") or [])
            ]
            EvalScoreHistory().record_eval_results(
                run_id, results, "report_draft", str(repo_root_path)
            )
            eval_count += len(results)
        except (OSError, json.JSONDecodeError):
            pass
    if keynote_path.is_file():
        try:
            scaffold = json.loads(keynote_path.read_text(encoding="utf-8"))
            results = [
                {
                    "name": "keynote_status",
                    "status": (
                        "pass"
                        if scaffold.get("status") not in {"blocked", "rejected"}
                        else "fail"
                    ),
                    "score": None,
                }
            ]
            EvalScoreHistory().record_eval_results(
                run_id, results, "keynote_scaffold", str(repo_root_path)
            )
            eval_count += len(results)
        except (OSError, json.JSONDecodeError):
            pass

    failures: list[Dict[str, Any]] = []
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for section in report.get("sections", []) or []:
                if not section.get("grounded"):
                    failures.append(
                        {
                            "reason_code": "ungrounded_section",
                            "failure_detail": (
                                f"section {section.get('section_title', '?')} "
                                f"({section.get('section_type', '?')}) "
                                f"had {len(section.get('unverified_citations', []) or [])} "
                                "unverified citations"
                            ),
                        }
                    )
        except (OSError, json.JSONDecodeError):
            pass
    if failures:
        ingest_result = FailurePatternIndex().ingest_failures(
            run_id, failures, str(repo_root_path)
        )
        pattern_count = (
            ingest_result.get("new_patterns", 0)
            + ingest_result.get("patterns_updated", 0)
        )
        for pattern in FailurePatternIndex().get_top_patterns(
            str(repo_root_path), n=50
        ):
            if int(pattern.get("occurrence_count", 0)) >= 3 and not pattern.get(
                "eval_candidate_id"
            ):
                cand = FailurePatternIndex().propose_eval_candidate(
                    pattern, str(repo_root_path)
                )
                if cand.get("status") == "success":
                    candidate_count += 1

    try:
        RunHistoryStore().write_run_history_projection(
            str(repo_root_path), vault
        )
    except Exception as exc:  # pragma: no cover
        print(f"warning: projection failed: {exc}", file=out)

    print(f"✓ run_id: {run_id}", file=out)
    print(f"✓ entry_id: {rh_result.get('entry_id', '')}", file=out)
    print(f"✓ eval results recorded: {eval_count}", file=out)
    print(f"✓ patterns updated: {pattern_count}", file=out)
    print(f"✓ candidates proposed: {candidate_count}", file=out)
    return 0


def record_outcome(
    *,
    outcome_type: str,
    source_id: str,
    paper_source_id: str,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase G: record a revision or mitigation outcome into harness memory."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root
    if outcome_type not in ("revision", "mitigation"):
        print("error: --type must be revision|mitigation", file=out)
        return 1

    store = OutcomeMemoryStore()
    if outcome_type == "revision":
        diffs = _load_paper_jsonl(repo_root_path, paper_source_id, "revision_diff.jsonl")
        instructions = _load_paper_jsonl(
            repo_root_path, paper_source_id, "revision_instructions.jsonl"
        )
        diff = next((d for d in diffs if d.get("diff_id") == source_id), None)
        if diff is None:
            print(f"error: revision_diff not found: {source_id}", file=out)
            return 1
        instruction = next(
            (i for i in instructions
             if i.get("instruction_id") == diff.get("instruction_id")),
            {},
        )
        result = store.record_revision_outcome(diff, instruction, str(repo_root_path))
    else:
        outcomes = _load_agency_outcomes(repo_root_path)
        outcome_record = next(
            (o for o in outcomes if o.get("outcome_id") == source_id),
            None,
        )
        if outcome_record is None:
            print(f"error: outcome_record not found: {source_id}", file=out)
            return 1
        if not outcome_record.get("paper_source_id"):
            outcome_record["paper_source_id"] = paper_source_id
        result = store.record_mitigation_outcome(
            outcome_record, str(repo_root_path)
        )

    if result["status"] != "success":
        print(f"error: {result.get('reason', '')}", file=out)
        return 1

    try:
        store.write_outcome_projection(str(repo_root_path), vault)
    except Exception as exc:  # pragma: no cover
        print(f"warning: projection failed: {exc}", file=out)

    rev_rate = store.get_effectiveness_rate("revision", str(repo_root_path))
    mit_rate = store.get_effectiveness_rate("mitigation", str(repo_root_path))
    print(f"✓ recorded {outcome_type} outcome: {result.get('record_id', '')}", file=out)
    print(
        f"✓ revision effectiveness: "
        f"{(rev_rate['effectiveness_rate'] or 0) * 100:.1f}% "
        f"({rev_rate['effective']}/{rev_rate['total']})",
        file=out,
    )
    print(
        f"✓ mitigation effectiveness: "
        f"{(mit_rate['effectiveness_rate'] or 0) * 100:.1f}% "
        f"({mit_rate['effective']}/{mit_rate['total']})",
        file=out,
    )
    return 0


def compare_runs(
    *,
    run_id_a: str,
    run_id_b: str,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase G: compare two synthesis runs across fixed dimensions."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root
    result = WorkflowComparator().compare(
        run_id_a, run_id_b, str(repo_root_path), vault_root=vault
    )
    if result["status"] != "success":
        print(f"error: {result.get('reason', '')}", file=out)
        return 1
    print(f"✓ comparison_id: {result['comparison_id']}", file=out)
    print(f"✓ summary: {result.get('summary', '')}", file=out)
    print(f"✓ recommended_action: {result.get('recommended_action', '')}", file=out)
    print(f"✓ json: {result.get('json_path', '')}", file=out)
    if result.get("vault_projection_path"):
        print(f"✓ vault: {result['vault_projection_path']}", file=out)
    return 0


def record_override(
    *,
    artifact_id: str,
    eval_or_block: str,
    rationale: str,
    human_id: str,
    decision_context: str,
    expires_days: int | None = None,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase G: record a human override (FINDING-G-006)."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root
    result = OverrideStore().record_override(
        decision_context=decision_context,
        overridden_artifact_id=artifact_id,
        overridden_eval_or_block=eval_or_block,
        rationale=rationale,
        overriding_human_id=human_id,
        repo_root=str(repo_root_path),
        expires_days=expires_days,
    )
    if result["status"] != "success":
        print(f"error: {result.get('reason', '')}", file=out)
        return 1
    try:
        OverrideStore().write_overrides_projection(str(repo_root_path), vault)
    except Exception as exc:  # pragma: no cover
        print(f"warning: projection failed: {exc}", file=out)

    print(f"✓ override_id: {result['override_id']}", file=out)
    print(f"✓ expires_at: {result['expires_at']}", file=out)
    if result.get("warning"):
        print("⚠ warning: this override expires within 30 days", file=out)
    return 0


def promote_eval_case(
    *,
    candidate_id: str,
    reviewer_id: str,
    note: str,
    auto_confirm: bool = False,
    repo_root: Path | None = None,
    in_stream=None,
    out_stream=None,
) -> int:
    """Phase G: human promotion of an eval_case_candidate (FINDING-G-003).

    The ONLY path that writes to contracts/evals/. Requires explicit confirm.
    """
    out = out_stream if out_stream is not None else sys.stdout
    inp = in_stream if in_stream is not None else sys.stdin
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    candidates_path = (
        repo_root_path / "harness" / "failures" / "eval_candidates.jsonl"
    )
    if not candidates_path.is_file():
        print(f"error: candidates file not found: {candidates_path}", file=out)
        return 1
    candidates: list[Dict[str, Any]] = []
    with candidates_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    target = next(
        (c for c in candidates if c.get("candidate_id") == candidate_id), None
    )
    if target is None:
        print(f"error: candidate not found: {candidate_id}", file=out)
        return 1
    if target.get("status") != "candidate":
        print(
            f"error: cannot promote — status={target.get('status')!r}",
            file=out,
        )
        return 1

    if not auto_confirm:
        print(
            "This will add a new eval case to contracts/evals/. "
            "Type 'confirm' to proceed:",
            file=out,
        )
        try:
            answer = inp.readline().strip()
        except (OSError, EOFError):
            answer = ""
        if answer != "confirm":
            print("aborted: confirmation not received", file=out)
            return 1

    artifact_type = str(target.get("proposed_target_artifact_type") or "report_draft")
    registry_path = (
        repo_root_path / "contracts" / "evals" / f"{artifact_type}_evals.json"
    )
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            if not isinstance(registry, list):
                registry = []
        except (OSError, json.JSONDecodeError):
            registry = []
    else:
        registry = []

    short_id = str(uuid.uuid4())[:8]
    eval_case = {
        "id": str(uuid.uuid4()),
        "name": f"EVAL-PROMOTED-{short_id}",
        "eval_type": str(target.get("proposed_eval_type") or "policy_alignment"),
        "metric_name": str(target.get("proposed_metric_name") or "promoted_metric"),
        "target_artifact_type": artifact_type,
        "required": True,
        "pass_condition": "boolean",
        "runner": "deterministic",
        "promoted_from_candidate_id": candidate_id,
        "promoted_by": reviewer_id,
        "promotion_note": note or "",
    }
    registry.append(eval_case)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    target["status"] = "promoted"
    target["promotion_note"] = note or ""
    with candidates_path.open("w", encoding="utf-8") as fh:
        for c in candidates:
            fh.write(json.dumps(c, sort_keys=True, separators=(",", ":")) + "\n")

    print(f"✓ promoted candidate {candidate_id} as {eval_case['name']}", file=out)
    print(f"✓ written to: {registry_path}", file=out)
    return 0


def audit_entropy(
    *,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase G: scan for entropy and produce a report (FINDING-G-007)."""
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root
    result = EntropyAuditor().run_audit(str(repo_root_path), vault)
    if result["status"] != "success":
        print(f"error: {result.get('reason', '')}", file=out)
        return 1
    report = result.get("report") or {}
    flagged = report.get("flagged_items") or []
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for item in flagged:
        sev = item.get("severity", "low")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    print(f"✓ report_id: {report.get('report_id', '')}", file=out)
    print(f"✓ total_flagged: {result.get('total_flagged', 0)}", file=out)
    print(
        f"✓ severity: high={severity_counts['high']} "
        f"medium={severity_counts['medium']} low={severity_counts['low']}",
        file=out,
    )
    print(
        "See harness/markdown/entropy.md for the full report.",
        file=out,
    )
    return 0


def _load_agency_outcomes(repo_root: Path) -> list[Dict[str, Any]]:
    """Scan agency/<slug>/mitigation_outcomes.jsonl across all slugs."""
    out: list[Dict[str, Any]] = []
    agency_root = repo_root / "agency"
    if not agency_root.is_dir():
        return out
    for slug_dir in sorted(agency_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        path = slug_dir / "mitigation_outcomes.jsonl"
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def ask_memory(
    *,
    task: str,
    question: str,
    vault: str | None = None,
) -> int:
    """Phase H: ONE entry point for AI memory queries.

    Loop: question -> retrieve -> bundle -> generate -> grounding eval ->
    advisory output. All AI output is advisory; the banner bookends the
    answer (FINDING-H-005 / RT5-003).
    """
    repo_root = _require_data_lake_store(sys.stderr)
    if repo_root is None:
        return 1

    # Banner BEFORE the work starts.
    print(_AI_ADVISORY_BANNER)

    # Validate task is a registered task_type before any API call.
    try:
        PromptRegistry().get(task, repo_root=str(repo_root))
    except (ValueError, FileNotFoundError) as exc:
        print(f"❌ Query failed: unregistered_task_type: {exc}", file=sys.stderr)
        return 1

    if not isinstance(question, str) or len(question.strip()) < 3:
        print("❌ Query failed: question must be at least 3 characters.", file=sys.stderr)
        return 1

    result = AIAdapter().query(
        task_type=task,
        question=question,
        repo_root=str(repo_root),
        vault_root=vault,
    )

    if result["status"] == "blocked":
        failure = result.get("failure") or {}
        print(f"❌ Query blocked: {result.get('reason', '')}", file=sys.stderr)
        if failure:
            print(
                f"   failure_type: {failure.get('failure_type', '?')}",
                file=sys.stderr,
            )
            print(
                f"   failure_detail: {failure.get('failure_detail', '?')}",
                file=sys.stderr,
            )
        return 1

    if result["status"] == "failure":
        print(f"❌ Query failed: {result.get('reason', '')}", file=sys.stderr)
        failure = result.get("failure") or {}
        if failure:
            print(
                f"   failure_type: {failure.get('failure_type', '?')}",
                file=sys.stderr,
            )
        return 1

    output = result["output"]
    cost_record = result.get("cost_record") or {}
    citations = output.get("citations", [])
    verified = output.get("verified_citations", [])
    unverified = output.get("unverified_citations", [])

    # Bookend banner BEFORE the answer (so it can't be missed).
    print(_AI_ADVISORY_BANNER)
    print("")
    print(f"Task: {output.get('task_type', '?')}")
    print(f"Question: {question}")
    print(f"Grounded: {bool(output.get('grounded'))}")
    print(f"Confidence: {output.get('confidence')}")
    print(f"Citations verified: {len(verified)}/{len(citations)}")
    print(
        f"Cost: ${float(cost_record.get('estimated_cost_usd', 0.0)):.6f}"
    )
    print(f"Output ID: {output.get('output_id', '')}")
    print("")

    raw_response = output.get("raw_response", {}) or {}
    if "answer" in raw_response:
        print(raw_response["answer"])
    else:
        print(json.dumps(raw_response, indent=2, sort_keys=True))
    print("")

    if unverified:
        print(
            "⚠️ Unverified citations: "
            + ", ".join(unverified),
            file=sys.stderr,
        )

    # Bookend banner AFTER the answer.
    print(_AI_ADVISORY_BANNER)
    return 0


def audit_governance(
    *,
    vault: str | None = None,
    repo_root: Path | None = None,
    out_stream=None,
) -> int:
    """Phase I: run all scanners and write the 30-line dashboard projection.

    Governance audit failures NEVER block the synthesis pipeline (FINDING-I:
    same rule as Phase G harness audits).
    """
    out = out_stream if out_stream is not None else sys.stdout
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root
    try:
        result = GovernanceDashboard().generate(repo_root_path, vault)
    except Exception as exc:  # pragma: no cover — fail-closed
        print(f"warning: governance audit failed: {exc}", file=out)
        return 0

    if result.get("status") != "success":
        print(
            f"warning: governance audit returned: {result.get('reason', '')}",
            file=out,
        )
        return 0

    audit_id = result.get("audit_id", "")
    total_flagged = int(result.get("total_flagged") or 0)
    high_count = int(result.get("high_count") or 0)
    print(f"✓ audit_id: {audit_id}", file=out)
    print(f"✓ total_flagged: {total_flagged}  high: {high_count}", file=out)

    dashboard = result.get("dashboard") or {}
    cost_status = (dashboard.get("cost_trend") or {}).get(
        "status", "insufficient_history"
    )
    print(f"✓ cost trend: {cost_status}", file=out)

    drift_signals = dashboard.get("drift_signals") or []
    if drift_signals:
        print("Top drift signals:", file=out)
        for sig in drift_signals[:3]:
            print(
                f"  - [{sig.get('signal_strength', '?')}] "
                f"{sig.get('signal_type', '?')}: "
                f"{(sig.get('detail') or '')[:120]}",
                file=out,
            )

    print(
        "See governance/markdown/dashboard.md for the 30-line summary.",
        file=out,
    )
    if high_count > 0:
        return 1
    return 0


def apply_compression_cli(
    *,
    candidate_id: str,
    action: str,
    human_id: str,
    note: str = "",
    yes: bool = False,
    repo_root: Path | None = None,
    out_stream=None,
    in_stream=None,
) -> int:
    """Phase I: human path to act on a compression_candidate.

    NEVER auto-deletes (FINDING-I-006). For action='remove' / 'merge' the
    CLI prints exact commands the human must run manually.
    """
    out = out_stream if out_stream is not None else sys.stdout
    inp = in_stream if in_stream is not None else sys.stdin
    store_root = _require_data_lake_store(out)
    if store_root is None:
        return 1
    repo_root_path = store_root

    print(
        f"This will {action} candidate {candidate_id}. "
        "The system will NOT auto-delete or auto-merge. "
        "Confirm? [yes/no]",
        file=out,
    )
    if not yes:
        try:
            answer = (inp.readline() or "").strip().lower()
        except Exception:
            answer = ""
        if answer != "yes":
            print("aborted: confirmation not received.", file=out)
            return 1

    out_lines: list[str] = []
    result = _apply_compression(
        candidate_id=candidate_id,
        action=action,
        human_id=human_id,
        note=note,
        repo_root=repo_root_path,
        out_lines=out_lines,
    )
    for line in out_lines:
        print(line, file=out)
    if result.get("status") != "success":
        print(f"error: {result.get('reason', '')}", file=out)
        return 1
    print(f"✓ candidate_id: {result.get('candidate_id', '')}", file=out)
    print(f"✓ action: {result.get('action', '')}", file=out)
    print(
        f"✓ applied_action_detail: {result.get('applied_action_detail', '')}",
        file=out,
    )
    return 0


def extract_docx(
    *,
    path: str,
    output_dir: str | None = None,
    out_stream=None,
) -> int:
    """Phase L.0: extract .docx transcript(s) to .txt for process-source.

    If --path is a file: extract that single file and print the result.
    If --path is a directory: extract all .docx files in the directory.
    On any failure: print reason and exit 1.
    On success: print "Extracted: <output_path>" for each file. Exit 0.
    """
    out = out_stream if out_stream is not None else sys.stdout
    p = Path(path)
    extractor = DocxExtractor()

    if p.is_file():
        dest = str(Path(output_dir) / (p.stem + ".txt")) if output_dir else None
        result = extractor.extract(str(p), output_path=dest)
        if result["status"] != "success":
            print(f"error: {result['reason']}", file=out)
            return 1
        print(f"Extracted: {result['output_path']}", file=out)
        return 0

    if p.is_dir():
        results = extractor.extract_batch(str(p), output_dir=output_dir)
        if not results:
            print("No .docx files found.", file=out)
            return 0
        failed = [r for r in results if r["status"] != "success"]
        for r in results:
            if r["status"] == "success":
                print(f"Extracted: {r['output_path']}", file=out)
            else:
                print(f"error: {r['reason']}", file=out)
        if failed:
            return 1
        return 0

    print(f"error: path does not exist or is not a file/directory: {path}", file=out)
    return 1


def run_pipeline(
    *,
    dry_run: bool = False,
    data_lake: str | None = None,
    force: bool = False,
    force_only_missing: bool = False,
    specific_source_id: str | None = None,
    out_stream=None,
) -> int:
    """Phase L.3: scan data-lake/store/raw/transcripts/ and drive the full
    5-stage pipeline (process-source → extract-stories → promote-knowledge
    → extract-claims → synthesize) on the unprocessed transcripts.

    With ``--force``, bypasses idempotency checks and re-runs every stage
    on every transcript. Existing artifacts are NEVER deleted; underlying
    extractors overwrite their own working files (pre-existing behavior).

    Returns:
        0 on success, partial success, or dry_run completion. 1 only when
        the scan itself fails (missing DATA_LAKE_PATH, directory not found).
    """
    out = out_stream if out_stream is not None else sys.stdout

    resolved = data_lake
    if not resolved:
        resolved = os.environ.get("DATA_LAKE_PATH", "")
    if not resolved:
        print(
            "error: DATA_LAKE_PATH not set and --data-lake not provided",
            file=out,
        )
        return 1
    if not Path(resolved).exists():
        print(
            f"error: data lake path does not exist: {resolved}",
            file=out,
        )
        return 1

    transcripts_dir = (
        Path(resolved) / "store" / "raw" / "transcripts"
    )

    print("=== Pipeline Orchestrator ===", file=out)
    if force:
        print("Mode: FORCE RE-PROCESS (all phases)", file=out)
    print(f"Scanning: {transcripts_dir}/", file=out)

    orchestrator = PipelineOrchestrator()
    # Always scan first so we have the per-transcript reason annotations
    # to surface alongside each Running:/Skipping: line.
    scan_result = orchestrator.scan(resolved, force=force)
    if scan_result["status"] != "success":
        print(f"error: scan failed: {scan_result.get('reason', '')}", file=out)
        return 1

    reason_by_filename: dict[str, str] = {
        e["filename"]: e.get("reason", "")
        for e in scan_result.get("unprocessed", [])
    }

    if dry_run:
        already = scan_result.get("already_processed", [])
        unproc = scan_result.get("unprocessed", [])
        print(
            f"Found {len(already) + len(unproc)} transcripts. "
            f"Already processed: {len(already)}. To run: {len(unproc)}.",
            file=out,
        )
        print("", file=out)
        if already:
            print("Already processed (skipping):", file=out)
            for entry in already:
                print(
                    f"  - {entry['filename']} "
                    f"(artifact: {entry['artifact_id']})",
                    file=out,
                )
        if unproc:
            print("Would run:", file=out)
            for entry in unproc:
                reason = entry.get("reason") or "no_processed_evidence"
                print(
                    f"  - {entry['filename']} ({reason})",
                    file=out,
                )
        else:
            print("Nothing to run.", file=out)
        return 0

    result = orchestrator.run(
        resolved,
        dry_run=False,
        force=force,
        force_only_missing=force_only_missing,
        specific_source_id=specific_source_id,
    )
    if result["status"] == "failure" and "scan_failed" in result.get(
        "reason", ""
    ):
        print(f"error: scan failed: {result['reason']}", file=out)
        return 1

    found = (
        len(result["processed_this_run"])
        + len(result["skipped_already_done"])
        + len(result["failed_this_run"])
    )
    print(
        f"Found {found} transcripts. "
        f"Already processed: {len(result['skipped_already_done'])}. "
        f"To run: {result['total_attempted']}.",
        file=out,
    )
    print("", file=out)

    # Per-transcript stage map for the summary output.
    stages_by_filename: dict[str, dict[str, str]] = {
        r["filename"]: r.get("pipeline_stages", {})
        for r in result.get("results", [])
    }

    for entry in result["skipped_already_done"]:
        stages = stages_by_filename.get(entry["filename"], {})
        annotation = _format_stage_summary(stages)
        print(
            f"Skipping (already processed): {entry['filename']} "
            f"(artifact: {entry['artifact_id']}){annotation}",
            file=out,
        )

    for entry in result["processed_this_run"]:
        scan_reason = reason_by_filename.get(entry["filename"], "")
        force_prefix = "[force] " if scan_reason == "forced" else ""
        annotation = (
            f" [{scan_reason}]"
            if scan_reason
            and scan_reason not in ("no_processed_evidence", "forced")
            else ""
        )
        stages = stages_by_filename.get(entry["filename"], {})
        stage_summary = _format_stage_summary(stages)
        print(
            f"{force_prefix}Running: {entry['filename']}{annotation} ... "
            f"✓ artifact: {entry['artifact_id']}{stage_summary}",
            file=out,
        )
    for entry in result["failed_this_run"]:
        scan_reason = reason_by_filename.get(entry["filename"], "")
        force_prefix = "[force] " if scan_reason == "forced" else ""
        annotation = (
            f" [{scan_reason}]"
            if scan_reason
            and scan_reason not in ("no_processed_evidence", "forced")
            else ""
        )
        print(
            f"{force_prefix}Running: {entry['filename']}{annotation} ... "
            f"✗ failed: {entry['reason']}",
            file=out,
        )

    print("", file=out)
    print("=== Summary ===", file=out)

    # Per-transcript "succeeded all stages" tally — every stage in
    # pipeline_stages must be success/forced/skipped (no failure).
    # "Partial" = Stage 1 succeeded but some later stage failed.
    succeeded_all = 0
    partial = 0
    for r in result.get("results", []):
        if r.get("status") not in ("success", "extraction_quality_warning"):
            continue
        stages = r.get("pipeline_stages", {})
        statuses = list(stages.values())
        if any(s == "failure" for s in statuses):
            partial += 1
        else:
            succeeded_all += 1

    print(
        f"Transcripts: {result['total_attempted']} | "
        f"Succeeded all stages: {succeeded_all} | "
        f"Partial: {partial} | Failed: {result['total_failed']}",
        file=out,
    )
    print(
        f"Stages completed: {result['total_stages_completed']} | "
        f"Stages failed: {result['total_stages_failed']}",
        file=out,
    )
    synth_status = result.get("synthesize_status", "not_run")
    synth_glyph = (
        "✓" if synth_status == "success"
        else "✗" if synth_status == "failure"
        else "—"
    )
    print(f"Synthesize: {synth_glyph} {synth_status}", file=out)
    if result["orchestration_record_path"]:
        print(f"Record: {result['orchestration_record_path']}", file=out)

    # Partial success is exit 0; only scan failures are exit 1.
    return 0


def _format_stage_summary(stages: dict[str, str]) -> str:
    """Render a compact one-line summary of pipeline_stages for a transcript."""
    if not stages:
        return ""
    glyphs = {
        "success": "✓",
        "forced": "↻",
        "skipped": "·",
        "failure": "✗",
        "not_run": "—",
    }
    short = {
        "process_source": "src",
        "extract_stories": "sty",
        "promote_knowledge": "knw",
        "extract_claims": "clm",
    }
    parts: list[str] = []
    for stage in (
        "process_source",
        "extract_stories",
        "promote_knowledge",
        "extract_claims",
    ):
        s = stages.get(stage, "not_run")
        parts.append(f"{short[stage]}{glyphs.get(s, '?')}")
    return "  [" + " ".join(parts) + "]"


def extract_typed(
    *,
    source_id: str | None = None,
    all_sources: bool = False,
    data_lake: str | None = None,
    force: bool = False,
    out_stream=None,
) -> int:
    """Phase M3 CLI command: run typed extraction.

    Exit codes:
      0 -- one or more sources succeeded (partial success accepted).
      1 -- nothing was processed (no chunks.jsonl found anywhere).
      2 -- failure in argument resolution (no source_id and no --all, or
           DATA_LAKE_PATH unset).
    """
    import sys
    from pathlib import Path as _Path
    from .extraction.typed_extraction_runner import (
        _resolve_store_root,
        _SOURCE_FAMILIES,
        run_typed_extraction,
    )

    out = out_stream or sys.stdout

    if not source_id and not all_sources:
        print("extract-typed: --source-id or --all required", file=out)
        return 2

    store_root = _resolve_store_root(data_lake)
    if store_root is None:
        print("extract-typed: DATA_LAKE_PATH not set or path missing", file=out)
        return 2

    targets: list[str] = []
    if source_id:
        targets.append(source_id)
    else:
        # Discover every source_id with a chunks.jsonl
        for family in _SOURCE_FAMILIES:
            base = store_root / "processed" / family
            if not base.is_dir():
                continue
            for src_dir in sorted(base.iterdir()):
                if not src_dir.is_dir():
                    continue
                if (src_dir / "stories" / "chunks.jsonl").is_file():
                    targets.append(src_dir.name)

    if not targets:
        print("extract-typed: no sources with chunks.jsonl found", file=out)
        return 1

    succeeded = 0
    skipped = 0
    failed = 0
    for sid in targets:
        result = run_typed_extraction(sid, data_lake=data_lake, force=force)
        status = result.get("status")
        if status == "success":
            succeeded += 1
            print(
                f"extract-typed [{sid}] OK  "
                f"decisions={result.get('decisions', 0)} "
                f"claims={result.get('claims', 0)} "
                f"action_items={result.get('action_items', 0)} "
                f"off_topic={result.get('off_topic_count', 0)}/"
                f"{result.get('total_chunks_classified', 0)} "
                f"warn={result.get('routing_quality_warning', False)}",
                file=out,
            )
        elif status == "skipped":
            skipped += 1
            print(
                f"extract-typed [{sid}] SKIP {result.get('reason', '')}",
                file=out,
            )
        else:
            failed += 1
            print(
                f"extract-typed [{sid}] FAIL {result.get('reason', '')}",
                file=out,
            )

    print(
        f"extract-typed summary: succeeded={succeeded} skipped={skipped} "
        f"failed={failed} total={len(targets)}",
        file=out,
    )
    return 0


def link_ground_truth(
    *,
    data_lake: str | None = None,
    process_minutes: bool = False,
    deduplicate: bool = False,
    out_stream=None,
) -> int:
    """Phase L.2: pair transcripts with meeting-minutes by date.

    With --process-minutes, runs MinutesProcessor over
    ``store/raw/minutes/`` first, then GroundTruthLinker. Without it,
    only the linker runs (assumes minutes_record artifacts already exist).

    With --deduplicate, retires duplicate minutes_record artifacts
    (grouped by raw_hash, oldest kept, rest moved to
    ``$SDL_ROOT/minutes/retired/``) BEFORE linking. Use after a run
    where MinutesProcessor produced duplicate artifacts; subsequent
    runs are idempotent so dedup becomes a no-op.

    Returns 0 on success or partial success. Returns 1 only when the
    DATA_LAKE_PATH cannot be resolved or a fatal linker failure occurs.
    """
    out = out_stream if out_stream is not None else sys.stdout

    resolved = data_lake or os.environ.get("DATA_LAKE_PATH", "")
    if not resolved:
        print(
            "error: DATA_LAKE_PATH not set and --data-lake not provided",
            file=out,
        )
        return 1
    if not Path(resolved).exists():
        print(
            f"error: data lake path does not exist: {resolved}",
            file=out,
        )
        return 1

    print("=== Ground Truth Linker ===", file=out)

    # Optional MinutesProcessor pass.
    minutes_results: list[Dict[str, Any]] = []
    if process_minutes:
        minutes_results = MinutesProcessor().process_directory(resolved)
        successes = [r for r in minutes_results if r.get("status") == "success"]
        skipped = [r for r in minutes_results if r.get("status") == "skipped"]
        failures = [
            r
            for r in minutes_results
            if r.get("status") not in ("success", "skipped")
        ]
        print(
            f"Minutes files processed: {len(successes)} "
            f"(failures: {len(failures)}, skipped: {len(skipped)})",
            file=out,
        )
        for r in failures:
            print(
                f"  ✗ {r.get('docx_path', '')}: {r.get('reason', '')}",
                file=out,
            )

    # Optional dedup pass — runs BEFORE linking so the linker sees
    # exactly one minutes_record per raw_hash.
    if deduplicate:
        dedup = deduplicate_minutes(resolved)
        if dedup["status"] != "success":
            print(
                f"warn: deduplicate failed: {dedup.get('reason', '')}",
                file=out,
            )
        groups = dedup.get("groups_found", 0)
        retired = dedup.get("records_retired", 0)
        kept = dedup.get("records_kept", 0)
        print(
            f"Found {groups} duplicate groups. "
            f"Retired {retired} duplicate records. Kept {kept} records.",
            file=out,
        )
        for inv in dedup.get("invalid_kept", []):
            print(
                f"  ! group kept intact (invalid leader): "
                f"raw_hash={inv.get('raw_hash', '')[:18]}... "
                f"reason={inv.get('reason', '')}",
                file=out,
            )

    # Linker run.
    linker = GroundTruthLinker()
    result = linker.link(resolved)
    if result["status"] == "failure":
        print(f"error: link failed: {result.get('reason', '')}", file=out)
        return 1

    pairs_high = (
        result["pairs_produced"] - result["pairs_pending_review"]
    )
    print(
        f"Pairs produced (high confidence): {pairs_high}",
        file=out,
    )
    print(
        f"Pairs pending review (medium confidence): "
        f"{result['pairs_pending_review']}",
        file=out,
    )
    # Idempotency breakdown — surfaced so re-runs make it visible when
    # existing pairs were skipped instead of re-written.
    print(
        f"Pairs produced (new): {result.get('pairs_new', 0)}",
        file=out,
    )
    print(
        f"Pairs already confirmed (skipped): "
        f"{result.get('pairs_already_confirmed', 0)}",
        file=out,
    )
    print(
        f"Pairs pending review (new): {result['pairs_pending_review']}",
        file=out,
    )
    print(
        f"Pairs already pending review (skipped): "
        f"{result.get('pairs_already_pending', 0)}",
        file=out,
    )

    unmatched_t = result.get("unmatched_transcripts", []) or []
    unmatched_m = result.get("unmatched_minutes", []) or []
    print(f"Unmatched transcripts: {len(unmatched_t)}", file=out)
    for entry in unmatched_t:
        date = entry.get("meeting_date") or "no-date"
        name = entry.get("meeting_name") or entry.get("source_id", "")
        reason = entry.get("reason", "")
        print(f"  - {name} ({date}) [{reason}]", file=out)
    print(f"Unmatched minutes: {len(unmatched_m)}", file=out)
    for entry in unmatched_m:
        date = entry.get("meeting_date") or "no-date"
        name = entry.get("meeting_name") or entry.get("minutes_id", "")
        reason = entry.get("reason", "")
        print(f"  - {name} ({date}) [{reason}]", file=out)

    print("", file=out)
    print(f"Linking report: {result['linking_report_path']}", file=out)
    return 0


def _resolve_pipeline_run_id_from_orchestration(
    data_lake_path: str,
) -> Optional[str]:
    """Read the latest orchestration_run_record run_id, if available.

    The orchestration_run_record uses ``run_id`` rather than the
    ``pipeline_run_id`` name the eval framework adopted. The eval
    framework intentionally renames at its own boundary so the
    orchestration schema does not need bumping for M.4.
    """
    if not data_lake_path:
        return None
    candidates = [
        Path(data_lake_path) / "store" / "artifacts" / "orchestration",
        Path(data_lake_path) / "store" / "orchestration",
    ]
    for d in candidates:
        if not d.is_dir():
            continue
        # Most-recent file wins (sorted by mtime).
        try:
            files = sorted(
                d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            continue
        for path in files:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            run_id = rec.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
    return None


def _orchestration_record_is_dry_run(
    data_lake_path: str, pipeline_run_id: Optional[str]
) -> bool:
    """If the orchestration_run_record for ``pipeline_run_id`` says
    dry_run=true, return True. Defaults to False if the record cannot
    be located -- the caller's --dry-run flag is the authoritative
    fallback.
    """
    if not data_lake_path or not pipeline_run_id:
        return False
    candidates = [
        Path(data_lake_path) / "store" / "artifacts" / "orchestration",
        Path(data_lake_path) / "store" / "orchestration",
    ]
    for d in candidates:
        if not d.is_dir():
            continue
        for path in d.glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("run_id") != pipeline_run_id:
                continue
            return bool(rec.get("dry_run", False))
    return False


def eval_ground_truth(
    *,
    data_lake: Optional[str] = None,
    pipeline_run_id: Optional[str] = None,
    pair_id: Optional[str] = None,
    prompt_version: str = "unspecified",
    set_baseline: bool = False,
    is_dry_run: bool = False,
    out_stream=None,
) -> int:
    """Phase M.4: evaluate the pipeline against confirmed ground_truth pairs.

    Returns 0 on completion (including partial / skipped). Returns 1
    only if SDL_ROOT/DATA_LAKE_PATH cannot be resolved.
    """
    from .evals.m4.runner import EvalRunner, format_cli_report

    out = out_stream if out_stream is not None else sys.stdout

    resolved = data_lake or os.environ.get("DATA_LAKE_PATH", "")
    if not resolved:
        print(
            "error: DATA_LAKE_PATH not set and --data-lake not provided",
            file=out,
        )
        return 1
    if not Path(resolved).exists():
        print(
            f"error: data lake path does not exist: {resolved}",
            file=out,
        )
        return 1

    # Resolve pipeline_run_id: explicit flag wins, then orchestration
    # record's run_id, then a generated UUID.
    if not pipeline_run_id:
        pipeline_run_id = _resolve_pipeline_run_id_from_orchestration(resolved)

    # Auto-detect dry-run from orchestration record so a dry-run
    # pipeline_run never produces eval artifacts even when the user
    # forgets the --dry-run flag on the eval command.
    if not is_dry_run and pipeline_run_id:
        if _orchestration_record_is_dry_run(resolved, pipeline_run_id):
            is_dry_run = True

    runner = EvalRunner(
        data_lake_path=resolved,
        pipeline_run_id=pipeline_run_id,
        prompt_version=prompt_version or "unspecified",
    )
    result = runner.run(
        pair_id_filter=pair_id,
        set_baseline=set_baseline,
        is_dry_run=is_dry_run,
    )
    print(format_cli_report(result), file=out, end="")
    return int(result.get("exit_code", 0))


# ---------------------------------------------------------------------------
# Phase O — verification commands
# ---------------------------------------------------------------------------


def _resolve_verification_sdl(
    data_lake: Optional[str], out
) -> tuple[Optional[Path], Optional[str]]:
    """Resolve (sdl_root, data_lake_path) for the verification commands.

    Returns ``(None, None)`` and prints to ``out`` if nothing can be
    resolved. SDL_ROOT env var wins; otherwise fall back to
    ``<data-lake>/store/artifacts``.
    """
    resolved_lake = data_lake or os.environ.get("DATA_LAKE_PATH", "") or ""
    env_sdl = os.environ.get("SDL_ROOT", "").strip()
    sdl_root: Optional[Path] = None
    if env_sdl:
        sdl_root = Path(env_sdl)
    elif resolved_lake:
        sdl_root = Path(resolved_lake) / "store" / "artifacts"

    if sdl_root is None:
        print(
            "error: SDL_ROOT not set and --data-lake not provided",
            file=out,
        )
        return (None, None)
    try:
        sdl_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: could not create SDL_ROOT '{sdl_root}': {exc}", file=out)
        return (None, None)
    return (sdl_root, resolved_lake)


def verify_pipeline_state(
    *,
    data_lake: Optional[str] = None,
    validate_schemas: bool = True,
    emit_actions_summary: bool = False,
    out_stream=None,
) -> int:
    """Phase O.0 — scan SDL_ROOT, classify artifacts, write pipeline_state_record.

    Exit 0 on completion (including empty SDL_ROOT — that's a finding,
    not a failure). Exit 1 only when SDL_ROOT can't be resolved or the
    record fails to write.
    """
    from .verification import (
        scan_pipeline_state as _scan,
        write_pipeline_state_record as _write,
        emit_actions_summary as _emit,
    )

    out = out_stream if out_stream is not None else sys.stdout
    sdl_root, resolved_lake = _resolve_verification_sdl(data_lake, out)
    if sdl_root is None:
        return 1

    record = _scan(
        data_lake_path=resolved_lake,
        validate_schemas=validate_schemas,
        sdl_root=str(sdl_root),
    )

    target = _write(record, sdl_root=sdl_root)
    if target is None:
        print(
            "error: failed to write pipeline_state_record (see "
            f"{sdl_root}/verifications/*.invalid.json)",
            file=out,
        )
        # Still surface the summary so the operator sees the findings.
    else:
        print(f"wrote: {target}", file=out)

    summary = _emit(record)
    print(summary, file=out, end="")

    if emit_actions_summary:
        gh_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
        if gh_path:
            try:
                with open(gh_path, "a", encoding="utf-8") as fh:
                    fh.write(summary)
            except OSError as exc:
                print(f"warning: GITHUB_STEP_SUMMARY append failed: {exc}", file=out)
    return 0 if target is not None else 1


def review_baseline_candidate(
    *,
    data_lake: Optional[str] = None,
    eval_summary_id: Optional[str] = None,
    out_stream=None,
) -> int:
    """Phase O.5 — print PASS/REVIEW sanity-bound checklist.

    READ-ONLY. Never installs a baseline. The operator must explicitly
    invoke ``eval-ground-truth --set-baseline`` after reviewing this
    output.
    """
    from .verification.findings_compiler import (
        SANITY_BOUNDS,
        _load_latest_eval_summary,
        _load_meeting_extractions_from_sdl,
        compute_extraction_rates,
    )

    out = out_stream if out_stream is not None else sys.stdout
    sdl_root, _resolved_lake = _resolve_verification_sdl(data_lake, out)
    if sdl_root is None:
        return 1

    if eval_summary_id:
        target = sdl_root / "evals" / f"eval_summary_{eval_summary_id}.json"
        if not target.is_file():
            print(
                f"error: eval_summary not found: {target}",
                file=out,
            )
            return 1
        try:
            summary = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read eval_summary: {exc}", file=out)
            return 1
    else:
        summary = _load_latest_eval_summary(sdl_root)
        if summary is None:
            print(
                "error: no eval_summary found under "
                f"{sdl_root}/evals/. Run eval-ground-truth first.",
                file=out,
            )
            return 1

    extractions = _load_meeting_extractions_from_sdl(sdl_root)
    rates = compute_extraction_rates(extractions)

    partial = bool(summary.get("partial_run_warning", False))

    print("=== review-baseline-candidate ===", file=out)
    print(f"eval_summary_id: {summary.get('eval_summary_id', '')}", file=out)
    print(
        f"pipeline_run_id: {summary.get('pipeline_run_id', '')}",
        file=out,
    )
    print(
        f"partial_run_warning: {partial}",
        file=out,
    )
    print("", file=out)
    print("Sanity bounds:", file=out)
    any_review = False
    for key in (
        "regulatory_verb_fallback_rate",
        "human_dedup_rate",
        "off_topic_rate",
    ):
        value = rates.get(key)
        bound = SANITY_BOUNDS[key]
        if value is None:
            label = "REVIEW"
            display = "n/a (no data)"
            any_review = True
        elif value < bound:
            label = "PASS"
            display = f"{value:.3f}"
        else:
            label = "REVIEW"
            display = f"{value:.3f}"
            any_review = True
        print(f"  - [{label}] {key} = {display}  (< {bound:.2f})", file=out)
    print("", file=out)

    if partial:
        any_review = True
        print(
            "  - [REVIEW] partial_run_warning is True on the eval_summary; "
            "--set-baseline will refuse.",
            file=out,
        )
        print("", file=out)

    if any_review:
        print(
            "One or more metrics need human review. Do NOT --set-baseline "
            "until those rates are below their bounds and partial_run_warning "
            "is False.",
            file=out,
        )
    else:
        print(
            "If all metrics PASS and you accept these baseline values, run:\n"
            "  python -m spectrum_systems_core.cli eval-ground-truth "
            "--set-baseline",
            file=out,
        )

    return 0


def compile_findings_cli(
    *,
    data_lake: Optional[str] = None,
    cycle_id: Optional[str] = None,
    out_stream=None,
) -> int:
    """Phase O.6 — write verification_findings artifact + Markdown summary."""
    from .verification import (
        compile_findings as _compile,
        write_verification_findings as _write,
        format_findings_markdown as _format,
    )

    out = out_stream if out_stream is not None else sys.stdout
    sdl_root, _resolved_lake = _resolve_verification_sdl(data_lake, out)
    if sdl_root is None:
        return 1

    cycle = cycle_id or f"phase-O-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}"
    record = _compile(cycle_id=cycle, sdl_root=sdl_root)
    target = _write(record, sdl_root=sdl_root)
    if target is None:
        print(
            "error: failed to write verification_findings (see "
            f"{sdl_root}/verifications/*.invalid.json)",
            file=out,
        )
        return 1
    print(f"wrote: {target}", file=out)

    markdown = _format(record)
    print(markdown, file=out, end="")
    gh_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if gh_path:
        try:
            with open(gh_path, "a", encoding="utf-8") as fh:
                fh.write(markdown)
        except OSError as exc:
            print(f"warning: GITHUB_STEP_SUMMARY append failed: {exc}", file=out)
    return 0


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

    fp = sub.add_parser(
        "format-paper",
        help="Phase J: format an approved revised_draft into a publication artifact.",
        description=(
            "Phase J publication formatting. Reads "
            "processed/<family>/<source_id>/paper/revised_draft.json, "
            "validates against the revised_draft schema, transforms into a "
            "formatted_paper_artifact with numbered citations and a "
            "deduplicated reference list, and writes "
            "paper/formatted/<paper_id>.json. Sets "
            "publication_metadata.status='ready_for_certification' so Phase "
            "K (GOV-10) can pick it up. Zero LLM calls."
        ),
    )
    fp.add_argument("--revised-draft-id", required=True)
    fp.add_argument(
        "--vault",
        help=(
            "If provided, also write a view-only Markdown projection at "
            "vault/Papers/<paper_id>.md."
        ),
    )

    cp = sub.add_parser(
        "certify-paper",
        help="Phase K (GOV-10): run terminal certification on a formatted paper.",
        description=(
            "Phase K certification gate. Runs 7 deterministic checks over "
            "the artifact chain for the given paper_id and emits a "
            "done_certification_record under "
            "governance/certifications/<certification_id>.json. On PASSED, "
            "also writes a release_artifact at "
            "paper/released/<paper_id>.json. Fail-closed; never raises."
        ),
    )
    cp.add_argument("--paper-id", required=True)
    cp.add_argument("--run-id", required=True)
    cp.add_argument(
        "--vault",
        help=(
            "If provided, also write a view-only Markdown projection at "
            "vault/Certifications/<certification_id>.md."
        ),
    )

    bap = sub.add_parser(
        "build-agency-profile",
        help="Phase E: ingest agency_comment issues into an agency profile.",
        description=(
            "Phase E agency profile builder. Reads paper/issues.jsonl issues "
            "with issue_type=='agency_comment' from the named paper, "
            "normalizes the agency name to a canonical agency_slug, and "
            "writes / updates agency/<slug>/profile.json + positions.jsonl + "
            "objection_history.jsonl. Runs AgencyEval after the build."
        ),
    )
    bap.add_argument("--paper-source-id", required=True)
    bap.add_argument("--agency-name", required=True)

    po = sub.add_parser(
        "predict-objections",
        help="Phase E: predict agency objections + suggest mitigations.",
        description=(
            "Phase E objection prediction + mitigation suggestion. Reads the "
            "paper's claims and the agency profile. Writes "
            "paper/objections/predictions.jsonl, mitigations.jsonl, and "
            "rebuilds agency/patterns.jsonl. Predictions are advisory only."
        ),
    )
    po.add_argument("--paper-source-id", required=True)
    po.add_argument("--agency-slug", required=True)

    to = sub.add_parser(
        "track-outcome",
        help="Phase E: record the outcome of an applied mitigation.",
        description=(
            "Phase E mitigation outcome tracker. Records the human-marked "
            "outcome and runs the secondary recurrence check. If the "
            "objection recurs from the same agency in the secondary source, "
            "the outcome is auto-downgraded to ineffective regardless of "
            "the human mark (FINDING-E-004)."
        ),
    )
    to.add_argument("--mitigation-id", required=True)
    to.add_argument("--agency-slug", required=True)
    to.add_argument("--paper-source-id", required=True)
    to.add_argument(
        "--outcome",
        required=True,
        choices=["effective", "ineffective", "partial", "unknown"],
    )
    to.add_argument("--secondary-source-id")

    sy = sub.add_parser(
        "synthesize",
        help="Phase F: assemble a context bundle and synthesize report+keynote.",
        description=(
            "Phase F synthesis run. Single compound entry point. Loop: "
            "structured retrieval -> context bundle -> Sonnet generation -> "
            "grounding eval -> human review. Writes synthesis/<run_id>/ with "
            "context_bundle.json, themes.jsonl, story_matrix.json, "
            "report_draft.json, keynote_scaffold.json, cost.jsonl, "
            "run_manifest.json, and view-only Markdown projections."
        ),
    )
    sy.add_argument(
        "--audience",
        required=True,
        choices=list(VALID_AUDIENCES),
        help="Fixed audience enum (FINDING-F-003).",
    )
    sy.add_argument(
        "--purpose",
        required=True,
        choices=list(VALID_PURPOSES),
        help="report | keynote | both",
    )
    sy.add_argument(
        "--recipe-id",
        help=(
            "Retrieval recipe id (default: default_report_v1 for report, "
            "default_keynote_v1 for keynote)."
        ),
    )
    sy.add_argument(
        "--vault",
        help=(
            "Path to Obsidian vault root. If provided, a review form is "
            "written to vault/Reviews/Synthesis/Pending/."
        ),
    )

    rr = sub.add_parser(
        "record-run",
        help="Phase G: record a synthesis run into harness memory.",
        description=(
            "Phase G harness recording. Reads synthesis/<run_id>/run_manifest.json "
            "and produces a run_history_entry under harness/runs/index.json. "
            "Also records eval results, ingests failures, and proposes "
            "eval_case_candidate artifacts for recurring patterns. NEVER blocks "
            "the synthesis pipeline (RT5-002)."
        ),
    )
    rr.add_argument("--run-id", required=True)
    rr.add_argument("--vault", help="Path to Obsidian vault root.")

    ro = sub.add_parser(
        "record-outcome",
        help="Phase G: record a revision or mitigation outcome.",
        description=(
            "Phase G outcome recording. Records into harness/outcomes/memory.jsonl "
            "(FINDING-G-004 — single store, outcome_type distinguishes flow)."
        ),
    )
    ro.add_argument(
        "--type", required=True, choices=["revision", "mitigation"], dest="otype"
    )
    ro.add_argument("--source-id", required=True, help="diff_id or outcome_id")
    ro.add_argument("--paper-source-id", required=True)
    ro.add_argument("--vault", help="Path to Obsidian vault root.")

    cmp_ = sub.add_parser(
        "compare-runs",
        help="Phase G: compare two synthesis runs across fixed dimensions.",
        description=(
            "Phase G workflow comparator. Writes harness/comparisons/<a>_vs_<b>.json "
            "and (with --vault) vault/Harness/comparisons/<a>_vs_<b>.md (FINDING-G-005)."
        ),
    )
    cmp_.add_argument("--run-a", required=True)
    cmp_.add_argument("--run-b", required=True)
    cmp_.add_argument("--vault", help="Path to Obsidian vault root.")

    rov = sub.add_parser(
        "record-override",
        help="Phase G: record a human override of an eval or block.",
        description=(
            "Phase G override store. Each override has expires_at (default 365 days). "
            "Warns if expiring within 30 days. Auto-archives expired overrides "
            "(FINDING-G-006)."
        ),
    )
    rov.add_argument("--artifact-id", required=True)
    rov.add_argument("--eval-or-block", required=True)
    rov.add_argument("--rationale", required=True)
    rov.add_argument("--human-id", required=True)
    rov.add_argument(
        "--decision-context",
        required=True,
        help="What decision is being overridden (>=10 chars).",
    )
    rov.add_argument("--expires-days", type=int)
    rov.add_argument("--vault", help="Path to Obsidian vault root.")

    pec = sub.add_parser(
        "promote-eval-case",
        help="Phase G: human promotion of an eval_case_candidate.",
        description=(
            "Phase G eval case promoter. The ONLY path that writes a new eval to "
            "contracts/evals/ (FINDING-G-003). Prompts for explicit 'confirm' "
            "before writing. Updates the candidate status to 'promoted'."
        ),
    )
    pec.add_argument("--candidate-id", required=True)
    pec.add_argument("--reviewer-id", required=True)
    pec.add_argument("--note", default="")
    pec.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive 'confirm' prompt (use carefully).",
    )

    ae = sub.add_parser(
        "audit-entropy",
        help="Phase G: scan for entropy and produce a report.",
        description=(
            "Phase G entropy auditor. Writes entropy_report under "
            "harness/entropy/reports.jsonl with flagged_items each carrying "
            "severity + recommended_action. NEVER auto-deletes (FINDING-G-007)."
        ),
    )
    ae.add_argument("--vault", help="Path to Obsidian vault root.")

    am = sub.add_parser(
        "ask-memory",
        help="Phase H: governed AI query over operational memory.",
        description=(
            "Phase H AI memory query. Single loop: question -> retrieve from "
            "governed memory -> assemble context bundle -> AI generation -> "
            "grounding eval -> advisory output. All AI output is advisory "
            "(FINDING-H-005). Only registered task types accepted "
            "(FINDING-H-001)."
        ),
    )
    am.add_argument(
        "--task",
        required=True,
        help=(
            "Registered task_type (memory_query | claim_check | "
            "objection_check | story_fit). Unknown task_types fail "
            "before any API call."
        ),
    )
    am.add_argument(
        "--question",
        required=True,
        help="The question or claim to evaluate against governed memory.",
    )
    am.add_argument(
        "--vault",
        help=(
            "Optional Obsidian vault root. If provided, a view-only "
            "advisory projection is written to vault/AI/<query_id>.md."
        ),
    )

    ag = sub.add_parser(
        "audit-governance",
        help="Phase I: run all governance scanners + dashboard.",
        description=(
            "Phase I governance audit. Runs every scanner (schema drift, "
            "eval coverage, decision divergence, exception accumulation, "
            "hidden logic creep, markdown authority, cost trend, "
            "compression scan) and writes governance/markdown/dashboard.md "
            "(capped at 30 lines). NEVER blocks synthesis."
        ),
    )
    ag.add_argument("--vault", help="Path to Obsidian vault root.")

    ac = sub.add_parser(
        "apply-compression",
        help="Phase I: human-gated compression candidate action.",
        description=(
            "Phase I human path to act on a compression_candidate. NEVER "
            "auto-deletes. For 'remove' or 'merge' the CLI prints the "
            "exact commands a human must run manually (FINDING-I-006)."
        ),
    )
    ac.add_argument("--candidate-id", required=True)
    ac.add_argument(
        "--action",
        required=True,
        choices=["remove", "merge", "deprecate", "investigate"],
    )
    ac.add_argument("--human-id", required=True)
    ac.add_argument("--note", default="")
    ac.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

    ed = sub.add_parser(
        "extract-docx",
        help="Phase L.0: extract .docx transcript(s) to .txt for process-source.",
        description=(
            "Phase L.0 pre-processing step. Reads a .docx file (or all .docx "
            "files in a directory) and writes .txt files with paragraphs joined "
            "by double newlines. The .txt output is ready for process-source / "
            "TranscriptIngestor. Does not write any pipeline artifact. Does not "
            "call any LLM. Does not modify SourceLoader or TranscriptIngestor."
        ),
    )
    ed.add_argument(
        "--path",
        required=True,
        help="Path to a single .docx file OR a directory of .docx files.",
    )
    ed.add_argument(
        "--output-dir",
        default=None,
        help="Directory where .txt files are written. Defaults to alongside the original.",
    )

    rp = sub.add_parser(
        "run-pipeline",
        help="Phase L.1: scan transcripts and run pipeline on the unprocessed ones.",
        description=(
            "Phase L.1 PipelineOrchestrator. Scans "
            "<data-lake>/store/raw/transcripts/ for .docx and .txt files, "
            "compares against on-disk processed evidence, and runs the "
            "Phase A pipeline only on the transcripts that have no record "
            "of a previous run. Writes one orchestration_run_record per "
            "invocation. Idempotent: re-running skips transcripts already "
            "processed."
        ),
    )
    rp.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report what would run, without executing or writing.",
    )
    rp.add_argument(
        "--data-lake",
        default=None,
        help=(
            "Path to the data lake root. Overrides the DATA_LAKE_PATH "
            "environment variable when provided."
        ),
    )
    rp.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass idempotency and re-run every stage on every transcript. "
            "Existing artifacts are NEVER deleted; underlying extractors "
            "overwrite their own working files (pre-existing behavior)."
        ),
    )
    rp.add_argument(
        "--force-only-missing",
        action="store_true",
        help=(
            "Phase O.3: combine with --force to skip source_ids that already "
            "have a meeting_extraction artifact. No effect without --force."
        ),
    )
    rp.add_argument(
        "--specific-source-id",
        default=None,
        help=(
            "Phase O.3: process only this source_id (slugified transcript "
            "filename). Overrides --force-only-missing."
        ),
    )

    egt = sub.add_parser(
        "eval-ground-truth",
        help=(
            "Phase M.4: evaluate the pipeline against confirmed "
            "ground_truth_pairs."
        ),
        description=(
            "Phase M.4 EvalRunner. Iterates confirmed ground_truth_pair "
            "artifacts under $SDL_ROOT/ground_truth/, loads each pair's "
            "source_record + minutes_text + extracted items, runs the "
            "EvalAligner (semantic + lexical), computes coverage / "
            "precision / items_requiring_review, aggregates an "
            "eval_summary, and asks the RegressionGate for a decision "
            "vs baseline. Writes alignment_result, eval_result, "
            "eval_summary, and gate_decision artifacts under "
            "$SDL_ROOT/evals/. pending_review pairs are excluded. "
            "dry-run pipeline runs are skipped without writing artifacts."
        ),
    )
    egt.add_argument(
        "--data-lake",
        default=None,
        help=(
            "Path to the data lake root. Overrides the DATA_LAKE_PATH "
            "environment variable when provided."
        ),
    )
    egt.add_argument(
        "--pipeline-run-id",
        default=None,
        help=(
            "Optional pipeline_run_id to record on every eval_result. "
            "Resolved from orchestration_run_record.run_id when omitted; "
            "auto-generated as a UUID when nothing else is available."
        ),
    )
    egt.add_argument(
        "--pair-id",
        default=None,
        help="Optional: evaluate only the named pair_id.",
    )
    egt.add_argument(
        "--prompt-version",
        default="unspecified",
        help=(
            "Tag or hash of the extraction prompts used for the run "
            "under evaluation. Recorded on every eval_result."
        ),
    )
    egt.add_argument(
        "--set-baseline",
        action="store_true",
        help=(
            "Explicitly write the current eval_summary as baseline, "
            "overwriting any existing baseline. Use after a deliberate "
            "rebaselining."
        ),
    )
    egt.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip the eval and exit 0 without writing artifacts. Maps "
            "to the dry-run pipeline mode."
        ),
    )

    et = sub.add_parser(
        "extract-typed",
        help="Phase M3: run typed extraction (decision/claim/action_item).",
        description=(
            "Phase M3 typed-extraction pipeline. Reads chunks.jsonl for the "
            "given source(s), classifies each chunk with ChunkClassifier "
            "(Haiku) + regulatory-verb fallback, routes classified chunks "
            "to the three typed extractors, and writes one "
            "meeting_extraction artifact per source under "
            "$SDL_ROOT/extractions/. Idempotent: skips sources whose "
            "meeting_extraction already exists unless --force is set."
        ),
    )
    et_target = et.add_mutually_exclusive_group(required=True)
    et_target.add_argument(
        "--source-id",
        default=None,
        help="Run typed extraction for a specific source_id.",
    )
    et_target.add_argument(
        "--all",
        action="store_true",
        help=(
            "Run typed extraction for every source with a chunks.jsonl "
            "under store/processed/."
        ),
    )
    et.add_argument(
        "--data-lake",
        default=None,
        help=(
            "Path to the data lake root. Overrides the DATA_LAKE_PATH "
            "environment variable when provided."
        ),
    )
    et.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-extract even if a meeting_extraction artifact already "
            "exists for the source."
        ),
    )

    lg = sub.add_parser(
        "link-ground-truth",
        help="Phase L.2: pair transcripts with meeting-minutes by date.",
        description=(
            "Phase L.2 GroundTruthLinker. Reads transcript source_records "
            "from store/processed/meetings/ and SDL_ROOT, and minutes_record "
            "artifacts from SDL_ROOT/minutes/. Pairs them by meeting_date: "
            "exact dates produce auto-confirmed pairs; ±1-day matches "
            "produce pending_review pairs requiring human confirmation. "
            "Unmatched transcripts and minutes are recorded explicitly in "
            "the linking_report. With --process-minutes, runs "
            "MinutesProcessor over store/raw/minutes/ first."
        ),
    )
    lg.add_argument(
        "--data-lake",
        default=None,
        help=(
            "Path to the data lake root. Overrides the DATA_LAKE_PATH "
            "environment variable when provided."
        ),
    )
    lg.add_argument(
        "--process-minutes",
        action="store_true",
        help=(
            "Run MinutesProcessor over store/raw/minutes/ before linking. "
            "Default: skip if minutes_records already exist."
        ),
    )
    lg.add_argument(
        "--deduplicate",
        action="store_true",
        help=(
            "Before linking, retire duplicate minutes_records grouped by "
            "raw_hash. Oldest is kept; the rest are moved to "
            "$SDL_ROOT/minutes/retired/ (never deleted). Use after a run "
            "that produced duplicates; subsequent idempotent runs make it "
            "a no-op."
        ),
    )

    vps = sub.add_parser(
        "verify-pipeline-state",
        help=(
            "Phase O.0: scan SDL_ROOT, validate schemas, write "
            "pipeline_state_record."
        ),
        description=(
            "Phase O.0 verification. Scans SDL_ROOT (and the data-lake's "
            "store/ tree) for JSON artifacts, classifies each by "
            "artifact_type (or artifact_kind as a legacy fallback), "
            "validates against the contract schema, and writes a "
            "pipeline_state_record under $SDL_ROOT/verifications/. "
            "Empty SDL_ROOT is reported as a finding, not silent success."
        ),
    )
    vps.add_argument("--data-lake", default=None)
    vps.add_argument(
        "--no-validate-schemas",
        action="store_true",
        help="Skip per-artifact schema validation (default: validate).",
    )
    vps.add_argument(
        "--emit-actions-summary",
        action="store_true",
        help="Append a Markdown summary to $GITHUB_STEP_SUMMARY if set.",
    )

    rbc = sub.add_parser(
        "review-baseline-candidate",
        help=(
            "Phase O.5: print PASS/REVIEW sanity-bound checklist before "
            "--set-baseline."
        ),
        description=(
            "Phase O.5 baseline checklist. Reads the most recent "
            "eval_summary (or the one specified by --eval-summary-id), "
            "aggregates rates from meeting_extraction artifacts under "
            "$SDL_ROOT, and prints a PASS/REVIEW label for each sanity "
            "bound. READ-ONLY — never installs a baseline."
        ),
    )
    rbc.add_argument("--data-lake", default=None)
    rbc.add_argument(
        "--eval-summary-id",
        default=None,
        help=(
            "Optional eval_summary pipeline_run_id to review. Defaults to "
            "the most recent eval_summary on disk."
        ),
    )

    cf = sub.add_parser(
        "compile-findings",
        help=(
            "Phase O.6: write verification_findings artifact + Markdown "
            "summary."
        ),
        description=(
            "Phase O.6 findings compiler. Reads the latest "
            "pipeline_state_record and the latest eval_summary, computes "
            "rates from meeting_extraction artifacts, and writes a "
            "verification_findings artifact under "
            "$SDL_ROOT/verifications/. Prints a Markdown summary to stdout "
            "and to $GITHUB_STEP_SUMMARY if set."
        ),
    )
    cf.add_argument("--data-lake", default=None)
    cf.add_argument(
        "--cycle-id",
        default=None,
        help=(
            "Optional cycle id (e.g. 'phase-O-2026-05-11'). Defaults to "
            "phase-O-<today>."
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
    if args.command == "format-paper":
        return format_paper(
            revised_draft_id=args.revised_draft_id,
            vault=args.vault,
        )
    if args.command == "certify-paper":
        return certify_paper(
            paper_id=args.paper_id,
            run_id=args.run_id,
            vault=args.vault,
        )
    if args.command == "build-agency-profile":
        return build_agency_profile(
            paper_source_id=args.paper_source_id,
            agency_name=args.agency_name,
        )
    if args.command == "predict-objections":
        return predict_objections(
            paper_source_id=args.paper_source_id,
            agency_slug=args.agency_slug,
        )
    if args.command == "track-outcome":
        return track_outcome(
            mitigation_id=args.mitigation_id,
            agency_slug=args.agency_slug,
            paper_source_id=args.paper_source_id,
            outcome=args.outcome,
            secondary_source_id=args.secondary_source_id,
        )
    if args.command == "synthesize":
        return synthesize(
            audience=args.audience,
            purpose=args.purpose,
            recipe_id=args.recipe_id,
            vault=args.vault,
        )
    if args.command == "record-run":
        return record_run(run_id=args.run_id, vault=args.vault)
    if args.command == "record-outcome":
        return record_outcome(
            outcome_type=args.otype,
            source_id=args.source_id,
            paper_source_id=args.paper_source_id,
            vault=args.vault,
        )
    if args.command == "compare-runs":
        return compare_runs(
            run_id_a=args.run_a,
            run_id_b=args.run_b,
            vault=args.vault,
        )
    if args.command == "record-override":
        return record_override(
            artifact_id=args.artifact_id,
            eval_or_block=args.eval_or_block,
            rationale=args.rationale,
            human_id=args.human_id,
            decision_context=args.decision_context,
            expires_days=args.expires_days,
            vault=args.vault,
        )
    if args.command == "promote-eval-case":
        return promote_eval_case(
            candidate_id=args.candidate_id,
            reviewer_id=args.reviewer_id,
            note=args.note,
            auto_confirm=args.yes,
        )
    if args.command == "audit-entropy":
        return audit_entropy(vault=args.vault)
    if args.command == "ask-memory":
        return ask_memory(
            task=args.task,
            question=args.question,
            vault=args.vault,
        )
    if args.command == "audit-governance":
        return audit_governance(vault=args.vault)
    if args.command == "extract-docx":
        return extract_docx(
            path=args.path,
            output_dir=args.output_dir,
        )
    if args.command == "run-pipeline":
        return run_pipeline(
            dry_run=args.dry_run,
            data_lake=args.data_lake,
            force=args.force,
            force_only_missing=args.force_only_missing,
            specific_source_id=args.specific_source_id,
        )
    if args.command == "extract-typed":
        return extract_typed(
            source_id=args.source_id,
            all_sources=args.all,
            data_lake=args.data_lake,
            force=args.force,
        )
    if args.command == "link-ground-truth":
        return link_ground_truth(
            data_lake=args.data_lake,
            process_minutes=args.process_minutes,
            deduplicate=args.deduplicate,
        )
    if args.command == "eval-ground-truth":
        return eval_ground_truth(
            data_lake=args.data_lake,
            pipeline_run_id=args.pipeline_run_id,
            pair_id=args.pair_id,
            prompt_version=args.prompt_version,
            set_baseline=args.set_baseline,
            is_dry_run=args.dry_run,
        )
    if args.command == "apply-compression":
        return apply_compression_cli(
            candidate_id=args.candidate_id,
            action=args.action,
            human_id=args.human_id,
            note=args.note,
            yes=args.yes,
        )
    if args.command == "verify-pipeline-state":
        return verify_pipeline_state(
            data_lake=args.data_lake,
            validate_schemas=not args.no_validate_schemas,
            emit_actions_summary=args.emit_actions_summary,
        )
    if args.command == "review-baseline-candidate":
        return review_baseline_candidate(
            data_lake=args.data_lake,
            eval_summary_id=args.eval_summary_id,
        )
    if args.command == "compile-findings":
        return compile_findings_cli(
            data_lake=args.data_lake,
            cycle_id=args.cycle_id,
        )
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
