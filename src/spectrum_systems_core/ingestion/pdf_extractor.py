"""PDFExtractor: deterministic, fail-closed PDF -> text extraction for books.

Reads raw/books/<source_id>/source.pdf and writes:
    raw/books/<source_id>/source.txt
    raw/books/<source_id>/pages.jsonl
    raw/books/<source_id>/extraction_report.json

No OCR. No LLM calls. pdfminer.six only. Records library version and
content hash so extraction can be replayed deterministically on the same
library version.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import schema_path
from .pdf_guard import PDFAdmissionGuard

_LIBRARY_NAME = "pdfminer.six"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failure_report(
    *,
    source_id: str,
    library_version: str,
    failure_reason: str,
    page_count: int = 0,
    total_char_count: int = 0,
    extracted_text_hash: str = "sha256:" + ("0" * 64),
    pdf_magic_valid: bool = False,
    private_use_only_verified: bool = False,
    scanned_pdf_suspected: bool = False,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_family": "books",
        "extraction_library": _LIBRARY_NAME,
        "extraction_library_version": library_version,
        "extracted_at": _now_iso(),
        "page_count": page_count,
        "total_char_count": total_char_count,
        "extracted_text_hash": extracted_text_hash,
        "pdf_magic_valid": pdf_magic_valid,
        "private_use_only_verified": private_use_only_verified,
        "min_char_threshold": PDFExtractor.MIN_CHAR_THRESHOLD,
        "scanned_pdf_suspected": scanned_pdf_suspected,
        "status": "failure",
        "failure_reason": failure_reason,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class PDFExtractor:
    """Extract text from a single PDF source under raw/books/<id>/."""

    MIN_CHAR_THRESHOLD = 500  # FINDING-B-002: blocks scanned PDFs

    def extract(self, source_id: str, repo_root: str) -> dict[str, Any]:
        env = os.environ.get("DATA_LAKE_PATH", "")
        if not env or not Path(env).exists():
            return {
                "status": "blocked",
                "extraction_report": None,
                "reason": "DATA_LAKE_PATH not set or does not exist",
            }
        store_root = Path(env) / "store"
        source_dir = store_root / "raw" / "books" / source_id
        report_path = source_dir / "extraction_report.json"
        try:
            library_version = importlib.metadata.version(_LIBRARY_NAME)
        except importlib.metadata.PackageNotFoundError:
            library_version = "unknown"

        # 1. Run the admission guard. Fail-closed.
        guard_result = PDFAdmissionGuard().validate(source_id, str(store_root))
        if guard_result["status"] != "pass":
            report = _failure_report(
                source_id=source_id,
                library_version=library_version,
                failure_reason=guard_result["reason"],
            )
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": guard_result["reason"],
            }

        # 2. Open and extract pages with pdfminer.
        pdf_path = source_dir / "source.pdf"
        try:
            from pdfminer.high_level import extract_pages
            from pdfminer.layout import LTTextContainer

            pages_text: list[str] = []
            for layout_page in extract_pages(str(pdf_path)):
                parts: list[str] = []
                for element in layout_page:
                    if isinstance(element, LTTextContainer):
                        parts.append(element.get_text())
                page_text = "\n".join(parts).rstrip()
                pages_text.append(page_text)
        except Exception as exc:  # pdfminer can raise broad exception types
            failure_reason = f"extraction_failed: {exc}"
            report = _failure_report(
                source_id=source_id,
                library_version=library_version,
                failure_reason=failure_reason,
                pdf_magic_valid=True,
                private_use_only_verified=True,
            )
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": failure_reason,
            }

        # 3. Build the source.txt content.
        full_text = "\n\n".join(pages_text)
        total_char_count = len(full_text)
        extracted_text_hash = "sha256:" + _sha256_hex(full_text.encode("utf-8"))
        scanned_pdf_suspected = total_char_count < self.MIN_CHAR_THRESHOLD

        # 4. Assemble the extraction_report.
        report: dict[str, Any] = {
            "source_id": source_id,
            "source_family": "books",
            "extraction_library": _LIBRARY_NAME,
            "extraction_library_version": library_version,
            "extracted_at": _now_iso(),
            "page_count": len(pages_text),
            "total_char_count": total_char_count,
            "extracted_text_hash": extracted_text_hash,
            "pdf_magic_valid": True,
            "private_use_only_verified": True,
            "min_char_threshold": self.MIN_CHAR_THRESHOLD,
            "scanned_pdf_suspected": scanned_pdf_suspected,
            "status": "success" if not scanned_pdf_suspected else "failure",
            "failure_reason": (
                ""
                if not scanned_pdf_suspected
                else "scanned_pdf_suspected: total_char_count below threshold"
            ),
        }

        # 5. FINDING-B-002: Block scanned PDFs.
        if scanned_pdf_suspected:
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": "scanned_pdf_suspected",
            }

        # 6. Validate report against schema.
        try:
            report_schema = json.loads(
                schema_path("extraction_report").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            failure_reason = f"extraction_report_schema_violation: {exc}"
            report["status"] = "failure"
            report["failure_reason"] = failure_reason
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": failure_reason,
            }
        try:
            jsonschema.Draft202012Validator(report_schema).validate(report)
        except jsonschema.ValidationError as exc:
            failure_reason = f"extraction_report_schema_violation: {exc.message}"
            report["status"] = "failure"
            report["failure_reason"] = failure_reason
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": failure_reason,
            }

        # 7. Build pages.jsonl entries. (FINDING-B-003: page_number is the
        # authoritative locator; char_*_advisory fields are advisory only.)
        try:
            page_schema = json.loads(
                schema_path("pdf_page").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as exc:
            failure_reason = f"page_schema_violation: {exc}"
            report["status"] = "failure"
            report["failure_reason"] = failure_reason
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": failure_reason,
            }

        page_validator = jsonschema.Draft202012Validator(page_schema)
        page_entries: list[dict[str, Any]] = []
        cumulative = 0
        separator_len = len("\n\n")
        for idx, page_text in enumerate(pages_text):
            entry = {
                "page_number": idx + 1,
                "source_id": source_id,
                "text": page_text,
                "text_hash": "sha256:" + _sha256_hex(page_text.encode("utf-8")),
                "char_count": len(page_text),
                "char_start_advisory": cumulative,
                "char_end_advisory": cumulative + len(page_text),
                "extraction_library": _LIBRARY_NAME,
                "extraction_library_version": library_version,
            }
            try:
                page_validator.validate(entry)
            except jsonschema.ValidationError as exc:
                failure_reason = (
                    f"page_schema_violation: page {idx + 1}: {exc.message}"
                )
                report["status"] = "failure"
                report["failure_reason"] = failure_reason
                try:
                    _write_report(report_path, report)
                except OSError:
                    pass
                return {
                    "status": "failure",
                    "extraction_report": report,
                    "reason": failure_reason,
                }
            page_entries.append(entry)
            cumulative += len(page_text)
            if idx < len(pages_text) - 1:
                cumulative += separator_len

        # 8. Write all output files (after every validation has passed).
        try:
            (source_dir / "source.txt").write_text(full_text, encoding="utf-8")
            with (source_dir / "pages.jsonl").open("w", encoding="utf-8") as fh:
                for entry in page_entries:
                    fh.write(
                        json.dumps(entry, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
            _write_report(report_path, report)
        except OSError as exc:
            failure_reason = f"write_error: {exc}"
            report["status"] = "failure"
            report["failure_reason"] = failure_reason
            try:
                _write_report(report_path, report)
            except OSError:
                pass
            return {
                "status": "failure",
                "extraction_report": report,
                "reason": failure_reason,
            }

        return {
            "status": "success",
            "extraction_report": report,
            "reason": "",
        }
