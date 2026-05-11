"""ExtractionMerger: combine typed-extractor outputs into one artifact.

Phase M3.1. Inputs are the three lists produced by ``DecisionExtractor``,
``ClaimExtractor`` and ``ActionItemExtractor`` plus the chunk
classifications. Output is a single ``meeting_extraction`` artifact.

Dedup policy:
- Items from **different** extractors that share any source_turn_id are
  flagged ``requires_human_dedup: true``. They are NEVER auto-removed
  because paraphrase-level dedup is lossy on regulatory language.
- Items from the **same** extractor with byte-identical primary text
  (decision_text / claim_text / action) ARE removed; the first
  occurrence wins. This is safe because the same extractor will not
  produce two genuinely different items with identical text.

Routing-quality warning: ``routing_quality_warning = True`` when more
than 20% of chunks were classified as off_topic. The threshold is
20% per Phase M3 spec: at this rate we are likely losing genuine
decisions to misclassification.

Atomic write: ``write_to`` writes to a ``.tmp`` file and renames
into place so a crash mid-write never leaves a partial artifact.
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ._prompt_blocks import CONFIDENCE_THRESHOLD


_ROUTING_QUALITY_THRESHOLD = 0.20  # >20% off_topic -> warning fires


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _dedup_exact(items: List[Dict[str, Any]], text_key: str) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        key = it.get(text_key)
        if not isinstance(key, str):
            continue
        norm = key.strip()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(it)
    return out


def _turn_set(item: Dict[str, Any]) -> Set[str]:
    raw = item.get("source_turn_ids") or []
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if isinstance(x, (str, int))}


def _merge_run_metadata(
    run_metadata: Optional[Sequence[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Aggregate per-extractor metadata into per-run fields.

    ``few_shot_injected`` is True iff at least one extractor successfully
    injected examples (a partial degraded run still benefited from few-shot
    where it could). ``few_shot_version`` is the version reported by any
    extractor that injected (all three load the same seed so versions
    agree in practice). Counts sum across extractors.

    ``omit_instruction_present`` is True iff at least one extractor
    confirmed (post-render) that its built prompt contained the OMIT
    block. We do NOT default this to True -- a decorative claim that
    drifts from reality would defeat the point of recording it.
    """
    out: Dict[str, Any] = {
        "few_shot_injected": False,
        "few_shot_version": None,
        "few_shot_example_count": 0,
        "omit_instruction_present": False,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "low_confidence_item_count": 0,
    }
    for meta in run_metadata or []:
        if not isinstance(meta, dict):
            continue
        if meta.get("few_shot_injected"):
            out["few_shot_injected"] = True
            v = meta.get("few_shot_version")
            if isinstance(v, str) and v:
                out["few_shot_version"] = v
        c = meta.get("few_shot_example_count", 0)
        if isinstance(c, int) and c > 0:
            out["few_shot_example_count"] += c
        if meta.get("omit_instruction_present") is True:
            out["omit_instruction_present"] = True
        low = meta.get("low_confidence_item_count", 0)
        if isinstance(low, int) and low > 0:
            out["low_confidence_item_count"] += low
    return out


class ExtractionMerger:
    """Merge three typed-extractor outputs into a meeting_extraction artifact."""

    SCHEMA_VERSION = "1.1.0"
    ROUTING_QUALITY_THRESHOLD: float = _ROUTING_QUALITY_THRESHOLD

    def merge(
        self,
        source_artifact_id: str,
        extraction_run_id: str,
        classifications: Sequence[Dict[str, Any]],
        decisions: Sequence[Dict[str, Any]],
        claims: Sequence[Dict[str, Any]],
        action_items: Sequence[Dict[str, Any]],
        run_metadata: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Return a meeting_extraction artifact dict. Never raises.

        ``run_metadata`` is the list of ``last_run_metadata`` dicts from
        the three typed extractors. When omitted (legacy callers / tests),
        run-level few-shot and confidence fields default to "no injection,
        no low-confidence items, threshold = CONFIDENCE_THRESHOLD".
        """
        decisions_in = list(decisions or [])
        claims_in = list(claims or [])
        actions_in = list(action_items or [])

        # 1. Same-extractor exact-text dedup.
        decisions_d = _dedup_exact(decisions_in, "decision_text")
        claims_d = _dedup_exact(claims_in, "claim_text")
        actions_d = _dedup_exact(actions_in, "action")

        # 2. Cross-extractor overlap flag.
        all_buckets: List[Tuple[List[Dict[str, Any]], str]] = [
            (decisions_d, "decision"),
            (claims_d, "claim"),
            (actions_d, "action_item"),
        ]
        dedup_count = 0
        for i, (bucket_a, _) in enumerate(all_buckets):
            for item_a in bucket_a:
                turns_a = _turn_set(item_a)
                if not turns_a:
                    continue
                for j, (bucket_b, _) in enumerate(all_buckets):
                    if i == j:
                        continue
                    for item_b in bucket_b:
                        turns_b = _turn_set(item_b)
                        if turns_a & turns_b:
                            if not item_a.get("requires_human_dedup"):
                                item_a["requires_human_dedup"] = True
                                dedup_count += 1
                            break
                    else:
                        continue
                    break

        # 3. Routing metrics.
        total = len(classifications or [])
        off_topic = sum(
            1 for c in (classifications or [])
            if isinstance(c, dict) and c.get("classification") == "off_topic"
        )
        verb_fallback = sum(
            1 for c in (classifications or [])
            if isinstance(c, dict) and c.get("regulatory_verb_fallback_applied")
        )
        warn = (total > 0) and ((off_topic / total) > self.ROUTING_QUALITY_THRESHOLD)

        run_fields = _merge_run_metadata(run_metadata)

        return {
            "meeting_extraction_id": str(uuid.uuid4()),
            "source_artifact_id": source_artifact_id,
            "artifact_type": "meeting_extraction",
            "schema_version": self.SCHEMA_VERSION,
            "created_at": _now_iso(),
            "decisions": decisions_d,
            "claims": claims_d,
            "action_items": actions_d,
            "total_chunks_classified": total,
            "off_topic_count": off_topic,
            "regulatory_verb_fallback_count": verb_fallback,
            "routing_quality_warning": warn,
            "requires_human_dedup_count": dedup_count,
            "extraction_run_id": extraction_run_id or "",
            "few_shot_injected": run_fields["few_shot_injected"],
            "few_shot_version": run_fields["few_shot_version"],
            "few_shot_example_count": run_fields["few_shot_example_count"],
            "omit_instruction_present": run_fields["omit_instruction_present"],
            "confidence_threshold": run_fields["confidence_threshold"],
            "low_confidence_item_count": run_fields["low_confidence_item_count"],
            "provenance": {"produced_by": "ExtractionMerger"},
        }

    @staticmethod
    def write_to(artifact: Dict[str, Any], path: Path) -> None:
        """Atomic write: serialize to ``.tmp`` then ``replace`` into place.

        Runs the artifact_validator on the artifact before write so the
        deprecation warning fires on every typed-extraction write path,
        not only on the legacy Promoter path. IO errors propagate to the
        caller; the pipeline wraps this in its own try/except.
        """
        # Local import keeps this module importable in tests that don't
        # need governance wiring.
        from spectrum_systems_core.governance.artifact_validator import (
            validate_and_log,
        )
        validate_and_log(artifact, schema_path=str(path))

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
