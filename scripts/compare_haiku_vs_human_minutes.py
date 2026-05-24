#!/usr/bin/env python3
"""Compare an extraction artifact against the human-minutes gold standard.

Reads:
  * The promoted ``meeting_minutes`` extraction artifact for one source
    (Haiku, Opus, or Sonnet — any producer).
  * The ``human_minutes`` artifact (NTIA-authored gold standard,
    produced by scripts/ingest_human_minutes.py) for the same source.

Writes a ``human_minutes_comparison`` artifact at::

    <data-lake>/store/processed/meetings/<source_id>/human_minutes_comparison__<source_id>.json

ZERO LLM calls. The match function is ``difflib.SequenceMatcher.ratio``
over case-folded text; a pair is a match when the ratio is at least
``--match-threshold`` (default 0.45). Determinism is by construction —
the same artifacts in the same order on disk produce the same scores.

A "true positive" is an extraction item that covers SOME human item.
A "false positive" is an extraction item that covers NO human item.
A "false negative" is a human item that NO extraction item covers.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

COMPARISON_ARTIFACT_TYPE = "human_minutes_comparison"
COMPARISON_SCHEMA_VERSION = "1.0.0"
DEFAULT_MATCH_THRESHOLD = 0.45

# Map from a human-minutes item type to the set of extraction array
# names that may "cover" it. Discussion items capture questions /
# decisions / positions; action items map to action-shaped extraction
# arrays; next-steps are forward-looking actions or scheduled events.
_HUMAN_TYPE_TO_EXTRACTION_ARRAYS: dict[str, tuple[str, ...]] = {
    "discussion": (
        "decisions",
        "open_questions",
        "claims",
        "position_statement",
        "issue_registry_entry",
        "dissent_or_objection",
        "agenda_item",
        "external_stakeholder_input",
        "topics",
    ),
    "action": (
        "action_items",
        "commitments",
    ),
    "next_step": (
        "action_items",
        "commitments",
        "scheduled_events",
    ),
}

# A claim-shaped item is an extraction item from the meeting_minutes
# extraction arrays. We count them collectively for the
# over_extraction_ratio metric.
_CLAIM_SHAPED_ARRAYS: tuple[str, ...] = (
    "decisions",
    "action_items",
    "open_questions",
    "commitments",
    "risks",
    "cross_references",
    "claims",
    "issue_registry_entry",
    "position_statement",
    "dissent_or_objection",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
    "sentiment_indicators",
    "meeting_phases",
    "attendees",
    "topics",
)

# Object-form text resolution order. Mirrors the existing comparison
# infrastructure (``compare_opus_haiku._GROUND_TRUTH_TEXT_FIELDS``) so
# an object-form decision/claim resolves to the same string the rest of
# the pipeline reads.
_OBJECT_TEXT_FIELDS: tuple[str, ...] = (
    "text",
    "decision_text",
    "question_text",
    "commitment_text",
    "risk_text",
    "claim_text",
    "reference_text",
    "ref_text",
    "parameter_name",
    "position_text",
    "objection_text",
    "input_text",
    "ruling_text",
    "text_preview",
    "term",
    "name",
    "title",
    "phase_name",
    "reference",
    "action",
)


class ComparisonError(RuntimeError):
    """Fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def text_similarity(a: str, b: str) -> float:
    """Normalized similarity in [0.0, 1.0]. Case-folded, whitespace-normalized."""
    a_n = _normalize_for_match(a)
    b_n = _normalize_for_match(b)
    if not a_n or not b_n:
        return 0.0
    return SequenceMatcher(None, a_n, b_n).ratio()


def _normalize_for_match(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.lower().split())


def _item_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in _OBJECT_TEXT_FIELDS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # No known text field — concatenate string-valued fields so
        # the matcher has something to chew on rather than 0.0.
        parts = [
            str(v).strip()
            for v in item.values()
            if isinstance(v, str) and v.strip()
        ]
        return " ".join(parts)
    return ""


def _human_item_text(item_type: str, item: dict[str, Any]) -> str:
    """Resolve the comparable string for one human-minutes item.

    Per the comparison spec, discussion items match against
    ``question_topic`` only (NOT the response, which is the discussion
    body and is much longer — concatenating dilutes the similarity
    ratio and hides real matches). Action items and next steps match
    against ``text``.
    """
    if item_type == "discussion":
        return item.get("question_topic", "").strip()
    return item.get("text", "").strip()


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(
            "unreadable_artifact", f"{path}: {exc}"
        ) from exc


def find_human_minutes(data_lake: Path, source_id: str) -> Path:
    meet_dir = data_lake / "store" / "processed" / "meetings" / source_id
    candidate = meet_dir / f"human_minutes__{source_id}.json"
    if not candidate.is_file():
        # Tolerate any human_minutes__*.json in the directory.
        matches = sorted(meet_dir.glob("human_minutes__*.json"))
        if not matches:
            raise ComparisonError(
                "missing_human_minutes",
                f"no human_minutes__*.json under {meet_dir}",
            )
        return matches[0]
    return candidate


def find_extraction_artifact(
    data_lake: Path,
    source_id: str,
    *,
    run_id: str | None = None,
    model_token: str | None = None,
) -> Path:
    meet_dir = data_lake / "store" / "processed" / "meetings" / source_id
    if not meet_dir.is_dir():
        raise ComparisonError(
            "missing_meeting_dir",
            f"{meet_dir} not on disk",
        )

    candidates = sorted(meet_dir.glob("meeting_minutes__*.json"))
    if run_id:
        candidates = [p for p in candidates if run_id in p.name]
    if not candidates:
        raise ComparisonError(
            "missing_extraction_artifact",
            f"no meeting_minutes__*.json under {meet_dir} matching run_id={run_id!r}",
        )

    if model_token:
        filtered: list[Path] = []
        for path in candidates:
            artifact = _load_json(path)
            prov = (artifact.get("payload") or {}).get("provenance") or {}
            mid = (prov.get("model_id") or "").lower()
            if model_token.lower() in mid:
                filtered.append(path)
        if not filtered:
            raise ComparisonError(
                "missing_extraction_artifact",
                f"no meeting_minutes__*.json under {meet_dir} with model_id "
                f"containing {model_token!r}",
            )
        candidates = filtered

    # Prefer the most-recently-modified candidate so re-runs target the
    # latest extraction without requiring a run_id.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _extraction_items_by_type(
    extraction_artifact: dict[str, Any],
) -> dict[str, list[Any]]:
    payload = extraction_artifact.get("payload") or {}
    by_type: dict[str, list[Any]] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            by_type[key] = value
    return by_type


def _flatten_human_items(
    human: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for d in human.get("discussion_items") or []:
        items.append(("discussion", d))
    for a in human.get("action_items") or []:
        items.append(("action", a))
    for s in human.get("next_steps") or []:
        # ``next_steps`` are strings per schema; wrap to keep the
        # ``dict`` invariant on the consumer side.
        items.append(("next_step", {"text": s}))
    return items


def compare(
    *,
    extraction_artifact: dict[str, Any],
    human_minutes: dict[str, Any],
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    extraction_by_type = _extraction_items_by_type(extraction_artifact)

    # Total extraction items: count items across ALL claim-shaped arrays.
    extraction_total_items = sum(
        len(extraction_by_type.get(t, [])) for t in _CLAIM_SHAPED_ARRAYS
    )
    extraction_claim_shaped_items = sum(
        len(extraction_by_type.get(t, []))
        for t in (
            "decisions",
            "action_items",
            "open_questions",
            "claims",
            "commitments",
            "issue_registry_entry",
            "position_statement",
            "dissent_or_objection",
            "agenda_item",
        )
    )

    human_items = _flatten_human_items(human_minutes)
    human_discussion_count = sum(1 for t, _ in human_items if t == "discussion")
    human_action_count = sum(1 for t, _ in human_items if t == "action")
    human_next_count = sum(1 for t, _ in human_items if t == "next_step")
    total_human_items = len(human_items)

    matched_pairs: list[dict[str, Any]] = []
    unmatched_human: list[dict[str, Any]] = []
    covered_extraction_ids: set[tuple[str, int]] = set()

    for h_type, h_item in human_items:
        h_text = _human_item_text(h_type, h_item)
        candidate_arrays = _HUMAN_TYPE_TO_EXTRACTION_ARRAYS.get(h_type, ())
        best: tuple[float, str, int, Any] | None = None
        for arr_name in candidate_arrays:
            for idx, e_item in enumerate(extraction_by_type.get(arr_name, [])):
                e_text = _item_text(e_item)
                sim = text_similarity(h_text, e_text)
                if sim >= match_threshold and (best is None or sim > best[0]):
                    best = (sim, arr_name, idx, e_item)
        if best is None:
            unmatched_human.append({"type": h_type, "text": h_text, "item": h_item})
        else:
            sim, arr_name, idx, e_item = best
            covered_extraction_ids.add((arr_name, idx))
            matched_pairs.append(
                {
                    "human_item": {"type": h_type, "text": h_text},
                    "extraction_item": {
                        "type": arr_name,
                        "text": _item_text(e_item),
                        "similarity": round(sim, 4),
                    },
                }
            )

    # Recall: how many human items found a covering extraction.
    true_positives = len(matched_pairs)
    false_negatives = len(unmatched_human)
    # Precision: how many extraction items covered at least one human.
    # An extraction item is a TP if its (array, idx) is in the covered set.
    extraction_tp_count = len(covered_extraction_ids)
    false_positives = extraction_total_items - extraction_tp_count

    precision = (
        (extraction_tp_count / extraction_total_items)
        if extraction_total_items
        else 0.0
    )
    recall = (
        (true_positives / total_human_items) if total_human_items else 0.0
    )
    f1 = (
        (2 * precision * recall / (precision + recall))
        if (precision + recall)
        else 0.0
    )

    over_extraction_ratio = (
        (extraction_total_items / total_human_items)
        if total_human_items
        else None
    )

    # Cap the sample of unmatched extraction items so the artifact stays
    # readable (the spec calls for first 20 only).
    unmatched_extraction_sample: list[dict[str, Any]] = []
    for arr_name in _CLAIM_SHAPED_ARRAYS:
        items = extraction_by_type.get(arr_name, [])
        for idx, e_item in enumerate(items):
            if (arr_name, idx) in covered_extraction_ids:
                continue
            unmatched_extraction_sample.append(
                {"type": arr_name, "text": _item_text(e_item)}
            )
            if len(unmatched_extraction_sample) >= 20:
                break
        if len(unmatched_extraction_sample) >= 20:
            break

    return {
        "match_threshold": match_threshold,
        "human_discussion_items_count": human_discussion_count,
        "human_action_items_count": human_action_count,
        "human_next_steps_count": human_next_count,
        "total_human_items": total_human_items,
        "extraction_total_items": extraction_total_items,
        "extraction_claim_shaped_items": extraction_claim_shaped_items,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision_vs_human": round(precision, 4),
        "recall_vs_human": round(recall, 4),
        "f1_vs_human": round(f1, 4),
        "over_extraction_ratio": (
            round(over_extraction_ratio, 4)
            if over_extraction_ratio is not None
            else None
        ),
        "matched_pairs": matched_pairs,
        "unmatched_human_items": unmatched_human,
        "unmatched_extraction_items_sample": unmatched_extraction_sample,
    }


def build_artifact(
    *,
    source_id: str,
    extraction_artifact_path: Path,
    human_minutes_path: Path,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_type": COMPARISON_ARTIFACT_TYPE,
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "source_id": source_id,
        "compared_at": _now_utc_iso(),
        "extraction_artifact": extraction_artifact_path.name,
        "human_minutes_artifact": human_minutes_path.name,
        **metrics,
    }


def write_comparison_artifact(
    artifact: dict[str, Any], *, data_lake: Path, source_id: str
) -> Path:
    out_dir = data_lake / "store" / "processed" / "meetings" / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"human_minutes_comparison__{source_id}.json"
    out_path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a meeting_minutes extraction artifact against the "
            "human-authored NTIA minutes for the same source. Writes a "
            "human_minutes_comparison artifact. ZERO LLM calls."
        )
    )
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument(
        "--match-threshold",
        type=float,
        default=DEFAULT_MATCH_THRESHOLD,
        help="Minimum SequenceMatcher ratio to count a pair as a match.",
    )
    parser.add_argument(
        "--extraction-run-id",
        default=None,
        help="Substring filter on the meeting_minutes filename; selects a "
             "specific extraction run rather than the most recent.",
    )
    parser.add_argument(
        "--model-token",
        default=None,
        help="Restrict to extraction artifacts whose provenance.model_id "
             "contains this token (e.g. 'haiku', 'opus', 'sonnet').",
    )
    parser.add_argument(
        "--print-scores",
        action="store_true",
        help="Print the human-minutes comparison metrics to stderr.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the comparison but do not write the artifact to disk.",
    )
    args = parser.parse_args(argv)

    data_lake = Path(args.data_lake)
    if not data_lake.is_absolute():
        data_lake = (_REPO_ROOT / data_lake).resolve()

    try:
        human_path = find_human_minutes(data_lake, args.source_id)
        extraction_path = find_extraction_artifact(
            data_lake,
            args.source_id,
            run_id=args.extraction_run_id,
            model_token=args.model_token,
        )
    except ComparisonError as exc:
        print(json.dumps({"status": "halt", "reason": exc.reason,
                          "detail": exc.detail}))
        return 1

    human_minutes = _load_json(human_path)
    extraction_artifact = _load_json(extraction_path)

    try:
        validate_artifact(human_minutes, "human_minutes", str(human_path))
    except ArtifactValidationError as exc:
        print(json.dumps({"status": "halt", "reason": "invalid_human_minutes",
                          "detail": str(exc)}))
        return 1

    metrics = compare(
        extraction_artifact=extraction_artifact,
        human_minutes=human_minutes,
        match_threshold=args.match_threshold,
    )

    artifact = build_artifact(
        source_id=args.source_id,
        extraction_artifact_path=extraction_path,
        human_minutes_path=human_path,
        metrics=metrics,
    )

    if args.print_scores:
        summary = (
            f"human_items={metrics['total_human_items']} "
            f"extraction_total={metrics['extraction_total_items']} "
            f"precision={metrics['precision_vs_human']} "
            f"recall={metrics['recall_vs_human']} "
            f"f1={metrics['f1_vs_human']} "
            f"over_extraction={metrics['over_extraction_ratio']}"
        )
        print(summary, file=sys.stderr)

    if args.dry_run:
        print(json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False))
        return 0

    out_path = write_comparison_artifact(
        artifact, data_lake=data_lake, source_id=args.source_id
    )
    print(json.dumps({
        "status": "success",
        "comparison_artifact_path": str(out_path),
        "summary": {
            "precision_vs_human": metrics["precision_vs_human"],
            "recall_vs_human": metrics["recall_vs_human"],
            "f1_vs_human": metrics["f1_vs_human"],
            "over_extraction_ratio": metrics["over_extraction_ratio"],
            "total_human_items": metrics["total_human_items"],
            "extraction_total_items": metrics["extraction_total_items"],
        },
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
