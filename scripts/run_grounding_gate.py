#!/usr/bin/env python3
"""Phase 4.A — run the substring grounding gate against an extraction.

Driven by ``.github/workflows/run-grounding-gate.yml`` but also
operator-runnable from a local checkout. The script does NOT call any
model and never touches the network.

For a given ``--source-id``:

  1. Locate the most recent ``meeting_minutes__*.json`` artifact under
     ``<data-lake>/store/processed/meetings/<source-id>/``. The
     extraction artifact is the gate's input.
  2. Locate the raw transcript at
     ``<data-lake>/store/raw/meetings/<source-id>/source.txt`` (and
     the optional ``chunks.jsonl`` next to it) for the gate's haystack.
  3. Run :func:`spectrum_systems_core.promotion.grounding_gate.check_grounding`
     against every claim-shaped item type in the artifact's payload.
  4. Write four artifacts back under the same processed/meetings dir
     (canonical-JSON, sorted keys, trailing newline):

       * ``grounded_items__<run_id>.json``   — payload with only the
         items that cleared the gate.
       * ``ungrounded_items/<run_id>.jsonl`` — append-only audit of
         every rejected item (one JSON object per line).
       * ``grounding_gate_result__<run_id>.json`` — totals, drop rate,
         per-failure reason codes, and warnings.
       * ``grounding_gate_bypass_record__<run_id>.json`` — written
         ONLY when ``--disable-grounding-gate`` is set, carrying who
         bypassed and when so every override is auditable.

The script's exit code:

  * ``0`` — gate passed (every item grounded) OR gate bypassed via
    the flag (the bypass record is the audit trail).
  * ``1`` — gate ran and at least one item failed. The artifacts ARE
    still written (the failure list is the deliverable); the non-zero
    exit lets a workflow show the run as failed.
  * ``2`` — a precondition failed (transcript not found, extraction
    artifact not found, etc.). Nothing is written. The script prints a
    one-line ``::error::`` to stderr so the GitHub workflow surfaces it.

The script emits a markdown step summary to ``$GITHUB_STEP_SUMMARY``
when that env var is set, so the dispatcher's UI shows totals, top-5
failure reasons, and a recall-collapse warning without scrolling
through raw logs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
for p in (str(SRC_ROOT), str(SCRIPTS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from spectrum_systems_core.promotion.grounding_gate import (  # noqa: E402
    CLAIM_SHAPED_TYPES,
    GROUNDING_GATE_SCHEMA_VERSION,
    GroundingResult,
    check_grounding,
    grounding_gate_bypass_record,
    grounding_gate_result_payload,
    split_grounded_and_ungrounded,
)
from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)


RECALL_COLLAPSE_THRESHOLD: float = 0.50
"""When ``grounded_items / total_items < 0.50`` the workflow step
summary surfaces a recall-collapse warning. The threshold is chosen
to match the comparison artifact's ``recall_collapse_warning`` flag
(see scripts/compare_opus_haiku.py extensions in this PR)."""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_grounding_gate.py",
        description=(
            "Run the Phase 4.A substring grounding gate against the most "
            "recent extraction for a source_id and write the result + "
            "ungrounded_items artifacts."
        ),
    )
    p.add_argument(
        "--source-id",
        required=True,
        help="Meeting / source identifier (the directory name under raw/ and processed/).",
    )
    p.add_argument(
        "--data-lake",
        required=True,
        help="Absolute path to the data-lake root (the dir that contains store/).",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help=(
            "Run identifier. When omitted, the script derives one from the "
            "extraction artifact's filename or, failing that, from the current "
            "UTC timestamp."
        ),
    )
    p.add_argument(
        "--extraction-artifact",
        default=None,
        help=(
            "Override path to the extraction artifact. When omitted, the most "
            "recently modified meeting_minutes__*.json under "
            "<data-lake>/store/processed/meetings/<source-id>/ is used."
        ),
    )
    p.add_argument(
        "--disable-grounding-gate",
        action="store_true",
        help=(
            "Bypass the gate. Writes a grounding_gate_bypass_record artifact "
            "with operator + timestamp so the override is auditable. EMERGENCY "
            "ROLLBACK ONLY — not a normal operating mode."
        ),
    )
    p.add_argument(
        "--operator",
        default=None,
        help=(
            "Operator identity for the bypass record. When omitted, the script "
            "reads $GITHUB_ACTOR (in CI) or $USER (locally) or 'unknown'."
        ),
    )
    return p


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------


def _processed_dir(data_lake: Path, source_id: str) -> Path:
    return data_lake / "store" / "processed" / "meetings" / source_id


def _raw_dir(data_lake: Path, source_id: str) -> Path:
    return data_lake / "store" / "raw" / "meetings" / source_id


def _find_latest_extraction(processed_dir: Path) -> Path | None:
    """Return the most recently modified meeting_minutes__*.json file."""
    if not processed_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in processed_dir.glob("meeting_minutes__*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_chunks(raw_dir: Path) -> dict[str, str]:
    """Load chunks.jsonl if present; map chunk_id → text. Empty dict otherwise."""
    chunks_path = raw_dir / "chunks.jsonl"
    if not chunks_path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        chunk_id = rec.get("chunk_id") or rec.get("id")
        text = rec.get("text") or rec.get("content")
        if isinstance(chunk_id, (str, int)) and isinstance(text, str):
            out[str(chunk_id)] = text
    return out


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(obj), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSONL: one canonical-JSON object per line (no indent), sorted keys,
    # trailing newline.
    lines = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _resolve_operator(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    return os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown"


def _derive_run_id(extraction_artifact_path: Path) -> str:
    """Derive a run_id from the artifact filename, or use a UTC timestamp."""
    name = extraction_artifact_path.stem  # "meeting_minutes__<slug>"
    if "__" in name:
        slug = name.split("__", 1)[1]
        if slug:
            return slug
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Step summary
# --------------------------------------------------------------------------


def _step_summary(
    result: GroundingResult,
    *,
    source_id: str,
    run_id: str,
    extraction_artifact_path: Path,
    bypassed: bool,
) -> str:
    lines: list[str] = []
    title = "## Phase 4.A grounding gate"
    if bypassed:
        title += " — **BYPASSED**"
    lines.append(title)
    lines.append("")
    lines.append(f"- source_id: `{source_id}`")
    lines.append(f"- run_id: `{run_id}`")
    lines.append(f"- extraction: `{extraction_artifact_path.name}`")
    lines.append(f"- total_items: {result.total_items}")
    lines.append(f"- grounded: {result.grounded_items}")
    lines.append(f"- ungrounded: {result.ungrounded_items}")
    drop_rate = (
        result.ungrounded_items / result.total_items if result.total_items else 0.0
    )
    lines.append(f"- gate_drop_rate: {drop_rate:.2%}")

    grounded_rate = (
        result.grounded_items / result.total_items if result.total_items else 1.0
    )
    if grounded_rate < RECALL_COLLAPSE_THRESHOLD and result.total_items > 0:
        lines.append("")
        lines.append(
            f"### ⚠️ RECALL COLLAPSE: grounded rate {grounded_rate:.2%} < "
            f"{RECALL_COLLAPSE_THRESHOLD:.0%}"
        )
        lines.append("")
        lines.append(
            "Most items failed to ground. Check the prompt's verbatim "
            "instruction, the chunk text vs. the model's quotes, and "
            "whether the transcript and chunks index are in sync."
        )

    if result.failures:
        reason_counts = Counter(f.reason.value for f in result.failures)
        lines.append("")
        lines.append("### Top failure reasons")
        lines.append("")
        for reason, count in reason_counts.most_common(5):
            lines.append(f"- `{reason}`: {count}")
            # First 3 item indices + types per reason.
            samples = [
                f for f in result.failures if f.reason.value == reason
            ][:3]
            for s in samples:
                lines.append(
                    f"  - {s.extraction_type}[{s.item_index}]: "
                    f"{s.detail[:160]}"
                )

    if result.warnings:
        lines.append("")
        lines.append(f"### Warnings ({len(result.warnings)})")
        lines.append("")
        for w in result.warnings[:5]:
            lines.append(f"- {w}")

    return "\n".join(lines) + "\n"


def _emit_step_summary(summary: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(summary)
    except OSError as e:
        print(f"::warning::could not write GITHUB_STEP_SUMMARY: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    data_lake = Path(args.data_lake).resolve()
    if not data_lake.is_dir():
        print(f"::error::data-lake not found: {data_lake}", file=sys.stderr)
        return 2

    processed_dir = _processed_dir(data_lake, args.source_id)
    raw_dir = _raw_dir(data_lake, args.source_id)

    if args.extraction_artifact:
        extraction_path = Path(args.extraction_artifact).resolve()
    else:
        extraction_path_opt = _find_latest_extraction(processed_dir)
        if extraction_path_opt is None:
            print(
                f"::error::no meeting_minutes__*.json under {processed_dir}",
                file=sys.stderr,
            )
            return 2
        extraction_path = extraction_path_opt

    if not extraction_path.is_file():
        print(f"::error::extraction artifact not found: {extraction_path}", file=sys.stderr)
        return 2

    transcript_path = raw_dir / "source.txt"
    if not transcript_path.is_file():
        print(f"::error::transcript not found: {transcript_path}", file=sys.stderr)
        return 2

    run_id = args.run_id or _derive_run_id(extraction_path)
    operator = _resolve_operator(args.operator)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    if args.disable_grounding_gate:
        # Bypass path: write the audit record and exit success. We do NOT
        # write grounded_items / ungrounded_items / grounding_gate_result
        # because the gate never ran — having the bypass record stand
        # alone makes the audit trail unambiguous.
        bypass = grounding_gate_bypass_record(
            source_id=args.source_id,
            extraction_artifact_path=str(extraction_path.relative_to(data_lake)),
            operator=operator,
            timestamp=timestamp,
        )
        bypass_path = processed_dir / f"grounding_gate_bypass_record__{run_id}.json"
        _write_json(bypass_path, bypass)
        # Compose a synthetic "0 items examined" result for the step summary.
        result = GroundingResult(
            passed=True, total_items=0, grounded_items=0, ungrounded_items=0,
        )
        summary = _step_summary(
            result,
            source_id=args.source_id,
            run_id=run_id,
            extraction_artifact_path=extraction_path,
            bypassed=True,
        )
        summary += f"\n- bypass record: `{bypass_path.relative_to(data_lake)}`\n"
        summary += f"- operator: `{operator}`\n"
        _emit_step_summary(summary)
        print(summary)
        return 0

    # Live gate path.
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))

    # CLAUDE.md integration co-requirement: validate the loaded artifact
    # against its schema BEFORE reading any field off it. A schema drift
    # at read time is exactly the failure class this guard exists to
    # catch — we'd rather refuse to ground a bad artifact than emit
    # silently-wrong grounded_items.
    try:
        validate_artifact(extraction, "meeting_minutes", str(extraction_path))
    except ArtifactValidationError as e:
        print(f"::error::extraction artifact failed schema validation: {e}", file=sys.stderr)
        return 2

    payload = extraction.get("payload", extraction)
    if not isinstance(payload, dict):
        print(
            f"::error::extraction artifact has no payload object: {extraction_path}",
            file=sys.stderr,
        )
        return 2

    chunks_by_id = _load_chunks(raw_dir)
    transcript_text = transcript_path.read_text(encoding="utf-8")

    # Only feed the gate the claim-shaped types; other keys pass through
    # unchanged via split_grounded_and_ungrounded.
    items_for_gate: dict[str, Any] = {
        k: v for k, v in payload.items() if k in CLAIM_SHAPED_TYPES
    }
    result = check_grounding(items_for_gate, chunks_by_id, transcript_text)

    # Build grounded / ungrounded payloads.
    grounded_payload_subset, ungrounded_by_type = split_grounded_and_ungrounded(
        items_for_gate, result
    )
    grounded_full_payload = dict(payload)
    for type_name in CLAIM_SHAPED_TYPES:
        if type_name in grounded_payload_subset:
            grounded_full_payload[type_name] = grounded_payload_subset[type_name]
        elif type_name in payload:
            # Every item in this type was ungrounded — set to empty list
            # so the downstream consumer never sees the unsanitized items.
            grounded_full_payload[type_name] = []

    # Stamp the grounded artifact's envelope with the Phase 4.A gate
    # schema_version so downstream consumers can recognize a gate-filtered
    # artifact at a glance. We do NOT change the original artifact's
    # schema_version — that belongs to the producer.
    grounded_artifact: dict[str, Any] = {
        "artifact_type": "grounded_meeting_minutes",
        "schema_version": GROUNDING_GATE_SCHEMA_VERSION,
        "source_id": args.source_id,
        "run_id": run_id,
        "source_extraction_artifact": str(
            extraction_path.relative_to(data_lake)
        ),
        "gate_passed": result.passed,
        "payload": grounded_full_payload,
    }

    grounded_path = processed_dir / f"grounded_items__{run_id}.json"
    ungrounded_path = processed_dir / "ungrounded_items" / f"{run_id}.jsonl"
    result_path = processed_dir / f"grounding_gate_result__{run_id}.json"

    _write_json(grounded_path, grounded_artifact)

    # Flatten ungrounded_by_type → one JSONL record per ungrounded item,
    # sorted by (extraction_type, item_index) for byte-stable output.
    ungrounded_records: list[dict[str, Any]] = []
    for extraction_type in sorted(ungrounded_by_type.keys()):
        for rec in ungrounded_by_type[extraction_type]:
            ungrounded_records.append(rec)
    ungrounded_records.sort(
        key=lambda r: (r.get("extraction_type", ""), r.get("item_index", 0))
    )
    _write_jsonl(ungrounded_path, ungrounded_records)

    gate_result_payload = grounding_gate_result_payload(
        result,
        source_id=args.source_id,
        run_id=run_id,
        trace_id=extraction.get("trace_id"),
        extraction_artifact_path=str(extraction_path.relative_to(data_lake)),
    )
    _write_json(result_path, gate_result_payload)

    summary = _step_summary(
        result,
        source_id=args.source_id,
        run_id=run_id,
        extraction_artifact_path=extraction_path,
        bypassed=False,
    )
    summary += f"\n- grounded artifact: `{grounded_path.relative_to(data_lake)}`\n"
    summary += f"- ungrounded artifact: `{ungrounded_path.relative_to(data_lake)}`\n"
    summary += f"- gate result: `{result_path.relative_to(data_lake)}`\n"
    _emit_step_summary(summary)
    print(summary)

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
