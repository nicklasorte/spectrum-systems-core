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
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure scripts/ is importable when invoked via ``python scripts/foo.py``.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

DEFAULT_FEW_SHOT_PATH = "store/artifacts/evals/few_shot/decision_examples_v1.json"
REVIEW_CHECKLIST_RELPATH = "store/artifacts/evals/few_shot/REVIEW_CHECKLIST.md"
NEEDS_REAL_EXAMPLES_RELPATH = (
    "store/artifacts/evals/few_shot/NEEDS_REAL_EXAMPLES.md"
)
PLACEHOLDER_ID_PREFIX = "phase-v-placeholder"

# Outcome types we target. One example per type, highest confidence first.
TARGET_OUTCOMES: Tuple[str, ...] = ("approval", "deferral", "action_required")

# Source families that the typed_extraction_runner walks when locating
# source_record.json. Kept in sync with
# ``extraction/typed_extraction_runner.py::_SOURCE_FAMILIES``.
_SOURCE_FAMILIES: Tuple[str, ...] = (
    "meetings", "books", "comments", "working_papers", "notes",
)


def _resolve_source_artifact_id(
    data_lake: Path, source_id: str
) -> Optional[str]:
    """Resolve a source_id slug to the source_record artifact_id.

    The typed_extraction_runner writes
    ``<sdl_root>/extractions/<source_artifact_id>_meeting_extraction.json``
    where ``source_artifact_id`` comes from
    ``<data_lake>/store/processed/<family>/<source_id>/source_record.json``
    (its ``artifact_id`` field), NOT from the source_id slug. This helper
    reproduces the runner's lookup so we can match the same artifact.
    """
    store_root = data_lake / "store"
    for family in _SOURCE_FAMILIES:
        sr_path = (
            store_root / "processed" / family / source_id / "source_record.json"
        )
        if sr_path.is_file():
            try:
                data = json.loads(sr_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            aid = data.get("artifact_id") if isinstance(data, dict) else None
            if isinstance(aid, str) and aid:
                return aid
    return None


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _load_meeting_extraction(
    data_lake: Path, source_id: str
) -> Optional[Dict[str, Any]]:
    """Locate the most-recent meeting_extraction artifact for source_id.

    Real artifacts written by the typed_extraction_runner identify their
    source via ``source_artifact_id`` (a UUID resolved from
    ``source_record.json``), NOT via ``source_id`` (the slug). We resolve
    the slug to the artifact_id and match on either field so the script
    finds both live-runner artifacts (matched by ``source_artifact_id``)
    AND synthetic test fixtures that carry a top-level ``source_id``
    field.

    Search order:
      1. ``<data_lake>/store/artifacts/extractions/*.json`` (canonical).
      2. ``<data_lake>/store/processed/meetings/<source_id>/`` as a
         legacy fallback for older directory layouts.
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

    resolved_artifact_id = _resolve_source_artifact_id(data_lake, source_id)

    chosen: Optional[Dict[str, Any]] = None
    chosen_path: Optional[Path] = None
    for path in candidates:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        doc_source_id = doc.get("source_id")
        doc_source_artifact_id = doc.get("source_artifact_id")
        matches = (
            (doc_source_id == source_id)
            or (
                resolved_artifact_id is not None
                and doc_source_artifact_id == resolved_artifact_id
            )
        )
        if not matches:
            continue
        # Prefer the most recently modified file.
        if chosen_path is None or path.stat().st_mtime > chosen_path.stat().st_mtime:
            chosen = doc
            chosen_path = path

    # Integration-hardening gate: validate the matched artifact against
    # the meeting_extraction schema BEFORE the caller reads any field
    # off of it. A field-name drift between writer and reader (e.g.
    # ``source_id`` vs ``source_artifact_id``) now fails here with a
    # message that names the failing field instead of silently producing
    # an empty selection downstream.
    if chosen is not None and chosen_path is not None:
        validate_artifact(chosen, "meeting_extraction", str(chosen_path))
    return chosen


def _decision_sort_key(d: Dict[str, Any]) -> Tuple[Any, ...]:
    """Sort key — grounded first, then confidence desc, then turn_ids for stability.

    Codex P2 fix: grounded examples are always preferred over ungrounded
    ones even when the ungrounded candidate has higher confidence.
    Few-shot examples MUST prioritise grounding because the prompt
    teaches the model to copy structure, not to invent it.
    """
    return (
        0 if d.get("grounding_verified") is True else 1,
        -float(d.get("confidence") or 0.0),
        ",".join(sorted(str(s) for s in (d.get("source_turn_ids") or []))),
    )


def _select_candidates_from_decisions(
    decisions: List[Dict[str, Any]],
    max_examples: int = 3,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return up to ``max_examples`` (outcome, decision) tuples.

    Strategy (sparse-coverage tolerant):

    1. Try to fill one slot per target outcome bucket
       (``approval`` / ``deferral`` / ``action_required``), choosing the
       grounded-then-highest-confidence decision in each bucket.
    2. Fill any remaining slots with the next-best decisions from any
       target-outcome bucket, regardless of outcome (no duplicates).
    3. Returns fewer than ``max_examples`` only when fewer decisions
       with target outcomes exist on disk.

    A transcript that only produced ``action_required`` decisions now
    yields multiple action_required candidates (up to ``max_examples``)
    rather than a single candidate plus two empty buckets. The caller
    is responsible for surfacing which buckets are missing — this
    function never pads with placeholders.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {o: [] for o in TARGET_OUTCOMES}
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        outcome = d.get("decision_outcome")
        if outcome in buckets:
            buckets[outcome].append(d)
    for outcome in TARGET_OUTCOMES:
        buckets[outcome].sort(key=_decision_sort_key)

    selected: List[Tuple[str, Dict[str, Any]]] = []
    used_ids: set[int] = set()

    # Pass 1: one per bucket.
    for outcome in TARGET_OUTCOMES:
        items = buckets[outcome]
        if items:
            chosen = items[0]
            selected.append((outcome, chosen))
            used_ids.add(id(chosen))
        if len(selected) >= max_examples:
            return selected[:max_examples]

    # Pass 2: fill remaining slots with best available regardless of bucket.
    remaining = max_examples - len(selected)
    if remaining > 0:
        leftover: List[Tuple[str, Dict[str, Any]]] = []
        for outcome in TARGET_OUTCOMES:
            for d in buckets[outcome]:
                if id(d) in used_ids:
                    continue
                leftover.append((outcome, d))
        leftover.sort(key=lambda t: _decision_sort_key(t[1]))
        selected.extend(leftover[:remaining])

    return selected


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


def _existing_examples(artifact_path: Path) -> List[Dict[str, Any]]:
    if not artifact_path.is_file():
        return []
    try:
        doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(doc, dict):
        return []
    examples = doc.get("examples") or []
    return [e for e in examples if isinstance(e, dict)]


def _has_only_placeholders(examples: List[Dict[str, Any]]) -> bool:
    if not examples:
        return False
    return all(
        str(ex.get("example_id", "")).startswith(PLACEHOLDER_ID_PREFIX)
        for ex in examples
    )


def _merge_with_existing(
    new_candidates: List[Dict[str, Any]],
    existing_examples: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge new candidates with existing verified examples.

    Rules:

    - Keep ALL existing examples where ``verified=True`` (operator
      approved — must never be overwritten by an automated run).
    - Drop ALL existing examples where ``verified`` is anything else
      (False, missing, or any non-True value).
    - Placeholders (``phase-v-placeholder-*``) are always treated as
      unverified, regardless of their flag.
    - Result is ``[verified_existing] + [new_real_candidates]``.

    The output list NEVER contains placeholder ids. If any survive the
    caller MUST refuse to exit 0 and write the NEEDS_REAL_EXAMPLES
    marker — this function does not assert it because callers may
    legitimately invoke it with an empty new_candidates list and let
    the post-write safety check catch the leak.
    """
    verified_existing = [
        e for e in existing_examples
        if e.get("verified") is True
        and not str(e.get("example_id", "")).startswith(PLACEHOLDER_ID_PREFIX)
    ]
    return verified_existing + list(new_candidates)


def _write_needs_real_examples(
    path: Path, source_id: str, reason: str, diagnostics: List[str]
) -> None:
    """Drop a NEEDS_REAL_EXAMPLES.md marker next to the few-shot artifact.

    The marker is written whenever the script cannot replace placeholder
    examples with real ones (no extraction artifact, zero decisions,
    zero target outcomes). It is the artifact-on-disk evidence the
    operator needs when a mobile workflow runs to completion but the
    placeholder file is still in place. The script ALSO exits non-zero;
    the marker is durable in addition to the non-zero exit.
    """
    lines: List[str] = [
        "# Few-shot examples still contain placeholders",
        "",
        f"- Source meeting: `{source_id}`",
        f"- Generated at: `{_now_iso()}`",
        f"- Reason: {reason}",
        "",
        "## What this means",
        "",
        "`decision_examples_v1.json` was NOT updated. The artifact still",
        "contains `phase-v-placeholder-*` examples that ship with the",
        "repo. The extraction prompt loader filters to `verified: true`",
        "examples only, so placeholders never reach the model — but the",
        "operator running the validate-and-baseline workflow MUST replace",
        "them with real, reviewed decisions before that pipeline can",
        "promote to a production baseline.",
        "",
        "## Diagnostics",
        "",
    ]
    for line in diagnostics:
        lines.append(f"- {line}")
    lines.extend([
        "",
        "## Next steps",
        "",
        "1. Confirm a `meeting_extraction` artifact exists for this",
        "   source id under `<data-lake>/store/artifacts/extractions/`.",
        "2. If the extraction is missing, run the extraction pipeline",
        "   for this source id first.",
        "3. If the extraction exists but has zero decisions in the",
        "   `approval` / `deferral` / `action_required` outcome buckets,",
        "   inspect the extraction run for off-topic-rate spikes.",
        "4. Re-run `scripts/select_few_shot_examples.py` once the",
        "   extraction artifact carries real decisions.",
        "",
        "This file is overwritten on every run and removed when real",
        "examples land.",
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

    # Strip whitespace from every string CLI arg. Mobile workflow_dispatch
    # inputs frequently arrive with a trailing space pasted from a phone
    # keyboard; an unstripped source_id then fails exact-string matches
    # against artifacts on disk.
    for _attr in vars(args):
        _val = getattr(args, _attr)
        if isinstance(_val, str):
            setattr(args, _attr, _val.strip())

    data_lake = Path(args.data_lake).resolve()
    if not data_lake.is_dir():
        print(f"error: --data-lake does not exist: {data_lake}", file=sys.stderr)
        return 1

    artifact_path = (
        Path(args.artifact_path) if args.artifact_path
        else data_lake / DEFAULT_FEW_SHOT_PATH
    )
    needs_real_path = (
        Path(args.artifact_path).parent / "NEEDS_REAL_EXAMPLES.md"
        if args.artifact_path
        else data_lake / NEEDS_REAL_EXAMPLES_RELPATH
    )
    existing = _existing_examples(artifact_path)
    only_placeholders = _has_only_placeholders(existing)

    try:
        extraction = _load_meeting_extraction(data_lake, args.source_id)
    except ArtifactValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _write_needs_real_examples(
            needs_real_path,
            args.source_id,
            reason=f"meeting_extraction artifact failed schema validation: {exc}",
            diagnostics=[
                f"data-lake: `{data_lake}`",
                f"existing_examples_are_placeholders: `{only_placeholders}`",
                "remediation: re-run extraction with `force=true` so the "
                "writer regenerates the artifact against the current schema.",
            ],
        )
        return 1
    resolved = _resolve_source_artifact_id(data_lake, args.source_id)
    print(
        f"diag: source_id={args.source_id!r} "
        f"resolved_source_artifact_id={resolved!r}"
    )
    if extraction is None:
        hint = (
            f" (resolved source_artifact_id={resolved})" if resolved
            else " (no source_record.json found under "
            f"{data_lake}/store/processed/<family>/{args.source_id}/)"
        )
        msg = (
            f"error: no meeting_extraction artifact found for "
            f"source_id={args.source_id}{hint} under {data_lake}. "
            f"Scanned {data_lake}/store/artifacts/extractions/ for files "
            f"with matching source_id or source_artifact_id."
        )
        print(msg, file=sys.stderr)
        _write_needs_real_examples(
            needs_real_path,
            args.source_id,
            reason="no meeting_extraction artifact found",
            diagnostics=[
                f"data-lake: `{data_lake}`",
                f"resolved source_artifact_id: `{resolved}`",
                f"existing_examples_are_placeholders: `{only_placeholders}`",
                "scanned: `store/artifacts/extractions/*.json` and "
                "`store/processed/meetings/<source_id>/`",
            ],
        )
        return 1

    decisions = extraction.get("decisions") or []
    if not isinstance(decisions, list):
        decisions = []

    outcome_counts = Counter(
        d.get("decision_outcome") for d in decisions if isinstance(d, dict)
    )
    print(f"diag: extraction decisions={len(decisions)}")
    print(f"diag: outcome distribution={dict(outcome_counts)}")

    chosen = _select_candidates_from_decisions(
        decisions, max_examples=max(1, args.max_examples)
    )
    if not chosen:
        msg = (
            f"error: no decisions with target outcomes "
            f"({', '.join(TARGET_OUTCOMES)}) found in the extraction "
            f"for source_id={args.source_id}. "
            f"Scanned {len(decisions)} decisions; "
            f"outcome distribution: {dict(outcome_counts)}"
        )
        print(msg, file=sys.stderr)
        _write_needs_real_examples(
            needs_real_path,
            args.source_id,
            reason=(
                "extraction artifact contains no decisions with target "
                f"outcomes ({', '.join(TARGET_OUTCOMES)})"
            ),
            diagnostics=[
                f"total decisions in extraction: `{len(decisions)}`",
                f"outcome distribution: `{dict(outcome_counts)}`",
                f"target outcomes: `{list(TARGET_OUTCOMES)}`",
                f"existing_examples_are_placeholders: `{only_placeholders}`",
            ],
        )
        return 2

    new_examples = [
        _decision_to_example(d, args.source_id, outcome) for outcome, d in chosen
    ]
    selected_outcomes = [
        ex["expected_output"].get("decision_outcome") for ex in new_examples
    ]
    print(f"diag: selected {len(new_examples)} candidates ({selected_outcomes})")

    # Sparse-coverage diagnostic: callers running on a single transcript
    # often see only one or two outcome buckets populated. Surface which
    # buckets are missing so the operator knows the file is incomplete
    # even though the script succeeded.
    filled_buckets = {outcome for outcome, _ in chosen}
    missing_buckets = set(TARGET_OUTCOMES) - filled_buckets
    if missing_buckets:
        print(
            f"diag: missing outcome buckets (no decisions in extraction): "
            f"{sorted(missing_buckets)}"
        )
        print(
            f"diag: filled {len(new_examples)} slot(s) from available decisions"
        )

    # Merge with verified-existing — preserves operator-approved examples
    # but never preserves placeholders or unverified rows.
    examples = _merge_with_existing(new_examples, existing)
    existing_audit: List[Dict[str, Any]] = []
    if artifact_path.is_file():
        try:
            old = json.loads(artifact_path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                existing_audit = list(old.get("audit_log") or [])
        except (OSError, json.JSONDecodeError):
            existing_audit = []

    # Only audit the NEW selections — verified-existing examples already
    # have their own audit_log entries from prior runs.
    for ex in new_examples:
        existing_audit.append({
            "action": "selected",
            "example_id": ex["example_id"],
            "at": _now_iso(),
            "actor": "scripts/select_few_shot_examples.py",
            "notes": f"outcome={ex['expected_output'].get('decision_outcome')}",
        })

    # Artifact-level verified flag: true iff every example is verified.
    # Matches verify_example.py's roll-up so the two scripts agree.
    artifact_verified = bool(examples) and all(
        ex.get("verified") is True for ex in examples
    )

    artifact = {
        "artifact_type": "decision_few_shot_examples",
        "schema_version": "1.0.0",
        "examples_version": "1",
        "extraction_type": "decision",
        "verified": artifact_verified,
        "created_at": _now_iso(),
        "examples": examples,
        "audit_log": existing_audit,
    }
    _write_artifact(artifact_path, artifact)

    # Final safety check: ensure no phase-v-placeholder ids leaked through.
    # _decision_to_example mints fresh UUIDs so this should be impossible —
    # but the script's contract is "NEVER exit 0 with placeholders intact",
    # so verify directly off disk before returning success.
    post_write = _existing_examples(artifact_path)
    leftover_placeholders = [
        ex.get("example_id") for ex in post_write
        if str(ex.get("example_id", "")).startswith(PLACEHOLDER_ID_PREFIX)
    ]
    if leftover_placeholders:
        msg = (
            f"error: placeholders still present after write "
            f"({leftover_placeholders}). Refusing to exit 0."
        )
        print(msg, file=sys.stderr)
        _write_needs_real_examples(
            needs_real_path,
            args.source_id,
            reason="placeholder ids survived the write step",
            diagnostics=[
                f"leftover_placeholders: `{leftover_placeholders}`",
                f"selected_count: `{len(examples)}`",
            ],
        )
        return 3

    # Real examples landed — clear the durable warning marker if present.
    if needs_real_path.is_file():
        try:
            needs_real_path.unlink()
        except OSError:
            pass

    checklist_path = (
        Path(args.artifact_path).parent / "REVIEW_CHECKLIST.md"
        if args.artifact_path
        else data_lake / REVIEW_CHECKLIST_RELPATH
    )
    _write_review_checklist(
        checklist_path,
        args.source_id,
        new_examples,
        transcript_hint=str(
            data_lake / "store" / "processed" / "meetings" / args.source_id
        ),
    )

    preserved_count = len(examples) - len(new_examples)
    print(
        f"wrote {len(new_examples)} new candidate(s) "
        f"(+ {preserved_count} verified-existing preserved) "
        f"-> {artifact_path}\n"
        f"checklist -> {checklist_path}\n"
        "NEXT: review each example, then run scripts/verify_example.py "
        "to mark verified=true.",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
