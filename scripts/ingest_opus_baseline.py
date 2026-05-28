#!/usr/bin/env python3
"""Ingest a locally-produced Opus meeting_minutes extraction into the data
lake as the canonical Opus reference baseline.

Distinct from ``scripts/create_opus_reference_baselines.py``, which is the
in-repo workflow that calls the Anthropic API itself against every raw
transcript and writes the same JSONL. This script is the OPERATOR-INITIATED
parallel: the operator runs Claude Opus locally (e.g. via the desktop app,
claude.ai, or any other surface) against the canonical extraction prompt
(``workflows/prompts/meeting_minutes_llm.md``) and a transcript already
staged in the data-lake. The model produces a ``meeting_minutes``-shaped
JSON file. This script is the gate between that local output and the
pipeline: it validates the JSON against the ``meeting_minutes`` schema,
looks up the canonical transcript UUID from ``source_record.json``, and
explodes the payload into a JSONL file that shares the on-disk shape of
``opus_reference_minutes.jsonl``::

    <data-lake>/store/processed/meetings/<source_id>/
        reference_baselines/
            opus_reference_minutes.jsonl    (this script writes)
            codex_reference_minutes.jsonl   (ingest_codex_baseline.py)

This is operator-initiated only — there is no CI workflow, NO Anthropic
SDK import, NO network or LLM call, and no autonomous re-extraction.
``create_opus_reference_baselines.py`` is the API-calling sibling; this
script never reads or constructs an LLM client.

Coexistence with ``create_opus_reference_baselines.py``: both writers
target the SAME on-disk path. Whichever writer runs first wins; the
second halts ``already_ingested`` and is never silently overwritten.
The operator removes the file deliberately in the data-lake repo before
re-ingesting from the other source.

Fail-closed contract:

* ``input_file_not_found`` / ``invalid_input_json`` — the JSON the
  operator pushed is missing or unparseable.
* ``schema_violation`` — the parsed JSON does not match the
  ``meeting_minutes`` schema. The script never massages the input; a
  schema mismatch halts so the bad artifact never enters the data lake.
* ``missing_source_record`` / ``invalid_source_record`` — no canonical
  transcript UUID for this ``source_id``. Run the normal ingestion (or
  ``create_opus_reference_baselines.py``) first.
* ``already_ingested`` — ``opus_reference_minutes.jsonl`` already
  exists for this ``source_id``. The data lake is append-only from
  core's perspective; the operator removes the file deliberately in the
  data-lake repo before re-ingesting.

Determinism: ``pair_id`` is a UUID5 over
``opus-ref-{source_id}-{extraction_type}-{index}`` so re-ingesting the
SAME input JSON over the SAME source assigns identical ids to identical
items. The namespace is frozen and SHARED with
``create_opus_reference_baselines.py`` so the two Opus writers
(API-call and local-ingest) produce byte-identical ``pair_id`` for the
same item slot — the comparison engine cannot tell them apart, by
design. Changing the namespace re-keys every future Opus baseline.

Provenance shape: every JSONL row stamps ``provenance.produced_by``
exactly equal to ``create_opus_reference_baselines``'s value
(``opus_reference_baseline_workflow``). The codex ingest ALSO adds a
``provenance.operator`` key; the Opus baseline shape on disk does NOT
carry that key, so this script accepts ``--operator`` on the CLI for
audit parity with the codex script and logs it to stderr, but does not
stamp it into the rows. The on-disk shape stays byte-identical to
``create_opus_reference_baselines.py``'s output.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from create_opus_reference_baselines import (  # noqa: E402
    extract_ground_truth_text,
    extraction_types,
)
from spectrum_systems_core.promotion.gate import (  # noqa: E402
    GROUNDING_BINDING_SCHEMA_VERSION,
)

# Workflow tag for every JSONL line's ``provenance.produced_by``.
# Identical to the value ``create_opus_reference_baselines.py`` writes,
# so the on-disk shape is byte-equivalent regardless of whether this
# script or the API-calling sibling produced it.
PRODUCED_BY = "opus_reference_baseline_workflow"

# Identifier the operator MAY stamp into the source meeting_minutes JSON's
# ``provenance.produced_by`` when local Opus is the producer. Accepted but
# not required — the schema gates accept any string.
OPUS_LOCAL_PRODUCED_BY = "opus_local"

# Frozen UUID5 namespace. SHARED with
# ``create_opus_reference_baselines.py::_OPUS_REF_NAMESPACE`` so a row
# produced by the API workflow and a row produced by this local ingest
# carry identical ``pair_id`` values for the same item slot — the
# comparison engine cannot tell them apart, by design. Changing this
# constant re-keys every future Opus baseline.
_OPUS_REF_NAMESPACE = uuid.UUID("3f1c9d8e-2b6a-5c7d-9e0f-1a2b3c4d5e6f")

_OUTPUT_SUBDIR = "reference_baselines"
_OUTPUT_FILENAME = "opus_reference_minutes.jsonl"


class OpusIngestError(RuntimeError):
    """Fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _meeting_dir(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake / "store" / "processed" / "meetings" / source_id
    )


def _output_path(data_lake: Path, source_id: str) -> Path:
    return (
        _meeting_dir(data_lake, source_id)
        / _OUTPUT_SUBDIR
        / _OUTPUT_FILENAME
    )


def _resolve_source_artifact_id(
    data_lake: Path, source_id: str
) -> str:
    """slug -> canonical transcript UUID via ``source_record.json``.

    Byte-identical contract to
    ``ingest_codex_baseline._resolve_source_artifact_id``: the same
    fail-closed checks on the same file produce the same UUID for the
    same ``source_id``, so the codex and opus baselines for one
    transcript share an identical ``source_artifact_id``. This script
    does NOT self-heal by re-running Stage-1 ingestion — the operator-
    initiated ingest only receives the local JSON, never the raw
    transcript, so a missing record halts with a clear ``run the
    normal ingestion first`` message.
    """
    sr_path = _meeting_dir(data_lake, source_id) / "source_record.json"
    if not sr_path.is_file():
        raise OpusIngestError(
            "missing_source_record",
            f"no source_record.json at {sr_path}; run the data-lake's "
            f"normal ingestion (or create_opus_reference_baselines.py) "
            f"for {source_id!r} before ingesting a local Opus baseline",
        )
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpusIngestError(
            "invalid_source_record",
            f"source_record.json at {sr_path} is unreadable/!json: {exc}",
        ) from exc
    if not isinstance(record, dict):
        raise OpusIngestError(
            "invalid_source_record",
            f"source_record.json at {sr_path} is "
            f"{type(record).__name__}, expected a JSON object",
        )
    artifact_id = record.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise OpusIngestError(
            "invalid_source_record",
            f"source_record.json at {sr_path} has no string "
            f"artifact_id (got {type(artifact_id).__name__})",
        )
    try:
        uuid.UUID(artifact_id)
    except ValueError as exc:
        raise OpusIngestError(
            "invalid_source_record",
            f"source_record.json at {sr_path} artifact_id "
            f"{artifact_id!r} is not a valid UUID: {exc}",
        ) from exc
    return artifact_id


def _extract_payload(
    raw_input: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Return ``(payload, meeting_date)`` from the operator's input JSON.

    The script accepts two equivalent shapes:

    1. A flat ``meeting_minutes`` payload — ``{"decisions": [...],
       "action_items": [...], ...}``. ``artifact_type`` and
       ``schema_version`` are added before schema validation.
    2. A wrapped envelope — ``{"artifact_type": "meeting_minutes",
       "schema_version": "...", "payload": {...}}`` — in which case
       the payload is read off the envelope.

    The flat shape is the natural output Opus produces from a
    "paste this transcript, return this schema" prompt; the wrapped
    shape is what a copy of a real promoted artifact looks like.
    Accepting both reduces the chance the operator has to massage the
    file by hand before ingest.
    """
    if not isinstance(raw_input, dict):
        raise OpusIngestError(
            "invalid_input_json",
            f"top-level value is {type(raw_input).__name__}, expected "
            f"a JSON object",
        )
    if "payload" in raw_input and isinstance(raw_input["payload"], dict):
        payload = raw_input["payload"]
    else:
        payload = raw_input
    meeting_date = payload.get("meeting_date")
    if not isinstance(meeting_date, str):
        meeting_date = None
    return payload, meeting_date


def _build_meeting_minutes_envelope(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Wrap ``payload`` so the meeting_minutes schema validator can read it.

    The schema's ``additionalProperties: false`` lives on the top-level
    object that carries ``artifact_type``, ``schema_version``, ``title``,
    ``summary``, etc. The validator is fed the FLAT shape (payload
    fields at the top level next to artifact_type / schema_version) —
    the same shape ``compare_opus_haiku.py`` constructs when it
    validates a candidate before reading it.
    """
    envelope: Dict[str, Any] = {"artifact_type": "meeting_minutes"}
    envelope.setdefault("schema_version", GROUNDING_BINDING_SCHEMA_VERSION)
    for k, v in payload.items():
        # Don't double-stamp artifact_type from a wrapped envelope.
        if k == "artifact_type":
            continue
        envelope[k] = v
    if "schema_version" not in envelope or not isinstance(
        envelope.get("schema_version"), str
    ):
        envelope["schema_version"] = GROUNDING_BINDING_SCHEMA_VERSION
    return envelope


def build_opus_records(
    *,
    payload: Dict[str, Any],
    types: List[str],
    source_id: str,
    source_artifact_id: str,
    model: str,
    meeting_date: Optional[str],
    created_at: str,
) -> List[Dict[str, Any]]:
    """Explode ``payload`` into JSONL records matching the Opus shape.

    Each extraction-type array becomes one row per item. The row carries
    exactly the fields a real ``create_opus_reference_baselines`` row
    carries — provenance is ``{"produced_by": PRODUCED_BY}`` ONLY (NO
    ``operator`` key; that is the codex shape, not the Opus shape).
    A content key whose value is not a list HALTs (``schema_violation``
    is raised upstream by the schema validator; this loop's check is
    the second-line defense for inputs that bypass the validator via a
    JSON quirk).
    """
    records: List[Dict[str, Any]] = []
    for etype in types:
        value = payload.get(etype)
        if value is None:
            continue
        if not isinstance(value, list):
            raise OpusIngestError(
                "schema_violation",
                f"{etype!r} in input is {type(value).__name__}, "
                f"expected a list",
            )
        for index, item in enumerate(value):
            ground_truth_text = extract_ground_truth_text(item, etype)
            item_data = (
                item if isinstance(item, dict) else {"text": item}
            )
            pair_id = str(
                uuid.uuid5(
                    _OPUS_REF_NAMESPACE,
                    f"opus-ref-{source_id}-{etype}-{index}",
                )
            )
            records.append(
                {
                    "pair_id": pair_id,
                    "source_id": source_id,
                    "source_artifact_id": source_artifact_id,
                    "extraction_type": etype,
                    "ground_truth_text": ground_truth_text,
                    "item_data": item_data,
                    "human_authored": False,
                    "model_authored": True,
                    "model_id": model,
                    "verified": False,
                    "status": "reference_only",
                    "provenance": {"produced_by": PRODUCED_BY},
                    "schema_version": GROUNDING_BINDING_SCHEMA_VERSION,
                    "meeting_date": meeting_date,
                    "created_at": created_at,
                    # Opus local-ingest receives the WHOLE transcript in
                    # one paste — no overlap chunking — so the default
                    # strategy value applies. Stamping it explicitly
                    # keeps the comparison engine's strategy cross-check
                    # happy against a default-off Haiku artifact.
                    "chunking_strategy_version": "speaker_turn_v1",
                }
            )
    return records


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    """Write JSONL in the exact format ``opus_reference_minutes.jsonl`` uses.

    Sorted keys + minimal separators + single trailing newline so two
    ingests of the same input produce byte-identical files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(r, sort_keys=True, separators=(",", ":"))
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_model_from_registry() -> str:
    """Return the Opus reference baseline model string from the registry.

    The registry is the single source of truth (CLAUDE.md model-string
    discipline). When the operator omits ``--model`` the script reads
    ``models.opus_reference_baseline`` (the existing canonical slot,
    which ``create_opus_reference_baselines.py``'s workflow already
    resolves) so a re-keying in the registry flows through
    automatically.

    NOTE: the codex script reads
    ``codex_reference_baseline.model_id`` (top-level object). The Opus
    side stores its model id in ``models.opus_reference_baseline``
    (string slot) — that is a pre-existing structural difference in
    the registry, not a divergence introduced here.
    """
    registry_path = (
        _REPO_ROOT / "ai" / "registry" / "model_registry.json"
    )
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpusIngestError(
            "missing_model",
            f"cannot read ai/registry/model_registry.json: {exc}",
        ) from exc
    models = registry.get("models")
    if not isinstance(models, dict):
        raise OpusIngestError(
            "missing_model",
            "ai/registry/model_registry.json has no 'models' object; "
            "add the opus_reference_baseline entry before ingesting",
        )
    model_id = models.get("opus_reference_baseline")
    if not isinstance(model_id, str) or not model_id.strip():
        raise OpusIngestError(
            "missing_model",
            "ai/registry/model_registry.json "
            "models.opus_reference_baseline is missing or empty",
        )
    return model_id.strip()


def ingest(
    *,
    input_file: Path,
    data_lake: Path,
    source_id: str,
    operator: str,
    model: Optional[str],
    dry_run: bool,
) -> Dict[str, Any]:
    """Orchestrate one ingest. Returns a summary dict; raises on any halt.

    ``operator`` is logged to the summary for audit but is NOT stamped
    into the JSONL provenance — the on-disk Opus baseline shape does
    not carry that key (that is the codex shape).
    """
    if not input_file.is_file():
        raise OpusIngestError(
            "input_file_not_found",
            f"no input JSON at {input_file}",
        )
    try:
        raw_input = json.loads(input_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpusIngestError(
            "invalid_input_json",
            f"{input_file} is not valid JSON: {exc}",
        ) from exc

    payload, meeting_date = _extract_payload(raw_input)
    envelope = _build_meeting_minutes_envelope(payload)
    try:
        validate_artifact(
            envelope, "meeting_minutes", str(input_file)
        )
    except ArtifactValidationError as exc:
        raise OpusIngestError(
            "schema_violation",
            f"input does not match meeting_minutes schema: {exc}",
        ) from exc

    source_artifact_id = _resolve_source_artifact_id(
        data_lake, source_id
    )

    resolved_model = (
        model.strip() if isinstance(model, str) and model.strip()
        else _resolve_model_from_registry()
    )

    out_path = _output_path(data_lake, source_id)
    if out_path.is_file():
        raise OpusIngestError(
            "already_ingested",
            f"{out_path} already exists; the data lake is append-only "
            f"so the operator removes the file deliberately in the "
            f"data-lake repo before re-ingesting",
        )

    types = extraction_types()
    records = build_opus_records(
        payload=payload,
        types=types,
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        model=resolved_model,
        meeting_date=meeting_date,
        created_at=_now_utc_iso(),
    )

    by_type: Dict[str, int] = {}
    for r in records:
        by_type[r["extraction_type"]] = (
            by_type.get(r["extraction_type"], 0) + 1
        )

    if not dry_run:
        _write_jsonl(out_path, records)

    return {
        "status": "success",
        "source_id": source_id,
        "source_artifact_id": source_artifact_id,
        "model": resolved_model,
        "operator": operator,
        "dry_run": dry_run,
        "total": len(records),
        "by_type": by_type,
        "output_path": str(out_path),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-file",
        required=True,
        help="Path to the locally-produced Opus meeting_minutes JSON.",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="Transcript slug under data-lake/store/processed/meetings/.",
    )
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Root path of the data-lake clone (the directory "
        "containing 'store/').",
    )
    parser.add_argument(
        "--operator",
        required=True,
        help="Identifier of the human pushing this artifact. Logged to "
        "the summary for audit but NOT stamped into JSONL rows — the "
        "on-disk Opus baseline shape does not carry an operator key.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model string. By default the script reads "
        "ai/registry/model_registry.json::models.opus_reference_baseline "
        "so a registry re-key flows through automatically.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and exit; write no JSONL.",
    )
    args = parser.parse_args(argv)

    # Mobile copy-paste foot-gun: trailing spaces from a phone keyboard
    # silently change a slug. Mirror the strip in
    # ingest_codex_baseline.main.
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

    input_file = Path(args.input_file)
    if not input_file.is_absolute():
        input_file = (Path.cwd() / input_file).resolve()
    data_lake = Path(args.data_lake)
    if not data_lake.is_absolute():
        data_lake = (Path.cwd() / data_lake).resolve()
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(
            f"FAIL: --data-lake is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 2

    try:
        result = ingest(
            input_file=input_file,
            data_lake=data_lake,
            source_id=args.source_id,
            operator=args.operator,
            model=args.model,
            dry_run=args.dry_run,
        )
    except OpusIngestError as exc:
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(f"FAIL: {exc.reason} — {exc.detail}", file=sys.stderr)
        # Exit codes per task spec (mirrors ingest_codex_baseline):
        #   1 — schema violation / data-shape rejection
        #   2 — file-not-found / malformed JSON / data-lake missing
        if exc.reason in (
            "input_file_not_found",
            "invalid_input_json",
        ):
            return 2
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    by_type_str = (
        ", ".join(
            f"{k}={result['by_type'][k]}"
            for k in sorted(result["by_type"])
        )
        or "-"
    )
    print(
        f"{result['source_id']} | "
        f"{'dry_run' if result['dry_run'] else 'written'} | "
        f"total={result['total']} | {by_type_str}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
