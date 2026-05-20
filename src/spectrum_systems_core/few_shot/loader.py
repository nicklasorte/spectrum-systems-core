"""Phase 3P few-shot examples loader + prompt-section injector.

The loader fails closed on:

- ``few_shot_manifest_unreadable`` — manifest missing or malformed.
- ``few_shot_manifest_hash_mismatch`` — declared sha256 does not match
  the JSONL file's canonical bytes.
- ``few_shot_entry_invalid`` — any entry violates the schema or the
  cross-field rules (synthetic + truthy ``source_quote`` are not
  reconciled here because the registry does not carry a source_quote
  field; the rule that a synthetic entry's chunk is fully constructed
  is documented and verified by the manifest's ``has_synthetic_entries``
  flag).
- ``few_shot_ordering_invalid`` — the registry MUST end with an
  ``implicit_decision`` entry (the recency-bias property required by
  the Phase 3P research synthesis). Position is enforced positionally,
  not by searching: the LAST entry in the JSONL is the one tested.

The loader returns a frozen :class:`FewShotRegistry` carrying the
parsed entries, the manifest version, and the canonical hash. The hash
is reused by the prompt-file audit comment so a reader can attribute
the section to a specific registry version.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from ..schemas import schema_path

_LOG = logging.getLogger(__name__)

FEW_SHOT_REGISTRY_VERSION: str = "1.0.0"
FEW_SHOT_SCHEMA_VERSION: str = "1.0.0"
ARTIFACT_TYPE: str = "few_shot_entry"

_DATA_DIR: Path = Path(__file__).resolve().parents[3] / "data" / "few_shot"
FEW_SHOT_EXAMPLES_PATH: Path = _DATA_DIR / "examples_v1.jsonl"
FEW_SHOT_MANIFEST_PATH: Path = _DATA_DIR / "MANIFEST.json"

# Delimiter pair used to slice the few-shot section out of the
# canonical prompt file. The CLI flag controls whether the section
# survives at runtime; the canonical text always lives in the prompt
# file so the audit trail is one place.
FEW_SHOT_BEGIN_MARKER: str = "<!-- FEW_SHOT_BLOCK_BEGIN -->"
FEW_SHOT_END_MARKER: str = "<!-- FEW_SHOT_BLOCK_END -->"

# The 22 array types the gold_extraction must declare on every entry —
# absence of any one of these would break the "absence is OK" pattern
# the model is supposed to learn from the empty arrays.
REQUIRED_GOLD_KEYS: tuple[str, ...] = (
    "decisions",
    "action_items",
    "claims",
    "risks",
    "commitments",
    "open_questions",
    "cross_references",
    "attendees",
    "topics",
    "regulatory_references",
    "technical_parameters",
    "named_artifacts",
    "scheduled_events",
    "sentiment_indicators",
    "meeting_phases",
    "issue_registry_entry",
    "position_statement",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
)


class FewShotError(Exception):
    """Fail-closed exception with a stable machine-readable ``reason``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class FewShotEntry:
    id: str
    source_transcript_id: str
    example_type: str
    chunk_text: str
    gold_extraction: dict[str, Any]
    rationale: str
    speaker_names_stripped: bool
    synthetic: bool


@dataclass(frozen=True)
class FewShotRegistry:
    entries: tuple[FewShotEntry, ...]
    version: str
    version_hash: str
    has_synthetic_entries: bool


def _canonical_entry_bytes(entry: dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_examples_hash(entries: list[dict[str, Any]]) -> str:
    """Compute the canonical sha256 over a list of registry entries.

    Each entry is canonicalised (sorted keys, compact separators) then
    joined by ``\\n`` with no trailing newline. Mirrors the layout of
    ``data/few_shot/examples_v1.jsonl``.
    """
    parts = [_canonical_entry_bytes(e) for e in entries]
    blob = b"\n".join(parts)
    return hashlib.sha256(blob).hexdigest()


def _load_schema_doc() -> dict[str, Any]:
    return json.loads(
        schema_path("few_shot_entry").read_text(encoding="utf-8")
    )


def validate_entry(entry: dict[str, Any]) -> list[str]:
    """Return a list of validation errors for ``entry`` (empty == OK)."""
    schema = _load_schema_doc()
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(entry), key=lambda e: e.path):
        errors.append(f"{err.message} at path={list(err.absolute_path)}")
    return errors


def _read_jsonl_entries(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    entries: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        if not line:
            raise FewShotError(
                "few_shot_entries_unreadable",
                f"empty line at index {idx}",
            )
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise FewShotError(
                "few_shot_entries_unreadable",
                f"line {idx}: {exc}",
            ) from exc
    return entries


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise FewShotError(
            "few_shot_manifest_unreadable",
            f"missing: {manifest_path}",
        )
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FewShotError(
            "few_shot_manifest_unreadable",
            str(exc),
        ) from exc


def load_few_shot_registry(
    examples_path: Path | None = None,
    manifest_path: Path | None = None,
) -> FewShotRegistry:
    """Load + verify the registry; fail closed on any drift."""
    examples_path = Path(examples_path) if examples_path else FEW_SHOT_EXAMPLES_PATH
    manifest_path = Path(manifest_path) if manifest_path else FEW_SHOT_MANIFEST_PATH

    manifest = _read_manifest(manifest_path)
    declared_hash = manifest.get("sha256_hash")
    declared_version = manifest.get("version") or FEW_SHOT_REGISTRY_VERSION
    declared_entry_count = manifest.get("entry_count")
    declared_has_synthetic = manifest.get("has_synthetic_entries")
    if not isinstance(declared_hash, str) or not declared_hash:
        raise FewShotError(
            "few_shot_manifest_unreadable",
            "missing sha256_hash",
        )

    if not examples_path.is_file():
        raise FewShotError(
            "few_shot_entries_unreadable",
            f"missing: {examples_path}",
        )

    raw_entries = _read_jsonl_entries(examples_path)
    actual_hash = compute_examples_hash(raw_entries)
    if actual_hash != declared_hash:
        raise FewShotError(
            "few_shot_manifest_hash_mismatch",
            f"manifest claims {declared_hash}, file hashes to {actual_hash}",
        )
    if isinstance(declared_entry_count, int) and declared_entry_count != len(raw_entries):
        raise FewShotError(
            "few_shot_manifest_hash_mismatch",
            (
                f"manifest entry_count={declared_entry_count} != "
                f"actual {len(raw_entries)}"
            ),
        )

    parsed: list[FewShotEntry] = []
    seen_ids: set[str] = set()
    any_synthetic = False
    for idx, raw in enumerate(raw_entries, start=1):
        errors = validate_entry(raw)
        if errors:
            raise FewShotError(
                "few_shot_entry_invalid",
                f"entry {idx}: {'; '.join(errors)}",
            )
        if not isinstance(raw.get("gold_extraction"), dict):
            raise FewShotError(
                "few_shot_entry_invalid",
                f"entry {idx}: gold_extraction not a dict",
            )
        missing_keys = [
            k for k in REQUIRED_GOLD_KEYS if k not in raw["gold_extraction"]
        ]
        if missing_keys:
            raise FewShotError(
                "few_shot_entry_invalid",
                (
                    f"entry {idx}: gold_extraction missing required arrays: "
                    f"{missing_keys}"
                ),
            )
        eid = str(raw["id"])
        if eid in seen_ids:
            raise FewShotError(
                "few_shot_entry_invalid",
                f"entry {idx}: duplicate id {eid!r}",
            )
        seen_ids.add(eid)
        if bool(raw.get("synthetic", False)):
            any_synthetic = True
        parsed.append(
            FewShotEntry(
                id=eid,
                source_transcript_id=str(raw["source_transcript_id"]),
                example_type=str(raw["example_type"]),
                chunk_text=str(raw["chunk_text"]),
                gold_extraction=dict(raw["gold_extraction"]),
                rationale=str(raw["rationale"]),
                speaker_names_stripped=bool(raw["speaker_names_stripped"]),
                synthetic=bool(raw["synthetic"]),
            )
        )

    # Ordering: the LAST entry must be implicit_decision. This is the
    # recency-bias property required by the Phase 3P research and is
    # enforced positionally, not by search.
    if not parsed:
        raise FewShotError(
            "few_shot_ordering_invalid",
            "registry is empty",
        )
    if parsed[-1].example_type != "implicit_decision":
        raise FewShotError(
            "few_shot_ordering_invalid",
            (
                "last entry must have example_type='implicit_decision' "
                f"(got {parsed[-1].example_type!r})"
            ),
        )

    # The manifest's has_synthetic_entries flag must match reality.
    # This is a guard against a forgotten manifest update — without it
    # a real-corpus refresh might leave the flag truthy and mask a
    # genuine post-refresh check.
    if isinstance(declared_has_synthetic, bool):
        if declared_has_synthetic != any_synthetic:
            raise FewShotError(
                "few_shot_manifest_hash_mismatch",
                (
                    "has_synthetic_entries manifest claim "
                    f"{declared_has_synthetic} disagrees with file ({any_synthetic})"
                ),
            )

    return FewShotRegistry(
        entries=tuple(parsed),
        version=str(declared_version),
        version_hash=actual_hash,
        has_synthetic_entries=any_synthetic,
    )


def build_few_shot_block(registry: FewShotRegistry) -> str:
    """Render the Few-Shot Examples prompt section.

    The output is plain Markdown with ``<!-- generated from ... -->``
    comments so a reader of the prompt file can attribute the section
    to the registry version + hash that produced it.
    """
    if not registry.entries:
        return ""
    lines: list[str] = [
        f"<!-- generated from data/few_shot/examples_v1.jsonl "
        f"version={registry.version} hash={registry.version_hash} -->",
        "# Few-Shot Examples (additive)",
        "",
        "The following examples demonstrate correct extraction from "
        "NTIA/DoD TIG transcripts. Study the pattern in each example. "
        "Pay close attention to:",
        "- What WAS extracted and why (see rationale)",
        "- What was NOT extracted (empty arrays mean \"nothing here\")",
        "- The LAST example (implicit decision) is the most important "
        "pattern to internalize",
        "",
        "---",
    ]
    pretty_titles = {
        "explicit_decision": "Explicit Decision",
        "non_decision": "Near-Miss Non-Decision",
        "implicit_decision": "Implicit / Guidance-Phrased Decision (STUDY THIS PATTERN)",
    }
    for idx, e in enumerate(registry.entries, start=1):
        title = pretty_titles.get(e.example_type, e.example_type)
        lines.extend(
            [
                "",
                f"### Example {idx}: {title}",
                "",
                "**Transcript chunk:**",
                "",
                "```",
                e.chunk_text,
                "```",
                "",
                "**Correct extraction:**",
                "",
                "```json",
                json.dumps(e.gold_extraction, sort_keys=True, indent=2),
                "```",
                "",
                f"**Why:** {e.rationale}",
                "",
                "---",
            ]
        )
    return "\n".join(lines)


def inject_or_strip_few_shot(prompt_text: str, *, enable: bool) -> str:
    """Either keep or remove the few-shot block from ``prompt_text``.

    The canonical prompt file always carries the Few-Shot Examples
    section between :data:`FEW_SHOT_BEGIN_MARKER` and
    :data:`FEW_SHOT_END_MARKER`. When ``enable=False`` the function
    strips the entire section (markers included). When ``enable=True``
    the function strips only the marker lines themselves so the
    rendered prompt sent to the model is identical to the canonical
    file minus the comment markers.

    If the markers are missing the prompt is returned unchanged. That
    case is observed: a future prompt file edit removed the section
    entirely, in which case there is nothing to inject or strip.
    """
    if FEW_SHOT_BEGIN_MARKER not in prompt_text or FEW_SHOT_END_MARKER not in prompt_text:
        return prompt_text
    begin_idx = prompt_text.find(FEW_SHOT_BEGIN_MARKER)
    end_idx = prompt_text.find(FEW_SHOT_END_MARKER, begin_idx)
    if begin_idx < 0 or end_idx < 0 or end_idx < begin_idx:
        return prompt_text
    end_idx_inclusive = end_idx + len(FEW_SHOT_END_MARKER)
    if enable:
        # Strip only the marker LINES (including the trailing newline
        # that typically follows them) so the rendered prompt is a
        # contiguous block without the audit comments.
        before = prompt_text[:begin_idx]
        block_with_markers = prompt_text[begin_idx:end_idx_inclusive]
        after = prompt_text[end_idx_inclusive:]
        # Drop the begin/end marker lines exactly. Replace each marker
        # with empty string; then collapse any double-blank-line the
        # removal created.
        stripped_block = block_with_markers.replace(
            FEW_SHOT_BEGIN_MARKER + "\n", ""
        ).replace("\n" + FEW_SHOT_END_MARKER, "")
        # Final safety: if either marker remained un-anchored to a
        # newline, just remove the bare marker string.
        stripped_block = stripped_block.replace(
            FEW_SHOT_BEGIN_MARKER, ""
        ).replace(FEW_SHOT_END_MARKER, "")
        return before + stripped_block + after
    # Disabled path — drop the whole section. Consume one trailing
    # newline after the end marker if present so the surrounding text
    # does not gain an extra blank line.
    if end_idx_inclusive < len(prompt_text) and prompt_text[end_idx_inclusive] == "\n":
        end_idx_inclusive += 1
    return prompt_text[:begin_idx] + prompt_text[end_idx_inclusive:]


def count_missing_reason_rate(extraction_payload: dict[str, Any]) -> float:
    """Compute the fraction of decisions+action_items missing ``reason``.

    Only object-form items are inspected; legacy string-form items
    pre-date the field and are excluded from the denominator. Returns
    0.0 when neither array contains any object-form item.
    """
    decisions = extraction_payload.get("decisions", []) or []
    action_items = extraction_payload.get("action_items", []) or []
    denom = 0
    missing = 0
    for item in decisions:
        if isinstance(item, dict):
            denom += 1
            if not item.get("reason"):
                missing += 1
    for item in action_items:
        if isinstance(item, dict):
            denom += 1
            if not item.get("reason"):
                missing += 1
    if denom == 0:
        return 0.0
    return missing / denom


__all__ = [
    "ARTIFACT_TYPE",
    "FEW_SHOT_BEGIN_MARKER",
    "FEW_SHOT_END_MARKER",
    "FEW_SHOT_EXAMPLES_PATH",
    "FEW_SHOT_MANIFEST_PATH",
    "FEW_SHOT_REGISTRY_VERSION",
    "FEW_SHOT_SCHEMA_VERSION",
    "REQUIRED_GOLD_KEYS",
    "FewShotEntry",
    "FewShotError",
    "FewShotRegistry",
    "build_few_shot_block",
    "compute_examples_hash",
    "count_missing_reason_rate",
    "inject_or_strip_few_shot",
    "load_few_shot_registry",
    "validate_entry",
]
