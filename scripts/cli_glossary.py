"""Phase 2P CLI shell exercising the ``--enable-glossary-injection`` flag.

This is the testable surface for the Phase 2P flag. The flag is CLI-
only — the parser deliberately does NOT consult environment variables
or config files. The disabled-by-default behaviour test calls this
script with ``ENABLE_GLOSSARY_INJECTION=true`` set in the env and
asserts the output contains no Terminology block; the enabled
behaviour test passes ``--enable-glossary-injection`` and asserts the
block appears.

When wired into the broader extraction pipeline (a follow-up landing
after Phase 2), the flag and loader are consumed via the
``spectrum_systems_core.glossary.loader`` API directly. This script
mirrors that integration so the contract is exercised end-to-end now.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# Allow ``python scripts/cli_glossary.py ...`` from a fresh checkout
# without ``pip install -e .``.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from spectrum_systems_core.glossary.loader import (  # noqa: E402
    DEFAULT_MAX_TERMS,
    GlossaryError,
    build_chunk_context,
    load_glossary,
)

_DEFAULT_GLOSSARY_DIR = _REPO_ROOT / "data" / "glossary"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 2P glossary-injection CLI shell. Reads a chunk text "
            "from --chunk-file (or stdin) and prints the chunk context, "
            "optionally prefixed with a Terminology block when "
            "--enable-glossary-injection is set."
        )
    )
    parser.add_argument(
        "--enable-glossary-injection",
        action="store_true",
        default=False,
        help=(
            "Inject per-chunk glossary terms into chunk context. "
            "DO NOT ENABLE in production until Phase 2 (eval-alignment) "
            "has landed -- measurement of impact requires that work. "
            "CLI-only: this flag is intentionally NOT read from env "
            "vars or config files."
        ),
    )
    parser.add_argument(
        "--chunk-file",
        type=pathlib.Path,
        default=None,
        help="Path to a UTF-8 text file containing one chunk. Defaults to stdin.",
    )
    parser.add_argument(
        "--glossary-dir",
        type=pathlib.Path,
        default=_DEFAULT_GLOSSARY_DIR,
        help="Directory containing the JSONL, MANIFEST.json, and allowed_sources.json.",
    )
    parser.add_argument(
        "--max-terms",
        type=int,
        default=DEFAULT_MAX_TERMS,
        help="Cap on injected terms per chunk (default: 3).",
    )
    args = parser.parse_args(argv)

    if args.chunk_file is not None:
        chunk_text = args.chunk_file.read_text(encoding="utf-8")
    else:
        chunk_text = sys.stdin.read()

    glossary = None
    if args.enable_glossary_injection:
        try:
            glossary = load_glossary(
                glossary_path=args.glossary_dir / "ntia_dod_spectrum_v1.jsonl",
                manifest_path=args.glossary_dir / "MANIFEST.json",
                allowed_sources_path=args.glossary_dir / "allowed_sources.json",
            )
        except GlossaryError as exc:
            print(f"FAIL {exc.reason}: {exc.detail}", file=sys.stderr)
            return 1

    terminology_block = build_chunk_context(
        chunk_text, glossary, max_terms=args.max_terms
    )
    # The CLI's stdout is "context block + chunk text" — mirroring how
    # the broader extraction pipeline composes the per-chunk prompt
    # context (Phase 2P only ships the prepended block; the chunk
    # itself passes through verbatim).
    parts = [p for p in (terminology_block, chunk_text) if p]
    output = "\n\n".join(parts)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
