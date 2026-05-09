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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
