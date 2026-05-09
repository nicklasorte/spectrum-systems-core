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

from .ingestion import (
    GroundingHelper,
    ObsidianProjection,
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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
