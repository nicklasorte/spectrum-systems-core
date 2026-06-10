#!/usr/bin/env python3
"""Operator-initiated, reproducible Opus reference-baseline EXTRACTION.

This captures the actual extraction orchestration used to produce the
committed Opus reference baselines on a Mac, as versioned code, so a
committed baseline can be regenerated from committed code. It is the
local-CLI sibling of ``scripts/create_opus_reference_baselines.py`` (the
Anthropic-API workflow): instead of constructing an SDK client it shells
out to the locally-authenticated ``claude`` CLI in print mode, exactly as
the operator did by hand. It then hands the model output to the
(now self-healing) ``scripts/ingest_opus_baseline.py`` gate.

End-to-end flow (one transcript):

  1. Resolve the pinned model from ``ai/registry/model_registry.json``
     (the ``models.opus_reference_baseline`` slot). No inline model
     literal; ``--model`` overrides only for a deliberate experiment.
  2. Load the canonical extraction prompt
     (``workflows/prompts/meeting_minutes_llm.md``) — the SAME prompt the
     Haiku pipeline and the API sibling use.
  3. Read the staged transcript text
     (``<data-lake>/store/raw/meetings/<source_id>/source.txt`` by
     default; ``--transcript`` overrides).
  4. Assemble the model input as ``<prompt>`` + ``"\n\n=== RAW
     TRANSCRIPT ===\n\n"`` + ``<transcript>`` — byte-for-byte the
     assembly that produced the committed baselines.
  5. Invoke ``DISABLE_AUTOUPDATER=1 claude -p --model <model>`` with the
     assembled input on stdin and capture stdout as the raw extraction.
     (A transport stub env var replaces ONLY this step under test — no
     network, no CLI — see ``_STUB_ENV``.)
  6. Add the four validation-only metadata-scaffolding fields
     (``artifact_type``, ``schema_version``, ``title``, ``summary``) that
     the strict ``meeting_minutes`` schema requires at the top level. The
     model returns a content-only object; these fields are never
     persisted to the baseline (the ingest explodes only the content
     arrays into JSONL), so synthesizing them is envelope scaffolding,
     NOT content fabrication. ``title`` is the transcript's first
     non-empty line; ``summary`` is labelled as ingest scaffolding.
  7. Hand the prepared file to ``ingest_opus_baseline.ingest`` — the
     single fail-closed gate. It self-heals the lossless input quirks
     (markdown fence / preamble, the hallucinated ``source_chunk_id``,
     stray disallowed keys on closed item types, and null-valued keys
     the schema forbids null on) and validates against the schema. It
     deliberately does NOT fabricate a value for a missing/null REQUIRED
     field — such an extraction HALTs ``schema_violation`` so the gap
     surfaces as a real signal rather than a silent ``"Unknown"``.

Reproducibility note: the LLM step itself is not byte-deterministic
run-to-run, so re-running this script does NOT guarantee the identical
``opus.json`` the committed baseline was built from. What IS reproducible
— and what closes the gap this script was written to close — is the
deterministic tail: feeding a SAVED raw extraction through metadata
scaffolding + the self-healing ingest reproduces the committed baseline
byte-for-byte (modulo the wall-clock ``created_at``). That is proven for
the committed baselines in ``scripts/_verify_opus_extract_repro.py``. To
regenerate from a saved raw output without a model call, set ``_STUB_ENV``
to its contents.

Fail-closed contract (every gate HALTs; nothing partial is ingested):

* ``missing_model`` — registry slot missing/empty and no ``--model``.
* ``missing_extraction_prompt`` — canonical prompt unreadable/empty.
* ``missing_transcript`` — transcript file missing or extracts to empty.
* ``extract_cli_not_found`` — the ``claude`` CLI is not on PATH (and no
  stub is set). Checked BEFORE the call.
* ``extract_transport_error`` — the CLI exits non-zero or returns empty
  stdout. No fallback model, no partial file.
* every ``ingest_opus_baseline`` halt (``invalid_input_json``,
  ``schema_violation``, ``missing_source_record``, ``already_ingested``,
  …) propagates unchanged — the ingest stays the one authority.

This script makes exactly ONE outbound call: the ``claude`` CLI (unless a
stub is set). It writes the raw extraction and the prepared file to a
work directory OUTSIDE the data-lake; only the ingest writes under the
data-lake, and only the canonical baseline JSONL.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_REPO_ROOT = _SCRIPTS_DIR.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import ingest_opus_baseline as iob  # noqa: E402
from create_opus_reference_baselines import (  # noqa: E402
    load_extraction_prompt,
)
from spectrum_systems_core.promotion.gate import (  # noqa: E402
    GROUNDING_BINDING_SCHEMA_VERSION,
)

# Transport stub seam. When set, its value is returned verbatim as the
# raw model output instead of invoking the ``claude`` CLI — the SAME
# env-var seam pattern ``create_opus_reference_baselines._STUB_ENV`` uses,
# so the verification harness and the integration contract test can drive
# the full prepare+ingest path with no CLI and no network. It activates
# only when explicitly set, so it can never silently shadow a real run. It
# is a TRANSPORT stub, NOT a model-string override — the resolved model is
# still what gets stamped into every JSONL line by the ingest.
_STUB_ENV = "EXTRACT_OPUS_BASELINE_STUB_RESPONSE"

# The exact separator the committed baselines were assembled with,
# between the canonical prompt and the raw transcript body.
_TRANSCRIPT_SEPARATOR = "\n\n=== RAW TRANSCRIPT ===\n\n"

# The CLI the operator runs locally. Print mode (``-p``) emits the model
# response to stdout. ``DISABLE_AUTOUPDATER=1`` keeps the auto-updater
# from writing to stdout/stderr mid-run (it would corrupt the captured
# JSON), matching the operator's invocation.
_CLAUDE_BIN = "claude"


class ExtractError(RuntimeError):
    """Fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _default_transcript_path(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake
        / "store"
        / "raw"
        / "meetings"
        / source_id
        / "source.txt"
    )


def _read_transcript(path: Path) -> str:
    if not path.is_file():
        raise ExtractError(
            "missing_transcript",
            f"no transcript at {path}; stage the meeting into the "
            f"data-lake (store/raw/meetings/<source_id>/source.txt) "
            f"before extracting",
        )
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ExtractError(
            "missing_transcript", f"transcript at {path} is empty"
        )
    return text


def _resolve_model(model_override: Optional[str]) -> str:
    """Pin the model from the registry unless explicitly overridden.

    Reuses ``ingest_opus_baseline._resolve_model_from_registry`` so both
    scripts read the SAME ``models.opus_reference_baseline`` slot and a
    registry re-key flows through both with no second source of truth.
    """
    if isinstance(model_override, str) and model_override.strip():
        return model_override.strip()
    try:
        return iob._resolve_model_from_registry()
    except iob.OpusIngestError as exc:
        raise ExtractError(exc.reason, exc.detail) from exc


def _invoke_claude(
    *, model: str, model_input: str, source_id: str
) -> str:
    """Run the local ``claude`` CLI (or the stub) and return raw stdout.

    Fail-closed: a missing CLI, a non-zero exit, or empty stdout HALTs —
    there is no fallback model and nothing partial is written.
    """
    stub = os.environ.get(_STUB_ENV)
    if stub is not None:
        return stub

    if shutil.which(_CLAUDE_BIN) is None:
        raise ExtractError(
            "extract_cli_not_found",
            f"the {_CLAUDE_BIN!r} CLI is not on PATH; install/authenticate "
            f"it, or set {_STUB_ENV} to a saved raw extraction to "
            f"regenerate from a prior output without a model call",
        )

    env = dict(os.environ)
    env["DISABLE_AUTOUPDATER"] = "1"
    try:
        proc = subprocess.run(
            [_CLAUDE_BIN, "-p", "--model", model],
            input=model_input,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise ExtractError(
            "extract_transport_error",
            f"failed to launch {_CLAUDE_BIN!r} for {source_id}: {exc}",
        ) from exc
    if proc.returncode != 0:
        raise ExtractError(
            "extract_transport_error",
            f"{_CLAUDE_BIN} exited {proc.returncode} for {source_id}: "
            f"{proc.stderr.strip()[:500]!r} — no fallback model, no "
            f"partial file written",
        )
    raw = proc.stdout
    if not raw.strip():
        raise ExtractError(
            "extract_transport_error",
            f"{_CLAUDE_BIN} returned empty stdout for {source_id} — the "
            f"extraction produced nothing; no partial file written",
        )
    return raw


def _transcript_title(transcript_text: str, source_id: str) -> str:
    """First non-empty transcript line, falling back to the source slug.

    Identical intent to the manual ``augment.py`` scaffolding step. The
    value is validation-only — the ingest never persists ``title`` to the
    baseline — so it cannot affect baseline bytes.
    """
    for line in transcript_text.splitlines():
        if line.strip():
            return line.strip()
    return source_id


def _scaffold_metadata(
    *, raw_extraction: str, transcript_text: str, source_id: str,
    raw_out: Path,
) -> Dict[str, Any]:
    """Parse the content-only raw extraction and add the four schema-
    required top-level metadata fields, returning the prepared object.

    Uses the ingest's own ``_extract_json_object_text`` to slice the
    object out of any markdown fence / preamble (class 1) so the same
    envelope-repair logic governs both scripts. A non-object extraction
    HALTs here rather than producing a malformed prepared file (the
    ingest would reject it anyway; failing here gives a clearer code).

    The four fields are VALIDATION-ONLY scaffolding: the ingest explodes
    only the content arrays into JSONL, so ``artifact_type`` /
    ``schema_version`` / ``title`` / ``summary`` never reach the
    baseline. They exist solely to satisfy the strict top-level schema.
    """
    json_text = iob._extract_json_object_text(raw_extraction, raw_out)
    try:
        doc = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ExtractError(
            "invalid_input_json",
            f"raw extraction for {source_id} is not valid JSON: {exc}",
        ) from exc
    if not isinstance(doc, dict):
        raise ExtractError(
            "invalid_input_json",
            f"raw extraction for {source_id} is "
            f"{type(doc).__name__}, expected a JSON object",
        )
    doc["artifact_type"] = "meeting_minutes"
    doc["schema_version"] = GROUNDING_BINDING_SCHEMA_VERSION
    doc["title"] = _transcript_title(transcript_text, source_id)
    doc["summary"] = (
        f"Opus reference baseline for {source_id}. Extracted content is "
        f"in the structured arrays; this summary is ingest scaffolding "
        f"and is not persisted to the baseline."
    )
    return doc


def extract(
    *,
    data_lake: Path,
    source_id: str,
    operator: str,
    model_override: Optional[str],
    transcript_path: Optional[Path],
    work_dir: Path,
    dry_run: bool,
) -> Dict[str, Any]:
    """Orchestrate one extraction + ingest. Returns a summary; raises on
    any halt."""
    model = _resolve_model(model_override)
    prompt = load_extraction_prompt()  # halts if missing/empty

    tpath = transcript_path or _default_transcript_path(
        data_lake, source_id
    )
    transcript_text = _read_transcript(tpath)

    model_input = prompt + _TRANSCRIPT_SEPARATOR + transcript_text
    raw_extraction = _invoke_claude(
        model=model, model_input=model_input, source_id=source_id
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    raw_out = work_dir / f"{source_id}.opus.json"
    raw_out.write_text(raw_extraction, encoding="utf-8")

    prepared = _scaffold_metadata(
        raw_extraction=raw_extraction,
        transcript_text=transcript_text,
        source_id=source_id,
        raw_out=raw_out,
    )
    prepared_out = work_dir / f"{source_id}.prepared.json"
    prepared_out.write_text(
        json.dumps(prepared, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Hand to the single fail-closed gate. It performs the lossless
    # self-heal (classes 1-3) and the schema validation; a null/missing
    # REQUIRED field is NOT fabricated and HALTs here as a real signal.
    ingest_summary = iob.ingest(
        input_file=prepared_out,
        data_lake=data_lake,
        source_id=source_id,
        operator=operator,
        model=model,
        dry_run=dry_run,
    )

    return {
        "status": "success",
        "source_id": source_id,
        "model": model,
        "operator": operator,
        "transcript": str(tpath),
        "raw_extraction": str(raw_out),
        "prepared_input": str(prepared_out),
        "stubbed": _STUB_ENV in os.environ,
        "ingest": ingest_summary,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-id",
        required=True,
        help="Transcript slug under data-lake/store/.../meetings/.",
    )
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Root path of the data-lake clone (contains 'store/').",
    )
    parser.add_argument(
        "--operator",
        required=True,
        help="Identifier of the human running the extraction (logged "
        "for audit; not stamped into the Opus baseline rows).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model string. By default the pinned "
        "ai/registry/model_registry.json::models.opus_reference_baseline "
        "is used so a registry re-key flows through automatically.",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="Override the transcript path. Defaults to "
        "<data-lake>/store/raw/meetings/<source-id>/source.txt.",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Directory for the raw extraction and prepared input "
        "(OUTSIDE the data-lake). Defaults to a fresh temp dir.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run extraction + validation but write no baseline JSONL.",
    )
    args = parser.parse_args(argv)

    # Mobile copy-paste foot-gun: strip trailing spaces from string args.
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

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

    transcript_path = (
        Path(args.transcript).resolve() if args.transcript else None
    )
    if args.work_dir:
        work_dir = Path(args.work_dir)
        if not work_dir.is_absolute():
            work_dir = (Path.cwd() / work_dir).resolve()
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="opus-extract-"))

    try:
        result = extract(
            data_lake=data_lake,
            source_id=args.source_id,
            operator=args.operator,
            model_override=args.model,
            transcript_path=transcript_path,
            work_dir=work_dir,
            dry_run=args.dry_run,
        )
    except (ExtractError, iob.OpusIngestError) as exc:
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
        # Exit codes mirror ingest_opus_baseline:
        #   2 — input/transport problem (file-not-found, malformed JSON,
        #       missing prompt/transcript/model, CLI transport)
        #   1 — schema/data-shape rejection
        if exc.reason in (
            "input_file_not_found",
            "invalid_input_json",
            "missing_model",
            "missing_extraction_prompt",
            "missing_transcript",
            "extract_cli_not_found",
            "extract_transport_error",
            "missing_schema",
        ):
            return 2
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    ing = result["ingest"]
    by_type = ing.get("by_type") or {}
    by_type_str = (
        ", ".join(f"{k}={by_type[k]}" for k in sorted(by_type)) or "-"
    )
    print(
        f"{result['source_id']} | "
        f"{'dry_run' if ing.get('dry_run') else 'written'} | "
        f"model={result['model']} | total={ing.get('total')} | "
        f"{by_type_str}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
