"""Phase P3-A T-1: chunk metadata contract gate.

Verifies that every chunk produced by ``chunker.py`` carries the
metadata fields required by downstream consumers:

  - ``chunk_id``  — the canonical turn identifier (used everywhere
    via ``source_turn_ids`` lists). Without it the source_turn
    validity gate cannot resolve item provenance.
  - ``speaker``   — required by speaker attribution and per-turn
    diversity metrics; a chunk without a speaker indicates the
    chunker fell back to character mode without preserving turn
    boundaries.
  - ``agenda_item_id`` — Phase X2 contract. Always a non-empty
    string when ``AGENDA_DETECTION_ENABLED=true`` (either an
    ``AI-NNN`` id or the literal string ``"unclassified"``). Absent
    only when agenda detection is disabled, which the gate treats
    as a permitted rollback path rather than a violation.

The gate is graceful-degradation by default: findings surface in
the run output but the extraction continues. Set
``STRICT_CHUNK_METADATA=true`` to promote the violations to a halt
(exit 1) so a CI run cannot silently process chunks missing
metadata.

The task spec (``Phase P3-A`` section C) names ``turn_id`` as the
required field; in this repo the canonical identifier is
``chunk_id``. The gate treats either name as satisfying the
contract so a future rename does not silently regress.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# The canonical identifier the codebase uses is ``chunk_id``. We
# accept ``turn_id`` as an alias because the research-doc task
# spec uses that name; the gate's contract is "either name
# satisfies the field" so a future rename is a no-op.
_CHUNK_ID_FIELDS: tuple = ("chunk_id", "turn_id")

# Required fields the gate checks. Order is preserved so the
# emitted findings list is deterministic across runs.
REQUIRED_CHUNK_FIELDS: tuple = ("chunk_id_or_turn_id", "speaker", "agenda_item_id")

STRICT_ENV_VAR: str = "STRICT_CHUNK_METADATA"


def _strict_mode_enabled() -> bool:
    """Read the strict-mode flag. ``true``/``1``/``yes`` enable; anything else is disabled."""
    raw = os.environ.get(STRICT_ENV_VAR, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


@dataclass
class ChunkMetadataFinding:
    """One per (chunk_index, field) violation. Order matters for tests."""

    chunk_index: int
    field_name: str
    # ``absent`` = key missing entirely; ``null`` = key present, value is None.
    # The two are reported separately so an operator sees whether the
    # writer never set the field vs. set it to None on purpose.
    kind: str

    def as_string(self) -> str:
        if self.kind == "absent":
            return (
                f"chunk {self.chunk_index}: field {self.field_name!r} absent "
                f"(key missing, not null)"
            )
        if self.kind == "null":
            return (
                f"chunk {self.chunk_index}: field {self.field_name!r} is null"
            )
        return (
            f"chunk {self.chunk_index}: field {self.field_name!r} {self.kind}"
        )


@dataclass
class ChunkMetadataReport:
    """The output of :func:`validate_chunk_metadata`."""

    findings: list[ChunkMetadataFinding] = field(default_factory=list)
    chunks_scanned: int = 0
    strict_mode: bool = False

    def has_violations(self) -> bool:
        return bool(self.findings)

    def as_strings(self) -> list[str]:
        return [f.as_string() for f in self.findings]

    def per_field_violation_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.field_name] = counts.get(f.field_name, 0) + 1
        return counts


def _chunk_id_present_value(chunk: dict[str, Any]) -> tuple:
    """Return ``(present, value)`` for the chunk_id-or-turn_id slot.

    ``present`` is True if either alias key is present (even with a
    None value). ``value`` is the non-None value when one of the
    aliases carries one; None otherwise.
    """
    present = False
    value: Any = None
    for name in _CHUNK_ID_FIELDS:
        if name in chunk:
            present = True
            if chunk[name] is not None:
                value = chunk[name]
                break
    return present, value


def validate_chunk_metadata(
    chunks: Sequence[dict[str, Any]],
    *,
    strict: bool | None = None,
) -> ChunkMetadataReport:
    """Validate every chunk against :data:`REQUIRED_CHUNK_FIELDS`.

    Args:
      chunks: list of chunk dicts (as written to ``chunks.jsonl``).
      strict: optional override; when None, reads
        ``STRICT_CHUNK_METADATA`` from the environment.

    Returns:
      A :class:`ChunkMetadataReport` listing every violation. The
      report's ``strict_mode`` field records the effective mode so
      a caller can decide whether to halt without re-reading the
      environment.

    Findings semantics:

      - ``kind="absent"`` means the key was not present in the
        chunk dict at all.
      - ``kind="null"`` means the key was present but the value
        was ``None``.

    Two states are reported separately so an operator can tell
    "the chunker never sets this" from "the chunker set it to
    None on purpose". The two have different remediations.
    """
    effective_strict = _strict_mode_enabled() if strict is None else bool(strict)
    report = ChunkMetadataReport(strict_mode=effective_strict)
    for i, chunk in enumerate(chunks):
        report.chunks_scanned += 1
        if not isinstance(chunk, dict):
            # Non-dict chunks are a hard violation -- report against
            # the first canonical field name so the operator gets a
            # pointer.
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i,
                    field_name="chunk_id",
                    kind="absent",
                )
            )
            continue
        # chunk_id (or turn_id alias)
        present, value = _chunk_id_present_value(chunk)
        if not present:
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="chunk_id", kind="absent",
                )
            )
        elif value is None:
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="chunk_id", kind="null",
                )
            )

        # speaker
        if "speaker" not in chunk:
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="speaker", kind="absent",
                )
            )
        elif chunk["speaker"] is None:
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="speaker", kind="null",
                )
            )

        # agenda_item_id: per Phase X2 contract, when agenda detection
        # is enabled the field is always a non-empty string (either an
        # ``AI-NNN`` id or the literal ``"unclassified"``). The gate
        # treats a missing key as a violation; a null value as a
        # violation; an empty string as a violation. A non-empty
        # string passes.
        if "agenda_item_id" not in chunk:
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="agenda_item_id", kind="absent",
                )
            )
        elif chunk["agenda_item_id"] is None:
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="agenda_item_id", kind="null",
                )
            )
        elif isinstance(chunk["agenda_item_id"], str) and not chunk["agenda_item_id"].strip():
            report.findings.append(
                ChunkMetadataFinding(
                    chunk_index=i, field_name="agenda_item_id", kind="null",
                )
            )

    return report


def format_report_for_log(report: ChunkMetadataReport) -> str:
    """Render the report as a single-line summary + per-finding lines.

    Used by the runner to produce a single log entry rather than one
    per finding (which would swamp the log on a fully-degraded run).
    """
    counts = report.per_field_violation_counts()
    head = (
        f"chunk_metadata_contract_violation: scanned={report.chunks_scanned} "
        f"violations={len(report.findings)} strict={report.strict_mode} "
        f"per_field={counts}"
    )
    if not report.findings:
        return head
    sample = report.findings[:5]
    body = "\n  ".join(f.as_string() for f in sample)
    suffix = ""
    if len(report.findings) > len(sample):
        suffix = f"\n  ... and {len(report.findings) - len(sample)} more"
    return f"{head}\n  {body}{suffix}"


__all__ = [
    "REQUIRED_CHUNK_FIELDS",
    "STRICT_ENV_VAR",
    "ChunkMetadataFinding",
    "ChunkMetadataReport",
    "format_report_for_log",
    "validate_chunk_metadata",
]
