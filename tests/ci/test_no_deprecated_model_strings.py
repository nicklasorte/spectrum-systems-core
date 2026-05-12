"""
CI check: no deprecated model strings in Python source files.

Fails PR if any .py file contains a hardcoded deprecated model string.
Update DEPRECATED_MODEL_STRINGS when Anthropic publishes new deprecations.

Current deprecation deadline: June 15, 2026.
  claude-sonnet-4-20250514 -> replace with claude-sonnet-4-6
  claude-opus-4-20250514   -> replace with claude-opus-4-7
"""
import pathlib

DEPRECATED_MODEL_STRINGS = [
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-opus-4-5",       # older, also deprecated
    "claude-haiku-3-5",      # retired Feb 19, 2026
    "claude-sonnet-3-7",     # retired Oct 28, 2025
]

# Files explicitly allowed to contain deprecated model strings.
# The first entry is this test file itself. The remaining entries are
# legacy modules that still hardcode pre-deprecation strings; they are
# grandfathered so the CI gate can ship now and block NEW occurrences.
# Migrating these to the model registry is tracked as separate work.
ALLOWED_LOCATIONS = [
    # CI check itself + sibling checks that must reference the strings
    # they detect:
    "tests/ci/test_no_deprecated_model_strings.py",
    "tests/ci/test_no_model_strings_in_workflows.py",
    "tests/ci/test_ci_check_infrastructure.py",
    # Grandfathered legacy modules (migration debt — replace with
    # model registry lookup when the registry lands):
    "src/spectrum_systems_core/synthesis/report_generator.py",
    "src/spectrum_systems_core/synthesis/keynote_generator.py",
    "src/spectrum_systems_core/paper/revision_workflow.py",
    # Grandfathered test fixtures/assertions for the legacy modules:
    "tests/synthesis/test_keynote_eval.py",
    "tests/synthesis/test_grounding_eval.py",
    "tests/synthesis/test_report_generator.py",
]

SCAN_EXTENSIONS = [".py"]
SCAN_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _normalize(path: pathlib.Path) -> str:
    """Return a forward-slash, repo-relative string for matching."""
    try:
        rel = path.resolve().relative_to(SCAN_ROOT)
    except ValueError:
        rel = path
    return str(rel).replace("\\", "/")


def get_python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for ext in SCAN_EXTENSIONS:
        files.extend(SCAN_ROOT.rglob(f"*{ext}"))
    allowed_norm = {a.replace("\\", "/") for a in ALLOWED_LOCATIONS}
    out: list[pathlib.Path] = []
    for f in files:
        norm = _normalize(f)
        if norm in allowed_norm:
            continue
        parts = set(f.parts)
        if ".git" in parts or "__pycache__" in parts:
            continue
        if ".venv" in parts or "venv" in parts or "build" in parts or "dist" in parts:
            continue
        out.append(f)
    return out


def test_no_deprecated_model_strings_in_python_source():
    """
    Scans all .py files for known deprecated model strings.
    Fails CI if any found outside of allowed locations.

    This test is the artifact-as-evidence that the model registry
    pattern is enforced. Prose in a PR description is not evidence.
    Fix: replace hardcoded string with the model registry lookup, or
         (only for legacy modules) add the path to ALLOWED_LOCATIONS
         with an explicit migration-debt note.

    Known limitation: this scan is line-oriented substring matching.
    A deprecated string deliberately split across lines via a Python
    backslash line-continuation (e.g. "claude-sonnet-4-\\\n20250514")
    will NOT be caught, because the literal backslash-newline breaks
    the substring on disk and is not removed before scanning.
    Acceptable trade-off: such constructs are extremely unusual in
    real source and would be caught by code review; tightening this
    would require tokenizing every .py file, which is out of scope
    for a fast CI gate.
    """
    violations = []
    python_files = get_python_files()

    for filepath in python_files:
        try:
            content = filepath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            continue

        if not any(s in content for s in DEPRECATED_MODEL_STRINGS):
            continue

        lines = content.splitlines()
        rel = _normalize(filepath)
        for i, line in enumerate(lines, 1):
            for deprecated_string in DEPRECATED_MODEL_STRINGS:
                if deprecated_string in line:
                    violations.append(
                        f"{rel}:{i}: found '{deprecated_string}' "
                        f"-- replace via model registry lookup"
                    )

    assert not violations, (
        f"Found {len(violations)} deprecated model string(s).\n"
        f"Replace each with a registry lookup (e.g. "
        f"ModelRegistry(data_lake).get('<task_type>')), or add a strictly "
        f"legacy file path to ALLOWED_LOCATIONS with a migration-debt note.\n"
        f"Violations:\n" + "\n".join(violations)
    )


def test_deprecated_list_is_not_empty():
    """Sanity: DEPRECATED_MODEL_STRINGS is maintained."""
    assert len(DEPRECATED_MODEL_STRINGS) > 0


def test_allowed_locations_exist():
    """
    Sanity: every ALLOWED_LOCATIONS path exists on disk.
    A stale exemption hides future violations; remove the path if the
    underlying file has been deleted or migrated off the deprecated string.
    """
    for location in ALLOWED_LOCATIONS:
        path = SCAN_ROOT / location
        assert path.exists(), (
            f"Allowed location {location} does not exist (resolved to "
            f"{path}). Remove from ALLOWED_LOCATIONS if no longer needed."
        )
