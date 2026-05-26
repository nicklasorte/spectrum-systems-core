"""Deterministic ``.docx`` → ``.txt`` converter for NTIA meeting minutes.

The minutes parser (``minutes_parser.parse_minutes_txt``) requires a
plain-text file whose layout matches the hand-authored NTIA minutes:

  - Section headers (``Meeting Overview``, ``Discussion/Questions Log``,
    ``Next Steps``, ``Action Items``) on their own lines.
  - Table rows as pipe-delimited (``|``) lines.
  - Blank lines separate paragraphs and section boundaries.

NTIA also publishes some minutes only as ``.docx``. Those files are
useless to the parser until their text is extracted into the same
layout. This module is that bridge — a pure, deterministic walk over
the ``.docx`` body (paragraphs + tables) that emits text byte-compatible
with the hand-authored ``.txt`` form.

ZERO LLM calls. No external services. ``convert_docx_to_txt`` is a pure
function of its input bytes: re-running it on the same ``.docx`` yields
byte-identical output.

The ``ingestion.docx_extractor.DocxExtractor`` already lifts text out of
``.docx`` files for the transcript path, but its output joins chunks
with ``\\n\\n``. That is fine for transcripts but rewrites the
``parse_minutes_txt`` table-folding contract — a non-pipe continuation
line inside a section gets folded into the previous row's final cell.
We need a converter whose output preserves single-newline runs inside
tables and reserves blank lines for paragraph / section separators.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

__all__ = ["convert_docx_to_txt"]


def convert_docx_to_txt(
    docx_path: Path, output_path: Path | None = None
) -> str:
    """Convert a minutes ``.docx`` to the pipe-delimited ``.txt`` layout.

    Args:
        docx_path: path to the source ``.docx`` file.
        output_path: when provided, write the converted text to disk.
            ``None`` returns the text only — used by the duplicate-check
            and validation paths so they never write a half-formed file.

    Returns the converted text (UTF-8 string). Determinism: two calls
    on the same input produce byte-identical output.
    """
    doc = Document(str(docx_path))

    lines: list[str] = []
    body = doc.element.body
    para_tag = qn("w:p")
    tbl_tag = qn("w:tbl")

    for element in body:
        tag = getattr(element, "tag", None)
        if tag == para_tag:
            text = _paragraph_text(element)
            if text.strip():
                lines.append(text.strip())
            else:
                # Preserve blank lines so section boundaries survive.
                lines.append("")
        elif tag == tbl_tag:
            for row in element:
                if row.tag != qn("w:tr"):
                    continue
                cells: list[str] = []
                for cell in row:
                    if cell.tag != qn("w:tc"):
                        continue
                    cells.append(_cell_text(cell))
                if any(cells):
                    lines.append(" | ".join(cells))
            # Blank line after the table so the next section header
            # starts on its own line.
            lines.append("")

    # Collapse leading / trailing blank lines and any run of >=3 blanks
    # to a single pair so identical inputs produce identical output
    # regardless of incidental ``.docx`` paragraph padding.
    result = _normalize_blank_runs(lines)

    if output_path is not None:
        output_path.write_text(result, encoding="utf-8")

    return result


def _paragraph_text(element) -> str:
    """Join every ``<w:t>`` text node under a paragraph element."""
    text_tag = qn("w:t")
    parts: list[str] = []
    for node in element.iter():
        if node.tag == text_tag and node.text:
            parts.append(node.text)
    return "".join(parts)


def _cell_text(cell) -> str:
    """Flatten a ``<w:tc>`` element to a single trimmed string."""
    text_tag = qn("w:t")
    parts: list[str] = []
    for node in cell.iter():
        if node.tag == text_tag and node.text:
            parts.append(node.text)
    # Cell text may span multiple paragraphs / runs; collapse to a
    # single line so the pipe-delimited row format is preserved.
    return " ".join("".join(parts).split()).strip()


def _normalize_blank_runs(lines: list[str]) -> str:
    """Trim leading/trailing blanks and cap interior runs at one blank."""
    out: list[str] = []
    blank_seen = False
    for line in lines:
        if line == "":
            if not out:
                continue  # skip leading blanks
            blank_seen = True
        else:
            if blank_seen:
                out.append("")
                blank_seen = False
            out.append(line)
    return "\n".join(out) + "\n"
