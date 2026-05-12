"""Phase V.2: per-chunk glossary term injection.

Lexical-match a chunk's text against the versioned glossary terms.
Matching is case-insensitive on both ``term`` and ``abbreviation``.
The number of injected terms is capped (default 10) and the
definition is truncated to ``short_definition`` (<= 200 chars) so the
prompt cannot be flooded with multi-paragraph definitions.

Per the red team:
- Case-insensitive (Attack 2).
- short_definition guards definition_truncated context-rot (Attack 3).
- ``find_matching_terms`` returns an empty list (not None) on no
  match, so downstream ``glossary_terms_injected`` is always a list
  shape (Attack 4 silent-degradation).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence


# Hard ceiling on injected terms per chunk. Override via env.
_MAX_TERMS_DEFAULT: int = 10
_MAX_TERMS_ENV: str = "MAX_GLOSSARY_TERMS_PER_CHUNK"

# Hard ceiling on each definition rendered in the block. Used when a
# term carries no ``short_definition``; truncates the long
# ``definition`` to this many characters.
MAX_DEFINITION_CHARS: int = 200


def _max_terms() -> int:
    raw = os.environ.get(_MAX_TERMS_ENV, "").strip()
    if not raw:
        return _MAX_TERMS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _MAX_TERMS_DEFAULT
    if value < 0:
        return 0
    return value


def _term_form(term: Dict[str, Any]) -> str:
    """Return the canonical term string used for matching/display."""
    return str(term.get("term") or "")


def _abbreviation_form(term: Dict[str, Any]) -> Optional[str]:
    abbrev = term.get("abbreviation")
    if isinstance(abbrev, str) and abbrev.strip():
        return abbrev
    return None


def find_matching_terms(
    chunk_text: str,
    glossary_terms: Sequence[Dict[str, Any]],
    *,
    max_terms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return glossary terms whose ``term`` or ``abbreviation`` appears
    in ``chunk_text`` (case-insensitive). Capped at ``max_terms``.

    Order preserves the input glossary order so the test of
    determinism is straightforward: same glossary + same chunk_text
    yields the same injection order.
    """
    if not isinstance(chunk_text, str) or not chunk_text.strip():
        return []
    cap = _max_terms() if max_terms is None else max(0, int(max_terms))
    if cap == 0:
        return []
    chunk_lower = chunk_text.lower()
    matched: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for term in glossary_terms or []:
        if not isinstance(term, dict):
            continue
        term_id = str(term.get("term_id") or "")
        if term_id and term_id in seen_ids:
            continue
        canonical = _term_form(term).lower()
        abbrev = _abbreviation_form(term)
        hit = False
        if canonical and canonical in chunk_lower:
            hit = True
        elif abbrev and abbrev.lower() in chunk_lower:
            hit = True
        if hit:
            matched.append(term)
            if term_id:
                seen_ids.add(term_id)
        if len(matched) >= cap:
            break
    return matched


def _definition_for_block(term: Dict[str, Any]) -> tuple[str, bool]:
    """Return ``(definition_to_render, truncated)``.

    Prefer ``short_definition``. Fall back to first
    ``MAX_DEFINITION_CHARS`` of ``definition`` and set the truncated
    flag so the caller can record it.
    """
    sd = term.get("short_definition")
    if isinstance(sd, str) and sd:
        return sd, False
    raw = term.get("definition")
    if not isinstance(raw, str):
        return "", False
    if len(raw) <= MAX_DEFINITION_CHARS:
        return raw, False
    return raw[:MAX_DEFINITION_CHARS], True


def build_terminology_block(matched_terms: Iterable[Dict[str, Any]]) -> str:
    """Render the ``TERMINOLOGY FOR THIS SECTION`` prompt block.

    Empty input -> empty string (caller can append unconditionally).
    """
    terms = list(matched_terms or [])
    if not terms:
        return ""
    lines: List[str] = [
        "TERMINOLOGY FOR THIS SECTION (read-only -- do not include in output):",
        "=" * 30,
    ]
    for term in terms:
        name = _term_form(term)
        if not name:
            continue
        abbrev = _abbreviation_form(term)
        defn, _trunc = _definition_for_block(term)
        suffix = f" ({abbrev})" if abbrev else ""
        lines.append(f"- {name}{suffix}: {defn}")
    lines.append("")
    return "\n".join(lines)


def summarize_injections(
    chunks_to_terms: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Build the ``glossary_injection_summary`` for orchestration_result.

    ``chunks_to_terms`` is a mapping from chunk_id to the list of
    matched term dicts. Returns the summary dict shape described in
    the Phase V design.
    """
    chunks_with = 0
    chunks_without = 0
    total_injections = 0
    total_chars = 0
    name_counts: Dict[str, int] = {}
    for terms in chunks_to_terms.values():
        if terms:
            chunks_with += 1
            total_injections += len(terms)
            for t in terms:
                name = _term_form(t)
                if name:
                    name_counts[name] = name_counts.get(name, 0) + 1
                defn, _ = _definition_for_block(t)
                total_chars += len(defn)
        else:
            chunks_without += 1
    most = sorted(
        name_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )[:5]
    return {
        "chunks_with_matches": chunks_with,
        "chunks_with_no_matches": chunks_without,
        "total_term_injections": total_injections,
        "most_injected_terms": [n for n, _ in most],
        "total_injection_chars": total_chars,
    }


__all__ = [
    "MAX_DEFINITION_CHARS",
    "build_terminology_block",
    "find_matching_terms",
    "summarize_injections",
]
