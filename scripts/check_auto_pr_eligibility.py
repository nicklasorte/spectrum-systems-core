"""Phase Y.7 — auto-PR eligibility checker (the workflow's only brain).

The ``correction-candidate-auto-pr.yml`` workflow shells out to this
script; ALL eligibility logic lives in
``spectrum_systems_core.extraction.auto_pr_eligibility`` (imported
here) so the YAML contains no decision logic of its own and the
artifact's stamped verdict cannot drift from the workflow's verdict.

Usage::

    python scripts/check_auto_pr_eligibility.py <candidate_evaluation.json>

Reads the ``candidate_evaluation`` artifact, validates it against its
schema BEFORE reading any field (CLAUDE.md artifact-reader co-
requirement), then prints a single JSON line::

    {"eligible": bool, "reasons": [...], "candidate_id": "...",
     "candidate_source": "...", "revert_of_prompt_addition_id": ... }

Exit code: 0 when eligible, 1 when ineligible, 2 on a missing or
invalid artifact (fail-closed — an unreadable artifact is never
treated as eligible).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_SRC = _SCRIPTS_DIR.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

from spectrum_systems_core.extraction.auto_pr_eligibility import (  # noqa: E402
    evaluate_eligibility,
)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: check_auto_pr_eligibility.py <candidate_evaluation.json>",
              file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"eligible": False,
                          "reasons": [f"unreadable_artifact:{exc}"]}))
        return 2
    try:
        validate_artifact(artifact, "candidate_evaluation", str(path))
    except ArtifactValidationError as exc:
        print(json.dumps({"eligible": False,
                          "reasons": [f"invalid_candidate_evaluation:{exc}"]}))
        return 2

    verdict = evaluate_eligibility(artifact)
    out = {
        "eligible": verdict.eligible,
        "reasons": verdict.reasons,
        "candidate_id": artifact.get("candidate_id"),
        "candidate_source": artifact.get("candidate_source"),
        "revert_of_prompt_addition_id": artifact.get(
            "revert_of_prompt_addition_id"
        ),
    }
    print(json.dumps(out))
    return 0 if verdict.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
