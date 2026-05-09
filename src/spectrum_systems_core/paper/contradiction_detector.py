"""ContradictionDetector: deterministic claim-pair contradiction flagging.

No LLM. Pairs claims that share >= 3 significant words and differ in
negation polarity. False positives are expected — the output is a flag
for human review, not a definitive contradiction finding.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..extraction._paths import find_processed_dir

NEGATION_WORDS = {
    "not", "no", "never", "neither", "nor", "without",
    "cannot", "won't", "doesn't", "isn't", "aren't",
    "fails", "lack", "absent", "zero",
}

_STOPWORDS = {
    "the", "and", "for", "from", "with", "that", "this", "than", "when",
    "while", "after", "before", "about", "their", "there", "these", "those",
    "into", "over", "such", "have", "been", "will", "would", "could", "should",
    "where", "which", "what", "whose", "they", "them", "were", "your",
    "more", "less", "much", "very", "some", "most", "also", "only", "just",
    "many", "must", "still", "even", "upon", "thus", "across", "among",
}

MIN_OVERLAP = 3


def _significant_words(text: str) -> List[str]:
    out: List[str] = []
    for raw in text.split():
        w = "".join(ch for ch in raw.lower() if ch.isalnum() or ch == "'")
        if len(w) > 4 and w not in _STOPWORDS:
            out.append(w)
    return out


def _has_negation(text: str) -> bool:
    tokens = {
        "".join(ch for ch in raw.lower() if ch.isalnum() or ch == "'")
        for raw in text.split()
    }
    return bool(tokens & NEGATION_WORDS)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class ContradictionDetector:
    """Flag potential contradictions between claim pairs.

    NOTE: This is a deterministic keyword + negation-polarity heuristic.
    False positives are expected. The output flags pairs for human review;
    it is not a definitive finding. Adding an LLM here would violate the
    determinism contract for evals (constitution).
    """

    def detect(self, claims: List[Dict[str, Any]]) -> Dict[str, Any]:
        pairs: List[Dict[str, Any]] = []
        total = 0
        n = len(claims)
        for i in range(n):
            for j in range(i + 1, n):
                total += 1
                a = claims[i]
                b = claims[j]
                a_text = a.get("claim_text", "") or ""
                b_text = b.get("claim_text", "") or ""
                a_words = set(_significant_words(a_text))
                b_words = set(_significant_words(b_text))
                overlap = a_words & b_words
                if len(overlap) < MIN_OVERLAP:
                    continue
                a_neg = _has_negation(a_text)
                b_neg = _has_negation(b_text)
                if a_neg == b_neg:
                    continue
                pairs.append(
                    {
                        "claim_id_a": a["claim_id"],
                        "claim_id_b": b["claim_id"],
                        "overlap_words": sorted(overlap),
                        "reason": "negation_polarity_difference",
                    }
                )
        return {
            "contradiction_pairs": pairs,
            "total_pairs_checked": total,
        }

    def run_on_source(
        self, source_id: str, repo_root: str
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        processed_dir, _ = find_processed_dir(repo_root_path, source_id)
        if processed_dir is None:
            return {
                "status": "failure",
                "contradiction_count": 0,
                "reason": "source_not_found",
            }
        paper_dir = processed_dir / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        claims_path = paper_dir / "claims.jsonl"
        claims = _read_jsonl(claims_path)

        detection = self.detect(claims)

        # Update claims.jsonl with contradicted_by_claim_ids.
        by_id = {c["claim_id"]: c for c in claims}
        for c in claims:
            c.setdefault("contradicted_by_claim_ids", [])
        for pair in detection["contradiction_pairs"]:
            a = by_id.get(pair["claim_id_a"])
            b = by_id.get(pair["claim_id_b"])
            if a is not None and pair["claim_id_b"] not in a["contradicted_by_claim_ids"]:
                a["contradicted_by_claim_ids"].append(pair["claim_id_b"])
            if b is not None and pair["claim_id_a"] not in b["contradicted_by_claim_ids"]:
                b["contradicted_by_claim_ids"].append(pair["claim_id_a"])

        try:
            with claims_path.open("w", encoding="utf-8") as fh:
                for c in claims:
                    fh.write(
                        json.dumps(c, sort_keys=True, separators=(",", ":")) + "\n"
                    )
        except OSError as exc:
            return {
                "status": "failure",
                "contradiction_count": 0,
                "reason": f"write_error: {exc}",
            }

        summary_path = paper_dir / "contradiction_summary.json"
        try:
            summary_path.write_text(
                json.dumps(
                    {
                        "source_id": source_id,
                        "contradiction_count": len(detection["contradiction_pairs"]),
                        "total_pairs_checked": detection["total_pairs_checked"],
                        "contradiction_pairs": detection["contradiction_pairs"],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return {
                "status": "failure",
                "contradiction_count": 0,
                "reason": f"write_error: {exc}",
            }

        return {
            "status": "success",
            "contradiction_count": len(detection["contradiction_pairs"]),
            "reason": "",
        }
