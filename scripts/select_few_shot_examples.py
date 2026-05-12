#!/usr/bin/env python3
"""Phase X2.2 — select few-shot decision example candidates.

Reads typed-extraction artifacts for a source_id and writes 3 candidate
decisions (one each per ``approval`` / ``deferral`` / ``action_required``
outcome) to ``decision_examples_v1.json`` with ``verified: false``.

The script NEVER sets ``verified: true``. Only a human running
``scripts/verify_example.py`` can do that — self-verification is a
governance violation.

After writing candidates the script writes
``REVIEW_CHECKLIST.md`` next to the artifact so the operator has a
file-on-disk record of what to inspect.

Run::

    python scripts/select_few_shot_examples.py \\
        --source-id <id> --data-lake data-lake/
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_FEW_SHOT_PATH = "store/artifacts/evals/few_shot/decision_examples_v1.json"
REVIEW_CHECKLIST_RELPATH = "store/artifacts/evals/few_shot/REVIEW_CHECKLIST.md"

# Outcome types we target. One example per type, highest confidence first.
TARGET_OUTCOMES: Tuple[str, ...] = ("approval", "deferral", "action_required")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _load_meeting_extraction(
    data_lake: Path, source_id: str
) -> Optional[Dict[str, Any]]:
    """Locate the most-recent meeting_extraction artifact for source_id.

    Looks under ``<data_lake>/store/artifacts/extractions/`` for files
    whose ``source_id`` field matches the requested id, then under
    ``<data_lake>/store/processed/meetings/<source_id>/`` as a fallback
    for older directory layouts.
    """
    extraction_dir = data_lake / "store" / "artifacts" / "extractions"
    candidates: List[Path] = []
    if extraction_dir.is_dir():
        candidates.extend(sorted(extraction_dir.glob("*.json")))

    processed_dir = (
        data_lake / "store" / "processed" / "meetings" / source_id
    )
    if processed_dir.is_dir():
        candidates.extend(
            sorted(processed_dir.rglob("meeting_extraction*.json"))
        )

    chosen: Optional[Dict[str, Any]] = None
    chosen_path: Optional[Path] = None
    for path in candidates:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("source_id") != source_id:
            continue
        # Prefer the most recently modified file.
        if chosen_path is None or path.stat().st_mtime > chosen_path.stat().st_mtime:
            chosen = doc
            chosen_path = path
    return chosen


def _select_candidates_from_decisions(
    decisions: List[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return up to 3 (outcome, decision) tuples — one per outcome.

    Within each outcome bucket we choose the GROUNDED decision with the
    highest confidence (grounded examples are always preferred over
    ungrounded ones even when the ungrounded candidate has higher
    confidence). Ties are broken by source_turn_ids (lexicographic) for
    determinism.

    Codex P2 fix: the previous key ordered by confidence first, so an
    ungrounded high-confidence decision could beat a grounded
    lower-confidence one. Few-shot examples MUST prioritise grounding
    because the prompt teaches the model to copy structure, not to
    invent it.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {o: [] for o in TARGET_OUTCOMES}
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        outcome = d.get("decision_outcome")
        if outcome in buckets:
            buckets[outcome].append(d)

    out: List[Tuple[str, Dict[str, Any]]] = []
    for outcome in TARGET_OUTCOMES:
        items = buckets[outcome]
        if not items:
            continue
        items.sort(
            key=lambda d: (
                0 if d.get("grounding_verified") is True else 1,
                -float(d.get("confidence") or 0.0),
                ",".join(sorted(str(s) for s in (d.get("source_turn_ids") or []))),
            )
        )
        out.append((outcome, items[0]))
    return out


def _decision_to_example(
    decision: Dict[str, Any], source_id: str, outcome: str
) -> Dict[str, Any]:
    expected = {
        "decision_text": decision.get("decision_text") or "",
        "decision_outcome": outcome,
        "regulatory_verb": decision.get("regulatory_verb"),
        "speaker": decision.get("speaker"),
        "confidence": decision.get("confidence"),
    }
    return {
        "example_id": str(uuid.uuid4()),
        "source_meeting_id": source_id,
        "source_turn_ids": list(decision.get("source_turn_ids") or []),
        "input_text": (decision.get("source_text") or decision.get("decision_text") or "")[:2000],
        "expected_output": {k: v for k, v in expected.items() if v is not None},
        "verified": False,
        "verified_by": None,
        "verified_at": None,
        "selected_at": _now_iso(),
        "selection_reason": f"highest_confidence_for_outcome={outcome}",
    }


def _write_artifact(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_review_checklist(
    path: Path,
    source_id: str,
    examples: List[Dict[str, Any]],
    transcript_hint: Optional[str],
) -> None:
    lines: List[str] = [
        "# Few-Shot Example Review Checklist",
        "",
        "Phase X2.2 — review each candidate below before it can reach the",
        "extraction prompt. Examples are loaded ONLY when `verified: true`.",
        "",
        f"- Source meeting: `{source_id}`",
    ]
    if transcript_hint:
        lines.append(f"- Transcript reference: `{transcript_hint}`")
    lines.extend([
        "",
        "## Review instructions",
        "",
        "1. Open the source transcript and locate each referenced `source_turn_ids`.",
        "2. Confirm the `expected_output.decision_text` matches what was actually said.",
        "3. Confirm the `decision_outcome` is correct",
        "   (`approval`, `deferral`, or `action_required`).",
        "4. If correct, run:",
        "",
        "   ```",
        "   python scripts/verify_example.py \\",
        "       --example-id <example_id> \\",
        "       --reviewer-id <your-name> \\",
        "       --data-lake <path>",
        "   ```",
        "",
        "Reviewer policy: the reviewer MUST be a different person from",
        "the operator who ran the extraction. The system cannot enforce",
        "this technically; the audit_log records the reviewer_id.",
        "",
        "## Candidates",
        "",
    ])
    for ex in examples:
        lines.extend([
            f"### `{ex['example_id']}`",
            "",
            f"- outcome: `{ex['expected_output'].get('decision_outcome')}`",
            f"- source_turn_ids: `{ex.get('source_turn_ids')}`",
            f"- confidence: `{ex['expected_output'].get('confidence')}`",
            "",
            "Decision text:",
            "",
            "> " + (ex['expected_output'].get('decision_text') or '') ,
            "",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _refuse_when_run_in_ci() -> Optional[str]:
    """Returns a message when the env signals this is a CI / automated run.

    Phase X2.2 amended attack: Claude Code (or any CI agent) must not
    self-select examples and silently set verified=true. Selecting
    candidates is fine; verifying them is not. We don't gate selection
    here — but `verify_example.py` gates verification.
    """
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument(
        "--artifact-path", default=None,
        help="Override path to decision_examples_v1.json (testing).",
    )
    parser.add_argument(
        "--max-examples", type=int, default=3,
        help="Cap on candidates written (1..3).",
    )
    args = parser.parse_args(argv)

    data_lake = Path(args.data_lake).resolve()
    if not data_lake.is_dir():
        print(f"error: --data-lake does not exist: {data_lake}", file=sys.stderr)
        return 1

    extraction = _load_meeting_extraction(data_lake, args.source_id)
    if extraction is None:
        print(
            f"error: no meeting_extraction artifact found for "
            f"source_id={args.source_id} under {data_lake}",
            file=sys.stderr,
        )
        return 1

    decisions = extraction.get("decisions") or []
    if not isinstance(decisions, list):
        decisions = []

    chosen = _select_candidates_from_decisions(decisions)[: max(1, args.max_examples)]
    if not chosen:
        print(
            f"error: no decisions with target outcomes "
            f"({', '.join(TARGET_OUTCOMES)}) found in the extraction "
            f"for source_id={args.source_id}",
            file=sys.stderr,
        )
        return 2

    examples = [_decision_to_example(d, args.source_id, outcome) for outcome, d in chosen]

    artifact_path = (
        Path(args.artifact_path) if args.artifact_path
        else data_lake / DEFAULT_FEW_SHOT_PATH
    )
    existing_audit: List[Dict[str, Any]] = []
    if artifact_path.is_file():
        try:
            old = json.loads(artifact_path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                existing_audit = list(old.get("audit_log") or [])
        except (OSError, json.JSONDecodeError):
            existing_audit = []

    for ex in examples:
        existing_audit.append({
            "action": "selected",
            "example_id": ex["example_id"],
            "at": _now_iso(),
            "actor": "scripts/select_few_shot_examples.py",
            "notes": f"outcome={ex['expected_output'].get('decision_outcome')}",
        })

    artifact = {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": "decision",
        "verified": False,
        "created_at": _now_iso(),
        "examples": examples,
        "audit_log": existing_audit,
    }
    _write_artifact(artifact_path, artifact)

    checklist_path = (
        Path(args.artifact_path).parent / "REVIEW_CHECKLIST.md"
        if args.artifact_path
        else data_lake / REVIEW_CHECKLIST_RELPATH
    )
    _write_review_checklist(
        checklist_path,
        args.source_id,
        examples,
        transcript_hint=str(
            data_lake / "store" / "processed" / "meetings" / args.source_id
        ),
    )

    print(
        f"wrote {len(examples)} candidate(s) -> {artifact_path}\n"
        f"checklist -> {checklist_path}\n"
        "NEXT: review each example, then run scripts/verify_example.py "
        "to mark verified=true.",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
