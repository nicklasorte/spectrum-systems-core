#!/usr/bin/env python3
"""Convert NTIA minutes ``.docx`` files into the pipe-delimited ``.txt``
layout the minutes parser expects.

The parser (``workflows.minutes_parser.parse_minutes_txt``) only reads
``.txt``. Some NTIA minutes are only published as ``.docx``, which the
parser cannot ingest until the text is extracted. This script is the
bridge: it walks a ``.docx`` via :mod:`workflows.docx_converter` and
writes a sibling ``.txt`` whose body parses cleanly with no edits.

Single-file form::

    python scripts/convert_docx_minutes.py \\
        --input "data-lake/store/raw/minutes/X.docx"

Batch form, skip any ``.docx`` that already has a sibling ``.txt``::

    python scripts/convert_docx_minutes.py \\
        --minutes-dir data-lake/store/raw/minutes \\
        --all-missing

Dry-run — never writes::

    python scripts/convert_docx_minutes.py \\
        --minutes-dir data-lake/store/raw/minutes \\
        --all-missing --dry-run

ZERO LLM calls. The conversion is byte-deterministic per file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from spectrum_systems_core.workflows.docx_converter import (  # noqa: E402
    convert_docx_to_txt,
)
from spectrum_systems_core.workflows.minutes_parser import (  # noqa: E402
    parse_minutes_text,
)


def convert_one(
    docx_path: Path, *, dry_run: bool
) -> tuple[str, str]:
    """Convert one ``.docx``. Returns ``(status, detail)``.

    Status is one of: ``"converted"``, ``"skipped"``, ``"failed"``.
    Detail is a short human-readable note (path, reason).
    """
    if docx_path.suffix.lower() != ".docx":
        return ("failed", f"not_a_docx:{docx_path.name}")
    txt_path = docx_path.with_suffix(".txt")
    if txt_path.exists():
        return ("skipped", f"already_has_txt:{txt_path.name}")
    try:
        text = convert_docx_to_txt(
            docx_path, output_path=None if dry_run else txt_path
        )
    except Exception as exc:  # noqa: BLE001
        return ("failed", f"convert_error:{type(exc).__name__}:{exc}")

    # Validate the converted output round-trips through the minutes
    # parser. A converter regression that produced un-parseable output
    # would silently succeed without this check.
    try:
        parse_minutes_text(text, source_path=str(docx_path))
    except Exception as exc:  # noqa: BLE001
        if not dry_run and txt_path.exists():
            txt_path.unlink()
        return ("failed", f"parse_error:{type(exc).__name__}:{exc}")

    return ("converted", str(txt_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--input",
        help="Convert a single .docx file.",
    )
    group.add_argument(
        "--minutes-dir",
        help="Directory containing .docx files to scan.",
    )
    parser.add_argument(
        "--all-missing",
        action="store_true",
        help=(
            "When --minutes-dir is set, convert every .docx that has no "
            "sibling .txt. Files that already have a .txt are skipped."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate only; do not write .txt files.",
    )
    args = parser.parse_args(argv)

    targets: list[Path]
    if args.input:
        docx_path = Path(args.input)
        if not docx_path.is_file():
            print(f"error: input not found: {docx_path}", file=sys.stderr)
            return 2
        targets = [docx_path]
    else:
        minutes_dir = Path(args.minutes_dir)
        if not minutes_dir.is_dir():
            print(
                f"error: --minutes-dir is not a directory: {minutes_dir}",
                file=sys.stderr,
            )
            return 2
        if not args.all_missing:
            print(
                "error: --minutes-dir requires --all-missing",
                file=sys.stderr,
            )
            return 2
        targets = sorted(minutes_dir.glob("*.docx"))

    converted = 0
    skipped = 0
    failed = 0
    for docx_path in targets:
        status, detail = convert_one(docx_path, dry_run=args.dry_run)
        prefix = "[dry-run] " if args.dry_run else ""
        if status == "converted":
            converted += 1
            print(f"{prefix}converted {docx_path.name} -> {detail}")
        elif status == "skipped":
            skipped += 1
            print(f"{prefix}skipped {docx_path.name} ({detail})")
        else:
            failed += 1
            print(
                f"{prefix}FAILED {docx_path.name} ({detail})",
                file=sys.stderr,
            )

    print(
        f"summary: converted={converted} skipped={skipped} failed={failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
