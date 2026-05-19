"""Deterministic Haiku-vs-Opus extraction comparison (System 1).

Reads the Opus reference baseline and the promoted Haiku
``meeting_minutes`` artifact for one ``source_id`` and emits a
``comparison_result`` artifact with recall / precision / F1 by
extraction type.

ZERO LLM CALLS. The comparison is pure, case-insensitive,
whitespace-normalized substring matching — the SAME match rule the
``extraction_within_source_required`` eval uses. This module never
imports ``anthropic`` and never imports the LLM client, so the
"no model call in the comparison" property is verifiable by static
scan (``tests/test_compare_opus_haiku.py`` asserts it). Comparing a
regex-extractor artifact against an Opus baseline is meaningless, so
the Haiku artifact's ``provenance.produced_by`` MUST be
``meeting_minutes_llm`` or the script halts fail-closed.

Fail-closed reason codes:

* ``missing_opus_baseline``       — no opus_reference_minutes.jsonl
* ``missing_haiku_llm_output``    — no promoted meeting_minutes artifact
                                    with provenance produced_by ==
                                    "meeting_minutes_llm"
* ``invalid_haiku_artifact``      — artifact present but fails the
                                    meeting_minutes schema
* ``data_lake_not_a_directory``   — --data-lake is not a directory
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# scripts/ on sys.path so the artifact validator import works whether
# this file is run as a script or imported as a module by tests / by
# the correction miner.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

_REPO_ROOT = _SCRIPTS_DIR.parent
_MEETING_MINUTES_SCHEMA = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)

COMPARISON_ARTIFACT_TYPE = "comparison_result"
COMPARISON_SCHEMA_VERSION = "1.0.0"
HAIKU_LLM_PROVENANCE = "meeting_minutes_llm"

# Text fields tried, in priority order, when a Haiku payload item is a
# structured object. This is a LOCAL, byte-identical copy of
# ``scripts/create_opus_reference_baselines._GROUND_TRUTH_TEXT_FIELDS``
# so ``_item_text`` resolves a structured item to the EXACT same string
# the Opus baseline producer's ``extract_ground_truth_text`` resolved it
# to — an asymmetric reader would make the Haiku-vs-Opus diff lie (the
# exact bug that read 0 Haiku items off an artifact whose object-form
# ``decisions`` had been extracted and grounded). The canonical
# extraction prompt lets the model return a structured object for ANY
# type (``decisions`` in particular arrive as
# ``{"text","verb","stakeholders","confidence","rationale"}``), so the
# reader must NOT be keyed on a per-type field. Kept LOCAL (never
# imported) so this module never transitively imports the LLM client and
# the zero-LLM property stays a static fact;
# ``tests/test_compare_opus_haiku.py`` asserts this tuple stays
# byte-identical to the producer's so they cannot drift.
_GROUND_TRUTH_TEXT_FIELDS = (
    "text",
    "question_text",
    "commitment_text",
    "risk_text",
    "reference_text",
    "parameter_name",
    "position_text",
    "objection_text",
    "input_text",
    "ruling_text",
    "term",
    "name",
    "title",
    "phase_name",
    "reference",
)

# Per-type maps retained ONLY as the documented cross-script mirror that
# ``tests/test_compare_opus_haiku.py`` asserts stays in sync with
# ``create_opus_reference_baselines``. They are NOT the text-resolution
# authority any more: ``_item_text`` reads structured items through the
# shared tolerant ``_GROUND_TRUTH_TEXT_FIELDS`` resolver above, exactly
# as the baseline producer's ``extract_ground_truth_text`` does, so the
# two readers are symmetric by construction. The producer treats its own
# ``_LEGACY_OBJECT_TEXT_FIELD`` the same way (retained-but-unused).
_PRIMARY_TEXT_FIELD: Dict[str, Optional[str]] = {
    "decisions": None,
    "action_items": None,
    "open_questions": None,
    "commitments": "commitment_text",
    "risks": "risk_text",
    "cross_references": "ref_text",
    "attendees": "name",
    "topics": "title",
    "regulatory_references": "reference_text",
    "technical_parameters": "value",
    "named_artifacts": "name",
    "scheduled_events": "title",
    "claims": "claim_text",
    "sentiment_indicators": "text_preview",
    "meeting_phases": "phase_name",
    # 1.3.0 additions — MUST stay byte-equal to the baseline producer's
    # map (asserted by tests/test_compare_opus_haiku.py); an asymmetric
    # reader would make the Haiku-vs-Opus diff lie.
    "issue_registry_entry": "title",
    "position_statement": "position_text",
    "dissent_or_objection": "objection_text",
    "agenda_item": "title",
    "precedent_reference": "reference_text",
    "external_stakeholder_input": "input_text",
    "glossary_definition": "term",
    "procedural_ruling": "ruling_text",
}

# Vestigial cross-script mirror (see the block comment above
# ``_PRIMARY_TEXT_FIELD``): kept byte-equal to the producer's map so the
# sync assertion holds; no longer consulted by ``_item_text``.
_LEGACY_OBJECT_TEXT_FIELD: Dict[str, str] = {
    "action_items": "action",
    "open_questions": "question_text",
}


class ComparisonError(RuntimeError):
    """Fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Match function — deterministic and SYMMETRIC by construction.
# --------------------------------------------------------------------------
def text_match(a: str, b: str) -> bool:
    """Case-insensitive, whitespace-normalized substring match.

    Symmetric: ``a in b or b in a`` is invariant under swapping ``a``
    and ``b`` (the disjunction is commutative and each operand is the
    mirror of the other). Same rule as the
    ``extraction_within_source_required`` eval. No embeddings, no fuzzy
    similarity — deterministic text only.
    """
    a_norm = " ".join((a or "").lower().split())
    b_norm = " ".join((b or "").lower().split())
    if not a_norm or not b_norm:
        return False
    return a_norm in b_norm or b_norm in a_norm


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def extraction_types() -> List[str]:
    """The extraction types, derived from the meeting_minutes schema.

    Every array property except ``grounding`` (Phase Y meta, not a
    content category). Deriving from the schema means a new type added
    there is automatically compared — no parallel list to drift.
    """
    try:
        schema = json.loads(
            _MEETING_MINUTES_SCHEMA.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(
            "missing_extraction_schema",
            f"cannot read meeting_minutes schema at "
            f"{_MEETING_MINUTES_SCHEMA}: {exc}",
        ) from exc
    props = schema.get("properties", {})
    types: List[str] = []
    for key, spec in props.items():
        if key == "grounding":
            continue
        if isinstance(spec, dict) and spec.get("type") == "array":
            types.append(key)
    return types


def _item_text(etype: str, item: Any) -> str:
    """Comparable string for one Haiku payload item.

    Mirrors ``create_opus_reference_baselines.extract_ground_truth_text``
    EXACTLY (type-agnostic tolerant resolution) so the Haiku reader and
    the Opus baseline producer can never read the same item differently
    — an asymmetric reader makes the diff lie. Resolution order:

    1. A plain string is returned as-is (whitespace-stripped; the Opus
       side is stripped by ``opus_items_by_type`` and ``text_match``
       whitespace-normalizes, so the strip is immaterial to matching).
    2. For a dict, the first present, non-empty *string* field from
       ``_GROUND_TRUTH_TEXT_FIELDS`` (priority order) wins — so an
       object-form ``decisions`` item resolves on ``text`` exactly like
       the producer, instead of being dropped as ``''``.
    3. Else the first non-empty string value anywhere in the dict.
    4. Else (no string content / a non-dict, non-string item) ``str()``
       of the item — the producer's never-drop fallback; mirrored so a
       pathological item is read identically on both sides rather than
       being silently dropped on only the Haiku side (which would itself
       be the asymmetry this fix removes).

    ``etype`` is accepted for call-site symmetry and a future per-type
    override seam; the resolution is deliberately type-agnostic because
    the canonical extraction prompt's object form is.
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for field in _GROUND_TRUTH_TEXT_FIELDS:
            val = item.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in item.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    return str(item).strip()


# --------------------------------------------------------------------------
# Loaders.
# --------------------------------------------------------------------------
def _meeting_dir(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake / "store" / "processed" / "meetings" / source_id
    )


def _opus_baseline_path(data_lake: Path, source_id: str) -> Path:
    """Path to the Opus reference baseline JSONL for one source.

    Single source of truth for the path so the ``--print-inputs`` debug
    readout cannot drift from the loader that actually reads it. Pure
    path construction — not comparison logic.
    """
    return (
        _meeting_dir(data_lake, source_id)
        / "reference_baselines"
        / "opus_reference_minutes.jsonl"
    )


def load_opus_baseline(
    data_lake: Path, source_id: str
) -> List[Dict[str, Any]]:
    """Read opus_reference_minutes.jsonl, or HALT missing_opus_baseline."""
    path = _opus_baseline_path(data_lake, source_id)
    if not path.is_file():
        raise ComparisonError(
            "missing_opus_baseline",
            f"no Opus reference baseline at {path}",
        )
    rows: List[Dict[str, Any]] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ComparisonError(
                "invalid_opus_baseline",
                f"non-JSON line {lineno} in {path}: {exc}",
            ) from exc
        if not isinstance(rec, dict):
            raise ComparisonError(
                "invalid_opus_baseline",
                f"line {lineno} in {path} is "
                f"{type(rec).__name__}, expected an object",
            )
        # Fail-closed: a baseline row missing extraction_type or
        # ground_truth_text would silently shrink the recall
        # denominator (making Haiku look better than it is). A drifted
        # baseline halts rather than inflating the metric.
        etype = rec.get("extraction_type")
        gtext = rec.get("ground_truth_text")
        if not isinstance(etype, str) or not etype.strip():
            raise ComparisonError(
                "invalid_opus_baseline",
                f"line {lineno} in {path} has no usable "
                f"extraction_type",
            )
        if not isinstance(gtext, str) or not gtext.strip():
            raise ComparisonError(
                "invalid_opus_baseline",
                f"line {lineno} in {path} has no usable "
                f"ground_truth_text",
            )
        rows.append(rec)
    return rows


def _haiku_recency_key(path: Path) -> Tuple[float, str]:
    """Order Haiku candidates oldest → newest (``max()`` picks newest).

    The selector must NOT order by filename: the on-disk filename is
    ``meeting_minutes__<artifact_id>.json`` and ``artifact_id`` is a
    content hash, so a stale all-empty run from an earlier extraction
    can sort BEFORE the current real one — the exact bug, where a
    0-array artifact named ``...67ccaa13dda9.json`` shadowed the real
    ``...eecbe9e2de04.json`` and halted the comparison at
    ``haiku_item_count == 0``. The envelope ``created_at`` is no help
    either: ``data_lake/pipeline.py`` freezes it to
    ``1970-01-01T00:00:00+00:00`` for determinism, and the
    meeting_minutes schema's ``provenance`` object declares only
    ``produced_by`` / ``phase`` (no ``created_at``), so the file's
    modification time is the only recency signal actually present.

    Key: ``(st_mtime, filename)`` — the most recently written artifact
    wins; the filename is the final, deterministic tiebreaker so two
    artifacts sharing an mtime tick still order total-deterministically
    rather than by glob/dict iteration order.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.name)


def find_haiku_artifact(
    data_lake: Path, source_id: str
) -> Tuple[Dict[str, Any], Path]:
    """Locate the promoted Haiku ``meeting_minutes`` artifact.

    Scans ``meeting_minutes__*.json`` for the source and returns the
    NEWEST artifact (latest file modification time — see
    ``_haiku_recency_key``) whose ``payload.provenance.produced_by`` is
    ``meeting_minutes_llm``. Selecting by recency rather than filename
    order is the fix for a stale all-empty earlier run shadowing the
    current real extraction.
    A regex-extractor artifact (``produced_by == "meeting_minutes"``) is
    NOT comparable against an Opus baseline, so its presence does not
    satisfy the requirement — if no LLM artifact exists the script halts
    ``missing_haiku_llm_output``. The SELECTED envelope is validated
    against the meeting_minutes schema before any field is read
    (CLAUDE.md read-path co-requirement).
    """
    mdir = _meeting_dir(data_lake, source_id)
    candidates = sorted(mdir.glob("meeting_minutes__*.json"))
    saw_non_llm = False
    llm_candidates: List[
        Tuple[Tuple[float, str], Dict[str, Any], Path]
    ] = []
    for path in candidates:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ComparisonError(
                "invalid_haiku_artifact",
                f"meeting_minutes artifact at {path} unreadable/!json: "
                f"{exc}",
            ) from exc
        if not isinstance(artifact, dict):
            continue
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            continue
        produced_by = (
            (payload.get("provenance") or {}).get("produced_by")
            if isinstance(payload.get("provenance"), dict)
            else None
        )
        if produced_by != HAIKU_LLM_PROVENANCE:
            saw_non_llm = True
            continue
        llm_candidates.append(
            (_haiku_recency_key(path), artifact, path)
        )

    if llm_candidates:
        # max() over (mtime, filename) picks the NEWEST LLM artifact.
        # Only the selected artifact is validated: a stale earlier run
        # must never be able to block the current real extraction by
        # failing schema.
        _key, artifact, path = max(llm_candidates, key=lambda c: c[0])
        payload = artifact["payload"]
        # meeting_minutes.schema.json describes the FLAT
        # ``{"artifact_type": "meeting_minutes", **payload}`` shape
        # (the exact object the in-loop strict-schema eval validates),
        # NOT the on-disk envelope. Validate that form before reading
        # any extraction field off the payload (CLAUDE.md read-path
        # co-requirement) so a drifted/garbage payload is refused here
        # instead of silently producing a meaningless diff.
        flat = {"artifact_type": "meeting_minutes", **payload}
        try:
            validate_artifact(flat, "meeting_minutes", str(path))
        except ArtifactValidationError as exc:
            raise ComparisonError(
                "invalid_haiku_artifact",
                f"meeting_minutes artifact at {path} failed schema: "
                f"{exc}",
            ) from exc
        return artifact, path

    detail = (
        f"no promoted meeting_minutes artifact with "
        f"provenance.produced_by == {HAIKU_LLM_PROVENANCE!r} under "
        f"{mdir}"
    )
    if saw_non_llm:
        detail += (
            " (a regex-extractor meeting_minutes artifact was found "
            "but comparing it against the Opus baseline is meaningless)"
        )
    raise ComparisonError("missing_haiku_llm_output", detail)


def load_gt_pairs(
    data_lake: Path, source_id: str
) -> Optional[List[Dict[str, Any]]]:
    """Read human_minutes_gt_pairs.jsonl, or None when absent.

    Absent GT is NOT a halt: the task says log and continue with GT
    metrics skipped (set to 0 with a presence flag).
    """
    path = (
        _meeting_dir(data_lake, source_id)
        / "ground_truth"
        / "human_minutes_gt_pairs.jsonl"
    )
    if not path.is_file():
        return None
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


# --------------------------------------------------------------------------
# Pure comparison core (imported and reused by the correction miner —
# NEVER reimplemented there).
# --------------------------------------------------------------------------
def opus_items_by_type(
    baseline_rows: List[Dict[str, Any]], types: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Group Opus baseline rows by extraction_type.

    Each item keeps its full record plus a ``_text`` key (the
    baseline's ``ground_truth_text``) used for matching.
    """
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in types}
    for rec in baseline_rows:
        etype = rec.get("extraction_type")
        if etype not in out:
            out.setdefault(etype, [])
        text = rec.get("ground_truth_text")
        if not isinstance(text, str) or not text.strip():
            continue
        item = dict(rec)
        item["_text"] = text.strip()
        out[etype].append(item)
    return out


def haiku_items_by_type(
    payload: Dict[str, Any], types: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Group Haiku artifact payload items by extraction_type.

    Each item is ``{"_text": <comparable string>, "item": <raw>}``.
    Items with no readable text are dropped (they cannot match and are
    not real extracted content for diff purposes).
    """
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in types}
    for etype in types:
        value = payload.get(etype)
        if not isinstance(value, list):
            continue
        for raw in value:
            text = _item_text(etype, raw)
            if not text:
                continue
            out[etype].append({"_text": text, "item": raw})
    return out


def _match_one_type(
    opus: List[Dict[str, Any]], haiku: List[Dict[str, Any]]
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Greedy one-to-one match for one extraction type.

    Deterministic (list order). Each Opus item is matched to AT MOST
    one Haiku item and each Haiku item is consumed AT MOST once, so a
    single Opus item cannot inflate TP by matching many Haiku items
    (and vice versa). Returns ``(true_positives, false_negatives,
    haiku_only)``.
    """
    matched_haiku: set[int] = set()
    true_positives = 0
    false_negatives: List[Dict[str, Any]] = []
    for o in opus:
        hit = None
        for idx, h in enumerate(haiku):
            if idx in matched_haiku:
                continue
            if text_match(o["_text"], h["_text"]):
                hit = idx
                break
        if hit is None:
            false_negatives.append(o)
        else:
            matched_haiku.add(hit)
            true_positives += 1
    haiku_only = [
        h for idx, h in enumerate(haiku) if idx not in matched_haiku
    ]
    return true_positives, false_negatives, haiku_only


def _f1(recall: float, precision: float) -> float:
    if recall + precision == 0.0:
        return 0.0
    return 2.0 * recall * precision / (recall + precision)


def _gt_recall(
    gt_pairs: List[Dict[str, Any]], candidate_texts: List[str]
) -> Tuple[int, int]:
    """(covered, total) — a GT pair is covered if any candidate text
    matches its ground_truth_text (cross-type, per the spec)."""
    total = 0
    covered = 0
    for pair in gt_pairs:
        gt_text = pair.get("ground_truth_text")
        if not isinstance(gt_text, str) or not gt_text.strip():
            continue
        total += 1
        if any(text_match(gt_text, ct) for ct in candidate_texts):
            covered += 1
    return covered, total


def compute_comparison(
    *,
    baseline_rows: List[Dict[str, Any]],
    haiku_payload: Dict[str, Any],
    gt_pairs: Optional[List[Dict[str, Any]]],
    types: List[str],
) -> Dict[str, Any]:
    """Pure metric computation. No I/O. Reused by the correction miner.

    Returns the ``summary`` / ``by_type`` / ``false_negatives`` /
    ``haiku_only_items`` / ``gt_missed`` building blocks.
    """
    opus_by_type = opus_items_by_type(baseline_rows, types)
    haiku_by_type = haiku_items_by_type(haiku_payload, types)

    by_type: Dict[str, Any] = {}
    total_tp = 0
    total_opus = 0
    total_haiku = 0
    fn_full: List[Dict[str, Any]] = []
    haiku_only_full: List[Dict[str, Any]] = []

    all_types = list(dict.fromkeys(list(types) + list(opus_by_type)))
    for etype in all_types:
        opus = opus_by_type.get(etype, [])
        haiku = haiku_by_type.get(etype, [])
        tp, fns, h_only = _match_one_type(opus, haiku)
        total_tp += tp
        total_opus += len(opus)
        total_haiku += len(haiku)
        by_type[etype] = {
            "opus_count": len(opus),
            "haiku_count": len(haiku),
            "true_positives": tp,
            "false_negatives": [
                {
                    "text_preview": o["_text"][:200],
                    "extraction_type": etype,
                }
                for o in fns
            ],
            "haiku_only": [
                {
                    "text_preview": h["_text"][:200],
                    "extraction_type": etype,
                }
                for h in h_only
            ],
        }
        for o in fns:
            full = {k: v for k, v in o.items() if k != "_text"}
            full["extraction_type"] = etype
            full["text_preview"] = o["_text"][:200]
            fn_full.append(full)
        for h in h_only:
            haiku_only_full.append(
                {
                    "extraction_type": etype,
                    "text_preview": h["_text"][:200],
                    "item": h["item"],
                }
            )

    recall = total_tp / total_opus if total_opus else 0.0
    precision = total_tp / total_haiku if total_haiku else 0.0
    f1 = _f1(recall, precision)

    gt_present = gt_pairs is not None
    gt_pairs = gt_pairs or []
    haiku_texts = [
        h["_text"] for items in haiku_by_type.values() for h in items
    ]
    opus_texts = [
        o["_text"] for items in opus_by_type.values() for o in items
    ]
    gt_cov_haiku, gt_total = _gt_recall(gt_pairs, haiku_texts)
    gt_cov_opus, _ = _gt_recall(gt_pairs, opus_texts)
    gt_missed: List[Dict[str, Any]] = []
    for pair in gt_pairs:
        gt_text = pair.get("ground_truth_text")
        if not isinstance(gt_text, str) or not gt_text.strip():
            continue
        if not any(text_match(gt_text, ht) for ht in haiku_texts):
            gt_missed.append(pair)

    gt_recall_haiku = (
        gt_cov_haiku / gt_total if gt_total else 0.0
    )
    gt_recall_opus = gt_cov_opus / gt_total if gt_total else 0.0

    summary = {
        "total_opus_items": total_opus,
        "total_haiku_items": total_haiku,
        "true_positives": total_tp,
        "false_negatives": len(fn_full),
        "haiku_only": len(haiku_only_full),
        "gt_covered_by_haiku": gt_cov_haiku,
        "gt_missed_by_haiku": len(gt_missed),
        "gt_covered_by_opus": gt_cov_opus,
        "haiku_recall_vs_opus": recall,
        "haiku_precision_vs_opus": precision,
        "haiku_f1_vs_opus": f1,
        "gt_recall_haiku": gt_recall_haiku,
        "gt_recall_opus": gt_recall_opus,
    }
    return {
        "summary": summary,
        "by_type": by_type,
        "false_negatives": fn_full,
        "haiku_only_items": haiku_only_full,
        "gt_missed": gt_missed,
        "gt_pairs_present": gt_present,
    }


# --------------------------------------------------------------------------
# Artifact + eval_history + summary table.
# --------------------------------------------------------------------------
def _haiku_run_id(artifact: Dict[str, Any]) -> str:
    payload = artifact.get("payload") or {}
    prov = payload.get("provenance") or {}
    for key in ("run_id", "trace_id"):
        v = prov.get(key)
        if isinstance(v, str) and v:
            return v
    v = artifact.get("trace_id")
    return v if isinstance(v, str) and v else ""


def build_comparison_artifact(
    *,
    source_id: str,
    haiku_artifact: Dict[str, Any],
    baseline_rows: List[Dict[str, Any]],
    metrics: Dict[str, Any],
    compared_at: str,
) -> Dict[str, Any]:
    opus_model_id = ""
    for rec in baseline_rows:
        mid = rec.get("model_id")
        if isinstance(mid, str) and mid:
            opus_model_id = mid
            break
    return {
        "artifact_type": COMPARISON_ARTIFACT_TYPE,
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "source_id": source_id,
        "haiku_run_id": _haiku_run_id(haiku_artifact),
        "opus_model_id": opus_model_id,
        "compared_at": compared_at,
        "gt_pairs_present": metrics["gt_pairs_present"],
        "summary": metrics["summary"],
        "by_type": metrics["by_type"],
        "false_negatives": metrics["false_negatives"],
        "haiku_only_items": metrics["haiku_only_items"],
        "gt_missed": metrics["gt_missed"],
    }


def _comparison_out_path(
    data_lake: Path, source_id: str, timestamp: str
) -> Path:
    safe_ts = timestamp.replace(":", "").replace("+", "")
    return (
        _meeting_dir(data_lake, source_id)
        / "comparisons"
        / f"haiku_vs_opus_{safe_ts}.json"
    )


def _append_eval_history(
    data_lake: Path, source_id: str, row: Dict[str, Any]
) -> Path:
    """APPEND one row to eval_history.jsonl — existing rows untouched.

    Opened in append mode so no prior byte is rewritten; the comparison
    row is purely additive to whatever the LLM workflow projection or a
    prior comparison already wrote.
    """
    path = _meeting_dir(data_lake, source_id) / "eval_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def render_summary_table(
    metrics: Dict[str, Any], types: List[str]
) -> str:
    by_type = metrics["by_type"]
    rows = [
        "Type              | Opus | Haiku | TP | FN | Haiku-only",
        "------------------+------+-------+----+----+-----------",
    ]
    seen = list(
        dict.fromkeys(list(types) + list(by_type.keys()))
    )
    tot_o = tot_h = tot_tp = tot_fn = tot_ho = 0
    for etype in seen:
        bt = by_type.get(etype)
        if not bt:
            continue
        o = bt["opus_count"]
        h = bt["haiku_count"]
        tp = bt["true_positives"]
        fn = len(bt["false_negatives"])
        ho = len(bt["haiku_only"])
        if o == 0 and h == 0:
            continue
        tot_o += o
        tot_h += h
        tot_tp += tp
        tot_fn += fn
        tot_ho += ho
        rows.append(
            f"{etype:<17} | {o:<4} | {h:<5} | {tp:<2} | {fn:<2} | {ho}"
        )
    rows.append(
        "------------------+------+-------+----+----+-----------"
    )
    rows.append(
        f"{'TOTAL':<17} | {tot_o:<4} | {tot_h:<5} | {tot_tp:<2} | "
        f"{tot_fn:<2} | {tot_ho}"
    )
    s = metrics["summary"]
    rows.append("")
    rows.append(
        f"Haiku recall vs Opus:    "
        f"{s['haiku_recall_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"Haiku precision vs Opus: "
        f"{s['haiku_precision_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"Haiku F1 vs Opus:        "
        f"{s['haiku_f1_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"GT recall (Haiku):       "
        f"{s['gt_recall_haiku'] * 100:.1f}%"
    )
    rows.append(
        f"GT recall (Opus):        "
        f"{s['gt_recall_opus'] * 100:.1f}%"
    )
    return "\n".join(rows)


def run_comparison(
    *,
    data_lake: Path,
    source_id: str,
    dry_run: bool,
    print_inputs: bool = False,
    print_scores: bool = False,
) -> Dict[str, Any]:
    """Orchestrate one comparison. Returns a summary dict; raises on halt.

    ``print_inputs`` / ``print_scores`` are observe-only debug readouts
    written to STDERR. STDOUT stays pure JSON so the workflow's
    summary/threshold steps still parse it; neither flag changes the
    comparison or what is written. ``dry_run`` runs the full comparison
    but writes no artifact and no eval_history row.
    """
    types = extraction_types()
    baseline_rows = load_opus_baseline(data_lake, source_id)
    haiku_artifact, haiku_path = find_haiku_artifact(data_lake, source_id)
    gt_pairs = load_gt_pairs(data_lake, source_id)
    if gt_pairs is None:
        print(
            "no GT pairs — skipping GT metrics", file=sys.stderr
        )

    haiku_payload = haiku_artifact.get("payload") or {}

    if print_inputs:
        opus_path = _opus_baseline_path(data_lake, source_id)
        opus_item_count = len(baseline_rows)
        haiku_item_count = sum(
            len(v)
            for v in (haiku_payload.get(t) for t in types)
            if isinstance(v, list)
        )
        print("=== print_inputs ===", file=sys.stderr)
        print(f"opus artifact path:  {opus_path}", file=sys.stderr)
        print(f"haiku artifact path: {haiku_path}", file=sys.stderr)
        print(f"opus item count:     {opus_item_count}", file=sys.stderr)
        print(
            f"haiku item count:    {haiku_item_count}", file=sys.stderr
        )
        print("=== /print_inputs ===", file=sys.stderr)

    metrics = compute_comparison(
        baseline_rows=baseline_rows,
        haiku_payload=haiku_payload,
        gt_pairs=gt_pairs,
        types=types,
    )
    compared_at = _now_utc_iso()
    artifact = build_comparison_artifact(
        source_id=source_id,
        haiku_artifact=haiku_artifact,
        baseline_rows=baseline_rows,
        metrics=metrics,
        compared_at=compared_at,
    )
    # Validate our OWN output before writing it (fail-closed: never
    # write a malformed comparison_result).
    validate_artifact(artifact, COMPARISON_ARTIFACT_TYPE)

    if print_scores:
        print("=== print_scores ===", file=sys.stderr)
        print(
            json.dumps(artifact, indent=2, sort_keys=True),
            file=sys.stderr,
        )
        print("=== /print_scores ===", file=sys.stderr)

    table = render_summary_table(metrics, types)
    # Human-readable table to STDERR so STDOUT stays pure JSON (the
    # workflow parses STDOUT for haiku_f1_vs_opus + the table).
    print(table, file=sys.stderr)

    out_path = _comparison_out_path(data_lake, source_id, compared_at)
    s = metrics["summary"]
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(artifact, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        _append_eval_history(
            data_lake,
            source_id,
            {
                "eval_type": "haiku_vs_opus_comparison",
                "haiku_recall_vs_opus": s["haiku_recall_vs_opus"],
                "haiku_precision_vs_opus": s["haiku_precision_vs_opus"],
                "haiku_f1_vs_opus": s["haiku_f1_vs_opus"],
                "gt_recall_haiku": s["gt_recall_haiku"],
                "gt_recall_opus": s["gt_recall_opus"],
                "timestamp": compared_at,
                "comparison_artifact_path": str(out_path),
            },
        )
    else:
        print("DRY RUN — artifact not written", file=sys.stderr)

    return {
        "status": "success",
        "source_id": source_id,
        "dry_run": dry_run,
        "haiku_artifact_path": str(haiku_path),
        "comparison_artifact_path": str(out_path),
        "gt_pairs_present": metrics["gt_pairs_present"],
        "summary": s,
        "table": table,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the diff; write no artifact and no eval_history.",
    )
    parser.add_argument(
        "--print-inputs",
        action="store_true",
        help=(
            "Observe-only: print opus/haiku artifact paths and item "
            "counts to STDERR before comparison runs."
        ),
    )
    parser.add_argument(
        "--print-scores",
        action="store_true",
        help=(
            "Observe-only: print the full comparison_result payload "
            "to STDERR after comparison runs."
        ),
    )
    args = parser.parse_args(argv)
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    try:
        result = run_comparison(
            data_lake=data_lake,
            source_id=args.source_id,
            dry_run=args.dry_run,
            print_inputs=args.print_inputs,
            print_scores=args.print_scores,
        )
    except ComparisonError as exc:
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
