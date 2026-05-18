#!/usr/bin/env python3
"""Debug the ``extraction_within_source_required`` eval.

Prints the EXACT normalized forms of the raw transcript and the items
that fail the within-source substring check, then — when an item is not
found — pinpoints the character-level difference (codepoint dump with
Unicode names) around the closest match. This is the diagnostic the
CLAUDE.md auto-debug rule requires: the operator never has to eyeball
the data-lake by hand.

The data-lake is NOT committed to spectrum-systems-core (code only), so
run this where the lake is on disk. Resolution order for the lake root:

  1. ``--lake-root <path>``
  2. ``$DATA_LAKE_ROOT``
  3. ``./data-lake`` then ``../data-lake`` (the clone-data-lake action's
     default checkout location)

Usage:
    python scripts/debug_within_source.py
    python scripts/debug_within_source.py --lake-root /path/to/data-lake
"""
from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from pathlib import Path

# Import THE binding match algorithm from the within-source eval so this
# script can never drift from what the gate actually does.
from spectrum_systems_core.evals.llm_extraction import _normalize  # noqa: E402

MEETING_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"

# The two chunk-67 (t0066) items the eval reports as not-in-source.
NEEDLES = [
    "Kerry and I will be collaborating to make sure that that we "
    "have the data that we needed",
    "we will share our our code for doing the analysis, so we have "
    "some consistent answers",
]


def _resolve_lake_root(cli_value: str | None) -> Path | None:
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("DATA_LAKE_ROOT")
    if env:
        return Path(env)
    for guess in (Path("data-lake"), Path("../data-lake")):
        if (guess / "store" / "raw" / "meetings").is_dir():
            return guess
    return None


def _cp_dump(s: str, limit: int = 120) -> str:
    """One line per codepoint: index, repr, U+XXXX, Unicode name."""
    out = []
    for i, ch in enumerate(s[:limit]):
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = "<no name>"
        out.append(f"  [{i:3d}] {ch!r:>8}  U+{ord(ch):04X}  {name}")
    if len(s) > limit:
        out.append(f"  ... (+{len(s) - limit} more codepoints)")
    return "\n".join(out)


def _first_divergence(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else -1


def _classify(haystack: str, needle: str) -> str:
    """Why is ``needle`` not a substring of ``haystack``?

    Compares the needle against the haystack window that shares its
    longest common prefix, then classifies the first differing char.
    """
    # Strip every char each side that the OTHER normalisations would
    # remove, to see if the miss is purely invisible/whitespace.
    def _strip_invisible(t: str) -> str:
        t = unicodedata.normalize("NFKC", t)
        return "".join(
            c for c in t if unicodedata.category(c) != "Cf"
        )

    if _strip_invisible(needle) in _strip_invisible(haystack):
        return (
            "INVISIBLE/COMPAT-CHAR DIFFERENCE — the needle matches once "
            "zero-width / soft-hyphen / NFKC-compatibility characters are "
            "neutralised. Fix belongs in _normalize (non-weakening)."
        )
    notext = "".join(
        c for c in needle if unicodedata.category(c)[0] != "P"
    )
    hay_notext = "".join(
        c for c in haystack if unicodedata.category(c)[0] != "P"
    )
    if notext and notext in hay_notext:
        return (
            "PUNCTUATION DIFFERENCE — the needle matches only when "
            "punctuation is dropped. The transcript has punctuation the "
            "model omitted (or vice versa). This is an EXTRACTION-side "
            "issue: the within-source gate is correctly failing a "
            "non-verbatim extraction. Do NOT loosen _normalize to mask "
            "it; fix the extraction prompt to copy punctuation verbatim."
        )
    return (
        "GENUINELY DIFFERENT TEXT — the needle is not present even after "
        "removing invisibles and punctuation. The model paraphrased / "
        "hallucinated. The gate is correctly blocking. Do NOT change "
        "_normalize."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lake-root", default=None)
    args = ap.parse_args()

    lake_root = _resolve_lake_root(args.lake_root)
    if lake_root is None:
        print(
            "DATA-LAKE NOT FOUND. Pass --lake-root, set $DATA_LAKE_ROOT, "
            "or run with ./data-lake checked out (clone-data-lake "
            "action). spectrum-systems-core is code-only by contract.",
            file=sys.stderr,
        )
        return 2

    source = (
        lake_root
        / "store"
        / "raw"
        / "meetings"
        / MEETING_ID
        / "source.txt"
    )
    if not source.is_file():
        print(f"transcript not found: {source}", file=sys.stderr)
        return 2

    raw = source.read_text(encoding="utf-8")
    haystack = _normalize(raw)

    print(f"transcript path : {source}")
    print(f"raw chars       : {len(raw)}")
    print(f"normalized chars: {len(haystack)}")
    print()

    any_miss = False
    for idx, needle in enumerate(NEEDLES, start=1):
        n = _normalize(needle)
        found = n in haystack
        print(f"needle_{idx}: {needle!r}")
        print(f"  normalized_needle_{idx}   : {n!r}")
        print(f"  needle_{idx}_in_haystack  : {found}")
        if not found:
            any_miss = True
            print(f"  classification         : {_classify(haystack, n)}")
            pos = haystack.find("kerry")
            if pos == -1:
                # Fall back to the longest needle prefix that DOES occur.
                pos = -1
                for cut in range(len(n), 4, -1):
                    p = haystack.find(n[:cut])
                    if p != -1:
                        pos = p
                        print(
                            f"  longest matching prefix: {cut} chars "
                            f"-> {n[:cut]!r}"
                        )
                        break
            else:
                print("  (anchored on 'kerry' in normalized haystack)")
            if pos != -1:
                lo = max(0, pos - 20)
                window = haystack[lo : pos + 200]
                print(f"  haystack @ {pos} (200 chars):")
                print(f"    {window!r}")
                div = _first_divergence(haystack[pos:], n)
                if div >= 0:
                    print(f"  first divergence at offset {div}:")
                    print(
                        f"    transcript: {haystack[pos + div:pos + div + 30]!r}"
                    )
                    print(f"    extracted : {n[div:div + 30]!r}")
                    print("  codepoints around divergence (transcript):")
                    print(_cp_dump(haystack[pos + max(0, div - 5):], 40))
                    print("  codepoints around divergence (extracted):")
                    print(_cp_dump(n[max(0, div - 5):], 40))
        print()

    if any_miss:
        print(
            "RESULT: at least one item is NOT a normalized substring. "
            "See the classification line for where the fix belongs."
        )
        return 1
    print("RESULT: all items are normalized substrings (eval would pass).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
