#!/usr/bin/env python3
"""Produce Opus *reference baseline* JSONL files from raw transcripts.

This is a reusable, triggerable workflow step — not a one-shot script.
For every raw transcript ``.docx`` in the data-lake it runs an Opus
model against the SAME canonical extraction system prompt the Haiku
pipeline uses (``workflows/prompts/meeting_minutes_llm.md``) and writes
one ``reference_baselines/opus_reference_minutes.jsonl`` per source.

Why a reference baseline (not ground truth, not a product artifact):
the lines are model-authored, unverified, and explicitly
``status: "reference_only"``. They exist so a human (or a future
eval) can compare the Haiku pipeline against a stronger model's read
of the same transcript. They are NEVER promoted, NEVER read back into
the governed loop, and NEVER mixed with product artifacts.

Fail-closed contract (every gate halts the run; nothing partial is
written):

* The canonical prompt file cannot be read  -> ``missing_extraction_prompt``
  (checked BEFORE any model call — a missing prompt can never reach the
  transport).
* ``source_record.json`` missing / not schema-valid -> ``missing_source_record``
  / ``invalid_source_record`` (no inference of the UUID).
* The model transport fails -> ``llm_transport_error`` (no fallback to a
  weaker model, no partial file).
* The model returns non-JSON / a non-object / a content array that is
  not a list / a structured item missing its schema-required primary
  text field -> ``malformed_llm_response`` (the whole transcript's file
  is never written — no partial JSONL).
* After writing, the artifact is shadowed by a ``.gitignore`` rule in
  the data-lake clone -> ``gitignore_blocks_artifact`` (we refuse to
  leave behind an artifact that can never be committed).

Determinism that matters here: ``pair_id`` is a UUID5 over
``opus-ref-{source_id}-{extraction_type}-{index}`` so two runs over the
same transcript text assign the same ids to the same items. The model
content itself is not guaranteed identical run-to-run (that is exactly
why ``--skip-existing`` is the default — a reference baseline is
written once and only regenerated deliberately).

The model string is NEVER hardcoded here. It arrives via ``--model``
(the workflow resolves it from ``ai/registry/model_registry.json`` at
run time and passes it through, and stamps it into every JSONL line as
``model_id`` so a past artifact keeps its exact model even after the
registry is rolled forward).

Test seam: ``create_baselines`` accepts an injected ``client`` callable
``(*, system: str, user: str) -> str`` — the SAME structural seam
``workflows/llm_client.py`` defines — so the suite runs with no API
key and no network.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# scripts/ on sys.path so the artifact validator import works whether
# this file is run as a script or imported as a module by tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

# Reused pipeline functions — NEVER reimplemented here. A drift in any
# of these would silently desync this workflow from the Haiku pipeline.
from spectrum_systems_core.ingestion.docx_extractor import (  # noqa: E402
    DocxExtractor,
)
from spectrum_systems_core.orchestration.pipeline_orchestrator import (  # noqa: E402
    _slugify,
)
from spectrum_systems_core.workflows import (  # noqa: E402
    meeting_minutes_llm as _mm_llm,
)
from spectrum_systems_core.workflows.llm_client import (  # noqa: E402
    AnthropicJSONClient,
    LLMClientError,
)

PRODUCED_BY = "opus_reference_baseline_workflow"

# Offline / test transport seam. When set, its value is returned
# verbatim as the model response instead of constructing the real
# Anthropic client — the SAME env-var seam pattern
# ``create_human_gt_pairs.py`` uses (``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE``)
# so the integration contract test can drive the full subprocess write
# path with no API key. It only activates when explicitly set, so it
# can never silently shadow a production run. It is a transport stub,
# NOT a model-string override — ``--model`` is still required and is
# what gets stamped into every line.
_STUB_ENV = "OPUS_REFERENCE_BASELINE_STUB_RESPONSE"

# Arbitrary but FROZEN namespace. Freezing it is the determinism
# contract: re-running over the same transcript must reproduce the same
# pair_ids. Changing this constant re-keys every future baseline, so it
# must never change once shipped.
_OPUS_REF_NAMESPACE = uuid.UUID("3f1c9d8e-2b6a-5c7d-9e0f-1a2b3c4d5e6f")

_RAW_TRANSCRIPTS_SUBPATH = ("store", "raw", "transcripts")
_OUTPUT_SUBDIR = "reference_baselines"
_OUTPUT_FILENAME = "opus_reference_minutes.jsonl"

# The canonical extraction prompt — resolved through the pipeline module
# so the path is the SINGLE source of truth. We do not re-derive or
# duplicate the prompt text; the file is read at run time.
_PROMPT_PATH: Path = _mm_llm._PROMPT_PATH

# Schema that defines every extraction type (PR #123). Extraction types
# are derived from THIS file's array properties so a schema change is
# the single place that adds a type — no parallel list to drift.
_MEETING_MINUTES_SCHEMA = (
    Path(_mm_llm.__file__).resolve().parents[1]
    / "schemas"
    / "meeting_minutes.schema.json"
)

# Primary text field per extraction type (the field that becomes
# ``ground_truth_text``). ``None`` means the item is a plain string
# (the three legacy arrays). Every non-``None`` field is a
# schema-*required* field of that type, so a missing value is a
# malformed-response signal, not a sparse-but-valid item.
_PRIMARY_TEXT_FIELD: Dict[str, Optional[str]] = {
    "decisions": None,
    "action_items": None,
    "open_questions": None,
    "commitments": "commitment_text",
    "risks": "risk_text",
    "cross_references": "ref_text",
    "attendees": "name",
    "topics": "title",
    "regulatory_references": "reference_text",
    "technical_parameters": "value",
    "named_artifacts": "name",
    "scheduled_events": "title",
}

# Object-form fallbacks for the three legacy arrays: the schema allows
# action_items / open_questions items to be a structured object as well
# as a string. The prompt asks for strings, but if a structured object
# arrives we read its schema-required text field rather than silently
# dropping a real item.
_LEGACY_OBJECT_TEXT_FIELD: Dict[str, str] = {
    "action_items": "action",
    "open_questions": "question_text",
}

# Header date patterns, tried in order. First hit wins. Returns an
# ISO YYYY-MM-DD string. ``meeting_date`` is null when no header date is
# present — we never infer one from the slug or the clock.
_HEADER_SCAN_LINES = 15
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b"),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November",
            "December",
        ],
        start=1,
    )
}
_MONTH_NAME_RE = re.compile(
    r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b"
)


class ReferenceBaselineError(RuntimeError):
    """A fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def load_extraction_prompt() -> str:
    """Read the canonical system prompt. Halt before any model call.

    This is the single source of truth shared with the Haiku pipeline.
    A read failure raises ``missing_extraction_prompt`` so a missing
    prompt can never reach the transport with a silent fallback.
    """
    try:
        text = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReferenceBaselineError(
            "missing_extraction_prompt",
            f"cannot read canonical extraction prompt at {_PROMPT_PATH}: "
            f"{exc}",
        ) from exc
    if not text.strip():
        raise ReferenceBaselineError(
            "missing_extraction_prompt",
            f"canonical extraction prompt at {_PROMPT_PATH} is empty",
        )
    return text


def extraction_types() -> List[str]:
    """The extraction types, derived from the meeting_minutes schema.

    Every array property except ``grounding`` (Phase Y meta, not an
    extracted content category). A schema array that has no entry in
    ``_PRIMARY_TEXT_FIELD`` is a hard error: it means a new type was
    added to the schema without teaching this workflow how to read its
    text, and we refuse to silently skip it.
    """
    try:
        schema = json.loads(
            _MEETING_MINUTES_SCHEMA.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceBaselineError(
            "missing_extraction_schema",
            f"cannot read meeting_minutes schema at "
            f"{_MEETING_MINUTES_SCHEMA}: {exc}",
        ) from exc
    props = schema.get("properties", {})
    types: List[str] = []
    for key, spec in props.items():
        if key == "grounding":
            continue
        if isinstance(spec, dict) and spec.get("type") == "array":
            if key not in _PRIMARY_TEXT_FIELD:
                raise ReferenceBaselineError(
                    "unmapped_extraction_type",
                    f"schema array property {key!r} has no "
                    f"_PRIMARY_TEXT_FIELD mapping — add one before "
                    f"running so the type is not silently skipped",
                )
            types.append(key)
    return types


def _resolve_source_artifact_id(data_lake: Path, source_id: str) -> str:
    """slug -> stable transcript UUID via source_record.json.

    Validates the record against ``source_record.schema.json`` before
    reading ``artifact_id`` off it (CLAUDE.md read-path co-requirement).
    Missing file or schema drift halts — the UUID is never inferred.
    """
    sr_path = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "source_record.json"
    )
    if not sr_path.is_file():
        raise ReferenceBaselineError(
            "missing_source_record",
            f"no source_record.json at {sr_path} — cannot resolve "
            f"source_artifact_id for {source_id!r}",
        )
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} is unreadable/!json: {exc}",
        ) from exc
    try:
        validate_artifact(record, "source_record", str(sr_path))
    except ArtifactValidationError as exc:
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} failed schema: {exc}",
        ) from exc
    artifact_id = record.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ReferenceBaselineError(
            "invalid_source_record",
            f"source_record.json at {sr_path} has no usable artifact_id",
        )
    return artifact_id


def _read_transcript_text(docx_path: Path) -> str:
    """Plain text from a raw transcript .docx via the pipeline extractor.

    Writes to a throwaway tempfile so nothing is ever written under the
    data-lake ``raw/`` tree (the data-lake contract forbids core
    writing to raw/).
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "transcript.txt"
        result = DocxExtractor().extract(
            str(docx_path), output_path=str(out)
        )
        if result.get("status") != "success":
            raise ReferenceBaselineError(
                "transcript_unreadable",
                f"docx extraction failed for {docx_path}: "
                f"{result.get('reason')!r}",
            )
        text = out.read_text(encoding="utf-8")
    if not text.strip():
        raise ReferenceBaselineError(
            "transcript_unreadable",
            f"transcript {docx_path} extracted to empty text",
        )
    return text


def _meeting_date_from_header(text: str) -> Optional[str]:
    """ISO date from the transcript header, or None. Never inferred."""
    head = "\n".join(text.splitlines()[:_HEADER_SCAN_LINES])
    for pat in _DATE_PATTERNS:
        m = pat.search(head)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return datetime.date(y, mo, d).isoformat()
            except ValueError:
                continue
    m = _MONTH_NAME_RE.search(head)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return datetime.date(
                    int(m.group(3)), month, int(m.group(2))
                ).isoformat()
            except ValueError:
                return None
    return None


def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    return body.strip()


def parse_response(raw: str) -> Dict[str, Any]:
    """Parse the model text into a JSON object, or HALT.

    A non-JSON / non-object response is a fail-closed halt — we never
    coerce, repair, or partially accept it.
    """
    body = _strip_fence(raw)
    if not body:
        raise ReferenceBaselineError(
            "malformed_llm_response", "model returned empty text"
        )
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"model response is not valid JSON: {exc}",
        ) from exc
    if not isinstance(doc, dict):
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"model response JSON is {type(doc).__name__}, not an object",
        )
    return doc


def _primary_text(etype: str, item: Any) -> str:
    """The ground_truth_text for one item, or HALT if malformed.

    String-typed arrays: the item must be a non-empty string. Structured
    arrays: the schema-required primary text field must be a non-empty
    string. Anything else is a malformed response — we do not emit a
    baseline line with an empty/synthetic primary text.
    """
    field = _PRIMARY_TEXT_FIELD[etype]
    if field is None:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            fb = _LEGACY_OBJECT_TEXT_FIELD.get(etype)
            if fb is not None:
                val = item.get(fb)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"{etype} item is not a usable string: {item!r}",
        )
    if not isinstance(item, dict):
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"{etype} item is {type(item).__name__}, expected object",
        )
    val = item.get(field)
    if not isinstance(val, str) or not val.strip():
        raise ReferenceBaselineError(
            "malformed_llm_response",
            f"{etype} item missing required text field {field!r}: "
            f"{item!r}",
        )
    return val.strip()


def build_records(
    *,
    parsed: Dict[str, Any],
    types: List[str],
    source_id: str,
    source_artifact_id: str,
    model: str,
    meeting_date: Optional[str],
    created_at: str,
) -> List[Dict[str, Any]]:
    """Build every JSONL record for one transcript, or HALT.

    All records are built in memory first. The caller only writes the
    file if this returns without raising — so a malformed item anywhere
    means the transcript's JSONL is never written (no partial output).
    """
    records: List[Dict[str, Any]] = []
    for etype in types:
        value = parsed.get(etype)
        if value is None:
            # Absent key == no items of this type. The prompt says
            # every key should be present, but an omitted key is a
            # valid empty category, not a malformed response.
            continue
        if not isinstance(value, list):
            raise ReferenceBaselineError(
                "malformed_llm_response",
                f"{etype!r} is {type(value).__name__}, expected a list",
            )
        for index, item in enumerate(value):
            ground_truth_text = _primary_text(etype, item)
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
                    "item_data": item,
                    "human_authored": False,
                    "model_authored": True,
                    "model_id": model,
                    "verified": False,
                    "status": "reference_only",
                    "provenance": {"produced_by": PRODUCED_BY},
                    "schema_version": "1.0.0",
                    "meeting_date": meeting_date,
                    "created_at": created_at,
                }
            )
    return records


def _jsonl_path(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / _OUTPUT_SUBDIR
        / _OUTPUT_FILENAME
    )


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(r, sort_keys=True, separators=(",", ":"))
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_git_worktree(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _assert_not_gitignored(data_lake: Path, abs_path: Path) -> None:
    """Refuse to leave behind a committed-never artifact.

    Mirrors ``scripts/_gitignore_audit.py``'s negation handling:
    ``git check-ignore -v`` returns rc=0 for ANY matched pattern
    including ``!`` un-ignore patterns, so a matched ``!``-pattern means
    the path is NOT ignored.
    """
    if not _is_git_worktree(data_lake):
        # Dev checkout / temp dir without a git repo — nothing can be
        # gitignored, so the guard is vacuously satisfied.
        return
    try:
        rel = abs_path.relative_to(data_lake)
    except ValueError:
        rel = abs_path
    result = subprocess.run(
        [
            "git", "-C", str(data_lake), "check-ignore", "-v",
            "--no-index", str(rel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return  # not ignored
    if result.returncode == 0:
        line = result.stdout.strip()
        try:
            head, _ = line.split("\t", 1)
            pattern = head.rsplit(":", 1)[1]
        except (IndexError, ValueError):
            pattern = ""
        if pattern.startswith("!"):
            return  # matched an un-ignore negation -> not ignored
        raise ReferenceBaselineError(
            "gitignore_blocks_artifact",
            f"{rel} is ignored by data-lake .gitignore rule: {line} "
            f"— git cannot re-include a file whose parent dir is "
            f"excluded, so add BOTH '!**/processed/**/' (re-include "
            f"the directory chain) and "
            f"'!**/processed/**/{_OUTPUT_SUBDIR}/{_OUTPUT_FILENAME}' "
            f"before committing",
        )
    raise ReferenceBaselineError(
        "gitignore_blocks_artifact",
        f"git check-ignore returned rc={result.returncode} for {rel}: "
        f"{result.stderr.strip()}",
    )


def _inventory_transcripts(
    data_lake: Path, source_id: Optional[str]
) -> List[Path]:
    """Raw transcript .docx files. Mirrors the pipeline's minutes filter.

    Files whose name contains 'minutes' are skipped (they belong in
    store/raw/minutes/ — running them as transcripts would produce the
    wrong baseline), exactly as PipelineOrchestrator does.
    """
    tdir = data_lake.joinpath(*_RAW_TRANSCRIPTS_SUBPATH)
    if not tdir.is_dir():
        raise ReferenceBaselineError(
            "missing_transcripts_dir",
            f"no raw transcripts directory at {tdir}",
        )
    docs = sorted(
        (
            p
            for p in tdir.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".docx"
            and "minutes" not in p.name.lower()
        ),
        key=lambda p: p.name,
    )
    if source_id is not None:
        docs = [p for p in docs if _slugify(p.stem) == source_id]
    return docs


def create_baselines(
    *,
    data_lake: Path,
    source_id: Optional[str],
    dry_run: bool,
    skip_existing: bool,
    model: str,
    client: Optional[Callable[..., str]] = None,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Orchestrate the run. Returns a summary dict; raises on any halt.

    ``client`` defaults to :class:`AnthropicJSONClient` constructed with
    the passed ``model``; tests inject a stub. ``system_prompt`` is
    loaded ONCE up front (before any client construction) so a missing
    prompt halts before the model is ever contacted.
    """
    if not model or not model.strip():
        raise ReferenceBaselineError(
            "missing_model",
            "--model is required and must be non-empty (the workflow "
            "resolves it from ai/registry/model_registry.json)",
        )
    model = model.strip()

    # Loaded before the client exists -> a missing prompt provably
    # cannot reach the transport.
    prompt = system_prompt if system_prompt is not None else (
        load_extraction_prompt()
    )
    types = extraction_types()

    if client is not None:
        active_client: Callable[..., str] = client
    else:
        stub = os.environ.get(_STUB_ENV)
        if stub is not None:
            def _stub_client(*, system: str, user: str) -> str:  # noqa: ARG001
                return stub
            active_client = _stub_client
        else:
            active_client = AnthropicJSONClient(model=model)

    transcripts = _inventory_transcripts(data_lake, source_id)

    per_transcript: List[Dict[str, Any]] = []
    for docx_path in transcripts:
        sid = _slugify(docx_path.stem)
        out_path = _jsonl_path(data_lake, sid)

        if skip_existing and out_path.is_file():
            per_transcript.append(
                {
                    "source_id": sid,
                    "status": "skipped",
                    "reason": "exists",
                    "total": 0,
                    "by_type": {},
                    "output_path": str(out_path),
                }
            )
            continue

        source_artifact_id = _resolve_source_artifact_id(data_lake, sid)
        transcript_text = _read_transcript_text(docx_path)
        meeting_date = _meeting_date_from_header(transcript_text)

        try:
            raw = active_client(system=prompt, user=transcript_text)
        except LLMClientError as exc:
            raise ReferenceBaselineError(
                "llm_transport_error",
                f"model transport failed for {sid}: {exc} — no "
                f"fallback model, no partial file written",
            ) from exc

        parsed = parse_response(raw)
        records = build_records(
            parsed=parsed,
            types=types,
            source_id=sid,
            source_artifact_id=source_artifact_id,
            model=model,
            meeting_date=meeting_date,
            created_at=_now_utc_iso(),
        )

        by_type: Dict[str, int] = {}
        for r in records:
            by_type[r["extraction_type"]] = (
                by_type.get(r["extraction_type"], 0) + 1
            )

        if dry_run:
            per_transcript.append(
                {
                    "source_id": sid,
                    "status": "dry_run",
                    "reason": "",
                    "total": len(records),
                    "by_type": by_type,
                    "output_path": str(out_path),
                }
            )
            continue

        _write_jsonl(out_path, records)
        _assert_not_gitignored(data_lake, out_path)
        per_transcript.append(
            {
                "source_id": sid,
                "status": "written",
                "reason": "",
                "total": len(records),
                "by_type": by_type,
                "output_path": str(out_path),
            }
        )

    return {
        "status": "success",
        "model": model,
        "dry_run": dry_run,
        "skip_existing": skip_existing,
        "transcripts": len(transcripts),
        "per_transcript": per_transcript,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument(
        "--source-id",
        default=None,
        help="Process only this slug. Omit to process all transcripts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written; write/commit nothing.",
    )
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip sources that already have the baseline JSONL "
        "(default true; pass --no-skip-existing to regenerate).",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model string, resolved by the workflow from "
        "ai/registry/model_registry.json. No default — the script "
        "never hardcodes a model.",
    )
    args = parser.parse_args(argv)

    # Mobile workflow_dispatch inputs frequently arrive with a trailing
    # space pasted from a phone keyboard; strip every string arg.
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

    source_id = args.source_id or None
    data_lake = Path(args.data_lake)
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
        result = create_baselines(
            data_lake=data_lake,
            source_id=source_id,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            model=args.model,
        )
    except ReferenceBaselineError as exc:
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
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    # Human-readable per-transcript summary: source_id | total | by type
    for t in result["per_transcript"]:
        by_type = t.get("by_type") or {}
        by_type_str = (
            ", ".join(f"{k}={by_type[k]}" for k in sorted(by_type))
            or "-"
        )
        print(
            f"{t['source_id']} | {t['status']} | total={t['total']} | "
            f"{by_type_str}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
