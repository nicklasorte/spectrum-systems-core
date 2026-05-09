"""PDF fixtures for ingestion tests.

MINIMAL_PDF: a single-page PDF whose extracted text ("Hello World", 11 chars)
falls below the 500-char scanned-PDF threshold. Doubles as the "scanned PDF
suspected" fixture in PDFExtractor tests.

build_rich_pdf(lines): build a single-page PDF whose extracted text is
guaranteed to exceed the threshold when given enough lines.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List


MINIMAL_PDF: bytes = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]\n"
    b"   /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    b"4 0 obj\n<< /Length 44 >>\nstream\n"
    b"BT /F1 12 Tf 100 700 Td (Hello World) Tj ET\n"
    b"endstream\nendobj\n"
    b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000058 00000 n \n"
    b"0000000115 00000 n \n"
    b"0000000266 00000 n \n"
    b"0000000360 00000 n \n"
    b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n441\n%%EOF\n"
)


def build_rich_pdf(lines: Iterable[str]) -> bytes:
    """Build a single-page PDF with the given text lines.

    Computes object offsets dynamically so the xref table matches whatever
    content stream is produced.
    """
    body: List[str] = ["BT", "/F1 10 Tf", "50 750 Td"]
    for i, line in enumerate(lines):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i == 0:
            body.append(f"({safe}) Tj")
        else:
            body.append("0 -14 Td")
            body.append(f"({safe}) Tj")
    body.append("ET")
    content_bytes = ("\n".join(body) + "\n").encode("latin-1")

    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        (
            b"<< /Length "
            + str(len(content_bytes)).encode("ascii")
            + b" >>\nstream\n"
            + content_bytes
            + b"endstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = b"%PDF-1.4\n"
    offsets: List[int] = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return out


_RICH_LINES = [
    "The quick brown fox jumps over the lazy dog. " * 2,
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do.",
    "Eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris.",
    "Nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in.",
    "Reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla.",
    "Pariatur. Excepteur sint occaecat cupidatat non proident.",
    "Sunt in culpa qui officia deserunt mollit anim id est laborum.",
    "The quick brown fox jumps over the lazy dog. " * 2,
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
]
RICH_PDF: bytes = build_rich_pdf(_RICH_LINES)


def write_book_metadata(
    repo_root: Path,
    *,
    source_id: str,
    private_use_only: bool = True,
    source_family: str = "books",
    raw_format: str = "pdf",
) -> Path:
    target = repo_root / "raw" / source_family / source_id
    target.mkdir(parents=True, exist_ok=True)
    metadata = {
        "source_id": source_id,
        "source_family": source_family,
        "source_type": "book_chapter",
        "title": f"Title for {source_id}",
        "description": "",
        "date": "2026-05-09",
        "author": "",
        "tags": [],
        "raw_format": raw_format,
        "private_use_only": private_use_only,
    }
    (target / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
