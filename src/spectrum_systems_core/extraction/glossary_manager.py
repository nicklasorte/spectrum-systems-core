"""GlossaryManager: load + inverted-index spectrum glossary terms.

Phase M2. Each glossary_term artifact lives under
``<SDL_ROOT>/glossary/<slug>.json`` (or any path passed to the
constructor). The manager loads them once, builds an inverted token
index once, and serves per-chunk retrieval with a small (top-5)
cap so the prompt-injection block stays under control.

Design rules:
- Index is built exactly once per instance. ``_build_index`` is
  idempotent and guarded by ``self._indexed``; an explicit
  ``index_built_count`` counter is exposed so tests can assert that
  building never happens per-chunk.
- Retrieval is case-insensitive substring match on the term itself.
  A term matches a chunk if any case-insensitive form of the term
  appears as a substring of the chunk text.
- Ranking: by character length of the matched term, descending
  (longer, more-specific terms win). Ties broken alphabetically by
  term for deterministic order. Capped at ``max_terms`` (default 5).
- The format block always includes a "do not include in output"
  instruction so the LLM cannot mistake injected definitions for
  desired output.
- All methods never raise. Failures degrade to an empty result.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_READ_ONLY_HEADER = (
    "TERMINOLOGY FOR THIS SECTION (read-only -- do not include in output):"
)
_BLOCK_DELIM = "---"


class GlossaryManager:
    """Load + retrieve glossary terms for prompt injection."""

    def __init__(self, glossary_path: Optional[str] = None) -> None:
        self._terms: Dict[str, Dict[str, Any]] = {}
        self._lower_to_term: Dict[str, str] = {}
        self._indexed: bool = False
        self.index_built_count: int = 0
        if glossary_path:
            self._load(glossary_path)
            self._build_index()

    # -- loading ----------------------------------------------------------

    def _load(self, glossary_path: str) -> None:
        root = Path(glossary_path)
        if not root.exists() or not root.is_dir():
            return
        for path in sorted(root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("artifact_type") != "glossary_term":
                continue
            term = data.get("term")
            if not isinstance(term, str) or not term:
                continue
            self._terms[term] = data

    def _build_index(self) -> None:
        """Build the inverted index. Idempotent; counted for tests."""
        if self._indexed:
            return
        self._lower_to_term = {t.lower(): t for t in self._terms}
        self._indexed = True
        self.index_built_count += 1

    # -- retrieval --------------------------------------------------------

    def retrieve_for_chunk(
        self,
        chunk_text: str,
        max_terms: int = 5,
    ) -> List[Dict[str, Any]]:
        """Return up to ``max_terms`` matching glossary_term artifacts.

        A term matches if its lowercase form appears as a substring of the
        lowercased chunk text. Results are ranked by term length
        (descending), with alphabetical tiebreak.

        Never raises. Returns ``[]`` for empty input or no matches.
        """
        if not isinstance(chunk_text, str) or not chunk_text.strip():
            return []
        if max_terms <= 0:
            return []
        # The index must exist before retrieval. If the caller forgot to
        # construct with a path, _indexed will be False; build it from
        # whatever we have (possibly empty) so retrieval is safe.
        if not self._indexed:
            self._build_index()

        chunk_lower = chunk_text.lower()
        matches: List[Dict[str, Any]] = []
        for term_lower, term in self._lower_to_term.items():
            if term_lower in chunk_lower:
                matches.append(self._terms[term])

        # Rank: longer terms first (more specific), then alphabetical.
        matches.sort(key=lambda t: (-len(t["term"]), t["term"]))
        return matches[:max_terms]

    # -- formatting -------------------------------------------------------

    def format_for_prompt(self, terms: Iterable[Dict[str, Any]]) -> str:
        """Format ``terms`` as a read-only prompt-injection block.

        Empty terms -> empty string. Never raises.
        """
        items = list(terms or [])
        if not items:
            return ""
        lines: List[str] = [_BLOCK_DELIM, _READ_ONLY_HEADER]
        for t in items:
            term = t.get("term", "")
            definition = t.get("definition", "")
            source = t.get("authoritative_source", "")
            lines.append(f"- {term}: {definition}")
            if source:
                lines.append(f"  Source: {source}")
            if t.get("is_regulatory_verb"):
                verb_def = t.get("canonical_verb_definition") or ""
                if verb_def:
                    lines.append(f"  Canonical use: {verb_def}")
        lines.append(_BLOCK_DELIM)
        return "\n".join(lines)

    # -- convenience ------------------------------------------------------

    @property
    def term_count(self) -> int:
        return len(self._terms)

    @property
    def is_indexed(self) -> bool:
        return self._indexed
