"""StoryworthyFilter: deterministic 5-dimension scoring for story candidates.

No LLM. Runs only on grounded candidates with status == "candidate".
Updates storyworthy_score and storyworthy_verdict in place.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._paths import find_processed_dir

SCORING_RULES: dict[str, dict[str, Any]] = {
    "five_second_moment": {
        "keywords": [
            "moment", "suddenly", "realized", "decided", "when", "then",
        ],
        "score_if_any": 2,
        "score_if_none": 0,
        "max": 3,
    },
    "stakes": {
        "keywords": [
            "risk", "loss", "failure", "cost", "threat", "impact", "consequence",
        ],
        "score_if_any": 2,
        "score_if_none": 0,
        "max": 3,
    },
    "central_question": {
        "keywords": ["whether", "if", "would", "could", "how", "why", "what"],
        "score_if_any": 2,
        "score_if_none": 0,
        "max": 3,
    },
    "vulnerability": {
        "keywords": [
            "difficult", "failed", "wrong", "mistake", "uncertain", "struggled",
        ],
        "score_if_any": 2,
        "score_if_none": 0,
        "max": 3,
    },
    "narrative_compression": {
        "max_words_for_full_score": 300,
        "score_if_under": 3,
        "score_if_over": 1,
        "max": 3,
    },
}

ADMIT_THRESHOLD = 10
REVISE_THRESHOLD = 6


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w])


def _keyword_score(rule: dict[str, Any], haystack_lower: str) -> int:
    for kw in rule["keywords"]:
        if kw in haystack_lower:
            return int(rule["score_if_any"])
    return int(rule["score_if_none"])


class StoryworthyFilter:
    """Score story candidates and assign admit/revise/reject verdicts."""

    def score(self, candidate: dict[str, Any]) -> dict[str, Any]:
        summary = candidate.get("story_summary", "") or ""
        why = candidate.get("why_it_might_work", "") or ""
        excerpt = candidate.get("source_excerpt", "") or ""

        haystack = (summary + " " + why).lower()

        scores: dict[str, int] = {}
        for name, rule in SCORING_RULES.items():
            if name == "narrative_compression":
                wc = _word_count(summary + " " + excerpt)
                if wc <= int(rule["max_words_for_full_score"]):
                    scores[name] = int(rule["score_if_under"])
                else:
                    scores[name] = int(rule["score_if_over"])
            else:
                scores[name] = _keyword_score(rule, haystack)

        total = sum(scores.values())
        scores["total"] = total
        candidate["storyworthy_score"] = scores

        if total >= ADMIT_THRESHOLD:
            verdict = "admit"
        elif total >= REVISE_THRESHOLD:
            verdict = "revise"
        else:
            verdict = "reject"
        candidate["storyworthy_verdict"] = verdict
        return candidate

    def run_on_source(
        self, source_id: str, repo_root: str
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {"status": "failure", "scored_count": 0, "reason": "source_not_found"}
        candidates_path = processed_dir / "stories" / "candidates.jsonl"
        if not candidates_path.is_file():
            return {
                "status": "failure",
                "scored_count": 0,
                "reason": "candidates_not_found",
            }
        candidates: list[dict[str, Any]] = []
        try:
            with candidates_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    candidates.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "failure",
                "scored_count": 0,
                "reason": f"candidates_unreadable: {exc}",
            }

        scored = 0
        for candidate in candidates:
            if candidate.get("status") != "candidate":
                continue
            if not candidate.get("grounded"):
                continue
            self.score(candidate)
            scored += 1

        try:
            with candidates_path.open("w", encoding="utf-8") as fh:
                for candidate in candidates:
                    fh.write(
                        json.dumps(
                            candidate, sort_keys=True, separators=(",", ":")
                        )
                        + "\n"
                    )
        except OSError as exc:
            return {
                "status": "failure",
                "scored_count": scored,
                "reason": f"write_error: {exc}",
            }

        return {
            "status": "success",
            "scored_count": scored,
            "candidates": candidates,
            "reason": "",
        }
