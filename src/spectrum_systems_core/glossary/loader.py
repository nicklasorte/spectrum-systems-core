"""Phase 2P NTIA/DoD glossary loader, matcher, and chunk-context block builder.

This is a standalone infrastructure module that lives alongside the
legacy ``glossary_builder`` / ``term_injector`` system. The two do not
share data — Phase 2P uses the JSONL artifact at
``data/glossary/ntia_dod_spectrum_v1.jsonl`` plus a manifest hash gate
under ``data/glossary/MANIFEST.json``. The legacy system stays in
place and is not touched by this phase.

The loader fails closed:
- ``glossary_manifest_unreadable`` when MANIFEST.json is missing or
  malformed JSON.
- ``glossary_manifest_hash_mismatch`` when the glossary JSONL hash
  does not match the manifest claim.
- ``glossary_allowed_sources_hash_mismatch`` when the
  ``allowed_sources.json`` hash does not match the manifest claim.

Modal verbs (``shall``, ``should``, ``may``, ``will``, ``would``) are
prohibited as a ``term`` or in ``aliases`` (case-insensitive). The
custom validator (``validate_entry``) rejects these — JSON Schema
cannot natively express the rule because it is per-field over a
dynamic list.

Matching contract:
- Pattern ``(?<![a-zA-Z0-9])TERM(?![a-zA-Z0-9])`` (alphanumeric
  negative lookarounds). The term is ``re.escape``-d so regex
  metacharacters in the term itself (e.g. ``I/N``) are treated as
  literals.
- ``is_acronym: true`` -> case-sensitive, uppercase-only match (the
  term is compared literally; lowercase occurrences are NOT matches).
- ``is_acronym: false`` -> case-insensitive.
- ``disambiguation_required: true`` -> the entry matches only when at
  least one of ``co_occurring_terms`` also appears in the chunk
  (same matching rules).
- Ranking: top ``max_terms`` by ``priority_weight`` descending, with
  ties broken alphabetically by ``term`` (ascending).
- Returns ``(matched, truncated)`` where ``truncated`` is the count
  of additional matches dropped by the cap (>= 0).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

GLOSSARY_VERSION: str = "1.0.0"
GLOSSARY_SCHEMA_VERSION: str = "1.0.0"
ARTIFACT_TYPE: str = "glossary_entry"
DEFAULT_MAX_TERMS: int = 3

# Modal verbs are governed by the prompt section, not the glossary.
MODAL_VERBS: frozenset[str] = frozenset(
    {"shall", "should", "may", "will", "would"}
)

# Field set the schema requires on every entry (mirrors the JSON
# Schema's ``required`` list). The loader checks this explicitly so a
# missing field surfaces as a clear ``glossary_entry_invalid`` error
# rather than a KeyError deep in the matcher.
REQUIRED_ENTRY_FIELDS: tuple[str, ...] = (
    "artifact_type",
    "schema_version",
    "term",
    "aliases",
    "definition",
    "authoritative_source",
    "category",
    "created_at",
    "is_acronym",
    "disambiguation_required",
    "co_occurring_terms",
    "priority_weight",
)


class GlossaryError(Exception):
    """Base class for fail-closed glossary load errors.

    Carries a short machine-readable ``reason`` string so callers can
    branch on the failure mode (manifest unreadable vs hash mismatch
    vs entry invalid) without parsing the exception message.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    aliases: tuple[str, ...]
    definition: str
    authoritative_source: str
    category: str
    is_acronym: bool
    disambiguation_required: bool
    co_occurring_terms: tuple[str, ...]
    priority_weight: float


@dataclass
class Glossary:
    entries: tuple[GlossaryEntry, ...]
    version: str
    version_hash: str
    _compiled: dict[str, list[tuple[re.Pattern[str], bool]]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        compiled: dict[str, list[tuple[re.Pattern[str], bool]]] = {}
        for entry in self.entries:
            patterns: list[tuple[re.Pattern[str], bool]] = []
            forms = [entry.term, *entry.aliases]
            for form in forms:
                if not form:
                    continue
                escaped = re.escape(form)
                # Negative lookarounds are alphanumeric-only so that
                # tokens like "CBRS-PAL", "CBRS_certified", "(CBRS)"
                # and "MSS." match, while "missions" and "MSSx" do
                # not.
                regex = re.compile(
                    rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])",
                    flags=0 if entry.is_acronym else re.IGNORECASE,
                )
                patterns.append((regex, entry.is_acronym))
            compiled[entry.term] = patterns
        object.__setattr__(self, "_compiled", compiled)

    def _term_present(self, text: str, term_or_alias: str, is_acronym: bool) -> bool:
        escaped = re.escape(term_or_alias)
        flags = 0 if is_acronym else re.IGNORECASE
        return re.search(
            rf"(?<![a-zA-Z0-9]){escaped}(?![a-zA-Z0-9])",
            text,
            flags=flags,
        ) is not None

    def _entry_matches(self, entry: GlossaryEntry, chunk_text: str) -> bool:
        patterns = self._compiled.get(entry.term, [])
        for regex, _ in patterns:
            if regex.search(chunk_text):
                if not entry.disambiguation_required:
                    return True
                # Disambiguation: require at least one co-occurring
                # term to also be present. Case-insensitive for
                # co-occurring terms (they are content words like
                # "band" / "frequency", not acronyms).
                for co in entry.co_occurring_terms:
                    if self._term_present(chunk_text, co, is_acronym=False):
                        return True
                return False
        return False

    def match(
        self, chunk_text: str, max_terms: int = DEFAULT_MAX_TERMS
    ) -> tuple[list[GlossaryEntry], int]:
        """Return ``(matched_entries, truncated_count)``.

        Matches against ``term`` and each entry in ``aliases``. Ranks
        all hits by ``priority_weight`` descending, breaking ties
        alphabetically by ``term`` ascending. Caps the returned list
        at ``max_terms``. The truncation count is the number of
        additional matches dropped by the cap (always >= 0).
        """
        if not isinstance(chunk_text, str) or not chunk_text:
            return [], 0
        if max_terms < 0:
            max_terms = 0
        hits: list[GlossaryEntry] = [
            e for e in self.entries if self._entry_matches(e, chunk_text)
        ]
        hits.sort(key=lambda e: (-e.priority_weight, e.term))
        truncated = max(0, len(hits) - max_terms)
        return hits[:max_terms], truncated


def _canonical_entry_bytes(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_glossary_hash(entries: list[dict[str, Any]]) -> str:
    """Compute the canonical sha256 over a list of glossary entries.

    Each entry is canonicalized (sorted keys, compact separators)
    then joined by ``\\n`` with no trailing newline. This matches the
    on-disk JSONL byte layout the manifest claims.
    """
    parts = [_canonical_entry_bytes(e) for e in entries]
    blob = b"\n".join(parts)
    return hashlib.sha256(blob).hexdigest()


def compute_allowed_sources_hash(allowed: list[str]) -> str:
    serialized = json.dumps(allowed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_entry(
    entry: dict[str, Any],
    allowed_sources: list[str],
) -> list[str]:
    """Return a list of validation errors for ``entry`` (empty == OK).

    Errors covered:
    - missing required fields,
    - wrong artifact_type / schema_version,
    - modal verbs in ``term`` or ``aliases`` (case-insensitive),
    - ``authoritative_source`` not matching any whitelisted prefix,
    - ``priority_weight`` out of [0.1, 10.0],
    - ``disambiguation_required: true`` with empty
      ``co_occurring_terms``.

    The JSON Schema separately guarantees field shapes; this function
    enforces the cross-field semantics the schema cannot express.
    """
    errors: list[str] = []
    for field_name in REQUIRED_ENTRY_FIELDS:
        if field_name not in entry:
            errors.append(f"missing_field:{field_name}")
    if entry.get("artifact_type") != ARTIFACT_TYPE:
        errors.append(
            f"artifact_type_invalid:{entry.get('artifact_type')!r}"
        )
    if entry.get("schema_version") != GLOSSARY_SCHEMA_VERSION:
        errors.append(
            f"schema_version_invalid:{entry.get('schema_version')!r}"
        )
    term = entry.get("term")
    if isinstance(term, str) and term.strip().lower() in MODAL_VERBS:
        errors.append(f"modal_verb_as_term:{term!r}")
    aliases = entry.get("aliases", [])
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip().lower() in MODAL_VERBS:
                errors.append(f"modal_verb_as_alias:{alias!r}")
    src = entry.get("authoritative_source", "")
    if isinstance(src, str) and src:
        if not _matches_allowed_source(src, allowed_sources):
            errors.append(f"authoritative_source_not_allowed:{src!r}")
    weight = entry.get("priority_weight")
    if isinstance(weight, (int, float)):
        if weight < 0.1 or weight > 10.0:
            errors.append(f"priority_weight_out_of_range:{weight!r}")
    if entry.get("disambiguation_required") is True:
        co = entry.get("co_occurring_terms", [])
        if not isinstance(co, list) or not co:
            errors.append("disambiguation_required_without_co_occurring_terms")
    return errors


def _matches_allowed_source(value: str, allowed: list[str]) -> bool:
    """Return True if ``value`` starts with one of ``allowed`` followed
    by end-of-string or a space.

    Prefix matching exists because real citations include section
    numbers (``47 CFR 96``, ``ITU-R F.758``, ``3GPP TS 38.101``) that
    cannot all be enumerated in advance. The whitelist binds the
    PREFIX so an attacker cannot smuggle ``"random source 47 CFR"``
    or ``"47 CFRevil"`` through.
    """
    for prefix in allowed:
        if value == prefix:
            return True
        if value.startswith(prefix + " "):
            return True
    return False


def _read_jsonl_entries(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    # Tolerate a single trailing newline but reject any extra blank
    # lines because they affect the canonical-bytes hash.
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    entries: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        if not line:
            raise GlossaryError(
                "glossary_entries_unreadable",
                f"empty line at index {idx}",
            )
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise GlossaryError(
                "glossary_entries_unreadable",
                f"line {idx}: {exc}",
            ) from exc
    return entries


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise GlossaryError(
            "glossary_manifest_unreadable",
            f"missing: {manifest_path}",
        )
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GlossaryError(
            "glossary_manifest_unreadable",
            str(exc),
        ) from exc


def _read_allowed_sources(path: Path) -> tuple[list[str], str]:
    if not path.is_file():
        raise GlossaryError(
            "glossary_allowed_sources_unreadable",
            f"missing: {path}",
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GlossaryError(
            "glossary_allowed_sources_unreadable",
            str(exc),
        ) from exc
    allowed = doc.get("allowed_sources")
    if not isinstance(allowed, list) or not all(isinstance(s, str) for s in allowed):
        raise GlossaryError(
            "glossary_allowed_sources_unreadable",
            "allowed_sources missing or wrong shape",
        )
    declared_hash = doc.get("sha256_hash", "")
    if not isinstance(declared_hash, str) or not declared_hash:
        raise GlossaryError(
            "glossary_allowed_sources_unreadable",
            "sha256_hash missing",
        )
    actual = compute_allowed_sources_hash(allowed)
    if actual != declared_hash:
        raise GlossaryError(
            "glossary_allowed_sources_hash_mismatch",
            f"expected {declared_hash}, got {actual}",
        )
    return allowed, actual


def load_glossary(
    glossary_path: Path,
    manifest_path: Path,
    allowed_sources_path: Path | None = None,
) -> Glossary:
    """Load the glossary, verifying both manifest hashes.

    Fails closed via ``GlossaryError`` on any of:
    - manifest unreadable / wrong shape,
    - glossary JSONL hash != manifest claim,
    - allowed_sources hash != manifest claim,
    - any entry failing :func:`validate_entry`.
    """
    glossary_path = Path(glossary_path)
    manifest_path = Path(manifest_path)
    if allowed_sources_path is None:
        allowed_sources_path = glossary_path.parent / "allowed_sources.json"
    else:
        allowed_sources_path = Path(allowed_sources_path)

    manifest = _read_manifest(manifest_path)
    declared_glossary_hash = manifest.get("sha256_hash")
    declared_allowed_hash = manifest.get("allowed_sources_hash")
    declared_version = manifest.get("version") or GLOSSARY_VERSION
    if (
        not isinstance(declared_glossary_hash, str)
        or not declared_glossary_hash
        or not isinstance(declared_allowed_hash, str)
        or not declared_allowed_hash
    ):
        raise GlossaryError(
            "glossary_manifest_unreadable",
            "missing sha256_hash or allowed_sources_hash",
        )

    allowed_sources, actual_allowed_hash = _read_allowed_sources(
        allowed_sources_path
    )
    if actual_allowed_hash != declared_allowed_hash:
        raise GlossaryError(
            "glossary_allowed_sources_hash_mismatch",
            f"manifest claims {declared_allowed_hash}, file hashes to {actual_allowed_hash}",
        )

    if not glossary_path.is_file():
        raise GlossaryError(
            "glossary_entries_unreadable",
            f"missing: {glossary_path}",
        )
    raw_entries = _read_jsonl_entries(glossary_path)
    actual_glossary_hash = compute_glossary_hash(raw_entries)
    if actual_glossary_hash != declared_glossary_hash:
        raise GlossaryError(
            "glossary_manifest_hash_mismatch",
            f"manifest claims {declared_glossary_hash}, file hashes to {actual_glossary_hash}",
        )

    seen_terms: dict[str, dict[str, Any]] = {}
    parsed: list[GlossaryEntry] = []
    for idx, raw in enumerate(raw_entries, start=1):
        errors = validate_entry(raw, allowed_sources)
        if errors:
            raise GlossaryError(
                "glossary_entry_invalid",
                f"entry {idx}: {';'.join(errors)}",
            )
        term_key = str(raw["term"])
        if term_key in seen_terms:
            raise GlossaryError(
                "glossary_duplicate_term",
                f"entry {idx}: duplicate term {term_key!r}",
            )
        seen_terms[term_key] = raw
        parsed.append(
            GlossaryEntry(
                term=str(raw["term"]),
                aliases=tuple(str(a) for a in raw.get("aliases", [])),
                definition=str(raw["definition"]),
                authoritative_source=str(raw["authoritative_source"]),
                category=str(raw["category"]),
                is_acronym=bool(raw["is_acronym"]),
                disambiguation_required=bool(raw["disambiguation_required"]),
                co_occurring_terms=tuple(
                    str(c) for c in raw.get("co_occurring_terms", [])
                ),
                priority_weight=float(raw["priority_weight"]),
            )
        )

    return Glossary(
        entries=tuple(parsed),
        version=str(declared_version),
        version_hash=actual_glossary_hash,
    )


def format_terminology_block(
    matched: list[GlossaryEntry],
    truncated: int,
    *,
    version_hash: str | None = None,
) -> str:
    """Render the chunk-context Terminology block.

    Includes the matched term, definition, and source on each line.
    Truncation count is rendered when positive. The version hash is
    embedded so a reader of the chunk context can attribute the
    matched terms to the specific glossary version (the artifact
    envelope itself is NOT modified by this phase).
    """
    if not matched:
        return ""
    lines: list[str] = ["Terminology relevant to this section:"]
    for entry in matched:
        lines.append(
            f"- {entry.term}: {entry.definition} "
            f"(source: {entry.authoritative_source})"
        )
    if truncated > 0:
        lines.append(
            f"({truncated} additional terms truncated by priority)"
        )
    if version_hash:
        lines.append(f"(glossary_version_hash: {version_hash[:16]})")
    return "\n".join(lines)


def build_chunk_context(
    chunk_text: str,
    glossary: Glossary | None,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
    existing_block: str = "",
) -> str:
    """Compose chunk context with optional glossary block.

    Returns ``existing_block`` unchanged when ``glossary`` is None or
    no entry matches. When a block is added it follows ``existing_block``
    separated by a blank line. The version hash is recorded in the
    appended block (and emitted to the debug log) so cross-version
    runs are distinguishable in the chunk context bytes.
    """
    if glossary is None:
        return existing_block
    matched, truncated = glossary.match(chunk_text, max_terms=max_terms)
    if not matched:
        return existing_block
    block = format_terminology_block(
        matched, truncated, version_hash=glossary.version_hash
    )
    _LOG.debug(
        "glossary_injected version=%s hash=%s n_matched=%d truncated=%d",
        glossary.version,
        glossary.version_hash,
        len(matched),
        truncated,
    )
    if not existing_block:
        return block
    return f"{existing_block}\n\n{block}"


__all__ = [
    "ARTIFACT_TYPE",
    "DEFAULT_MAX_TERMS",
    "GLOSSARY_SCHEMA_VERSION",
    "GLOSSARY_VERSION",
    "MODAL_VERBS",
    "REQUIRED_ENTRY_FIELDS",
    "Glossary",
    "GlossaryEntry",
    "GlossaryError",
    "build_chunk_context",
    "compute_allowed_sources_hash",
    "compute_glossary_hash",
    "format_terminology_block",
    "load_glossary",
    "validate_entry",
]
