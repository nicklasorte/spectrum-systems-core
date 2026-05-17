"""Phase V.6: scope-overgeneralization detector.

Pattern: ``source_text`` contains a SPECIFIC band reference (numeric
MHz/GHz/kHz value) AND the extracted text uses an overly broad term
from ``OVERGENERALIZATION_MARKERS``. The finding name is
``scope_overgeneralization``.

Gated by ``GENERALIZATION_CHECK_ENABLED`` (default true). Set to
false to suppress the detector wholesale without a code revert.

Red-team-fixed properties:
- Returns None (not finding) when extracted_text is None / empty so
  a failed extraction does not crash the checker (Attack 14).
- Returns None when source has no specific band reference -- the
  detector does not fire on legitimately broad source statements
  ("spectrum policy is complex" -> "all spectrum") (Attack 7).
- Context dict always includes ``source_band_ref`` (list of band
  refs found in source) and ``triggered_markers`` (list of markers
  found in extracted text). Both required.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from typing import Any

from ..config.taxonomy import OVERGENERALIZATION_MARKERS
from ..health.finding import HealthFinding

_LOG = logging.getLogger(__name__)

_GEN_CHECK_ENV: str = "GENERALIZATION_CHECK_ENABLED"

# Matches strings like "7 GHz", "6525 MHz", "12.7 GHz", "30 kHz",
# "6525MHz" (no space). Does NOT match "item 7" or "7 meetings".
BAND_PATTERN: re.Pattern[str] = re.compile(
    r"\b\d{1,5}(?:\.\d+)?\s*(?:MHz|GHz|kHz)\b",
    re.IGNORECASE,
)


def _enabled() -> bool:
    raw = os.environ.get(_GEN_CHECK_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def find_band_refs(text: str) -> list[str]:
    """Return all specific-band references found in ``text``.

    Empty list when ``text`` is None / empty or carries no
    MHz/GHz/kHz value. Order preserves first-occurrence in the
    source string.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    return [m.group(0) for m in BAND_PATTERN.finditer(text)]


def find_overgeneralization_markers(text: str) -> list[str]:
    """Return the markers from ``OVERGENERALIZATION_MARKERS`` that
    occur in ``text`` (case-insensitive)."""
    if not isinstance(text, str) or not text.strip():
        return []
    lower = text.lower()
    return [m for m in OVERGENERALIZATION_MARKERS if m in lower]


def check_generalization_bias(
    source_text: str | None,
    extracted_text: str | None,
    item_id: str,
    *,
    pipeline_run_id: str | None = None,
) -> HealthFinding | None:
    """Return a ``scope_overgeneralization`` finding or None.

    Returns None when:
    - the detector is disabled via env;
    - ``extracted_text`` is None / empty;
    - ``source_text`` carries no specific band reference;
    - ``extracted_text`` carries no overgeneralization marker.
    """
    if not _enabled():
        return None
    if not extracted_text:
        return None
    band_refs = find_band_refs(source_text or "")
    if not band_refs:
        return None
    triggered = find_overgeneralization_markers(extracted_text)
    if not triggered:
        return None
    return HealthFinding(
        finding_code="scope_overgeneralization",
        severity="warn",
        context={
            "item_id": item_id,
            "source_band_ref": band_refs,
            "triggered_markers": triggered,
        },
        remediation=(
            "Either narrow the extraction to the specific band cited "
            "in the source, or open a correction candidate if the "
            "broad claim is actually warranted."
        ),
        pipeline_run_id=pipeline_run_id,
    )


def scan_items(
    items: Sequence[dict[str, Any]],
    *,
    source_text_key: str,
    extracted_text_key: str,
    pipeline_run_id: str | None = None,
) -> list[HealthFinding]:
    """Convenience: scan a list of dicts and return findings.

    Each item must carry an ``id`` (or ``decision_id`` / ``claim_id``)
    field; falls back to the index as a string.
    """
    out: list[HealthFinding] = []
    for idx, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        item_id = str(
            item.get("id")
            or item.get("decision_id")
            or item.get("claim_id")
            or idx
        )
        finding = check_generalization_bias(
            source_text=item.get(source_text_key),
            extracted_text=item.get(extracted_text_key),
            item_id=item_id,
            pipeline_run_id=pipeline_run_id,
        )
        if finding is not None:
            out.append(finding)
    return out


__all__ = [
    "BAND_PATTERN",
    "check_generalization_bias",
    "find_band_refs",
    "find_overgeneralization_markers",
    "scan_items",
]
