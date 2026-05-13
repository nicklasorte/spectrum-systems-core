"""
CI check: no workflow may checkout a repository other than nicklasorte/spectrum-systems-core.

Cross-repo checkouts require a PAT with cross-repo scope.  GITHUB_TOKEN is
scoped to the repo it runs in and cannot access other repositories.  Since
data-lake was merged into this repo (PR #85), any checkout that names an
external repository is a bug — it will fail at runtime with a 403 and there
is no secret to fix it with.

This test catches the pattern before the PR is opened so the failure is
pre-PR, not post-CI.
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
THIS_REPO = "nicklasorte/spectrum-systems-core"


def _checkout_steps(parsed: dict) -> list[dict]:
    """Return every step dict that uses an actions/checkout action."""
    steps = []
    for job in (parsed.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            uses = step.get("uses") or ""
            if uses.startswith("actions/checkout"):
                steps.append(step)
    return steps


@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")
def test_no_cross_repo_checkout():
    """
    Every actions/checkout step must either omit 'repository' (defaults to
    the current repo) or explicitly name nicklasorte/spectrum-systems-core.

    A cross-repo checkout requires a PAT.  GITHUB_TOKEN is repo-scoped and
    will 403 against any other repository.
    """
    if not WORKFLOWS_ROOT.exists():
        pytest.skip(f"No workflows directory at {WORKFLOWS_ROOT}")

    violations: list[str] = []

    for wf_path in sorted(WORKFLOWS_ROOT.glob("*.yml")):
        try:
            content = wf_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
        except Exception:
            # Syntax errors are caught by test_workflow_yaml_validity.py
            continue

        if not isinstance(parsed, dict):
            continue

        for step in _checkout_steps(parsed):
            repo = (step.get("with") or {}).get("repository")
            if repo is None:
                continue  # defaults to current repo — fine
            if repo == THIS_REPO:
                continue  # explicit self-reference — fine
            violations.append(
                f"{wf_path.name}: step '{step.get('name', '(unnamed)')}' "
                f"checks out external repo '{repo}'. "
                f"GITHUB_TOKEN cannot access repos other than {THIS_REPO}. "
                f"Remove the checkout (data-lake is a local subdirectory) "
                f"or use a PAT secret if a genuine cross-repo checkout is needed."
            )

    assert not violations, (
        f"Found {len(violations)} cross-repo checkout(s) in workflow files:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
