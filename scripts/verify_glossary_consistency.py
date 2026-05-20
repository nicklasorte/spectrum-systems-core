"""Phase 2P glossary internal-consistency verifier.

Loads the glossary JSONL and asserts that, for any two entries whose
``term`` or ``aliases`` overlap, the ``definition`` is byte-equal
after whitespace normalization.

If the definitions differ while the alias sets overlap, the script
exits non-zero with a clear message naming the conflicting entries.
Pure stdlib so it works in any checkout without ``pip install``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_GLOSSARY_PATH = (
    REPO_ROOT / "data" / "glossary" / "ntia_dod_spectrum_v1.jsonl"
)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_def(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _alias_set(entry: dict) -> set[str]:
    terms = [str(entry.get("term", ""))]
    aliases = entry.get("aliases", [])
    if isinstance(aliases, list):
        terms.extend(str(a) for a in aliases)
    return {t.strip().lower() for t in terms if isinstance(t, str) and t.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Phase 2P glossary internal consistency."
    )
    parser.add_argument(
        "--glossary",
        default=str(DEFAULT_GLOSSARY_PATH),
        help="Path to the glossary JSONL file.",
    )
    args = parser.parse_args(argv)

    glossary_path = pathlib.Path(args.glossary)
    if not glossary_path.is_file():
        print(f"FAIL glossary_unreadable: missing {glossary_path}")
        return 2
    raw = glossary_path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    entries: list[dict] = []
    for idx, line in enumerate(lines, start=1):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"FAIL glossary_unreadable: line {idx}: {exc}")
            return 2

    failures: list[str] = []
    seen: dict[str, tuple[int, dict]] = {}
    for idx, entry in enumerate(entries, start=1):
        my_aliases = _alias_set(entry)
        my_def = _normalize_def(str(entry.get("definition", "")))
        for alias in my_aliases:
            if alias in seen:
                prev_idx, prev_entry = seen[alias]
                prev_def = _normalize_def(str(prev_entry.get("definition", "")))
                if prev_def != my_def:
                    failures.append(
                        f"alias_definition_conflict: alias={alias!r} "
                        f"entry#{prev_idx}(term={prev_entry.get('term')!r}) "
                        f"vs entry#{idx}(term={entry.get('term')!r}); "
                        "definitions differ after whitespace normalization."
                    )
            else:
                seen[alias] = (idx, entry)

    if failures:
        for line in failures:
            print(f"FAIL {line}")
        return 1
    print(f"OK glossary_consistency_verified: {len(entries)} entries, no conflicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
