"""PDFAdmissionGuard: fail-closed checks before PDF text extraction.

Runs eight ordered checks against a book source under raw/books/<source_id>/.
Stops at the first failure. Never raises. Returns
{"status": "pass"|"fail", "reason": str}.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import jsonschema

from ._paths import schema_path


_PDF_MAGIC = b"%PDF"


class PDFAdmissionGuard:
    """Validate that a book source is ready for PDF extraction."""

    def validate(self, source_id: str, repo_root: str) -> Dict[str, str]:
        repo_root_path = Path(repo_root).resolve()
        source_dir = repo_root_path / "raw" / "books" / source_id

        # CHECK-G-001: metadata.json exists and is valid JSON.
        metadata_path = source_dir / "metadata.json"
        if not metadata_path.is_file():
            return _fail(
                "metadata_missing",
                f"no metadata.json at {metadata_path}",
            )
        try:
            metadata_bytes = metadata_path.read_bytes()
        except OSError as exc:
            return _fail("metadata_missing", str(exc))
        try:
            metadata = json.loads(metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _fail("metadata_invalid_json", str(exc))
        if not isinstance(metadata, dict):
            return _fail(
                "metadata_invalid_json",
                "metadata.json must be a JSON object",
            )

        # CHECK-G-002: metadata validates against source_metadata schema.
        try:
            schema = json.loads(
                schema_path("source_metadata").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            return _fail("metadata_schema_violation", str(exc))
        try:
            jsonschema.Draft202012Validator(schema).validate(metadata)
        except jsonschema.ValidationError as exc:
            return _fail("metadata_schema_violation", exc.message)

        # CHECK-G-003: source_family must be "books".
        if metadata.get("source_family") != "books":
            return _fail(
                "wrong_source_family",
                f"expected 'books', got {metadata.get('source_family')!r}",
            )

        # CHECK-G-004: raw_format must be "pdf".
        if metadata.get("raw_format") != "pdf":
            return _fail(
                "wrong_raw_format",
                f"expected 'pdf', got {metadata.get('raw_format')!r}",
            )

        # CHECK-G-005: private_use_only must be true. (FINDING-B-004 fix)
        if metadata.get("private_use_only") is not True:
            return _fail(
                "private_use_only_required",
                "Books must declare private_use_only: true. Do not ingest "
                "copyrighted material without explicit private use authorization.",
            )

        # CHECK-G-006: source.pdf exists.
        pdf_path = source_dir / "source.pdf"
        if not pdf_path.is_file():
            return _fail("pdf_not_found", f"no source.pdf at {pdf_path}")

        # CHECK-G-007: PDF magic bytes valid.
        try:
            with pdf_path.open("rb") as fh:
                head = fh.read(4)
        except OSError as exc:
            return _fail("invalid_pdf_magic", str(exc))
        if head != _PDF_MAGIC:
            return _fail(
                "invalid_pdf_magic",
                f"first 4 bytes are {head!r}, expected b'%PDF'",
            )

        # CHECK-G-008: source.txt must NOT already exist.
        txt_path = source_dir / "source.txt"
        if txt_path.exists():
            return _fail(
                "already_extracted",
                f"source.txt exists at {txt_path}. Delete it to re-extract.",
            )

        return {"status": "pass", "reason": ""}


def _fail(reason: str, detail: str = "") -> Dict[str, str]:
    msg = reason if not detail else f"{reason}: {detail}"
    return {"status": "fail", "reason": msg}
