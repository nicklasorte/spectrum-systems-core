"""Phase 1 fixture inventory check.

The promotion gate's behaviour is proved by per-category fixtures
under ``tests/fixtures/grounding_rejections/``. This script enforces
that the four CANONICAL categories each have at least one fixture so a
regression cannot land that silently drops a category's coverage.

Exit code:
  0 — every required category has >= 1 fixture
  1 — at least one category has 0 fixtures (with details on stderr)

Usage:
  python scripts/verify_grounding_fixtures.py

The script does NOT invoke the gate — that's what
``tests/grounding/`` does. It only counts. The gate-execution tests
iterate the same fixtures and assert each rejects with the correct
``reason_code``; this script's job is the inventory contract.
"""
from __future__ import annotations

import pathlib
import sys

REQUIRED_CATEGORIES: tuple[str, ...] = (
    "missing_field",
    "offset_mismatch",
    "exact_text_not_in_transcript",
    "paraphrase_near_miss",
)

# These additional categories are not in the mandatory "4 categories"
# but are documented and should be reported on for visibility. Their
# absence does NOT fail the script — only the canonical four do.
ADDITIONAL_CATEGORIES: tuple[str, ...] = (
    "grounding_rate_below_floor",
    "unknown_turn_id",
)

FIXTURE_ROOT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "grounding_rejections"
)


def _count_fixtures(category_dir: pathlib.Path) -> int:
    """A fixture is a subdirectory that contains an ``artifact.json``
    and a ``transcript.txt``. Counting is per-fixture, not per-file."""
    if not category_dir.is_dir():
        return 0
    if (category_dir / "artifact.json").is_file() and (
        category_dir / "transcript.txt"
    ).is_file():
        return 1
    # Allow nested layout: <category>/<fixture_name>/{artifact.json,transcript.txt}.
    count = 0
    for child in category_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / "artifact.json").is_file() and (
            child / "transcript.txt"
        ).is_file():
            count += 1
    return count


def main() -> int:
    if not FIXTURE_ROOT.is_dir():
        print(
            f"ERROR: fixture root does not exist: {FIXTURE_ROOT}",
            file=sys.stderr,
        )
        return 1

    failed = False
    print(f"Phase 1 fixture inventory under {FIXTURE_ROOT}:")
    for category in REQUIRED_CATEGORIES:
        count = _count_fixtures(FIXTURE_ROOT / category)
        status = "OK" if count > 0 else "MISSING"
        print(f"  [{status}] {category}: {count} fixture(s)")
        if count == 0:
            failed = True
    for category in ADDITIONAL_CATEGORIES:
        count = _count_fixtures(FIXTURE_ROOT / category)
        print(f"  [extra] {category}: {count} fixture(s)")

    if failed:
        print(
            "FAIL: at least one required category has zero fixtures. "
            "Add the missing fixture(s) before opening the PR.",
            file=sys.stderr,
        )
        return 1
    print("OK: all 4 required categories have >= 1 fixture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
