"""OutcomeMemoryStore — unified revision + mitigation outcome memory.

FINDING-G-004: revision and mitigation outcomes share one schema and one jsonl
file. outcome_type='revision'|'mitigation' distinguishes them.

Pattern keywords are significant words from action_taken (length > 4, dedup,
top 10) used for Jaccard-based similar outcome retrieval.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from ..utils.text_similarity import jaccard
from ._io import append_jsonl, read_jsonl, utcnow_iso
from ._paths import outcomes_memory_path
from ._schema import validate_harness_artifact

_LOG = logging.getLogger(__name__)
_PATTERN_KEYWORD_MIN_LEN = 4
_PATTERN_KEYWORD_MAX = 10


def _pattern_keywords(text: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in text.split():
        word = "".join(c.lower() for c in raw if c.isalnum())
        if len(word) <= _PATTERN_KEYWORD_MIN_LEN:
            continue
        if word in seen_set:
            continue
        seen_set.add(word)
        seen.append(word)
        if len(seen) >= _PATTERN_KEYWORD_MAX:
            break
    return seen


class OutcomeMemoryStore:
    def record_revision_outcome(
        self,
        revision_diff: dict[str, Any],
        instruction: dict[str, Any],
        repo_root: str | Path,
    ) -> dict[str, Any]:
        try:
            action_taken = str(instruction.get("instruction_text") or "")
            human_marked = (
                "effective"
                if revision_diff.get("status") == "success"
                else "ineffective"
            )
            record = {
                "record_id": str(uuid.uuid4()),
                "outcome_type": "revision",
                "source_artifact_id": str(
                    revision_diff.get("diff_id") or revision_diff.get("revision_id") or ""
                ),
                "paper_source_id": str(
                    revision_diff.get("paper_source_id")
                    or revision_diff.get("source_id")
                    or instruction.get("paper_source_id")
                    or ""
                ),
                "issue_type": str(
                    instruction.get("issue_type")
                    or instruction.get("instruction_type")
                    or ""
                ),
                "issue_severity": str(
                    instruction.get("priority")
                    or instruction.get("severity")
                    or ""
                ),
                "action_taken": action_taken,
                "human_marked_outcome": human_marked,
                "final_outcome": human_marked,
                "auto_downgraded": False,
                "secondary_check_performed": False,
                "pattern_keywords": _pattern_keywords(action_taken),
                "recorded_at": utcnow_iso(),
            }
            ok, err = validate_harness_artifact(record, "outcome_memory_record")
            if not ok:
                return {"status": "failure", "reason": f"schema_violation: {err}"}
            append_jsonl(outcomes_memory_path(repo_root), record)
            return {"status": "success", "record_id": record["record_id"]}
        except OSError as exc:  # pragma: no cover
            _LOG.warning("record_revision_outcome failed: %s", exc)
            return {"status": "failure", "reason": str(exc)}

    def record_mitigation_outcome(
        self,
        outcome_record: dict[str, Any],
        repo_root: str | Path,
    ) -> dict[str, Any]:
        try:
            mitigation_text = self._load_mitigation_text(
                outcome_record.get("mitigation_id") or "",
                outcome_record.get("paper_source_id") or "",
                repo_root,
            )
            human_marked = str(
                outcome_record.get("human_marked_outcome") or "unknown"
            )
            final = str(outcome_record.get("final_outcome") or human_marked)
            auto_downgraded = (
                final == "ineffective" and human_marked != "ineffective"
            )
            secondary_done = bool(
                outcome_record.get("secondary_check_source_id")
            )
            record = {
                "record_id": str(uuid.uuid4()),
                "outcome_type": "mitigation",
                "source_artifact_id": str(
                    outcome_record.get("outcome_id") or ""
                ),
                "paper_source_id": str(
                    outcome_record.get("paper_source_id") or ""
                ),
                "issue_type": str(
                    outcome_record.get("issue_type")
                    or outcome_record.get("objection_type")
                    or "agency_objection"
                ),
                "issue_severity": str(
                    outcome_record.get("issue_severity")
                    or outcome_record.get("severity")
                    or ""
                ),
                "action_taken": mitigation_text,
                "human_marked_outcome": human_marked,
                "final_outcome": final,
                "auto_downgraded": auto_downgraded,
                "secondary_check_performed": secondary_done,
                "pattern_keywords": _pattern_keywords(mitigation_text),
                "recorded_at": utcnow_iso(),
            }
            ok, err = validate_harness_artifact(record, "outcome_memory_record")
            if not ok:
                return {"status": "failure", "reason": f"schema_violation: {err}"}
            append_jsonl(outcomes_memory_path(repo_root), record)
            return {"status": "success", "record_id": record["record_id"]}
        except OSError as exc:  # pragma: no cover
            _LOG.warning("record_mitigation_outcome failed: %s", exc)
            return {"status": "failure", "reason": str(exc)}

    def _load_mitigation_text(
        self,
        mitigation_id: str,
        paper_source_id: str,
        repo_root: str | Path,
    ) -> str:
        if not mitigation_id or not paper_source_id:
            return ""
        try:
            from ..extraction._paths import find_processed_dir
        except ImportError:  # pragma: no cover
            return ""
        processed_dir, _ = find_processed_dir(Path(repo_root), paper_source_id)
        if processed_dir is None:
            return ""
        path = processed_dir / "paper" / "objections" / "mitigations.jsonl"
        records = read_jsonl(path)
        for rec in records:
            if rec.get("mitigation_id") == mitigation_id:
                return str(rec.get("mitigation_text") or "")
        return ""

    def find_similar_outcomes(
        self,
        action_text: str,
        repo_root: str | Path,
        top_n: int = 5,
    ) -> list[tuple[dict[str, Any], float]]:
        records = read_jsonl(outcomes_memory_path(repo_root))
        if not records:
            return []
        action_words = " ".join(_pattern_keywords(action_text))
        scored: list[tuple[dict[str, Any], float]] = []
        for rec in records:
            keywords = " ".join(rec.get("pattern_keywords") or [])
            score = jaccard(action_words, keywords, min_word_length=1)
            if score >= 0.5:
                scored.append((rec, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(0, int(top_n))]

    def get_effectiveness_rate(
        self,
        outcome_type: str,
        repo_root: str | Path,
    ) -> dict[str, Any]:
        records = [
            r for r in read_jsonl(outcomes_memory_path(repo_root))
            if r.get("outcome_type") == outcome_type
        ]
        total = len(records)
        if total == 0:
            return {
                "outcome_type": outcome_type,
                "total": 0,
                "effective": 0,
                "ineffective": 0,
                "partial": 0,
                "effectiveness_rate": None,
            }
        effective = sum(1 for r in records if r.get("final_outcome") == "effective")
        ineffective = sum(1 for r in records if r.get("final_outcome") == "ineffective")
        partial = sum(1 for r in records if r.get("final_outcome") == "partial")
        return {
            "outcome_type": outcome_type,
            "total": total,
            "effective": effective,
            "ineffective": ineffective,
            "partial": partial,
            "effectiveness_rate": effective / total,
        }

    def write_outcome_projection(
        self,
        repo_root: str | Path,
        vault_root: str | Path | None = None,
    ) -> str:
        from ..ingestion.obsidian_projection import ObsidianProjection

        return ObsidianProjection().write_outcomes_projection(
            repo_root, vault_root
        )
