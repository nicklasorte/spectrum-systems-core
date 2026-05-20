#!/usr/bin/env python3
"""Verify the Dec 18 Opus baseline (or any Opus baseline) is consistent
with the canonical Opus prompt.

This script answers two questions:

1. Is the baseline's item count within a documented sane range
   ([90, 125])?  The Dec 18 legacy baseline is documented at 106
   items across 22 types; a tight reference range of [100, 112]
   emits a WARNING when violated (the operator decides whether to
   accept the new run); the wider range [90, 125] is the hard fail
   bound.
2. If the baseline carries a ``prompt_content_hash`` in its
   provenance, log it so the operator can compare against the
   freshly-canonicalised prompt hash from
   ``workflows/prompts/meeting_minutes_opus.md``.  A missing hash is
   a pre-Phase-2 legacy artifact and is logged as a WARNING (not a
   failure).

The script reads from two layouts:

* The Phase-4a layout:
  ``<lake>/processed/meetings/<sid>/meeting_minutes_opus__*.json``
* The legacy layout:
  ``<lake>/store/processed/meetings/<sid>/reference_baselines/opus_reference_minutes.jsonl``

Exit code:

* 0 — every selected baseline passed the hard range check (warnings
  may have been emitted).
* 1 — at least one baseline was out of the hard range, or the lake
  contained no Opus baseline at all.
* 2 — argument / IO error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# 23 content arrays the Opus prompt produces. Drives the per-type
# breakdown so an operator can see which type drifted, not just the
# total. ``grounding`` is the meta-array and is excluded.
CONTENT_ARRAYS: Tuple[str, ...] = (
    "decisions",
    "action_items",
    "open_questions",
    "commitments",
    "risks",
    "claims",
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
    "dissent_or_objection",
    "agenda_item",
    "precedent_reference",
    "external_stakeholder_input",
    "glossary_definition",
    "procedural_ruling",
)

# Hard bound — outside this range exits 1. Originally [100, 115] in the
# Phase 4a spec; widened to [90, 125] after Red Team Pass 1 #6 because
# Opus extraction has non-deterministic surface variance and a ±15-item
# spread around the legacy 106-item baseline is the realistic envelope.
HARD_RANGE: Tuple[int, int] = (90, 125)

# Reference range — within this band the run is "consistent" with the
# legacy 106-item baseline. Outside [100, 112] but inside HARD_RANGE
# emits a WARNING; the operator decides whether to accept.
REFERENCE_RANGE: Tuple[int, int] = (100, 112)

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
CANONICAL_PROMPT_PATH: Path = (
    REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_opus.md"
)


def canonical_prompt_hash() -> Optional[str]:
    """sha256 of the canonical Opus prompt, or None if it is missing."""
    if not CANONICAL_PROMPT_PATH.is_file():
        return None
    try:
        text = CANONICAL_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _processed_meetings_root(lake_root: Path) -> List[Path]:
    """Yield every ``processed/meetings/`` root present in the lake.

    Both the modern layout (``<lake>/processed/meetings/``) and the
    legacy layout (``<lake>/store/processed/meetings/``) are scanned
    so a verifier run against either works.
    """
    candidates = [
        lake_root / "processed" / "meetings",
        lake_root / "store" / "processed" / "meetings",
    ]
    return [c for c in candidates if c.is_dir()]


def _list_baseline_artifacts(
    lake_root: Path, source_id: Optional[str]
) -> List[Path]:
    """List every Opus baseline file under the lake.

    Returns a sorted list of (path, layout-token) where layout-token
    is either ``"phase_4a"`` (the JSON envelope) or ``"legacy_jsonl"``
    (the per-item rows).
    """
    found: List[Path] = []
    for root in _processed_meetings_root(lake_root):
        sources: Iterable[Path]
        if source_id is not None:
            sources = [root / source_id] if (root / source_id).is_dir() else []
        else:
            sources = [p for p in root.iterdir() if p.is_dir()]
        for s in sources:
            for p in sorted(s.glob("meeting_minutes_opus__*.json")):
                found.append(p)
            legacy = s / "reference_baselines" / "opus_reference_minutes.jsonl"
            if legacy.is_file():
                found.append(legacy)
    return found


def _read_phase_4a_artifact(path: Path) -> Tuple[int, dict, Optional[str]]:
    """Return (item_count, per_type_counts, prompt_hash_or_None)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    payload = data.get("payload") or {}
    per_type = {k: len(payload.get(k) or []) for k in CONTENT_ARRAYS}
    total = sum(per_type.values())
    prov = payload.get("provenance") or {}
    p_hash = prov.get("prompt_content_hash")
    if not isinstance(p_hash, str) or not p_hash.strip():
        p_hash = None
    return total, per_type, p_hash


def _read_legacy_artifact(path: Path) -> Tuple[int, dict, Optional[str]]:
    """Count rows per extraction_type from the legacy JSONL.

    The legacy artifact does not record a prompt content hash because
    it predates Phase 2 — the function returns ``None`` for the hash,
    which the verifier surfaces as a WARNING (not a failure).
    """
    per_type: dict = {k: 0 for k in CONTENT_ARRAYS}
    total = 0
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = rec.get("extraction_type")
        if isinstance(etype, str) and etype in per_type:
            per_type[etype] += 1
            total += 1
        elif isinstance(etype, str):
            # Type we don't track — still counts toward the total so
            # a schema-extended baseline does not silently shrink.
            per_type.setdefault(etype, 0)
            per_type[etype] += 1
            total += 1
    return total, per_type, None


def _classify_count(count: int) -> str:
    lo_hard, hi_hard = HARD_RANGE
    lo_ref, hi_ref = REFERENCE_RANGE
    if count < lo_hard or count > hi_hard:
        return "FAIL"
    if count < lo_ref or count > hi_ref:
        return "WARN"
    return "OK"


def verify_artifact(path: Path) -> dict:
    """Verify one artifact and return a result dict.

    Result fields:
      path:        str
      layout:      "phase_4a" | "legacy_jsonl"
      item_count:  int
      classification: "OK" | "WARN" | "FAIL"
      prompt_content_hash: str | None
      messages:    List[str]  — warnings + info, all printed
    """
    messages: List[str] = []
    if path.suffix == ".jsonl":
        layout = "legacy_jsonl"
        total, per_type, p_hash = _read_legacy_artifact(path)
        messages.append(
            f"INFO: {path}: legacy JSONL layout ({total} rows across "
            f"{len([k for k,v in per_type.items() if v])} types)"
        )
    else:
        layout = "phase_4a"
        total, per_type, p_hash = _read_phase_4a_artifact(path)
        messages.append(
            f"INFO: {path}: Phase 4a envelope ({total} items across "
            f"{len([k for k,v in per_type.items() if v])} types)"
        )

    classification = _classify_count(total)
    if classification == "FAIL":
        messages.append(
            f"FAIL: item_count={total} outside the hard range "
            f"{HARD_RANGE} — audit the baseline before merging"
        )
    elif classification == "WARN":
        messages.append(
            f"WARN: item_count={total} outside the reference range "
            f"{REFERENCE_RANGE} but inside the hard range {HARD_RANGE} — "
            f"operator decides whether to accept"
        )

    if p_hash is None:
        messages.append(
            f"WARN: {path}: no prompt_content_hash in provenance — "
            f"this is a pre-Phase-2 legacy artifact; hash tracking "
            f"unavailable (not a failure)"
        )
    else:
        canon = canonical_prompt_hash()
        if canon is None:
            messages.append(
                f"WARN: canonical Opus prompt at {CANONICAL_PROMPT_PATH} "
                f"is missing or unreadable; cannot compare"
            )
        else:
            tag = "MATCH" if canon == p_hash else "DIFFERS"
            messages.append(
                f"INFO: prompt_content_hash {tag}: artifact={p_hash[:16]}.. "
                f"canonical={canon[:16]}.."
            )
            if tag == "DIFFERS":
                messages.append(
                    "INFO: a differing hash is EXPECTED when the prompt "
                    "evolves; the operator decides whether to regenerate"
                )

    return {
        "path": str(path),
        "layout": layout,
        "item_count": total,
        "classification": classification,
        "prompt_content_hash": p_hash,
        "per_type": per_type,
        "messages": messages,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify an Opus reference baseline's item count + prompt "
            "hash. Exits 0 on pass, 1 on fail, 2 on argument/IO error."
        )
    )
    parser.add_argument(
        "--lake",
        required=True,
        help="Path to the data lake root.",
    )
    parser.add_argument(
        "--source-id",
        default=None,
        help="Restrict the check to one source_id. Default: every "
        "source under the lake.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit the per-artifact result as JSON on stdout.",
    )
    args = parser.parse_args(argv)

    lake = Path(args.lake)
    if not lake.is_dir():
        print(f"ERROR: --lake {lake} is not a directory", file=sys.stderr)
        return 2

    artifacts = _list_baseline_artifacts(lake, args.source_id)
    if not artifacts:
        print(
            f"FAIL: no Opus baseline artifacts found under {lake}",
            file=sys.stderr,
        )
        return 1

    results = [verify_artifact(a) for a in artifacts]
    if args.emit_json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for r in results:
            for msg in r["messages"]:
                print(msg)
            print(
                f"  classification={r['classification']} "
                f"layout={r['layout']} item_count={r['item_count']}"
            )

    has_fail = any(r["classification"] == "FAIL" for r in results)
    return 1 if has_fail else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
