"""Phase AA.5 — harness code-candidate auto-PR eligibility checker.

The ``harness-code-candidate-pr.yml`` workflow shells out to this
script; ALL eligibility logic lives in
``spectrum_systems_core.harness.code_pr_eligibility`` (imported here)
so the YAML carries no decision logic and the artifact's stamped
verdict cannot drift from the workflow's verdict — the exact invariant
Phase Y.7 enforces for prompt candidates.

Usage::

    python scripts/check_harness_code_pr_eligibility.py \
        <harness_code_candidate_evaluation.json>

Reads the ``harness_code_candidate_evaluation`` artifact, validates it
against its schema BEFORE reading any field (CLAUDE.md artifact-reader
co-requirement), then prints a single JSON line::

    {"eligible": bool, "reason": "...", "candidate_id": "..."}

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

from spectrum_systems_core.harness.code_pr_eligibility import (  # noqa: E402
    evaluate_code_eligibility,
)

_EXPECTED = "harness_code_candidate_evaluation"


def _normalize(doc: dict) -> dict:
    """Accept either a bare evaluation payload or a full envelope.

    The evaluator returns ``new_artifact`` (an envelope whose
    ``payload`` carries the evaluation). On disk a caller may persist
    either the envelope or the flat payload; pick whichever actually
    carries ``artifact_type == harness_code_candidate_evaluation``.
    """
    payload = doc.get("payload")
    if isinstance(payload, dict) and payload.get("artifact_type") == _EXPECTED:
        return payload
    return doc


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(
            "usage: check_harness_code_pr_eligibility.py "
            "<harness_code_candidate_evaluation.json>",
            file=sys.stderr,
        )
        return 2
    path = Path(args[0])
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "eligible": False,
            "reason": f"unreadable_artifact:{exc}",
        }))
        return 2
    if not isinstance(doc, dict):
        print(json.dumps({
            "eligible": False,
            "reason": "unreadable_artifact:not_an_object",
        }))
        return 2

    evaluation = _normalize(doc)
    try:
        validate_artifact(evaluation, _EXPECTED, str(path))
    except ArtifactValidationError as exc:
        print(json.dumps({
            "eligible": False,
            "reason": f"invalid_{_EXPECTED}:{exc}",
        }))
        return 2

    verdict = evaluate_code_eligibility(evaluation)
    print(json.dumps({
        "eligible": verdict.eligible,
        "reason": verdict.reason,
        "candidate_id": evaluation.get("candidate_id"),
    }))
    return 0 if verdict.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
