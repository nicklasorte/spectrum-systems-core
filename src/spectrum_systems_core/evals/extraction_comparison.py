"""Phase Y.2 — Haiku-vs-ceiling alignment comparator.

Pure function. NO model calls. Deterministic for the same inputs and
``alignment_contract_version``: every float that flows from the
vectorizer (``iou``, ``cosine``) is rounded to 6 dp and every list is
sorted on a stable key, so two runs over identical artifacts produce a
byte-identical ``extraction_alignment_comparison``.

The alignment predicate is frozen in
``docs/contracts/extraction_alignment.md``. This module reads the
version off that file and REFUSES to run if the caller-supplied
``alignment_contract_version`` does not equal it (Phase Y red-team
Pass 1 #1 — a comparison computed under a drifted predicate must not
silently pass the gate).
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..artifacts import Artifact, new_artifact

ARTIFACT_TYPE = "extraction_alignment_comparison"
SCHEMA_VERSION = "1.0.0"

# Frozen predicate thresholds (mirror the contract; the contract file
# is the binding source and is validated against on every call).
IOU_THRESHOLD = 0.5
COSINE_THRESHOLD = 0.7
_ROUND_DP = 6

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "contracts"
    / "extraction_alignment.md"
)
_VERSION_RE = re.compile(
    r"alignment_contract_version[\"'`:\s]*([0-9]+\.[0-9]+\.[0-9]+)"
)


class AlignmentContractError(RuntimeError):
    """Caller's ``alignment_contract_version`` disagrees with the
    binding contract file, or the contract file is unreadable. Carries
    ``reason_code`` so a gate reads a value, not a message."""

    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


def contract_version() -> str:
    """The version declared in the binding contract file.

    A missing/unparseable contract file fails closed — the comparator
    must not invent a version and proceed.
    """
    try:
        text = _CONTRACT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise AlignmentContractError(
            f"contract_file_unreadable:{exc}",
            reason_code="contract_file_unreadable",
        ) from exc
    match = _VERSION_RE.search(text)
    if not match:
        raise AlignmentContractError(
            "contract_version_not_found_in_file",
            reason_code="contract_version_not_found",
        )
    return match.group(1)


def _iou(a: list[str], b: list[str]) -> float:
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        # Two ungrounded items are not a confident span match.
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return round(len(sa & sb) / len(union), _ROUND_DP)


def _cosine_matrix(texts: list[str]):
    """TF-IDF cosine for ``texts``. Returns an NxN python-float matrix.

    A corpus whose entire vocabulary is empty (all blank texts) yields
    an all-zero matrix instead of raising — zero similarity is the
    correct fail-closed answer for "no shared content".
    """
    n = len(texts)
    zero = [[0.0] * n for _ in range(n)]
    cleaned = [(t or "").strip().lower() for t in texts]
    if not any(cleaned):
        return zero
    try:
        tfidf = TfidfVectorizer().fit_transform(cleaned)
    except ValueError:
        # empty vocabulary (e.g. only stop-words / punctuation)
        return zero
    sim = cosine_similarity(tfidf)
    return [
        [round(float(sim[i][j]), _ROUND_DP) for j in range(n)]
        for i in range(n)
    ]


def _items(artifact: Artifact) -> list[dict]:
    items = artifact.payload.get("extracted_items")
    return items if isinstance(items, list) else []


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def compare_extractions(
    *,
    ceiling_artifact: Artifact,
    haiku_artifact: Artifact,
    alignment_contract_version: str,
) -> Artifact:
    """Compare a Haiku extraction against the frozen Opus ceiling.

    Raises ``AlignmentContractError`` if ``alignment_contract_version``
    does not match the binding contract file.
    """
    file_version = contract_version()
    if alignment_contract_version != file_version:
        raise AlignmentContractError(
            "alignment_contract_version mismatch: caller passed "
            f"{alignment_contract_version!r} but the binding contract "
            f"file declares {file_version!r}",
            reason_code="alignment_contract_version_mismatch",
        )

    ceiling_items = _items(ceiling_artifact)
    haiku_items = _items(haiku_artifact)

    c_texts = [str(i.get("source_text") or "") for i in ceiling_items]
    h_texts = [str(i.get("source_text") or "") for i in haiku_items]
    sim = _cosine_matrix(c_texts + h_texts)
    n_c = len(ceiling_items)

    schema_types = sorted(
        {str(i.get("schema_type")) for i in ceiling_items}
        | {str(i.get("schema_type")) for i in haiku_items}
    )

    aligned_pairs: list[dict] = []
    matched_ceiling: set[int] = set()
    matched_haiku: set[int] = set()
    per_type_metrics: dict[str, dict] = {}

    for stype in schema_types:
        c_idx = [
            k for k, it in enumerate(ceiling_items)
            if str(it.get("schema_type")) == stype
        ]
        h_idx = [
            k for k, it in enumerate(haiku_items)
            if str(it.get("schema_type")) == stype
        ]
        # Candidate pairs satisfying the full predicate, ordered
        # deterministically by (ceiling_item_id, haiku_item_id).
        candidates: list[tuple[str, str, int, int, float, float]] = []
        for ci in c_idx:
            for hj in h_idx:
                iou = _iou(
                    ceiling_items[ci].get("source_turn_ids", []),
                    haiku_items[hj].get("source_turn_ids", []),
                )
                cos = sim[ci][n_c + hj]
                if iou >= IOU_THRESHOLD and cos >= COSINE_THRESHOLD:
                    candidates.append(
                        (
                            str(ceiling_items[ci].get("item_id")),
                            str(haiku_items[hj].get("item_id")),
                            ci,
                            hj,
                            iou,
                            cos,
                        )
                    )
        candidates.sort(key=lambda t: (t[0], t[1]))
        tp = 0
        for cid, hid, ci, hj, iou, cos in candidates:
            if ci in matched_ceiling or hj in matched_haiku:
                continue
            matched_ceiling.add(ci)
            matched_haiku.add(hj)
            tp += 1
            aligned_pairs.append(
                {
                    "ceiling_item_id": cid,
                    "haiku_item_id": hid,
                    "iou": iou,
                    "cosine": cos,
                }
            )
        ceiling_count = len(c_idx)
        haiku_count = len(h_idx)
        fn = ceiling_count - tp
        fp = haiku_count - tp
        recall = tp / ceiling_count if ceiling_count else 0.0
        precision = tp / haiku_count if haiku_count else 0.0
        per_type_metrics[stype] = {
            "recall": recall,
            "precision": precision,
            "f1": _f1(precision, recall),
            "ceiling_count": ceiling_count,
            "haiku_count": haiku_count,
            "true_positives": tp,
            "false_negatives": fn,
            "false_positives": fp,
        }

    total_tp = sum(m["true_positives"] for m in per_type_metrics.values())
    total_c = sum(m["ceiling_count"] for m in per_type_metrics.values())
    total_h = sum(m["haiku_count"] for m in per_type_metrics.values())
    total_recall = total_tp / total_c if total_c else 0.0
    total_precision = total_tp / total_h if total_h else 0.0

    false_negatives: list[dict] = []
    for k, it in enumerate(ceiling_items):
        if k in matched_ceiling:
            continue
        false_negatives.append(
            {
                "schema_type": str(it.get("schema_type")),
                "ceiling_item_id": str(it.get("item_id")),
                "source_turn_ids": [
                    str(t) for t in (it.get("source_turn_ids") or [])
                ],
                "source_text": str(it.get("source_text") or ""),
                "ceiling_payload": it.get("payload")
                if isinstance(it.get("payload"), dict)
                else {},
            }
        )
    false_negatives.sort(key=lambda f: (f["schema_type"], f["ceiling_item_id"]))
    aligned_pairs.sort(key=lambda p: (p["ceiling_item_id"], p["haiku_item_id"]))

    payload = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "transcript_id": ceiling_artifact.payload.get("transcript_id", ""),
        "ceiling_artifact_id": ceiling_artifact.artifact_id,
        "haiku_artifact_id": haiku_artifact.artifact_id,
        "alignment_contract_version": alignment_contract_version,
        "per_type_metrics": per_type_metrics,
        "total_metrics": {
            "recall": total_recall,
            "precision": total_precision,
            "f1": _f1(total_precision, total_recall),
        },
        "aligned_pairs": aligned_pairs,
        "false_negatives": false_negatives,
    }
    return new_artifact(
        artifact_type=ARTIFACT_TYPE,
        payload=payload,
        trace_id=ceiling_artifact.trace_id or f"cmp-{uuid.uuid4().hex[:16]}",
        status="draft",
        input_refs=[ceiling_artifact.artifact_id, haiku_artifact.artifact_id],
    )


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "IOU_THRESHOLD",
    "COSINE_THRESHOLD",
    "AlignmentContractError",
    "contract_version",
    "compare_extractions",
]
