#!/usr/bin/env python3
"""Normalize the ``verified`` field on existing few-shot decision examples.

Background: ``select_few_shot_examples.py`` writes new candidates with
``verified: false`` and ``verify_example.py`` is the only path that
promotes an example to ``verified: true``. When a prior session left
the artifact in an inconsistent state -- e.g. the per-example
``verified`` field is missing entirely, is the string ``"true"``
instead of a boolean, or is ``None`` even though an ``audit_log``
entry of ``action == "verified"`` exists for that example -- the
Phase W wiring signal ``few_shot_present_with_verified`` flips to
MISSING because the workflow's predicate is::

    any(isinstance(ex, dict) and ex.get("verified") is True for ex in examples)

This script repairs that state durably:

  * For every example with EVIDENCE of prior operator verification --
    namely an ``audit_log`` entry with ``action`` in
    ``{verified, force-verified}`` and the matching ``example_id``,
    OR a non-null ``verified_by`` field on the example itself -- the
    ``verified`` field is coerced to boolean ``True`` (the only value
    the signal predicate accepts).
  * Examples WITHOUT evidence are left untouched. Auto-promoting an
    unverified example would violate the governance rule in
    CLAUDE.md ("Models do not promote their own corrections into the
    trust system").
  * The artifact-level ``verified`` flag is recomputed from the
    per-example flags after repair, matching the convention in
    ``verify_example.py``.

The script is idempotent: running it twice over the same file
produces the same bytes. It writes the file with sorted keys and
trailing newline so it can be re-pushed through ``push-data-lake``
without diff noise.

CI guard: refuses to run when ``ANTHROPIC_API_KEY`` is set unless
``--force`` is also passed. The workflow that invokes this script
passes ``--force`` because the workflow itself IS the human-approved
action; the secret is exported by the runtime and never inspected by
the script's logic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

DEFAULT_FEW_SHOT_PATH = (
    "store/artifacts/evals/few_shot/decision_examples_v1.json"
)

_VERIFICATION_ACTIONS = frozenset({"verified", "force-verified"})


def _load_artifact(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_artifact(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _verified_example_ids(audit_log: List[Dict[str, Any]]) -> set:
    out: set = set()
    for entry in audit_log or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("action") in _VERIFICATION_ACTIONS:
            eid = entry.get("example_id")
            if isinstance(eid, str) and eid:
                out.add(eid)
    return out


def _has_evidence(
    example: Dict[str, Any], verified_ids_in_log: set
) -> bool:
    """Return True iff there is evidence the operator verified this
    example previously. Evidence is one of:

      * An ``audit_log`` entry with ``action`` in
        ``{verified, force-verified}`` and the matching example_id.
      * A non-null ``verified_by`` field on the example itself
        (``verify_example.py`` always sets this field together with
        ``verified: true``, so a populated ``verified_by`` is a
        reliable trace of prior verification even when the
        ``verified`` field has been corrupted).
    """
    eid = example.get("example_id")
    if isinstance(eid, str) and eid in verified_ids_in_log:
        return True
    vby = example.get("verified_by")
    if isinstance(vby, str) and vby.strip():
        return True
    return False


def repair(
    artifact_path: Path,
    *,
    dry_run: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    """Repair the ``verified`` field on examples with evidence.

    Returns ``(exit_code, summary)`` where ``summary`` carries
    ``examples_scanned``, ``examples_repaired``, and
    ``artifact_verified_after``. Exit codes:
      0 -- repair completed (zero or more changes).
      1 -- artifact missing or unreadable.
      2 -- artifact failed schema validation after repair.
    """
    summary: Dict[str, Any] = {
        "examples_scanned": 0,
        "examples_repaired": 0,
        "artifact_verified_after": False,
        "skipped_no_evidence": [],
    }
    doc = _load_artifact(artifact_path)
    if doc is None:
        print(
            f"error: artifact not found or unreadable: {artifact_path}",
            file=sys.stderr,
        )
        return 1, summary

    examples = doc.get("examples")
    if not isinstance(examples, list):
        print(
            f"error: artifact has no examples list: {artifact_path}",
            file=sys.stderr,
        )
        return 1, summary

    audit_log = doc.get("audit_log") if isinstance(doc.get("audit_log"), list) else []
    verified_ids_in_log = _verified_example_ids(audit_log)

    repaired = 0
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        summary["examples_scanned"] += 1
        already_correct = ex.get("verified") is True
        if already_correct:
            continue
        if not _has_evidence(ex, verified_ids_in_log):
            summary["skipped_no_evidence"].append(
                ex.get("example_id") or "<unknown>"
            )
            continue
        # Coerce to boolean True (the only value the signal predicate
        # accepts). Preserve verified_by / verified_at when present so
        # the audit trail is not erased; only stamp verified_by when
        # it is currently null, and never touch verified_at because
        # the original verification timestamp -- if any -- is the
        # truthful one.
        ex["verified"] = True
        if ex.get("verified_by") is None:
            ex["verified_by"] = "repair_few_shot_verified.py"
        repaired += 1

    artifact_verified = bool(examples) and all(
        isinstance(ex, dict) and ex.get("verified") is True
        for ex in examples
    )
    doc["verified"] = artifact_verified

    summary["examples_repaired"] = repaired
    summary["artifact_verified_after"] = artifact_verified

    # Schema-validate the repaired doc BEFORE writing. A repair that
    # produces an invalid artifact is a regression, not a fix.
    try:
        validate_artifact(
            doc, "decision_few_shot_examples", str(artifact_path)
        )
    except ArtifactValidationError as exc:
        print(f"error: repaired artifact fails validation: {exc}", file=sys.stderr)
        return 2, summary

    if not dry_run and repaired > 0:
        _write_artifact(artifact_path, doc)

    return 0, summary


def _refuse_in_ci(force: bool) -> Optional[str]:
    if force:
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "repair_few_shot_verified.py refuses to run with "
            "ANTHROPIC_API_KEY set: this script must run only in an "
            "operator-approved context. Pass --force from a workflow "
            "step that the operator has authorized."
        )
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", default=None)
    parser.add_argument(
        "--artifact-path", default=None,
        help="Override --data-lake-based path resolution (testing).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the CI refusal (workflow context).",
    )
    args = parser.parse_args(argv)

    refusal = _refuse_in_ci(args.force)
    if refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 4

    if args.artifact_path:
        artifact_path = Path(args.artifact_path)
    elif args.data_lake:
        artifact_path = Path(args.data_lake) / DEFAULT_FEW_SHOT_PATH
    else:
        print(
            "error: either --artifact-path or --data-lake is required",
            file=sys.stderr,
        )
        return 1

    code, summary = repair(artifact_path, dry_run=args.dry_run)
    print(
        f"repair_few_shot_verified: scanned={summary['examples_scanned']} "
        f"repaired={summary['examples_repaired']} "
        f"artifact_verified_after={summary['artifact_verified_after']}"
    )
    if summary["skipped_no_evidence"]:
        print(
            "skipped (no audit_log / verified_by evidence): "
            + ", ".join(summary["skipped_no_evidence"]),
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
