"""DocxExtractor: .docx -> .txt pre-processing step.

Extracts plain text from a .docx file and writes it as a .txt file at the
same path (or a caller-specified output path). The resulting .txt is a
ready input for the existing process-source / TranscriptIngestor pipeline.

This is a pre-processing step only. It:
- does NOT call any LLM
- does NOT write any pipeline artifact (source_record, context_bundle, etc.)
- does NOT modify SourceLoader or TranscriptIngestor
- does NOT read from or write to store/artifacts/
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from docx import Document


class DocxExtractor:
    """Extract plain text from .docx files and write .txt files."""

    def extract(
        self,
        docx_path: str,
        output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract plain text from a .docx file and write it as a .txt file.

        Args:
            docx_path: absolute path to the .docx file.
            output_path: where to write the .txt file. If None, writes to the
                same directory as docx_path with .txt extension.

        Returns:
            {
                "status": "success" | "failure",
                "output_path": str or None,
                "paragraph_count": int,
                "character_count": int,
                "reason": str  (empty string on success)
            }

        Never raises. Always returns a dict.
        """
        try:
            return self._extract(docx_path, output_path)
        except Exception as exc:  # defensive: should not be reached
            return {
                "status": "failure",
                "output_path": None,
                "paragraph_count": 0,
                "character_count": 0,
                "reason": f"unexpected_error:{exc}",
            }

    def _extract(
        self,
        docx_path: str,
        output_path: Optional[str],
    ) -> Dict[str, Any]:
        src = Path(docx_path)

        if not src.is_file():
            return _failure(f"file_not_found:{docx_path}")

        try:
            doc = Document(str(src))
        except Exception as exc:
            return _failure(f"docx_parse_error:{exc}")

        paragraphs = [p.text.strip() for p in doc.paragraphs]
        non_empty = [p for p in paragraphs if p]

        full_text = "\n\n".join(non_empty)

        if not full_text.strip():
            return _failure(f"empty_document:{docx_path}")

        dest = Path(output_path) if output_path is not None else src.with_suffix(".txt")
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            dest.write_text(full_text, encoding="utf-8")
        except OSError as exc:
            return _failure(f"write_error:{exc}")

        return {
            "status": "success",
            "output_path": str(dest),
            "paragraph_count": len(non_empty),
            "character_count": len(full_text),
            "reason": "",
        }

    def extract_batch(
        self,
        docx_dir: str,
        output_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Extract all .docx files in a directory.

        Args:
            docx_dir: directory containing .docx files.
            output_dir: where to write .txt files. If None, writes alongside originals.

        Returns:
            list of extract() result dicts, one per .docx file found.
            Empty list if no .docx files found (not an error).
            Callers must inspect each dict's "status" field — there is no
            top-level aggregate status.

        Raises nothing. Returns [] for a non-existent or empty directory.
        """
        src_dir = Path(docx_dir)
        if not src_dir.is_dir():
            return [_failure(f"directory_not_found:{docx_dir}")]
        docx_files = sorted(src_dir.glob("*.docx"))

        results: List[Dict[str, Any]] = []
        for docx_file in docx_files:
            if output_dir is not None:
                dest = str(Path(output_dir) / (docx_file.stem + ".txt"))
            else:
                dest = None
            results.append(self.extract(str(docx_file), output_path=dest))

        return results


def _failure(reason: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "output_path": None,
        "paragraph_count": 0,
        "character_count": 0,
        "reason": reason,
    }
