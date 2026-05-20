"""Phase 3P heuristic scanner for un-stripped speaker names.

Walks every entry in ``data/few_shot/examples_v1.jsonl`` and flags any
sequence of two-or-more capitalised words that does NOT match an
expected SPEAKER_X placeholder and is NOT in the NTIA/DoD glossary
whitelist. WARNING-only (the scanner is heuristic and would false-
positive on glossary acronym pairs like ``CBRS NTIA``); CI does not
hard-fail on a finding, but a finding is documented in the PR
description.

CI runs this on every PR that modifies ``data/few_shot/``. Operators
also run it locally before flipping the few-shot flag default to ON.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Acronyms and proper nouns that ARE allowed to appear capitalised.
# These come from the NTIA/DoD spectrum domain.
_DOMAIN_WHITELIST: frozenset[str] = frozenset(
    {
        # Speaker placeholders (never flag these)
        "SPEAKER_A",
        "SPEAKER_B",
        "SPEAKER_C",
        "SPEAKER_D",
        "SPEAKER_E",
        "SPEAKER_F",
        "SPEAKER_G",
        "SPEAKER_H",
        # Common acronyms (acronym-only, all uppercase)
        "NTIA",
        "DoD",
        "DOD",
        "FCC",
        "TIG",
        "ITU",
        "CBRS",
        "FSS",
        "MSS",
        "SAS",
        "PAL",
        "GAA",
        "AFC",
        "DPA",
        "EIRP",
        "MHz",
        "GHz",
        "kHz",
        # Geography / domain proper nouns
        "United",
        "States",
        # Title-case helpers
        "U.S.",
        "US",
    }
)

# Sequence of two-or-more capitalised English words. Excludes "I" by
# requiring the first word be 2+ chars. Captures "John Smith" but not
# "Has anyone".
_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z'\-]{1,})(?:\s+([A-Z][a-zA-Z'\-]+)){1,}\b")


def _scan_text(text: str) -> list[str]:
    suspects: list[str] = []
    for match in _NAME_RE.finditer(text):
        token = match.group(0)
        parts = token.split()
        # Whitelist hits: if every part is in the whitelist we accept.
        if all(p.strip(",.;:") in _DOMAIN_WHITELIST for p in parts):
            continue
        # Speaker placeholder pattern: SPEAKER_X (uppercase _ letter)
        if all(re.fullmatch(r"SPEAKER_[A-Z]", p.strip(",.;:")) for p in parts):
            continue
        # Sentence-start patterns ("So before", "Has anyone") are noise
        # — both words must be plausibly a person name (each starts
        # uppercase but is not a common stopword). The simplest filter:
        # any word that is a common English start-of-sentence connective
        # is rejected.
        common_starts = {
            "So", "Yes", "No", "Maybe", "Good", "Then", "Let", "Lets",
            "Then", "Industry", "Several", "Members", "Has", "Anyone",
            "Should", "If", "We", "We've", "Our",
        }
        if any(p in common_starts for p in parts):
            continue
        suspects.append(token)
    return suspects


def _scan_entry(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Return [(field, suspect)] pairs for any name-like sequence."""
    out: list[tuple[str, str]] = []
    chunk_text = entry.get("chunk_text", "")
    if isinstance(chunk_text, str):
        for s in _scan_text(chunk_text):
            out.append(("chunk_text", s))
    gold = entry.get("gold_extraction", {})
    if isinstance(gold, dict):
        # Walk all string leaves in gold_extraction
        serialised = json.dumps(gold, sort_keys=True)
        for s in _scan_text(serialised):
            out.append(("gold_extraction", s))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Heuristic scan for un-stripped speaker names in the "
            "Phase 3P few-shot registry. WARNING-only — exits 0 even "
            "when suspects are found, but prints findings to stderr."
        )
    )
    parser.add_argument(
        "--examples",
        type=pathlib.Path,
        default=_REPO_ROOT / "data" / "few_shot" / "examples_v1.jsonl",
        help="Path to examples JSONL.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on any finding (used by tests).",
    )
    args = parser.parse_args(argv)

    if not args.examples.is_file():
        print(f"FAIL examples missing: {args.examples}", file=sys.stderr)
        return 1

    findings: list[tuple[str, str, str]] = []
    for line in args.examples.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        for field, suspect in _scan_entry(entry):
            findings.append((entry.get("id", "?"), field, suspect))

    if findings:
        print(
            "WARNING: heuristic scanner flagged possible un-stripped names. "
            "Manually review each:",
            file=sys.stderr,
        )
        for eid, field, suspect in findings:
            print(f"  {eid}:{field}: {suspect!r}", file=sys.stderr)
        if args.strict:
            return 1
        return 0
    print("OK: no suspected un-stripped speaker names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
