"""Phase R.3: post-hoc source verification via token overlap.

For each *confirmed* extraction item, check whether the
``candidate_evidence`` text from stage 1 actually appears (approximately)
in the source chunks referenced by ``source_turn_ids`` /
``source_turns``. Items below the overlap threshold are flagged with a
``spurious_add_candidate`` artifact and a ``grounded=False`` annotation
on the canonical item. They are NOT removed -- this is a finding, not a
halt.

When the per-transcript ``spurious_add_rate`` (spurious / total
confirmed) exceeds ``SPURIOUS_ADD_RATE_THRESHOLD`` the verifier emits a
single ``spurious_add_warning`` so the eval summary can surface the
finding.

Rollback: ``POST_HOC_VERIFICATION_ENABLED=false`` skips the entire pass.
The post-hoc verifier under ``verification/`` (Phase V) is a SEPARATE
LLM-driven verifier with a different cost profile. This module is the
deterministic lexical sibling per the research recommendation 8b/8c.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_LOG = logging.getLogger(__name__)


POST_HOC_VERIFICATION_ENABLED_ENV: str = "POST_HOC_VERIFICATION_ENABLED"
_DISABLED_VALUES: frozenset = frozenset({"false", "0", "no", "off"})

# Threshold: 40% token overlap with the cited source chunk text. Below
# this the item is marked spurious.
SOURCE_GROUNDING_OVERLAP_THRESHOLD: float = 0.4

# Transcript-level alarm threshold: > 30% of confirmed items spurious.
SPURIOUS_ADD_RATE_THRESHOLD: float = 0.30


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def post_hoc_verification_enabled() -> bool:
    raw = os.environ.get(POST_HOC_VERIFICATION_ENABLED_ENV, "").strip().lower()
    if raw in _DISABLED_VALUES:
        return False
    return True


def _tokenize(text: str) -> List[str]:
    if not isinstance(text, str) or not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def compute_token_overlap(evidence: str, chunk_text: str) -> float:
    """Fraction of evidence tokens present in the chunk text.

    Returns a value in ``[0.0, 1.0]``. Empty evidence yields 0.0 so a
    missing ``candidate_evidence`` never silently scores 100% grounded.
    """
    ev_tokens = _tokenize(evidence)
    if not ev_tokens:
        return 0.0
    chunk_set = set(_tokenize(chunk_text))
    if not chunk_set:
        return 0.0
    hits = sum(1 for t in ev_tokens if t in chunk_set)
    return hits / float(len(ev_tokens))


def _resolve_item_evidence(item: Dict[str, Any]) -> str:
    """Pull the evidence string off an item.

    Stage 1 stamps ``candidate_evidence``. Some upstream extractors use
    ``evidence`` or ``rationale``; we treat ``candidate_evidence`` as
    canonical and fall back so callers do not have to rename.
    """
    for key in ("candidate_evidence", "evidence", "rationale"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _resolve_item_turn_ids(item: Dict[str, Any]) -> List[str]:
    for key in ("source_turn_ids", "source_turns"):
        v = item.get(key)
        if isinstance(v, list):
            return [str(x) for x in v if isinstance(x, (str, int))]
    return []


def verify_source_grounding(
    item: Dict[str, Any],
    chunks_by_id: Dict[str, Dict[str, Any]],
    *,
    threshold: float = SOURCE_GROUNDING_OVERLAP_THRESHOLD,
) -> Dict[str, Any]:
    """Run token-overlap verification for one item.

    Returns::

        {
          "grounded": bool,
          "overlap": float,
          "cited_chunk_ids": [str, ...],
          "missing_chunk_ids": [str, ...],
          "evidence": str,           # the actual evidence used
        }

    ``grounded`` is True iff:
      - at least one cited chunk_id was found in ``chunks_by_id``, AND
      - overlap >= ``threshold``.

    A cited chunk_id that doesn't exist in the index contributes nothing
    to the chunk text pool but does land in ``missing_chunk_ids`` so the
    operator can spot dangling references.
    """
    evidence = _resolve_item_evidence(item)
    cited = _resolve_item_turn_ids(item)
    missing: List[str] = []
    chunk_texts: List[str] = []
    for tid in cited:
        chunk = chunks_by_id.get(tid)
        if not isinstance(chunk, dict):
            missing.append(tid)
            continue
        text = chunk.get("text") or ""
        chunk_texts.append(text)

    if not chunk_texts:
        return {
            "grounded": False,
            "overlap": 0.0,
            "cited_chunk_ids": cited,
            "missing_chunk_ids": missing,
            "evidence": evidence,
        }

    overlap = compute_token_overlap(evidence, "\n".join(chunk_texts))
    return {
        "grounded": overlap >= threshold,
        "overlap": round(float(overlap), 6),
        "cited_chunk_ids": cited,
        "missing_chunk_ids": missing,
        "evidence": evidence,
    }


def build_spurious_add_candidate(
    item: Dict[str, Any],
    result: Dict[str, Any],
    *,
    source_id: str,
    extraction_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a spurious_add_candidate artifact for one ungrounded item."""
    return {
        "artifact_type": "spurious_add_candidate",
        "schema_version": "1.0.0",
        "spurious_add_candidate_id": str(uuid.uuid4()),
        "source_id": source_id or "",
        "extraction_run_id": extraction_run_id or "",
        "item_text": str(
            item.get("decision_text")
            or item.get("claim_text")
            or item.get("action")
            or item.get("text")
            or ""
        )[:1000],
        "evidence": str(result.get("evidence") or "")[:1000],
        "overlap": float(result.get("overlap") or 0.0),
        "cited_chunk_ids": list(result.get("cited_chunk_ids") or []),
        "missing_chunk_ids": list(result.get("missing_chunk_ids") or []),
        "threshold": float(SOURCE_GROUNDING_OVERLAP_THRESHOLD),
        "created_at": _now_iso(),
    }


def build_spurious_add_warning(
    *,
    source_id: str,
    confirmed_count: int,
    spurious_count: int,
    extraction_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the per-transcript ``spurious_add_warning`` artifact.

    Only emitted when ``spurious_add_rate > SPURIOUS_ADD_RATE_THRESHOLD``.
    """
    rate = (
        spurious_count / float(confirmed_count) if confirmed_count > 0 else 0.0
    )
    return {
        "artifact_type": "spurious_add_warning",
        "schema_version": "1.0.0",
        "spurious_add_warning_id": str(uuid.uuid4()),
        "source_id": source_id or "",
        "extraction_run_id": extraction_run_id or "",
        "confirmed_items_count": int(confirmed_count),
        "spurious_items_count": int(spurious_count),
        "spurious_add_rate": round(float(rate), 6),
        "threshold": float(SPURIOUS_ADD_RATE_THRESHOLD),
        "created_at": _now_iso(),
    }


def verify_extraction_grounding(
    confirmed_items: List[Dict[str, Any]],
    chunks_by_id: Dict[str, Dict[str, Any]],
    *,
    source_id: str,
    extraction_run_id: Optional[str] = None,
    threshold: float = SOURCE_GROUNDING_OVERLAP_THRESHOLD,
) -> Dict[str, Any]:
    """Run R.3 over an entire transcript's confirmed extraction.

    Returns a summary dict with:
      - ``annotated_items``: each confirmed item with a ``grounded``
        boolean added (the canonical record).
      - ``spurious_add_candidates``: artifact dicts for ungrounded items.
      - ``spurious_add_warning``: a single per-transcript artifact
        when the spurious_add_rate exceeds threshold, else ``None``.
      - ``spurious_add_rate``: float in ``[0, 1]``.

    The denominator for ``spurious_add_rate`` is the count of *confirmed*
    items only -- rejected items from stage 2 do not contribute (RT1
    finding: a denominator that includes rejected items would dilute the
    rate and hide the regression).
    """
    annotated: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    spurious_count = 0
    confirmed_count = 0

    for item in confirmed_items or []:
        if not isinstance(item, dict):
            continue
        confirmed_count += 1
        result = verify_source_grounding(
            item, chunks_by_id, threshold=threshold,
        )
        out = dict(item)
        out["grounded"] = bool(result["grounded"])
        out["grounding_overlap"] = float(result["overlap"])
        annotated.append(out)
        if not result["grounded"]:
            spurious_count += 1
            candidates.append(
                build_spurious_add_candidate(
                    item, result,
                    source_id=source_id,
                    extraction_run_id=extraction_run_id,
                )
            )

    rate = (
        spurious_count / float(confirmed_count) if confirmed_count > 0 else 0.0
    )
    warning: Optional[Dict[str, Any]] = None
    if rate > SPURIOUS_ADD_RATE_THRESHOLD and confirmed_count > 0:
        warning = build_spurious_add_warning(
            source_id=source_id,
            confirmed_count=confirmed_count,
            spurious_count=spurious_count,
            extraction_run_id=extraction_run_id,
        )

    return {
        "annotated_items": annotated,
        "spurious_add_candidates": candidates,
        "spurious_add_warning": warning,
        "spurious_add_rate": round(float(rate), 6),
        "confirmed_items_count": confirmed_count,
        "spurious_items_count": spurious_count,
    }


def write_grounding_artifacts(
    summary: Dict[str, Any],
    sdl_root: Optional[Path],
) -> Dict[str, List[Path]]:
    """Persist the spurious_add_candidate + warning artifacts to disk.

    Failure is logged, never raised. Returns the paths written so
    callers can include them in the run summary.
    """
    out: Dict[str, List[Path]] = {"candidates": [], "warnings": []}
    if sdl_root is None:
        return out
    target_dir = Path(sdl_root) / "grounding"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning("grounding_dir_create_failed: %s", exc)
        return out
    for c in summary.get("spurious_add_candidates") or []:
        p = target_dir / f"{c['spurious_add_candidate_id']}.json"
        try:
            p.write_text(
                json.dumps(c, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            out["candidates"].append(p)
        except OSError as exc:
            _LOG.warning("spurious_add_candidate_write_failed: %s", exc)
    warning = summary.get("spurious_add_warning")
    if isinstance(warning, dict):
        p = target_dir / f"{warning['spurious_add_warning_id']}.json"
        try:
            p.write_text(
                json.dumps(warning, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            out["warnings"].append(p)
        except OSError as exc:
            _LOG.warning("spurious_add_warning_write_failed: %s", exc)
    return out


__all__ = [
    "POST_HOC_VERIFICATION_ENABLED_ENV",
    "SOURCE_GROUNDING_OVERLAP_THRESHOLD",
    "SPURIOUS_ADD_RATE_THRESHOLD",
    "build_spurious_add_candidate",
    "build_spurious_add_warning",
    "compute_token_overlap",
    "post_hoc_verification_enabled",
    "verify_extraction_grounding",
    "verify_source_grounding",
    "write_grounding_artifacts",
]
