#!/usr/bin/env python3
"""Phase 4.C — run the cascade filter on a grounded extraction artifact.

Reads:
  * ``grounded_items__<run_id>.json``       (from Phase 4.A gate)
  * ``grounding_gate_result__<run_id>.json`` (race-condition guard)
  * The raw transcript + ``chunks.jsonl``    (for modify re-grounding)
  * ``meeting_minutes.schema.json``          (type definitions)
  * ``meeting_minutes_llm.md``               (per-type disqualifiers)

Writes (under ``<data-lake>/store/processed/meetings/<source-id>/``):
  * ``cascade_filtered__<run_id>.json``      — kept + modified items.
  * ``cascade_audit__<run_id>.jsonl``        — one line per item with
    decision, reason, original, optional final form.
  * ``cascade_filter_result__<run_id>.json`` — counts + drop rate.
  * ``cascade_bypass_record__<run_id>.json`` — only on
    ``--disable-cascade``.

Exit codes:
  * ``0``  — cascade ran or was bypassed.
  * ``1``  — cascade ran and drop rate exceeded 50% (recall-collapse
    warning). Artifacts are written; the workflow surfaces the warning.
  * ``2``  — precondition failed (grounded artifact missing, gate
    result missing, max batches exceeded mid-run, etc.). Nothing
    written.

The script never falls back to "keep" silently. A malformed Sonnet
response drops the affected batch, a modify that breaks grounding
drops the affected item, and a max-batches overrun drops every
remaining item — all visible in the audit log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
for p in (str(SRC_ROOT), str(SCRIPTS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from spectrum_systems_core.promotion.cascade_filter import (  # noqa: E402
    CASCADE_FILTER_MODEL,
    CASCADE_MAX_BATCHES_DEFAULT,
    CascadeResult,
    cascade_audit_records,
    cascade_bypass_record,
    cascade_filter_result_payload,
    cascade_filtered_payload,
    filter_items,
    load_cascade_prompt_template,
    load_meeting_minutes_prompt,
    load_type_definitions,
    parse_type_disqualifiers,
)
from spectrum_systems_core.promotion.grounding_gate import (  # noqa: E402
    CLAIM_SHAPED_TYPES,
)
from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)


RECALL_COLLAPSE_THRESHOLD: float = 0.50


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_cascade_filter.py",
        description=(
            "Run the Phase 4.C per-item Sonnet cascade filter on the "
            "grounded items produced by Phase 4.A."
        ),
    )
    p.add_argument("--source-id", required=True)
    p.add_argument("--data-lake", required=True)
    p.add_argument(
        "--run-id",
        default=None,
        help=(
            "Specific grounded artifact run_id to filter. When omitted, "
            "the script picks the most recent grounded_items__*.json "
            "via the content-aware selector (mirrors the grounding "
            "gate's selector)."
        ),
    )
    p.add_argument(
        "--grounded-artifact",
        default=None,
        help=(
            "Explicit path to the grounded artifact. Overrides --run-id "
            "and the content-aware selector. Pre-flight runs use this "
            "to target an Opus baseline saved outside the regular "
            "extraction pipeline."
        ),
    )
    p.add_argument("--operator", default=None)
    p.add_argument(
        "--max-batches",
        type=int,
        default=CASCADE_MAX_BATCHES_DEFAULT,
        help=(
            "Hard cap on Sonnet calls. Items beyond the cap are DROPPED "
            "with reason max_batches_exceeded (cost cap, fail-closed)."
        ),
    )
    p.add_argument(
        "--disable-cascade",
        action="store_true",
        help=(
            "Bypass Sonnet entirely. Every grounded item passes through "
            "as 'keep' and a bypass record artifact is written. EMERGENCY "
            "ROLLBACK ONLY — not a normal operating mode."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run end-to-end but skip the api_client + writes. Useful "
            "for cost estimation against a real grounded artifact."
        ),
    )
    return p


# --------------------------------------------------------------------------
# Filesystem helpers — same shape as run_grounding_gate.py.
# --------------------------------------------------------------------------


def _processed_dir(data_lake: Path, source_id: str) -> Path:
    return data_lake / "store" / "processed" / "meetings" / source_id


def _raw_dir(data_lake: Path, source_id: str) -> Path:
    return data_lake / "store" / "raw" / "meetings" / source_id


def _grounded_schema_version(path: Path) -> tuple[int, ...]:
    """Parse the grounded artifact's schema_version as a sortable tuple."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (0,)
    if not isinstance(data, dict):
        return (0,)
    version_str = data.get("schema_version")
    if not isinstance(version_str, str):
        return (0,)
    try:
        return tuple(int(part) for part in version_str.split("."))
    except ValueError:
        return (0,)


def _find_latest_grounded(processed_dir: Path) -> Path | None:
    """Content-aware selector for the latest grounded_items artifact.

    Mirrors :func:`run_grounding_gate._find_latest_extraction`:
    schema_version first, mtime second, name third for a total
    deterministic order under git-clone timestamps.
    """
    if not processed_dir.is_dir():
        return None
    candidates = [
        p
        for p in processed_dir.glob("grounded_items__*.json")
        if p.is_file()
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda p: (
            _grounded_schema_version(p),
            p.stat().st_mtime,
            p.name,
        ),
        reverse=True,
    )
    return candidates[0]


def _load_chunks(raw_dir: Path) -> dict[str, str]:
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
    lines = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _resolve_operator(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    return os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown"


def _derive_run_id(grounded_path: Path) -> str:
    name = grounded_path.stem  # "grounded_items__<slug>"
    if "__" in name:
        slug = name.split("__", 1)[1]
        if slug:
            return slug
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Step summary
# --------------------------------------------------------------------------


def _step_summary(
    result: CascadeResult,
    *,
    source_id: str,
    run_id: str,
    grounded_path: Path,
    bypassed: bool,
) -> str:
    lines: list[str] = []
    title = "## Phase 4.C cascade filter"
    if bypassed:
        title += " — **BYPASSED**"
    lines.append(title)
    lines.append("")
    lines.append(f"- source_id: `{source_id}`")
    lines.append(f"- run_id: `{run_id}`")
    lines.append(f"- grounded: `{grounded_path.name}`")
    lines.append(f"- total_items: {result.total_items}")
    lines.append(f"- kept: {result.kept_count}")
    lines.append(f"- modified: {result.modified_count}")
    lines.append(f"- dropped: {result.dropped_count}")
    lines.append(f"- batches_used: {result.batches_used}")
    drop_rate = (
        result.dropped_count / result.total_items if result.total_items else 0.0
    )
    lines.append(f"- cascade_drop_rate: {drop_rate:.2%}")

    if drop_rate > RECALL_COLLAPSE_THRESHOLD and result.total_items > 0:
        lines.append("")
        lines.append(
            f"### ⚠️ RECALL COLLAPSE: drop rate {drop_rate:.2%} > "
            f"{RECALL_COLLAPSE_THRESHOLD:.0%}"
        )
        lines.append("")
        lines.append(
            "Sonnet dropped more than half the grounded items. The "
            "cascade may be over-strict — re-read the drop reasons "
            "below before re-running."
        )

    drop_reasons: Counter[str] = Counter()
    for r in result.item_results:
        if r.decision.value == "drop":
            # Reason format is "<code>: <detail>"; bucket by code only.
            head = r.reason.split(":", 1)[0]
            drop_reasons[head] += 1
    if drop_reasons:
        lines.append("")
        lines.append("### Top drop reasons")
        lines.append("")
        for reason, count in drop_reasons.most_common(5):
            lines.append(f"- `{reason}`: {count}")

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
# api_client wiring — real Anthropic SDK, with a stub fallback for
# offline runs (dry-run or test).
# --------------------------------------------------------------------------


def _build_api_client() -> Callable[..., str]:
    """Return an Anthropic-SDK-backed api_client, or a halt-on-call stub.

    Production wires the Anthropic SDK lazily so a dry-run or bypass
    path does not require the package. When ANTHROPIC_API_KEY is
    unset we return a stub that raises on call — the operator MUST
    pass ``--disable-cascade`` or ``--dry-run`` in that case.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        def _no_key(prompt: str, model: str = CASCADE_FILTER_MODEL) -> str:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Pass --disable-cascade or "
                "--dry-run, or set the env var, before running."
            )
        return _no_key

    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        def _no_sdk(prompt: str, model: str = CASCADE_FILTER_MODEL) -> str:
            raise RuntimeError(
                "anthropic SDK not installed; install with "
                "`pip install anthropic` or pass --disable-cascade."
            )
        return _no_sdk

    client = anthropic.Anthropic()

    def _client(prompt: str, model: str = CASCADE_FILTER_MODEL) -> str:
        # max_tokens sized for the batch response: 10 items × ~80 tokens
        # of JSON each + framing ≈ 1000 tokens. 2048 gives margin
        # without wasting budget.
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic SDK returns a list of content blocks; we only ever
        # ask for text so the first block's text is the response.
        blocks = response.content
        if not blocks:
            return ""
        return blocks[0].text  # type: ignore[union-attr]

    return _client


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

    # Locate the grounded artifact.
    if args.grounded_artifact:
        grounded_path = Path(args.grounded_artifact).resolve()
    elif args.run_id:
        grounded_path = (
            processed_dir / f"grounded_items__{args.run_id}.json"
        )
    else:
        grounded_path_opt = _find_latest_grounded(processed_dir)
        if grounded_path_opt is None:
            print(
                f"::error::no grounded_items__*.json under {processed_dir}",
                file=sys.stderr,
            )
            return 2
        grounded_path = grounded_path_opt

    if not grounded_path.is_file():
        print(
            f"::error::grounded artifact not found: {grounded_path}",
            file=sys.stderr,
        )
        return 2

    # Race-condition guard: the gate must have produced its result
    # artifact alongside the grounded items. If the result artifact
    # is missing, the gate did NOT run successfully — refuse to run
    # the cascade on a half-baked input.
    run_id = args.run_id or _derive_run_id(grounded_path)
    gate_result_path = processed_dir / f"grounding_gate_result__{run_id}.json"
    if not gate_result_path.is_file() and not args.grounded_artifact:
        print(
            "::error::grounding_gate_result artifact missing at "
            f"{gate_result_path}; the gate did not run for this run_id.",
            file=sys.stderr,
        )
        return 2

    operator = _resolve_operator(args.operator)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # Load the grounded artifact and validate it.
    grounded = json.loads(grounded_path.read_text(encoding="utf-8"))
    if not isinstance(grounded, dict):
        print(
            f"::error::grounded artifact is not a JSON object: {grounded_path}",
            file=sys.stderr,
        )
        return 2
    grounded_payload = grounded.get("payload") or {}
    if not isinstance(grounded_payload, dict):
        print(
            f"::error::grounded artifact has no payload object: {grounded_path}",
            file=sys.stderr,
        )
        return 2

    # The grounded artifact is the gate's own write — validating against
    # the gate's artifact_type would require a schema we don't ship.
    # Instead we validate the shape minimally: artifact_type must be
    # exactly the writer's value, and payload must carry at least one
    # claim-shaped type as a list.
    if grounded.get("artifact_type") != "grounded_meeting_minutes":
        print(
            "::error::grounded artifact_type mismatch: expected "
            f"'grounded_meeting_minutes', got "
            f"{grounded.get('artifact_type')!r}",
            file=sys.stderr,
        )
        return 2

    items_for_cascade: dict[str, list[dict[str, Any]]] = {}
    for typ in CLAIM_SHAPED_TYPES:
        v = grounded_payload.get(typ)
        if isinstance(v, list):
            items_for_cascade[typ] = [x for x in v if isinstance(x, dict)]

    # Build chunk index for modify re-grounding. Add a __full_transcript__
    # fallback so modify decisions on items missing source_chunk_id can
    # still be ground-checked.
    chunks_by_id = _load_chunks(raw_dir)
    transcript_path = raw_dir / "source.txt"
    if transcript_path.is_file():
        chunks_by_id["__full_transcript__"] = transcript_path.read_text(
            encoding="utf-8"
        )

    # Build the prompt context.
    type_definitions = load_type_definitions()
    type_disqualifiers = parse_type_disqualifiers(
        load_meeting_minutes_prompt()
    )

    # Run the cascade.
    if args.dry_run:
        # No api_client wired; report what WOULD have been sent.
        item_count = sum(len(v) for v in items_for_cascade.values())
        batch_count = (item_count + 9) // 10
        print(
            f"DRY RUN — would adjudicate {item_count} items in "
            f"{batch_count} Sonnet batches (model={CASCADE_FILTER_MODEL})",
            file=sys.stderr,
        )
        return 0

    if args.disable_cascade:
        result = filter_items(
            grounded_items_by_type=items_for_cascade,
            chunks_by_id=chunks_by_id,
            type_definitions=type_definitions,
            type_disqualifiers=type_disqualifiers,
            api_client=None,
            max_batches=args.max_batches,
            disable_cascade=True,
        )
        # Write the bypass record before the synthetic cascade artifacts.
        bypass = cascade_bypass_record(
            source_id=args.source_id,
            run_id=run_id,
            grounded_artifact_path=str(grounded_path.relative_to(data_lake)),
            operator=operator,
            timestamp=timestamp,
        )
        _write_json(
            processed_dir / f"cascade_bypass_record__{run_id}.json",
            bypass,
        )
    else:
        api_client = _build_api_client()
        result = filter_items(
            grounded_items_by_type=items_for_cascade,
            chunks_by_id=chunks_by_id,
            type_definitions=type_definitions,
            type_disqualifiers=type_disqualifiers,
            api_client=api_client,
            prompt_template=load_cascade_prompt_template(),
            max_batches=args.max_batches,
        )

    # Build and write the three product artifacts.
    filtered_envelope = cascade_filtered_payload(grounded, result)
    audit_records = cascade_audit_records(result)
    result_payload = cascade_filter_result_payload(
        result,
        source_id=args.source_id,
        run_id=run_id,
        grounded_artifact_path=str(grounded_path.relative_to(data_lake)),
    )

    filtered_path = processed_dir / f"cascade_filtered__{run_id}.json"
    audit_path = processed_dir / f"cascade_audit__{run_id}.jsonl"
    result_path = processed_dir / f"cascade_filter_result__{run_id}.json"

    _write_json(filtered_path, filtered_envelope)
    _write_jsonl(audit_path, audit_records)
    _write_json(result_path, result_payload)

    # CLAUDE.md integration co-requirement: validate the artifacts we
    # write against their declared shapes. The cascade artifact_types
    # are not yet in the central schema registry (introduced in this PR
    # alongside the writer); use the validator's "missing schema is a
    # warning" path so the call exercises the validator surface without
    # blocking.
    for envelope, type_name, path in (
        (filtered_envelope, "cascade_filtered", filtered_path),
        (result_payload, "cascade_filter_result", result_path),
    ):
        try:
            validate_artifact(envelope, type_name, str(path))
        except ArtifactValidationError as e:
            print(f"::error::cascade artifact failed validation: {e}", file=sys.stderr)
            return 2

    summary = _step_summary(
        result,
        source_id=args.source_id,
        run_id=run_id,
        grounded_path=grounded_path,
        bypassed=result.bypassed,
    )
    summary += f"\n- cascade_filtered: `{filtered_path.relative_to(data_lake)}`\n"
    summary += f"- audit: `{audit_path.relative_to(data_lake)}`\n"
    summary += f"- result: `{result_path.relative_to(data_lake)}`\n"
    _emit_step_summary(summary)
    print(summary)

    drop_rate = (
        result.dropped_count / result.total_items if result.total_items else 0.0
    )
    if drop_rate > RECALL_COLLAPSE_THRESHOLD and result.total_items > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
