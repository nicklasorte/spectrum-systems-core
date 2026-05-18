"""Phase AA.3 — harness file allowlist + mutation validator.

``validate_diff(diff_text)`` parses a unified diff for every touched
file path and checks each against the allowlist defined in the YAML
block of ``docs/contracts/harness_allowlist.md``.

Hard rules (all enforced here, none in YAML):

* The allowlist is read from the contract file at runtime — never
  hardcoded. If the contract is missing, unreadable, has no YAML
  block, or the block is structurally wrong, the result is
  ``valid=False`` with reason ``allowlist_unavailable``. The validator
  NEVER allows by default.
* Forbidden is checked before allowed. One forbidden (or one
  not-in-allowlist) path rejects the whole diff.
* An empty diff (no parseable paths) is ``valid=False`` with reason
  ``no_paths_in_diff`` — an empty change can never be "allowed".

This module is read-only with respect to the proposer: the proposer
(AA.4) MUST NOT import or call it. Only the outer-loop driver (AA.7)
and the defense-in-depth recheck in the code-candidate evaluator
(AA.5) validate diffs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

_REPO_ROOT_MARKER = "pyproject.toml"
_CONTRACT_RELPATH = "docs/contracts/harness_allowlist.md"
_YAML_BLOCK_RE = re.compile(
    r"```yaml\s*\n(.*?)\n```", re.DOTALL
)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str
    rejected_paths: list[str] = field(default_factory=list)
    touched_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Allowlist:
    version: str
    allowed_paths: tuple[str, ...]
    forbidden_path_patterns: tuple[str, ...]


def _find_repo_root(start: Path) -> Path | None:
    for parent in [start.resolve(), *start.resolve().parents]:
        if (parent / _REPO_ROOT_MARKER).is_file():
            return parent
    return None


def _load_allowlist(contract_path: Path) -> _Allowlist | None:
    """Parse the YAML block. Returns ``None`` (fail-closed) on ANY
    structural problem — a missing file, no fenced yaml block, invalid
    YAML, a missing ``harness_allowlist`` key, or non-list path
    collections. The caller turns ``None`` into ``allowlist_unavailable``.
    """
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _YAML_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        doc = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    block = doc.get("harness_allowlist")
    if not isinstance(block, dict):
        return None
    allowed = block.get("allowed_paths")
    forbidden = block.get("forbidden_path_patterns")
    if not isinstance(allowed, list) or not allowed:
        return None
    if not isinstance(forbidden, list):
        return None
    if not all(isinstance(p, str) and p for p in allowed):
        return None
    if not all(isinstance(p, str) and p for p in forbidden):
        return None
    version = block.get("version")
    return _Allowlist(
        version=str(version) if version is not None else "unknown",
        allowed_paths=tuple(allowed),
        forbidden_path_patterns=tuple(forbidden),
    )


@cache
def _allowlist_for(contract_path_str: str) -> _Allowlist | None:
    return _load_allowlist(Path(contract_path_str))


def _default_contract_path() -> Path | None:
    root = _find_repo_root(Path(__file__).parent)
    if root is None:
        return None
    return root / _CONTRACT_RELPATH


_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_DIFF_OLD_RE = re.compile(r"^--- (?:a/)?(.+?)\s*$")
_DIFF_NEW_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$")


def parse_touched_paths(diff_text: str) -> list[str]:
    """Every distinct file path a unified diff touches.

    Reads ``diff --git`` headers AND the ``---``/``+++`` hunk headers so
    a pure ``---/+++`` diff (no ``diff --git`` line) is still parsed.
    ``/dev/null`` (add/delete sentinel) is ignored; the real path comes
    from the other side of the pair.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        p = p.strip()
        if not p or p == "/dev/null":
            return
        # Drop a trailing tab+timestamp some diff tools emit.
        p = p.split("\t", 1)[0].strip()
        if p and p not in seen:
            seen.add(p)
            paths.append(p)

    for line in diff_text.splitlines():
        mg = _DIFF_GIT_RE.match(line)
        if mg:
            _add(mg.group(1))
            _add(mg.group(2))
            continue
        mo = _DIFF_OLD_RE.match(line)
        if mo:
            _add(mo.group(1))
            continue
        mn = _DIFF_NEW_RE.match(line)
        if mn:
            _add(mn.group(1))
    return paths


def _is_forbidden(path: str, patterns: tuple[str, ...]) -> bool:
    anchored = "/" + path.lstrip("/")
    for frag in patterns:
        needle = "/" + frag.lstrip("/")
        if needle in anchored:
            return True
    return False


def _is_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    norm = path.lstrip("/")
    for entry in allowed:
        e = entry.lstrip("/")
        if e.endswith("/"):
            if norm == e.rstrip("/") or norm.startswith(e):
                return True
        elif norm == e:
            return True
    return False


def validate_diff(
    diff_text: str, *, contract_path: Path | str | None = None
) -> ValidationResult:
    """Validate a unified diff against the harness allowlist contract."""
    if contract_path is not None:
        allowlist = _load_allowlist(Path(contract_path))
    else:
        cp = _default_contract_path()
        allowlist = _allowlist_for(str(cp)) if cp is not None else None

    if allowlist is None:
        return ValidationResult(
            valid=False,
            reason="allowlist_unavailable",
            rejected_paths=[],
            touched_paths=[],
        )

    touched = parse_touched_paths(diff_text or "")
    if not touched:
        return ValidationResult(
            valid=False,
            reason="no_paths_in_diff",
            rejected_paths=[],
            touched_paths=[],
        )

    rejected: list[str] = []
    for path in touched:
        if _is_forbidden(path, allowlist.forbidden_path_patterns):
            rejected.append(path)
        elif not _is_allowed(path, allowlist.allowed_paths):
            rejected.append(path)

    if rejected:
        return ValidationResult(
            valid=False,
            reason="forbidden_or_unlisted_path",
            rejected_paths=sorted(rejected),
            touched_paths=touched,
        )
    return ValidationResult(
        valid=True,
        reason="all_paths_allowed",
        rejected_paths=[],
        touched_paths=touched,
    )


__all__ = [
    "ValidationResult",
    "validate_diff",
    "parse_touched_paths",
]
