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


class ExtractionMerger:
    """Merge three typed-extractor outputs into a meeting_extraction artifact."""

    SCHEMA_VERSION = "1.0.0"
    ROUTING_QUALITY_THRESHOLD: float = _ROUTING_QUALITY_THRESHOLD

    def merge(
        self,
        source_artifact_id: str,
        extraction_run_id: str,
        classifications: Sequence[Dict[str, Any]],
        decisions: Sequence[Dict[str, Any]],
        claims: Sequence[Dict[str, Any]],
        action_items: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return a meeting_extraction artifact dict. Never raises."""
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
