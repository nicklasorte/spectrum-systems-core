"""Deterministic text similarity helpers.

Shared by Phase D's IssueRegistry and Phase E's PatternIndexer.

Per SSC-VISION-001: vector/embedding/semantic similarity is out of scope
until structured retrieval and Jaccard word similarity prove insufficient.
"""
from __future__ import annotations


def jaccard(text_a: str, text_b: str, min_word_length: int = 4) -> float:
    """Jaccard word-set similarity.

    Lowercases each text, splits on whitespace, drops words shorter than
    min_word_length, and returns |A ∩ B| / |A ∪ B|. Returns 0.0 if either
    side is empty after filtering.
    """
    words_a = {w.lower() for w in (text_a or "").split() if len(w) >= min_word_length}
    words_b = {w.lower() for w in (text_b or "").split() if len(w) >= min_word_length}
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)
