#!/usr/bin/env python3
"""Phase X2.2 — human verification of a few-shot example.

Sets ``verified: true`` on a specific example after a human has
reviewed it against the source transcript. Records reviewer_id and
timestamp; appends an entry to ``audit_log``.

Run::

    python scripts/verify_example.py \\
        --example-id <uuid> \\
        --reviewer-id <your-name> \\
        --data-lake <path>

CI guard: when ``ANTHROPIC_API_KEY`` is set in the environment AND
``--force`` is NOT passed, the script refuses to run. The refusal
exists because Claude Code (or any LLM agent invoked with an API key)
must not self-verify examples it generated; that is self-grading and
violates the reviewer-policy in CLAUDE.md.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_FEW_SHOT_PATH = "store/artifacts/evals/few_shot/decision_examples_v1.json"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


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


def _refuse_in_ci(force: bool) -> Optional[str]:
    if force:
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "verify_example.py refuses to run with ANTHROPIC_API_KEY set: "
            "this script is human-only. If you really want to run it from "
            "an automated context, pass --force AND record your reviewer_id."
        )
    return None


def verify_example(
    artifact_path: Path,
    example_id: str,
    reviewer_id: str,
    notes: Optional[str],
    *,
    force: bool = False,
) -> int:
    doc = _load_artifact(artifact_path)
    if doc is None:
        print(
            f"error: artifact not found or unreadable: {artifact_path}",
            file=sys.stderr,
        )
        return 1

    examples = doc.get("examples")
    if not isinstance(examples, list):
        print("error: artifact has no examples list", file=sys.stderr)
        return 1

    target: Optional[Dict[str, Any]] = None
    for ex in examples:
        if isinstance(ex, dict) and ex.get("example_id") == example_id:
            target = ex
            break
    if target is None:
        print(
            f"error: example_id={example_id} not found in {artifact_path}",
            file=sys.stderr,
        )
        return 2

    audit_log = list(doc.get("audit_log") or [])
    if target.get("verified") is True:
        if not force:
            print(
                f"warn: example {example_id} is already verified by "
                f"{target.get('verified_by')!r} at "
                f"{target.get('verified_at')!r}. Pass --force to re-verify.",
                file=sys.stderr,
            )
            return 3
        audit_log.append({
            "action": "force-verified",
            "example_id": example_id,
            "at": _now_iso(),
            "actor": reviewer_id,
            "notes": notes,
        })
    else:
        audit_log.append({
            "action": "verified",
            "example_id": example_id,
            "at": _now_iso(),
            "actor": reviewer_id,
            "notes": notes,
        })

    target["verified"] = True
    target["verified_by"] = reviewer_id
    target["verified_at"] = _now_iso()
    doc["audit_log"] = audit_log

    # Recompute artifact-level verified flag: true iff every example
    # has verified=true. This lets a caller answer "is the whole set
    # ready?" with a single field check.
    doc["verified"] = all(
        bool(ex.get("verified")) for ex in examples if isinstance(ex, dict)
    )

    _write_artifact(artifact_path, doc)
    print(
        f"verified example {example_id} by {reviewer_id} at "
        f"{target['verified_at']}"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example-id", required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--data-lake", default=None)
    parser.add_argument(
        "--artifact-path", default=None,
        help="Override --data-lake-based path resolution (testing).",
    )
    parser.add_argument("--notes", default=None)
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass CI refusal and allow re-verification.",
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

    return verify_example(
        artifact_path,
        args.example_id,
        args.reviewer_id,
        args.notes,
        force=args.force,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
