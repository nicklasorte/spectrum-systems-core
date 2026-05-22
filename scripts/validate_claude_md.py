"""CLAUDE.md pattern guard.

Enforces three invariants on ``CLAUDE.md`` and every file it ``@``-imports:

1. **Section length**: no section (the body between two headers, excluding
   fenced code blocks) exceeds 60 lines. Long code blocks are reference
   material and do not count toward the operational-precision limit; long
   prose is what trips Claude into missing instructions.
2. **Constitution alignment**: the word ``autonomous`` never appears in a
   paragraph that does not also contain ``governed`` or a clear negation
   cue (e.g. ``no autonomous``, ``not autonomous``, ``never``,
   ``deliberately not``, ``do not add``, ``rejected``). The constitution
   forbids autonomous agents in this repo; any reference to ``autonomous``
   must either reinforce that stance or sit beside the ``governed`` framing.
3. **Field-name discipline**: the field name ``artifact_type`` must appear
   at least once, and ``artifact_kind`` must not appear except in a
   teaching context (``instead of`` on the same line). This catches drift
   back to the deprecated field name flagged repeatedly by ``/ship``.

Exit code:

- ``0`` if all checks pass.
- ``1`` on any failure. A structured report is printed to stdout so the
  Stop hook surfaces the exact violations.

Pure stdlib; runs under any checkout without ``pip install``.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

SECTION_LINE_LIMIT = 60

_HEADER_RE = re.compile(r"^#{1,4}\s")
_IMPORT_RE = re.compile(r"^@([\w./\-]+\.md)\s*$")
_CODE_FENCE_RE = re.compile(r"^```")

_NEGATION_CUES: tuple[str, ...] = (
    "no autonomous",
    "not autonomous",
    "never",
    "deliberately not",
    "do not add",
    "reject",
    "forbid",
    "non-goal",
    "out of scope",
)


def _imported_targets(text: str) -> list[str]:
    """Return relative paths of every ``@path/to/file.md`` import line."""
    targets: list[str] = []
    for line in text.splitlines():
        m = _IMPORT_RE.match(line.strip())
        if m:
            targets.append(m.group(1))
    return targets


def _iter_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield CLAUDE.md, then each imported file once (depth-first, deduped)."""
    seen: set[pathlib.Path] = set()
    stack: list[pathlib.Path] = [root]
    while stack:
        path = stack.pop().resolve()
        if path in seen or not path.is_file():
            if path not in seen and not path.is_file():
                # Surface missing imports loudly so the hook fails closed.
                yield path
                seen.add(path)
            continue
        seen.add(path)
        yield path
        text = path.read_text(encoding="utf-8")
        for target in _imported_targets(text):
            stack.append((REPO_ROOT / target))


def _split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Group lines by header. The body of a section excludes its header line."""
    sections: list[tuple[str, list[str]]] = []
    current_name = "(preamble)"
    current_body: list[str] = []
    in_code = False
    for line in lines:
        if _CODE_FENCE_RE.match(line):
            in_code = not in_code
            current_body.append(line)
            continue
        if not in_code and _HEADER_RE.match(line):
            sections.append((current_name, current_body))
            current_name = line.strip()
            current_body = []
            continue
        current_body.append(line)
    sections.append((current_name, current_body))
    return sections


def _non_code_length(body: list[str]) -> int:
    in_code = False
    count = 0
    for line in body:
        if _CODE_FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        count += 1
    return count


def _split_paragraphs(lines: list[str]) -> list[str]:
    """A paragraph is a contiguous block of non-blank, non-fence lines."""
    paragraphs: list[str] = []
    buf: list[str] = []
    in_code = False
    for line in lines:
        if _CODE_FENCE_RE.match(line):
            in_code = not in_code
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        if in_code:
            continue
        if line.strip() == "":
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        buf.append(line.strip())
    if buf:
        paragraphs.append(" ".join(buf))
    return paragraphs


def _check_section_lengths(path: pathlib.Path, text: str) -> list[str]:
    failures: list[str] = []
    sections = _split_sections(text.splitlines())
    for name, body in sections:
        n = _non_code_length(body)
        if n > SECTION_LINE_LIMIT:
            failures.append(
                f"{path}: section {name!r} has {n} non-code lines (limit "
                f"{SECTION_LINE_LIMIT})"
            )
    return failures


def _check_autonomous(path: pathlib.Path, text: str) -> list[str]:
    failures: list[str] = []
    for para in _split_paragraphs(text.splitlines()):
        lower = para.lower()
        if "autonomous" not in lower:
            continue
        if "governed" in lower:
            continue
        if any(cue in lower for cue in _NEGATION_CUES):
            continue
        snippet = para if len(para) <= 160 else para[:157] + "..."
        failures.append(
            f"{path}: 'autonomous' used without 'governed' or negation cue "
            f"in paragraph: {snippet!r}"
        )
    return failures


def _check_artifact_field_names(
    path: pathlib.Path, text: str
) -> tuple[bool, list[str]]:
    """Return (has_artifact_type, failures_for_artifact_kind)."""
    failures: list[str] = []
    has_type = "artifact_type" in text
    for i, line in enumerate(text.splitlines(), start=1):
        if "artifact_kind" not in line:
            continue
        # Allow a single teaching reference: ``artifact_kind`` ... instead of ... artifact_type
        if "instead of" in line and "artifact_type" in line:
            continue
        failures.append(
            f"{path}:{i}: 'artifact_kind' appears outside a teaching context: "
            f"{line.strip()!r}"
        )
    return has_type, failures


def main() -> int:
    if not CLAUDE_MD.is_file():
        print(f"FAIL: {CLAUDE_MD} not found")
        return 1

    all_failures: list[str] = []
    any_has_type = False
    files_checked: list[pathlib.Path] = []

    for path in _iter_files(CLAUDE_MD):
        if not path.is_file():
            all_failures.append(f"missing @import target: {path}")
            continue
        files_checked.append(path)
        text = path.read_text(encoding="utf-8")
        all_failures.extend(_check_section_lengths(path, text))
        all_failures.extend(_check_autonomous(path, text))
        has_type, kind_failures = _check_artifact_field_names(path, text)
        any_has_type = any_has_type or has_type
        all_failures.extend(kind_failures)

    if not any_has_type:
        all_failures.append(
            "'artifact_type' does not appear in CLAUDE.md or any @import target"
        )

    print("CLAUDE.md pattern guard")
    print(f"  files checked: {len(files_checked)}")
    for p in files_checked:
        print(f"    - {p.relative_to(REPO_ROOT)}")
    print(f"  section line limit: {SECTION_LINE_LIMIT} (excluding code blocks)")

    if all_failures:
        print(f"FAIL ({len(all_failures)} finding(s))")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
