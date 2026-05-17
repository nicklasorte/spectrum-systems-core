"""
CI check: all GitHub Actions workflow YAML files are syntactically valid.

Catches the class of error that caused a past workflow to silently fail:
invalid YAML syntax that GitHub Actions couldn't parse.

This runs locally (no GitHub API call required) so failures surface
pre-PR, not after the workflow runs and fails mysteriously.
"""
import pathlib

import pytest

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

SCAN_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = SCAN_ROOT / ".github" / "workflows"


def get_workflow_files() -> list[pathlib.Path]:
    if not WORKFLOWS_ROOT.exists():
        return []
    return sorted(
        list(WORKFLOWS_ROOT.glob("*.yml")) + list(WORKFLOWS_ROOT.glob("*.yaml"))
    )


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")
def test_all_workflow_yaml_files_are_valid():
    """
    Validates every .yml/.yaml file in .github/workflows/ is parseable YAML.

    Does NOT validate GitHub Actions schema (that requires the Actions API).
    Validates only: syntactically parseable, top-level mapping, has 'on' and
    'jobs' keys.

    Catches indentation errors, missing colons, bad escaping, etc. -- the
    kind of bug that turns a workflow into a no-op without any obvious
    failure signal.
    """
    workflow_files = get_workflow_files()

    if not workflow_files:
        pytest.skip(f"No workflow files found in {WORKFLOWS_ROOT}")

    invalid: list[str] = []

    for workflow_path in workflow_files:
        name = workflow_path.name
        try:
            content = workflow_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError) as e:
            invalid.append(f"{name}: file read error: {e}")
            continue

        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as e:
            invalid.append(f"{name}: YAML parse error: {e}")
            continue

        if parsed is None:
            invalid.append(f"{name}: file is empty or all comments")
            continue

        if not isinstance(parsed, dict):
            invalid.append(
                f"{name}: expected mapping at root, got "
                f"{type(parsed).__name__}"
            )
            continue

        # YAML parses the bare 'on:' key as the Python boolean True.
        if "on" not in parsed and True not in parsed:
            invalid.append(f"{name}: missing 'on:' trigger")

        if "jobs" not in parsed:
            invalid.append(f"{name}: missing 'jobs:' block")

    assert not invalid, (
        f"Found {len(invalid)} invalid workflow file(s) in {WORKFLOWS_ROOT}:\n"
        + "\n".join(f"  - {msg}" for msg in invalid)
    )


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")
def test_workflow_files_found():
    """Sanity: at least one workflow file exists."""
    workflow_files = get_workflow_files()
    assert len(workflow_files) > 0, (
        f"No workflow files found in {WORKFLOWS_ROOT}. "
        f"Check that the directory exists."
    )


def test_yaml_package_installed():
    """
    Ensures PyYAML is available for workflow validation.
    PyYAML is declared in pyproject.toml [project.dependencies] (>=6.0);
    if this test fails the install is broken.
    """
    assert YAML_AVAILABLE, (
        "PyYAML is not installed. "
        "Without it, workflow YAML validation is silently skipped. "
        "Reinstall the package or check pyproject.toml [project.dependencies]."
    )
