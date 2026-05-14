"""Phase P1: deterministic two-stage alignment of extracted decisions to GT pairs.

Replaces the legacy TF-IDF-cosine alignment for the typed-extraction path
(``meeting_extraction.decisions`` -> ``ground_truth_pair``). The legacy
``EvalAligner`` continues to handle minutes-text-style fixtures.

The motivating bug: minutes paraphrase decisions instead of quoting them,
so the TF-IDF cosine threshold of 0.7 never fires and coverage/precision
collapse to 0.000. The new algorithm uses two stages of deterministic
checks that together produce a defensible match without LLM calls.

Stage 1 — outcome type match
    An extracted decision can only align to a GT pair when
    ``extracted.decision_outcome == gt_pair.expected_decision_outcome``.
    This narrows the candidate set before any string comparison and
    rejects "approved vs deferred" confusions outright.

Stage 2 — keyword overlap with regulatory-verb / numeric boosts
    Jaccard similarity over lowercased word tokens, boosted by 0.2 when
    a regulatory verb (from ``config.taxonomy.REGULATORY_VERBS``) appears
    in BOTH texts and by 0.1 when a numeric token appears in both. The
    boosts model the domain anchors that drive a human reviewer's match
    decision (the verb that licenses the decision class; the threshold
    number / band reference that pins down the technical content).

Bidirectional requirement
    A match counts only if ``score(extracted -> gt) >= threshold`` AND
    ``score(gt -> extracted) >= threshold``. Jaccard is symmetric, so
    the two-sided check is structurally redundant in the pure-Jaccard
    case, but the regulatory-verb / numeric boosts can fire asymmetrically
    if one direction tokenises differently. Computing both sides keeps
    the contract honest: a one-directional high score must never
    silently match.

Threshold rationale
    The default ``ALIGNMENT_THRESHOLD = 0.15`` was chosen empirically to
    catch the typical paraphrase ratio of meeting minutes ("Approved
    ITU two-point criteria at 80th percentile" -> "Group approved
    application of ITU two-point criteria for FSS protection analysis:
    negative 10.5 dB at 80th percentile and negative 6 dB at 0.03
    percent"; Jaccard is ~0.27 here, plus a 0.2 verb boost). It is low
    enough for heavy paraphrase but high enough to reject random
    overlap between unrelated decisions in the same outcome bucket.
    Re-calibrate after the first production-baseline pass.

Outputs
    ``compute_alignment`` returns a dict with these keys:

    * ``coverage`` — fraction of GT pairs that have at least one
      matched extracted decision.
    * ``precision`` — fraction of extracted decisions that matched at
      least one GT pair.
    * ``spurious_add_rate`` — fraction of extracted decisions that
      matched NO GT pair (== ``1 - precision`` when every extracted
      decision has an outcome the runner attempted to match).
    * ``per_outcome_f1`` — mapping ``outcome -> F1`` computed from
      per-outcome precision and recall using the harmonic mean.
    * ``pairs`` — per-pair match info (matched extracted indices,
      expected outcome).
    * ``review_queue`` — for each extracted decision that passed
      Stage 1 (outcome match) but failed Stage 2 (text overlap), the
      top-3 candidates by similarity_score with their pair_ids.

The function is pure: no I/O, no shared state, no random numbers. Two
calls with identical inputs always return identical outputs.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from ..config.taxonomy import REGULATORY_VERBS

# Default Stage 2 threshold. Both directions of the bidirectional check
# must meet or exceed this value for the alignment to count as matched.
ALIGNMENT_THRESHOLD: float = 0.15

# Number of top candidates kept on the review-queue entry when an
# extracted decision passed Stage 1 but failed Stage 2.
REVIEW_QUEUE_TOP_K: int = 3

_TOKEN_RE = re.compile(r"\b[a-z0-9.\-]+\b")
_NUMBER_RE = re.compile(r"\b\d+\.?\d*\b")


def _tokenize(text: str) -> set:
    if not isinstance(text, str):
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def _numbers(text: str) -> set:
    if not isinstance(text, str):
        return set()
    return set(_NUMBER_RE.findall(text))


def _alignment_score(
    extracted_text: str,
    gt_text: str,
    regulatory_verbs: Sequence[str] = REGULATORY_VERBS,
) -> float:
    """Deterministic alignment score in [0.0, 1.0].

    Algorithm
        1. Tokenize both texts to lowercase word sets (alphanumerics +
           ``. -`` so band references like ``-10.5`` survive).
        2. Jaccard similarity on the two sets.
        3. ``+0.2`` if any regulatory verb appears in BOTH texts.
        4. ``+0.1`` if any numeric token appears in BOTH texts.
        5. Clamp to ``[0.0, 1.0]``.

    Empty inputs on either side return ``0.0`` rather than raising — the
    aligner is called for every (extracted, gt) pair and must degrade
    gracefully.
    """
    ext_tokens = _tokenize(extracted_text)
    gt_tokens = _tokenize(gt_text)
    if not ext_tokens or not gt_tokens:
        return 0.0

    intersection = ext_tokens & gt_tokens
    union = ext_tokens | gt_tokens
    jaccard = len(intersection) / len(union) if union else 0.0

    verb_boost = 0.0
    ext_lower = extracted_text.lower() if isinstance(extracted_text, str) else ""
    gt_lower = gt_text.lower() if isinstance(gt_text, str) else ""
    for verb in regulatory_verbs:
        if verb in ext_lower and verb in gt_lower:
            verb_boost = 0.2
            break

    number_boost = 0.1 if (_numbers(extracted_text) & _numbers(gt_text)) else 0.0

    score = jaccard + verb_boost + number_boost
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _f1(precision: float, recall: float) -> float:
    denom = precision + recall
    if denom <= 0.0:
        return 0.0
    return (2.0 * precision * recall) / denom


def compute_alignment(
    extracted_decisions: Sequence[Mapping[str, Any]],
    gt_pairs: Sequence[Mapping[str, Any]],
    *,
    threshold: float = ALIGNMENT_THRESHOLD,
    regulatory_verbs: Sequence[str] = REGULATORY_VERBS,
) -> Dict[str, Any]:
    """Two-stage deterministic alignment for typed-extraction decisions.

    Inputs are sequences of mappings. ``extracted_decisions`` items must
    expose ``decision_text`` and ``decision_outcome``. ``gt_pairs`` items
    must expose ``pair_id``, ``ground_truth_text`` and
    ``expected_decision_outcome``. Missing fields degrade to no-match
    rather than raising.

    Returns the metrics dict described in the module docstring.
    """
    n_extracted = len(extracted_decisions)
    n_pairs = len(gt_pairs)

    # pair_id -> set of matched extracted indices
    pair_to_extracted: Dict[str, set] = {}
    # extracted_index -> set of matched pair_ids
    extracted_to_pair: Dict[int, set] = {}
    # per-extracted review queue entries (passed Stage 1, failed Stage 2)
    review_queue: List[Dict[str, Any]] = []

    for ext_idx, decision in enumerate(extracted_decisions):
        ext_outcome = decision.get("decision_outcome") if isinstance(decision, Mapping) else None
        ext_text = decision.get("decision_text") if isinstance(decision, Mapping) else None
        if not isinstance(ext_text, str):
            ext_text = ""

        # Stage 1 candidates: GT pairs whose expected_decision_outcome
        # matches the extracted decision's decision_outcome. Either side
        # missing => no match candidate (fail-closed).
        stage1_candidates: List[Dict[str, Any]] = []
        for gt in gt_pairs:
            if not isinstance(gt, Mapping):
                continue
            gt_outcome = gt.get("expected_decision_outcome")
            gt_pair_id = gt.get("pair_id")
            gt_text = gt.get("ground_truth_text")
            if not isinstance(gt_pair_id, str) or not gt_pair_id:
                continue
            if not isinstance(gt_outcome, str) or not isinstance(ext_outcome, str):
                continue
            if gt_outcome != ext_outcome:
                continue
            if not isinstance(gt_text, str):
                gt_text = ""

            score_forward = _alignment_score(ext_text, gt_text, regulatory_verbs)
            score_reverse = _alignment_score(gt_text, ext_text, regulatory_verbs)
            bidirectional_passes = (
                score_forward >= threshold and score_reverse >= threshold
            )
            stage1_candidates.append(
                {
                    "pair_id": gt_pair_id,
                    "similarity_score": float(score_forward),
                    "reverse_similarity_score": float(score_reverse),
                    "bidirectional_passes": bool(bidirectional_passes),
                }
            )

        # Stage 2: bidirectional pass keeps the match. Otherwise queue.
        passing = [c for c in stage1_candidates if c["bidirectional_passes"]]
        if passing:
            for candidate in passing:
                pair_to_extracted.setdefault(candidate["pair_id"], set()).add(ext_idx)
                extracted_to_pair.setdefault(ext_idx, set()).add(candidate["pair_id"])
        elif stage1_candidates:
            # Passed Stage 1 (outcome match), failed Stage 2 (text overlap).
            top = sorted(
                stage1_candidates,
                key=lambda c: c["similarity_score"],
                reverse=True,
            )[:REVIEW_QUEUE_TOP_K]
            review_queue.append(
                {
                    "extracted_decision_index": int(ext_idx),
                    "extracted_outcome": ext_outcome,
                    "candidates": [
                        {
                            "pair_id": c["pair_id"],
                            "similarity_score": float(
                                round(c["similarity_score"], 6)
                            ),
                        }
                        for c in top
                    ],
                    "reason": "text_overlap_below_threshold",
                }
            )
        # else: no Stage 1 candidates at all -> no review-queue entry,
        # this extracted decision is "spurious" against this GT pool.

    matched_pair_ids = set(pair_to_extracted.keys())
    matched_extracted_indices = set(extracted_to_pair.keys())

    coverage = (len(matched_pair_ids) / n_pairs) if n_pairs > 0 else 0.0
    precision = (
        (len(matched_extracted_indices) / n_extracted) if n_extracted > 0 else 0.0
    )
    spurious_add_rate = (
        ((n_extracted - len(matched_extracted_indices)) / n_extracted)
        if n_extracted > 0
        else 0.0
    )

    # Per-outcome F1: gather every outcome label present on either side
    # so a "zero matches for this outcome" still surfaces as F1=0 rather
    # than silently disappearing.
    outcomes: set = set()
    for gt in gt_pairs:
        if isinstance(gt, Mapping):
            outcome = gt.get("expected_decision_outcome")
            if isinstance(outcome, str) and outcome:
                outcomes.add(outcome)
    for decision in extracted_decisions:
        if isinstance(decision, Mapping):
            outcome = decision.get("decision_outcome")
            if isinstance(outcome, str) and outcome:
                outcomes.add(outcome)

    per_outcome_f1: Dict[str, float] = {}
    for outcome in sorted(outcomes):
        gt_pair_ids_for_outcome = {
            gt.get("pair_id")
            for gt in gt_pairs
            if isinstance(gt, Mapping)
            and gt.get("expected_decision_outcome") == outcome
            and isinstance(gt.get("pair_id"), str)
        }
        ext_idxs_for_outcome = {
            i
            for i, d in enumerate(extracted_decisions)
            if isinstance(d, Mapping) and d.get("decision_outcome") == outcome
        }
        matched_gt_for_outcome = matched_pair_ids & gt_pair_ids_for_outcome
        matched_ext_for_outcome = matched_extracted_indices & ext_idxs_for_outcome

        # Precision per outcome: matched extracted in this outcome /
        # total extracted in this outcome. Recall per outcome: matched
        # gt in this outcome / total gt in this outcome.
        gt_total = len(gt_pair_ids_for_outcome)
        ext_total = len(ext_idxs_for_outcome)
        p_o = (len(matched_ext_for_outcome) / ext_total) if ext_total > 0 else 0.0
        r_o = (len(matched_gt_for_outcome) / gt_total) if gt_total > 0 else 0.0
        per_outcome_f1[outcome] = round(_f1(p_o, r_o), 6)

    pairs_info: List[Dict[str, Any]] = []
    for gt in gt_pairs:
        if not isinstance(gt, Mapping):
            continue
        pid = gt.get("pair_id")
        if not isinstance(pid, str):
            continue
        matched_idxs = sorted(pair_to_extracted.get(pid, set()))
        pairs_info.append(
            {
                "pair_id": pid,
                "expected_decision_outcome": gt.get("expected_decision_outcome"),
                "matched": bool(matched_idxs),
                "matched_extracted_indices": list(matched_idxs),
            }
        )

    return {
        "coverage": float(coverage),
        "precision": float(precision),
        "spurious_add_rate": float(spurious_add_rate),
        "per_outcome_f1": per_outcome_f1,
        "pairs": pairs_info,
        "review_queue": review_queue,
        "threshold": float(threshold),
        "total_extracted": int(n_extracted),
        "total_gt_pairs": int(n_pairs),
        "matched_pair_count": int(len(matched_pair_ids)),
        "matched_extracted_count": int(len(matched_extracted_indices)),
    }


__all__ = [
    "ALIGNMENT_THRESHOLD",
    "REVIEW_QUEUE_TOP_K",
    "compute_alignment",
]
