"""
CI check: no hardcoded Anthropic model strings in GitHub Actions workflows.

Model strings in workflows must come from the model registry at runtime,
not be hardcoded. This enforces the same model registry discipline
as test_no_deprecated_model_strings.py does for Python files.

Pattern to catch: any line in a .yml file containing a claude-* string
that looks like a model identifier.
"""
import pathlib
import re
import pytest

SCAN_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = SCAN_ROOT / ".github" / "workflows"

# Pattern matching Anthropic model strings.
# Shape: claude-<tier>-<digit>...   e.g. claude-sonnet-4-6, claude-opus-4-7,
# claude-haiku-4-5-20251001, claude-sonnet-4-20250514.
# Anchored on the tier word and a following digit so non-model identifiers
# like "claude-bot", "claude-code-action", or "CLAUDE.md" don't match.
#
# Intentional non-match: identifiers shaped like "claude-sonnet-analysis"
# (tier word followed by a non-digit token) are NOT matched. The trailing
# \d is required precisely because Anthropic model strings always include
# a version number after the tier; otherwise the regex would fire on any
# project, branch, or job name that happened to contain a tier word.
MODEL_STRING_PATTERN = re.compile(
    r"claude-(?:opus|sonnet|haiku|mythos)-\d[\w-]*"
)

# Lines that are explicitly allowed to contain model strings:
# - Comments (# ...)
# - echo statements documenting what NOT to do
ALLOWED_LINE_PATTERNS = [
    re.compile(r"^\s*#"),                  # whole-line comments
    re.compile(r"echo.*deprecated", re.I), # docs/warnings about deprecation
]

# Workflows explicitly exempted. Should stay empty -- a workflow that
# truly needs a hardcoded string should be fixed, not exempted.
# Paths are repo-relative with forward slashes.
EXEMPTED_WORKFLOWS: set[str] = set()


def _rel(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(SCAN_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def get_workflow_files() -> list[pathlib.Path]:
    if not WORKFLOWS_ROOT.exists():
        return []
    return sorted(
        list(WORKFLOWS_ROOT.glob("*.yml")) + list(WORKFLOWS_ROOT.glob("*.yaml"))
    )


def _strip_inline_comment(line: str) -> str:
    """
    Return the line with any trailing ' # comment' portion removed.
    Naive: does not account for '#' inside quoted strings, which is fine
    because workflow YAML rarely embeds '#' in quoted values, and matching
    inside a quoted value still represents a hardcoded model string.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # Treat as comment only if preceded by whitespace or start-of-line.
            if i == 0 or line[i - 1].isspace():
                return line[:i]
    return line


def test_no_model_strings_in_workflow_yaml():
    """
    Scans all .yml/.yaml workflow files for hardcoded Anthropic model strings.

    Model strings in workflows must be fetched at runtime via the model
    registry CLI (e.g. `python -m spectrum_systems_core.cli get-model ...`),
    never hardcoded as env vars or step arguments.

    Catches the bug-class: MODEL_ID=claude-sonnet-4-6 written into a workflow
    step. Such lines silently keep using a stale model after the registry is
    updated and bypass the registry entirely.
    """
    violations: list[str] = []
    workflow_files = get_workflow_files()

    for workflow_path in workflow_files:
        rel = _rel(workflow_path)
        if rel in EXEMPTED_WORKFLOWS:
            continue

        try:
            content = workflow_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            continue

        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if any(p.search(line) for p in ALLOWED_LINE_PATTERNS):
                continue
            scanned = _strip_inline_comment(line)
            matches = MODEL_STRING_PATTERN.findall(scanned)
            if matches:
                violations.append(
                    f"{rel}:{i}: hardcoded model string(s) {matches!r}\n"
                    f"  Line: {line.strip()}\n"
                    f"  Fix: replace with a runtime registry lookup, e.g.\n"
                    f"    MODEL=$(python -m spectrum_systems_core.cli "
                    f"get-model --task <type> --data-lake \"$DATA_LAKE_PATH\")"
                )

    assert not violations, (
        f"Found {len(violations)} hardcoded model string(s) in workflows.\n"
        f"Use the model registry at runtime instead of literal strings.\n\n"
        + "\n".join(violations)
    )


def test_exempted_workflows_still_exist():
    """
    Sanity: any exempted workflow still exists on disk.
    If it has been deleted, remove the entry from EXEMPTED_WORKFLOWS.
    """
    for workflow_str in EXEMPTED_WORKFLOWS:
        path = SCAN_ROOT / workflow_str
        assert path.exists(), (
            f"Exempted workflow {workflow_str} no longer exists at {path}. "
            f"Remove it from EXEMPTED_WORKFLOWS."
        )


def test_model_pattern_matches_known_strings():
    """
    Sanity: the regex catches every known Anthropic model string we care
    about. Update the pattern if Anthropic changes their naming format.
    """
    known_strings = [
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-haiku-3-5",
        "claude-sonnet-3-7",
    ]
    for s in known_strings:
        assert MODEL_STRING_PATTERN.search(s), (
            f"MODEL_STRING_PATTERN does not match known string: {s}. "
            f"Update the regex."
        )


def test_model_pattern_ignores_non_model_claude_references():
    """
    Sanity: pattern does not match non-model 'claude' references such as
    docs filenames, action repos, or domain names.
    """
    non_model_strings = [
        "CLAUDE.md",
        "claude_ai",
        "claude.ai",
        "anthropics/claude-code-action",
        "claude-bot",
    ]
    for s in non_model_strings:
        assert not MODEL_STRING_PATTERN.search(s), (
            f"Pattern unexpectedly matched non-model string: {s!r}. "
            f"Tighten the regex."
        )
