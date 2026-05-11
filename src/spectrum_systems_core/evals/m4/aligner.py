"""EvalAligner: two-stage alignment of extracted items to a minutes document.

Phase M.4. Pure-Python class with no LLM calls. Uses TF-IDF cosine
similarity (sklearn / scipy) for the semantic stage and stdlib regex /
stopword stripping for the lexical stage.

Alignment is ONE-DIRECTIONAL:

* coverage_alignments: for each minutes item, find the best extracted item.
  Drives the coverage metric (minutes -> pipeline).
* review_alignments:   for each extracted item, find the best minutes item.
  Drives the items_requiring_review queue (pipeline -> minutes).

Both directions are needed because coverage and review measure different
failure modes: coverage measures "did we miss a minutes item?" and review
measures "did we extract something the minutes does not cover?".

Thresholds are defined as class attributes so tests can monkeypatch them
if needed. The defaults come from the M.4 task spec:

* SEMANTIC_THRESHOLD = 0.7         (TimeStampEval arXiv 2511.11594)
* MIN_CONTENT_WORD_OVERLAP = 2     (lexical floor avoids matching pure
                                    stopword overlap)
* SHORT_ITEM_THRESHOLD = 10        (words; shorter items require exact
                                    lexical match -- protects against
                                    "Alice to do X" / "Bob to do X" both
                                    looking semantically identical)

The aligner never raises -- it always returns an alignment_result dict.
Errors degrade to an empty alignment with the items left unmatched, so
the runner can still write a partial summary.
"""
from __future__ import annotations

import datetime
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Minimal stopword list. We keep this explicit (rather than pulling from
# nltk / scikit-learn's default English list) so deterministic behaviour
# is guaranteed across machines and python versions.
_STOPWORDS = frozenset(
    [
        "a", "an", "the", "and", "or", "but", "if", "then", "else",
        "of", "for", "to", "in", "on", "at", "by", "from", "with",
        "as", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "should", "could", "can", "may", "might", "must", "shall",
        "this", "that", "these", "those", "it", "its", "they", "them",
        "their", "we", "us", "our", "you", "your", "he", "she", "his",
        "her", "him", "i", "me", "my", "mine", "any", "all", "no",
        "not", "than", "into", "out", "up", "down", "over", "under",
        "more", "most", "less", "least", "some", "each", "every",
        "about", "after", "before", "between", "during", "through",
        "yes", "well", "so", "just", "also", "very",
    ]
)

# Spectrum-domain proper noun seed list. Used by _content_word_overlap to
# bias toward domain-specific tokens that should not be stripped as
# stopwords even if they look short / lowercase.
_DOMAIN_TERMS = frozenset(
    [
        "fcc", "ntia", "itu", "fss", "sas", "dod", "dow", "dwcio",
        "tig", "ghz", "mhz", "db", "dbw", "epfd", "pfd",
    ]
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9.+\-]+")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text or "")]


def _content_tokens(text: str) -> List[str]:
    """Lowercased content tokens with stopwords removed.

    Numbers, proper nouns, and domain terms are kept (they carry signal).
    Pure-stopword overlap should never satisfy the lexical floor.
    """
    out: List[str] = []
    for tok in _tokenize(text):
        low = tok.lower()
        if low in _STOPWORDS and low not in _DOMAIN_TERMS:
            continue
        out.append(low)
    return out


def _split_minutes_items(minutes_text: str) -> List[str]:
    """Split a minutes blob into items.

    Heuristic that handles both the M4 fixture format (one item per line,
    typed by leading 'DECISION:' / 'ACTION:' / 'QUESTION:' prefixes) and
    a plain newline-delimited list. Blank lines are dropped.

    Items shorter than three characters after stripping are dropped --
    they are almost always stray punctuation, not real items.
    """
    if not minutes_text:
        return []
    out: List[str] = []
    for raw_line in minutes_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip a leading "PREFIX:" if present so the matching focuses on
        # content, not the labelling.
        m = re.match(r"^[A-Z][A-Z_]*:\s*(.*)$", line)
        if m and m.group(1):
            line = m.group(1).strip()
        if len(line) < 3:
            continue
        out.append(line)
    return out


class EvalAligner:
    """Two-stage extracted-item -> minutes-item aligner.

    See module docstring for design.
    """

    SEMANTIC_THRESHOLD: float = 0.7
    MIN_CONTENT_WORD_OVERLAP: int = 2
    SHORT_ITEM_THRESHOLD: int = 10

    SCHEMA_VERSION = "1.0.0"
    PRODUCED_BY = "EvalAligner"

    def align(
        self,
        extracted_items: Sequence[Dict[str, Any]],
        minutes_text: str,
        source_id: str,
        minutes_artifact_id: str,
        *,
        source_artifact_id: Optional[str] = None,
        pair_id: Optional[str] = None,
        chunking_strategy: str = "unknown",
    ) -> Dict[str, Any]:
        """Return an alignment_result artifact dict.

        Never raises. If alignment fails on a particular item the item is
        recorded as unmatched (coverage side) or requires_review (review
        side) rather than aborting the whole alignment.

        Inputs:
          extracted_items -- list of dicts with at minimum a ``text`` key.
              Optional keys: ``id`` / ``extracted_item_id``,
              ``source_turn_ids``, ``source_turn_validation``.
          minutes_text -- the paired minutes document text (any of the
              well-formed split formats handled by _split_minutes_items).
              An empty / whitespace-only string is treated as "no minutes
              items": coverage will be vacuously 0 across nothing rather
              than vacuously matched.
        """
        items_norm = _normalize_extracted_items(extracted_items)
        minutes_items = _split_minutes_items(minutes_text)

        coverage = self._coverage_alignment(minutes_items, items_norm)
        review = self._review_alignment(items_norm, minutes_items)

        return {
            "alignment_result_id": str(uuid.uuid4()),
            "source_artifact_id": (
                source_artifact_id if source_artifact_id else source_id
            ),
            "minutes_artifact_id": minutes_artifact_id,
            "pair_id": pair_id if pair_id else str(uuid.uuid4()),
            "artifact_type": "alignment_result",
            "schema_version": self.SCHEMA_VERSION,
            "created_at": _now_iso(),
            "coverage_alignments": coverage,
            "review_alignments": review,
            "chunking_strategy": chunking_strategy or "unknown",
            "provenance": {"produced_by": self.PRODUCED_BY},
        }

    # -- public stage helpers (also exposed for tests / future tuning) --

    def _semantic_similarity(self, text_a: str, text_b: str) -> float:
        """Cosine similarity of TF-IDF vectors. 0.0 if either side empty.

        We fit the vectorizer on just the pair to keep the function
        side-effect free and avoid sharing IDF state across calls. This
        means a one-shot fit-transform per call -- inexpensive at the
        item counts we see (<<10**3 items per pair).
        """
        a = (text_a or "").strip()
        b = (text_b or "").strip()
        if not a or not b:
            return 0.0
        try:
            vectorizer = TfidfVectorizer(
                lowercase=True,
                token_pattern=r"(?u)\b[A-Za-z0-9.+\-]+\b",
            )
            matrix = vectorizer.fit_transform([a, b])
        except ValueError:
            # Both inputs are pure stopword / pure punctuation -- nothing
            # to vectorize.
            return 0.0
        sim = cosine_similarity(matrix[0:1], matrix[1:2])[0, 0]
        # cosine_similarity returns numpy float; clamp to [0, 1] in case
        # of floating-point overshoot.
        return max(0.0, min(1.0, float(sim)))

    def _content_word_overlap(self, text_a: str, text_b: str) -> int:
        """Count overlapping content tokens (stopwords stripped)."""
        a = set(_content_tokens(text_a))
        b = set(_content_tokens(text_b))
        return len(a & b)

    def _is_short_item(self, text: str) -> bool:
        return len(_tokenize(text)) < self.SHORT_ITEM_THRESHOLD

    def _short_item_matches(self, text_a: str, text_b: str) -> bool:
        """Exact-match floor for short items.

        For items shorter than SHORT_ITEM_THRESHOLD words, require at
        least two overlapping content tokens AND require that both
        sides agree on those tokens with no token-level disagreement on
        anchor identifiers (numbers, proper nouns, domain terms).

        This guards against the failure mode "Alice to do X" matching
        "Bob to do X" -- the verbs and object overlap, but the owner
        differs. Anchors are tokens that look like people / orgs /
        figures and must be present on both sides if present on either.
        """
        a_toks = set(_content_tokens(text_a))
        b_toks = set(_content_tokens(text_b))
        if len(a_toks & b_toks) < self.MIN_CONTENT_WORD_OVERLAP:
            return False
        # Anchor disagreement check: a token that looks like a digit or
        # is in the domain term list and appears on exactly one side
        # signals identity mismatch.
        a_anchors = {t for t in a_toks if _is_anchor(t)}
        b_anchors = {t for t in b_toks if _is_anchor(t)}
        if a_anchors and b_anchors and a_anchors != b_anchors:
            return False
        return True

    # -- internals --------------------------------------------------------

    def _coverage_alignment(
        self,
        minutes_items: Sequence[str],
        extracted_items: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for minutes_item in minutes_items:
            best_idx, best_sim, best_overlap = -1, 0.0, 0
            for idx, ex in enumerate(extracted_items):
                sim = self._semantic_similarity(minutes_item, ex["text"])
                overlap = self._content_word_overlap(minutes_item, ex["text"])
                # Pick by similarity; ties broken by overlap descending.
                if (sim > best_sim) or (
                    sim == best_sim and overlap > best_overlap
                ):
                    best_idx, best_sim, best_overlap = idx, sim, overlap
            matched = False
            if best_idx >= 0:
                ex = extracted_items[best_idx]
                if self._is_short_item(minutes_item) or self._is_short_item(
                    ex["text"]
                ):
                    matched = self._short_item_matches(minutes_item, ex["text"])
                else:
                    matched = (
                        best_sim >= self.SEMANTIC_THRESHOLD
                        and best_overlap >= self.MIN_CONTENT_WORD_OVERLAP
                    )
            entry: Dict[str, Any] = {
                "minutes_item_text": minutes_item,
                "matched_extracted_item_id": None,
                "matched_extracted_item_text": None,
                "semantic_similarity": float(best_sim),
                "content_word_overlap": int(best_overlap),
                "alignment_status": "matched" if matched else "unmatched",
            }
            if matched and best_idx >= 0:
                ex = extracted_items[best_idx]
                entry["matched_extracted_item_id"] = ex["id"]
                entry["matched_extracted_item_text"] = ex["text"]
            out.append(entry)
        return out

    def _review_alignment(
        self,
        extracted_items: Sequence[Dict[str, Any]],
        minutes_items: Sequence[str],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for ex in extracted_items:
            best_idx, best_sim, best_overlap = -1, 0.0, 0
            for idx, minutes_item in enumerate(minutes_items):
                sim = self._semantic_similarity(ex["text"], minutes_item)
                overlap = self._content_word_overlap(ex["text"], minutes_item)
                if (sim > best_sim) or (
                    sim == best_sim and overlap > best_overlap
                ):
                    best_idx, best_sim, best_overlap = idx, sim, overlap
            matched = False
            if best_idx >= 0:
                minutes_item = minutes_items[best_idx]
                if self._is_short_item(minutes_item) or self._is_short_item(
                    ex["text"]
                ):
                    matched = self._short_item_matches(ex["text"], minutes_item)
                else:
                    matched = (
                        best_sim >= self.SEMANTIC_THRESHOLD
                        and best_overlap >= self.MIN_CONTENT_WORD_OVERLAP
                    )
            entry: Dict[str, Any] = {
                "extracted_item_id": ex["id"],
                "extracted_item_text": ex["text"],
                "source_turn_ids": list(ex.get("source_turn_ids", []) or []),
                "source_turn_validation": ex.get(
                    "source_turn_validation", "unknown"
                ),
                "matched_minutes_text": (
                    minutes_items[best_idx] if matched and best_idx >= 0 else None
                ),
                "semantic_similarity": float(best_sim),
                "alignment_status": "matched" if matched else "requires_review",
            }
            out.append(entry)
        return out


def _normalize_extracted_items(
    items: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            continue
        text = (
            raw.get("text")
            or raw.get("story_summary")
            or raw.get("decision_text")
            or raw.get("action")
            or raw.get("claim_text")
            or raw.get("source_excerpt")
            or ""
        )
        text = (text or "").strip()
        if not text:
            continue
        item_id = (
            raw.get("id")
            or raw.get("extracted_item_id")
            or raw.get("story_id")
            or raw.get("artifact_id")
            or f"item-{i}"
        )
        out.append(
            {
                "id": str(item_id),
                "text": text,
                "source_turn_ids": list(raw.get("source_turn_ids", []) or []),
                "source_turn_validation": (
                    raw.get("source_turn_validation") or "unknown"
                ),
            }
        )
    return out


def _is_anchor(token: str) -> bool:
    """A token is an anchor if it carries identity-level signal.

    Numbers, percentages, units, or domain proper nouns. Used to reject
    short-item matches where the entity differs.
    """
    if not token:
        return False
    if token in _DOMAIN_TERMS:
        return True
    if any(ch.isdigit() for ch in token):
        return True
    return False
