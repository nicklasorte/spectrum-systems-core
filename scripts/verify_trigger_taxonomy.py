"""Phase 1.4 verification: implicit-decision trigger taxonomy is present.

Reads ``src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md``
and asserts the taxonomy section landed intact. Pure text assertions —
NO LLM calls, NO subprocess, NO network. Exits 0 on every assertion
passing, exits 1 on the first failure with a one-line cause that names
the missing token.

Run as: ``python scripts/verify_trigger_taxonomy.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPT = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)

_TAXONOMY_HEADER = (
    "# Implicit Decision Trigger Taxonomy (NTIA/DoD TIG — additive, 1.4.0)"
)

_SUBTYPE_HEADERS: List[Tuple[str, str]] = [
    ("issue", "## Sub-type 1: Issue identification"),
    ("proposal", "## Sub-type 2: Proposal / Direction"),
    ("resolution", "## Sub-type 3: Resolution / Agreement"),
    ("scope", "## Sub-type 4: Scope / Boundary ruling"),
]

_MODAL_HEADER = "## Modal verb policy"
_HALLUCINATION_HEADER = "## Hallucination defense"
_HALLUCINATION_SENTENCE = (
    "Hallucination defense: extract ONLY if the trigger phrase appears"
)

_DOMAIN_HEADER = "## Domain notes (NTIA/DoD TIG)"
_DOMAIN_NOTE_MARKERS: List[Tuple[str, str]] = [
    (
        "domain note 1 (regulatory recaps)",
        "Regulatory recaps are NOT new decisions",
    ),
    (
        "domain note 2 (procedural commitments)",
        "Procedural commitments are `action_items`",
    ),
    (
        "domain note 3 (single-speaker opinion)",
        '"I think / I believe" from a single speaker is NOT a group',
    ),
]


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if not _PROMPT.is_file():
        _fail(f"prompt file missing at {_PROMPT}")
    text = _PROMPT.read_text(encoding="utf-8")

    if _TAXONOMY_HEADER not in text:
        _fail(f"missing taxonomy section header: {_TAXONOMY_HEADER!r}")

    for label, header in _SUBTYPE_HEADERS:
        if header not in text:
            _fail(f"missing Fernández sub-type ({label}) header: {header!r}")

    if _MODAL_HEADER not in text:
        _fail(f"missing modal verb policy header: {_MODAL_HEADER!r}")
    for modal in ('"shall"', '"will"', '"should"', '"may"', '"would"'):
        if modal not in text:
            _fail(f"missing modal verb in policy: {modal}")

    if _HALLUCINATION_HEADER not in text:
        _fail(
            f"missing hallucination defense header: {_HALLUCINATION_HEADER!r}"
        )
    if _HALLUCINATION_SENTENCE not in text:
        _fail("missing verbatim hallucination defense sentence")

    if _DOMAIN_HEADER not in text:
        _fail(f"missing domain notes header: {_DOMAIN_HEADER!r}")
    for label, marker in _DOMAIN_NOTE_MARKERS:
        if marker not in text:
            _fail(f"missing {label}: {marker!r}")

    print("OK: implicit-decision trigger taxonomy present")
    print(f"  - taxonomy header: {_TAXONOMY_HEADER}")
    for _, header in _SUBTYPE_HEADERS:
        print(f"  - sub-type: {header}")
    print(f"  - {_MODAL_HEADER}")
    print(f"  - {_HALLUCINATION_HEADER}")
    print(f"  - {_DOMAIN_HEADER} (3/3 notes)")


if __name__ == "__main__":
    main()
