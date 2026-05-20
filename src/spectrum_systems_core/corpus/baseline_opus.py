"""Phase 4a — ``spectrum-core baseline-opus`` implementation.

Produces the canonical Opus reference baseline for one or more sources
in the corpus. The Opus baseline is the **ceiling reference** against
which the Haiku production extraction is measured.

Per-source flow:

1. Load the corpus manifest (with hash verification).
2. Resolve the source's ``source_record.json`` under the data lake.
3. Read the transcript via ``source_record.payload.raw_path`` (the
   per-PR-#188 contract).
4. Load the canonical Opus prompt at
   ``src/spectrum_systems_core/workflows/prompts/meeting_minutes_opus.md``.
   A missing or empty file HALTS with ``opus_prompt_not_found`` —
   never falls back to the Haiku prompt.
5. Call the Anthropic Opus model with (system=prompt, user=transcript).
   A test stub may inject a response via the
   ``BASELINE_OPUS_STUB_RESPONSE`` env var (mirrors the
   ``OPUS_REFERENCE_BASELINE_STUB_RESPONSE`` seam of
   ``scripts/create_opus_reference_baselines.py``).
6. Parse the JSON response. A non-JSON / non-object response HALTS
   with ``malformed_llm_response`` — never silently emits an empty
   extraction.
7. Build the ``meeting_minutes_opus`` envelope and write it to
   ``processed/meetings/<source_id>/meeting_minutes_opus__<timestamp>.json``.
8. Update the manifest's ``observed.ingestion_status`` to
   ``baseline_complete`` and refresh the hash.

``--all`` mode requires ``--confirm-cost`` and prints the Opus +
Haiku-equivalent cost estimate BEFORE prompting for confirmation. The
``--confirm-cost`` flag is CLI-only — no env var can bypass it.

The model string is resolved at runtime from
``ai/registry/model_registry.json::opus_reference_baseline`` (currently
``claude-opus-4-7``). The string is stamped into every artifact
``provenance.model_id`` so a past artifact keeps its exact model even
after the registry is rolled forward.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from ..cost.estimator import estimate_extraction_cost
from .manifest_loader import (
    find_source,
    load_manifest,
    rewrite_manifest_with_observed,
)


# Reason codes the baseline-opus CLI emits at its surface. They are part
# of the public contract; tests assert on these strings and the operator
# runbook references them by name.
OPUS_PROMPT_NOT_FOUND: str = "opus_prompt_not_found"
SOURCE_RECORD_NOT_FOUND: str = "source_record_not_found"
SOURCE_RECORD_INVALID: str = "source_record_invalid"
TRANSCRIPT_NOT_FOUND: str = "baseline_opus_transcript_not_found"
TRANSCRIPT_UNREADABLE: str = "baseline_opus_transcript_unreadable"
BASELINE_SOURCE_UNKNOWN: str = "baseline_opus_source_id_unknown"
MALFORMED_LLM_RESPONSE: str = "malformed_llm_response"
LLM_TRANSPORT_ERROR: str = "llm_transport_error"
CONFIRM_COST_REQUIRED: str = "confirm_cost_required"
CONFIRM_COST_DECLINED: str = "confirm_cost_declined"
MODEL_REGISTRY_ERROR: str = "model_registry_error"
BASELINE_OPUS_WRITTEN: str = "baseline_opus_written"

# The test stub seam — set the env var to a JSON string and that
# value is returned in place of the live model response. The flag
# only activates when explicitly set, so it can never silently shadow
# a production run.
STUB_ENV: str = "BASELINE_OPUS_STUB_RESPONSE"

# CLI-only confirmation flag. The CLI argument layer is responsible for
# rejecting an env-var bypass; this constant exists as documentation of
# the contract and as a single-string token tests assert on.
CONFIRM_COST_ENV_REJECTED_TOKEN: str = "confirm_cost_must_be_cli_flag"

_PROMPT_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "workflows"
    / "prompts"
    / "meeting_minutes_opus.md"
)

_MODEL_REGISTRY_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "ai"
    / "registry"
    / "model_registry.json"
)
_OPUS_REGISTRY_KEY: str = "opus_reference_baseline"

# Artifact constants. The filename pattern matches
# ``corpus.status._has_opus_baseline`` so a successful run advances
# the status rollup to ``baseline_complete``.
ARTIFACT_TYPE: str = "meeting_minutes_opus"
ARTIFACT_SCHEMA_VERSION: str = "1.0.0"
PRODUCED_BY: str = "opus_baseline_cli"

# The Haiku-class equivalent model used for the "comparison cost"
# print-out before the confirm prompt. Cost constants for this id
# must exist in ``data/cost_constants.json``.
_HAIKU_EQUIVALENT_MODEL: str = "claude-haiku-4-7"


class BaselineOpusError(ValueError):
    """Raised on any baseline-opus failure.

    Carries the contract reason code in :attr:`reason_code` so callers
    (the CLI, tests) can branch on it without string-matching the
    message.
    """

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class BaselineOpusOutcome:
    """One row of the per-source baseline-opus summary."""

    source_id: str
    status: str  # baseline_complete | failed
    reason_code: Optional[str]
    artifact_path: Optional[str]
    item_count: Optional[int]
    estimated_cost_usd: Optional[str]
    message: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class BaselineOpusRunSummary:
    """Aggregate result of one ``baseline-opus`` invocation."""

    manifest_hash: str
    model_id: str
    prompt_content_hash: str
    outcomes: List[BaselineOpusOutcome]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "model_id": self.model_id,
            "prompt_content_hash": self.prompt_content_hash,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


# ---------------------------------------------------------------------------
# Prompt + model resolution.
# ---------------------------------------------------------------------------


def load_opus_prompt(path: Path | str | None = None) -> str:
    """Read the canonical Opus reference prompt. HALT before any model call.

    A missing / empty / unreadable prompt file HALTS with
    ``opus_prompt_not_found`` so the transport can never be reached
    with a silent fallback to the Haiku prompt.
    """
    p = Path(path) if path is not None else _PROMPT_PATH
    if not p.is_file():
        raise BaselineOpusError(
            OPUS_PROMPT_NOT_FOUND,
            f"canonical Opus prompt not found at {p}",
        )
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineOpusError(
            OPUS_PROMPT_NOT_FOUND,
            f"could not read Opus prompt at {p}: {exc}",
        ) from exc
    if not text.strip():
        raise BaselineOpusError(
            OPUS_PROMPT_NOT_FOUND,
            f"Opus prompt at {p} is empty",
        )
    return text


def prompt_content_hash(prompt_text: str) -> str:
    """sha256 hex of the verbatim prompt text."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def resolve_opus_model() -> str:
    """Resolve the Opus model id from ``ai/registry/model_registry.json``.

    Fail-closed: a missing file, malformed JSON, or a missing
    ``opus_reference_baseline`` entry HALTS with
    ``model_registry_error``. The string is stamped into every produced
    artifact's ``provenance.model_id`` so a past artifact remains
    traceable to its exact model after the registry rolls forward.
    """
    try:
        raw = _MODEL_REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineOpusError(
            MODEL_REGISTRY_ERROR,
            f"cannot read model registry at {_MODEL_REGISTRY_PATH}: {exc}",
        ) from exc
    try:
        registry = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BaselineOpusError(
            MODEL_REGISTRY_ERROR,
            f"model registry at {_MODEL_REGISTRY_PATH} is not valid JSON: {exc}",
        ) from exc
    models = registry.get("models") if isinstance(registry, dict) else None
    if not isinstance(models, dict):
        raise BaselineOpusError(
            MODEL_REGISTRY_ERROR,
            f"model registry at {_MODEL_REGISTRY_PATH} has no `models` map",
        )
    model_id = models.get(_OPUS_REGISTRY_KEY)
    if not isinstance(model_id, str) or not model_id.strip():
        raise BaselineOpusError(
            MODEL_REGISTRY_ERROR,
            f"model registry at {_MODEL_REGISTRY_PATH} has no string "
            f"`{_OPUS_REGISTRY_KEY}` entry",
        )
    return model_id.strip()


# ---------------------------------------------------------------------------
# Source resolution.
# ---------------------------------------------------------------------------


def _processed_meeting_dir(lake_root: Path, source_id: str) -> Path:
    """Resolve the per-source processed directory.

    The corpus subsystem writes under ``<lake>/processed/meetings/<sid>/``
    (the modern layout shared with ``corpus.status``). Older trees that
    nest under ``<lake>/store/processed/meetings/`` are detected and
    used when the modern path is absent — same fallback the status
    rollup applies.
    """
    p1 = lake_root / "processed" / "meetings" / source_id
    if p1.exists():
        return p1
    p2 = lake_root / "store" / "processed" / "meetings" / source_id
    if p2.exists():
        return p2
    return p1


def _load_source_record(lake_root: Path, source_id: str) -> Dict[str, Any]:
    """Read ``source_record.json`` for one source or HALT.

    The record is the per-PR-#188 contract surface that carries the
    transcript ``raw_path``. A missing or malformed record HALTs.
    """
    sr_path = _processed_meeting_dir(lake_root, source_id) / "source_record.json"
    if not sr_path.is_file():
        raise BaselineOpusError(
            SOURCE_RECORD_NOT_FOUND,
            f"no source_record.json at {sr_path}; run "
            f"`spectrum-core ingest-corpus --source-id {source_id}` first",
        )
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineOpusError(
            SOURCE_RECORD_INVALID,
            f"source_record.json at {sr_path} unreadable/!json: {exc}",
        ) from exc
    if not isinstance(record, dict):
        raise BaselineOpusError(
            SOURCE_RECORD_INVALID,
            f"source_record.json at {sr_path} is "
            f"{type(record).__name__}, expected a JSON object",
        )
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise BaselineOpusError(
            SOURCE_RECORD_INVALID,
            f"source_record.json at {sr_path} has no payload object",
        )
    raw_path = payload.get("raw_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise BaselineOpusError(
            SOURCE_RECORD_INVALID,
            f"source_record.json at {sr_path} has no string "
            f"payload.raw_path; the ingest CLI must populate it",
        )
    return record


def _read_transcript(lake_root: Path, raw_path: str) -> str:
    """Load the raw transcript text from disk."""
    p = (lake_root / raw_path).resolve()
    if not p.is_file():
        raise BaselineOpusError(
            TRANSCRIPT_NOT_FOUND,
            f"transcript file {p} (resolved from raw_path {raw_path!r}) "
            f"is missing",
        )
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise BaselineOpusError(
            TRANSCRIPT_UNREADABLE,
            f"could not read transcript at {p}: {exc}",
        ) from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineOpusError(
            TRANSCRIPT_UNREADABLE,
            f"transcript at {p} is not valid UTF-8: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# LLM call + parse.
# ---------------------------------------------------------------------------


def _build_client(model_id: str) -> Callable[..., str]:
    """Construct the Opus client. The stub env var wins when set."""
    stub = os.environ.get(STUB_ENV)
    if stub is not None:
        def _stub(*, system: str, user: str) -> str:  # noqa: ARG001
            return stub
        return _stub
    # Lazy import: the SDK is only needed for a real run.
    from ..workflows.llm_client import AnthropicJSONClient

    return AnthropicJSONClient(model=model_id)


def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body[3:]
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    return body.strip()


def _parse_response(raw: str) -> Dict[str, Any]:
    """Parse the model's text into a JSON object, or HALT.

    Tolerates a markdown fence around the JSON (the model occasionally
    wraps its response). A non-JSON / non-object response HALTs with
    ``malformed_llm_response`` — never silently emits an empty
    extraction.
    """
    body = _strip_fence(raw)
    if not body:
        raise BaselineOpusError(
            MALFORMED_LLM_RESPONSE, "model returned empty text"
        )
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BaselineOpusError(
            MALFORMED_LLM_RESPONSE,
            f"model response is not valid JSON: {exc}",
        ) from exc
    if not isinstance(doc, dict):
        raise BaselineOpusError(
            MALFORMED_LLM_RESPONSE,
            f"model response JSON is {type(doc).__name__}, not an object",
        )
    return doc


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )


def _now_compact_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Artifact build + write.
# ---------------------------------------------------------------------------


# The 23 content arrays the Opus prompt produces (no `grounding`, which
# is a meta-array). Drives the item-count assertion.
_CONTENT_ARRAYS: tuple[str, ...] = (
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


def count_extracted_items(payload: Mapping[str, Any]) -> int:
    """Count items across all 23 content arrays in a parsed payload."""
    total = 0
    for key in _CONTENT_ARRAYS:
        v = payload.get(key)
        if isinstance(v, list):
            total += len(v)
    return total


def _content_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_artifact(
    *,
    source_id: str,
    parsed_payload: Mapping[str, Any],
    raw_hash: str,
    transcript_hash: str,
    prompt_hash: str,
    model_id: str,
    trace_id: str,
) -> Dict[str, Any]:
    """Build the meeting_minutes_opus envelope.

    The envelope is the standard 9-field core artifact shape:
    ``artifact_id`` is deterministic over (source, prompt, transcript),
    ``created_at`` is wall-clock (acceptable for a non-determinism-path
    reference artifact). The ``provenance`` block carries
    ``produced_by``, ``model_id``, ``prompt_content_hash`` and
    ``transcript_hash`` so a future audit can reproduce the run from
    the artifact alone.
    """
    artifact_seed = f"{source_id}|{transcript_hash}|{prompt_hash}|{model_id}"
    artifact_id = hashlib.sha256(artifact_seed.encode("utf-8")).hexdigest()

    payload: Dict[str, Any] = {
        # Preserve every emitted array. The 23 content arrays + grounding.
        key: parsed_payload.get(key, []) for key in (*_CONTENT_ARRAYS, "grounding")
    }
    payload["provenance"] = {
        "produced_by": PRODUCED_BY,
        "model_id": model_id,
        "prompt_content_hash": prompt_hash,
        "transcript_hash": transcript_hash,
        "raw_hash": raw_hash,
    }

    return {
        "artifact_id": artifact_id,
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "promoted",
        "created_at": _now_iso(),
        "trace_id": trace_id,
        "input_refs": [f"source_record:{source_id}"],
        "content_hash": _content_hash(payload),
        "payload": payload,
    }


def _write_artifact(
    artifact: Mapping[str, Any], *, lake_root: Path, source_id: str
) -> Path:
    out_dir = _processed_meeting_dir(lake_root, source_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"meeting_minutes_opus__{_now_compact_iso()}.json"
    out_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Cost estimate + confirmation.
# ---------------------------------------------------------------------------


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def _format_cost(cost: Decimal) -> str:
    return f"${cost:,.4f}"


def _estimate_cost_pair(
    *, byte_length: int, opus_model: str
) -> tuple[Decimal, Decimal]:
    """Estimate (opus_cost, haiku_equivalent_cost) for a single source."""
    opus = estimate_extraction_cost(byte_length, opus_model)
    try:
        haiku = estimate_extraction_cost(byte_length, _HAIKU_EQUIVALENT_MODEL)
    except Exception:  # noqa: BLE001 — haiku price is informational
        haiku = Decimal("0")
    return opus, haiku


def format_all_mode_confirmation(
    *,
    per_source: List[tuple[str, int]],
    opus_model: str,
) -> str:
    """Render the operator-facing cost summary for --all mode.

    Returns the formatted string with both Opus and Haiku-equivalent
    totals. The Haiku number is for context — it answers "how much
    more expensive is the ceiling reference than a production run?".
    """
    total_opus = Decimal("0")
    total_haiku = Decimal("0")
    for _, byte_len in per_source:
        opus_cost, haiku_cost = _estimate_cost_pair(
            byte_length=byte_len, opus_model=opus_model
        )
        total_opus += opus_cost
        total_haiku += haiku_cost
    if total_haiku > 0:
        ratio = total_opus / total_haiku
        ratio_text = f"({ratio:.0f}× more expensive)"
    else:
        ratio_text = "(Haiku price unavailable)"
    return (
        f"Estimated cost for Opus baseline across all "
        f"{len(per_source)} sources:\n"
        f"  Haiku equivalent: {_format_cost(total_haiku)}\n"
        f"  Opus:             {_format_cost(total_opus)} {ratio_text}\n"
    )


# ---------------------------------------------------------------------------
# Per-source state hook (idempotent; informational).
# ---------------------------------------------------------------------------


def _maybe_record_baseline_in_state(
    *, source_id: str, lake_root: Path, artifact_path: Path
) -> None:
    """Append a "baseline_opus_produced" marker to the per-source state
    artifact when one exists. The variance-budget hook is OUT of scope:
    variance is computed from Haiku-vs-Opus comparison F1 (the
    ``update_per_source_state`` path), and the Opus baseline run does
    not produce a comparison. This function intentionally does NOT
    increment ``runs_observed`` — that would game the variance signal.

    The marker is a diagnostic only: a future operator audit can use
    it to see when a baseline was last regenerated. The hook never
    raises (write failures are swallowed) — a diagnostic write must
    not take down the baseline run.
    """
    try:
        # Anchor the diagnostics dir under the SAME meeting tree the
        # artifact was just written into so the two stay co-located on
        # either the modern or legacy lake layout.
        state_dir = _processed_meeting_dir(lake_root, source_id) / "diagnostics"
        state_dir.mkdir(parents=True, exist_ok=True)
        marker = state_dir / "baseline_opus_history.jsonl"
        line = json.dumps(
            {
                "source_id": source_id,
                "artifact_path": str(artifact_path),
                "recorded_at": _now_iso(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        # Append-only; the file may not exist yet.
        with marker.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — diagnostics must never break the run
        return


# ---------------------------------------------------------------------------
# The orchestrator.
# ---------------------------------------------------------------------------


def _run_one_source(
    *,
    entry: Dict[str, Any],
    lake_root: Path,
    prompt_text: str,
    prompt_hash: str,
    model_id: str,
    client: Callable[..., str],
    trace_id: str,
) -> BaselineOpusOutcome:
    sid = entry["source_id"]
    record = _load_source_record(lake_root, sid)
    raw_path = record["payload"]["raw_path"]
    raw_hash = record.get("raw_hash") or record["payload"].get("raw_hash") or ""
    transcript = _read_transcript(lake_root, raw_path)
    t_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

    opus_cost, _ = _estimate_cost_pair(
        byte_length=_byte_length(transcript), opus_model=model_id
    )

    try:
        raw_resp = client(system=prompt_text, user=transcript)
    except Exception as exc:  # noqa: BLE001 — surface any transport failure
        raise BaselineOpusError(
            LLM_TRANSPORT_ERROR,
            f"Opus model call failed for {sid}: {exc}",
        ) from exc

    parsed = _parse_response(raw_resp)
    artifact = _build_artifact(
        source_id=sid,
        parsed_payload=parsed,
        raw_hash=raw_hash,
        transcript_hash=t_hash,
        prompt_hash=prompt_hash,
        model_id=model_id,
        trace_id=trace_id,
    )
    out_path = _write_artifact(artifact, lake_root=lake_root, source_id=sid)
    _maybe_record_baseline_in_state(
        source_id=sid, lake_root=lake_root, artifact_path=out_path
    )
    return BaselineOpusOutcome(
        source_id=sid,
        status="baseline_complete",
        reason_code=BASELINE_OPUS_WRITTEN,
        artifact_path=str(out_path),
        item_count=count_extracted_items(artifact["payload"]),
        estimated_cost_usd=str(opus_cost),
        message=None,
    )


def run_baseline_opus(
    *,
    lake_root: Path | str,
    manifest_path: Path | str | None = None,
    source_ids: Optional[Iterable[str]] = None,
    all_sources: bool = False,
    confirm_cost: bool = False,
    confirm_input: Optional[Callable[[str], str]] = None,
    confirm_output: Optional[Callable[[str], None]] = None,
    client_factory: Optional[Callable[[str], Callable[..., str]]] = None,
    trace_id: Optional[str] = None,
) -> BaselineOpusRunSummary:
    """Drive the baseline-opus run.

    Selection: exactly one of ``source_ids`` (one or more) or
    ``all_sources=True``. The CLI layer normalises the mutex; the
    entry point accepts both for testability.

    Cost confirmation: when ``all_sources=True``, ``confirm_cost``
    MUST be True (the CLI argument layer enforces this from the
    explicit ``--confirm-cost`` flag — env vars are rejected). The
    confirmation prompt is then printed via ``confirm_output`` and the
    operator's "y"/"n" answer is read via ``confirm_input``; tests
    inject deterministic callables, production uses ``print`` and
    ``input``.

    Returns a :class:`BaselineOpusRunSummary` describing every
    per-source outcome. A per-source halt is captured as a failed
    outcome — the loop continues for the remaining sources.
    """
    lake_root = Path(lake_root)
    manifest = load_manifest(manifest_path)

    # Selection.
    if all_sources:
        selected = list(manifest.payload["sources"])
    elif source_ids:
        selected = []
        for sid in source_ids:
            entry = find_source(manifest, sid)
            if entry is None:
                raise BaselineOpusError(
                    BASELINE_SOURCE_UNKNOWN,
                    f"source_id {sid!r} not in corpus manifest at "
                    f"{manifest.path}",
                )
            selected.append(entry)
    else:
        raise BaselineOpusError(
            BASELINE_SOURCE_UNKNOWN,
            "no source selected: pass source_ids or all_sources=True",
        )

    # Load prompt + resolve model BEFORE the confirmation prompt; a
    # bad prompt or registry MUST halt before the operator answers
    # y/N (otherwise the operator's confirmation is wasted).
    prompt_text = load_opus_prompt()
    p_hash = prompt_content_hash(prompt_text)
    model_id = resolve_opus_model()

    # Cost confirmation for --all mode.
    if all_sources:
        if not confirm_cost:
            raise BaselineOpusError(
                CONFIRM_COST_REQUIRED,
                "--all requires --confirm-cost (CLI-only; env var bypass "
                "is rejected)",
            )
        # Collect per-source byte counts BEFORE prompting so the user
        # sees a real estimate. A missing transcript at this stage is
        # surfaced via _read_transcript when the per-source loop
        # actually runs.
        per_source: List[tuple[str, int]] = []
        for entry in selected:
            sid = entry["source_id"]
            try:
                record = _load_source_record(lake_root, sid)
                raw_path = record["payload"]["raw_path"]
                p = (lake_root / raw_path).resolve()
                byte_len = p.stat().st_size if p.is_file() else 0
            except BaselineOpusError:
                byte_len = 0
            per_source.append((sid, byte_len))
        summary = format_all_mode_confirmation(
            per_source=per_source, opus_model=model_id
        )
        printer = confirm_output if confirm_output is not None else print
        printer(summary)
        reader = confirm_input if confirm_input is not None else input
        answer = reader("Confirm? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise BaselineOpusError(
                CONFIRM_COST_DECLINED,
                "operator declined the --all cost confirmation",
            )

    # Build the client. A test stub via BASELINE_OPUS_STUB_RESPONSE wins
    # before any SDK import — the factory below honours that.
    factory = client_factory if client_factory is not None else _build_client
    client = factory(model_id)
    tid = trace_id or _now_compact_iso()

    outcomes: List[BaselineOpusOutcome] = []
    observed_updates: Dict[str, Dict[str, Any]] = {}
    for entry in selected:
        sid = entry["source_id"]
        try:
            outcome = _run_one_source(
                entry=entry,
                lake_root=lake_root,
                prompt_text=prompt_text,
                prompt_hash=p_hash,
                model_id=model_id,
                client=client,
                trace_id=tid,
            )
        except BaselineOpusError as exc:
            outcomes.append(
                BaselineOpusOutcome(
                    source_id=sid,
                    status="failed",
                    reason_code=exc.reason_code,
                    artifact_path=None,
                    item_count=None,
                    estimated_cost_usd=None,
                    message=str(exc),
                )
            )
            continue

        outcomes.append(outcome)
        # Update the manifest entry only when the run succeeded. The
        # writer preserves declared and merges observed atomically.
        prior_observed = entry.get("observed", {}) or {}
        observed_updates[sid] = {
            "detected_speaker_count": prior_observed.get(
                "detected_speaker_count"
            ),
            "detected_word_count": prior_observed.get("detected_word_count"),
            "ingestion_status": "baseline_complete",
            "last_updated": _now_iso(),
        }

    if observed_updates:
        rewrite_manifest_with_observed(
            path=manifest.path,
            observed_updates=observed_updates,
        )

    refreshed = load_manifest(manifest.path)
    return BaselineOpusRunSummary(
        manifest_hash=refreshed.manifest_hash,
        model_id=model_id,
        prompt_content_hash=p_hash,
        outcomes=outcomes,
    )


def format_summary_table(summary: BaselineOpusRunSummary) -> str:
    """Human-readable summary table for stdout."""
    lines = [
        f"manifest_hash: {summary.manifest_hash}",
        f"model_id: {summary.model_id}",
        f"prompt_content_hash: {summary.prompt_content_hash}",
        "",
        "source_id".ljust(50)
        + " | "
        + "status".ljust(18)
        + " | "
        + "items".rjust(5)
        + " | "
        + "reason_code",
        "-" * 110,
    ]
    for row in summary.outcomes:
        lines.append(
            row.source_id.ljust(50)
            + " | "
            + row.status.ljust(18)
            + " | "
            + (str(row.item_count) if row.item_count is not None else "-").rjust(5)
            + " | "
            + (row.reason_code or "")
        )
    return "\n".join(lines) + "\n"


def estimate_dry_run(
    *,
    lake_root: Path | str,
    source_id: str,
    manifest_path: Path | str | None = None,
) -> Dict[str, Any]:
    """Cost estimate for one source. Does NOT call the model.

    The CLI ``--dry-run`` flag reaches this entry point so an operator
    can preview the cost and the resolved model + prompt hash without
    spending API credits. A missing prompt / source_record / transcript
    still halts here (the prompt resolution and the byte length are
    real reads), so the dry-run shares the failure surface of a real
    run.
    """
    lake_root = Path(lake_root)
    manifest = load_manifest(manifest_path)
    entry = find_source(manifest, source_id)
    if entry is None:
        raise BaselineOpusError(
            BASELINE_SOURCE_UNKNOWN,
            f"source_id {source_id!r} not in corpus manifest at "
            f"{manifest.path}",
        )
    prompt_text = load_opus_prompt()
    p_hash = prompt_content_hash(prompt_text)
    model_id = resolve_opus_model()
    record = _load_source_record(lake_root, source_id)
    raw_path = record["payload"]["raw_path"]
    transcript = _read_transcript(lake_root, raw_path)
    byte_len = _byte_length(transcript)
    opus_cost, haiku_cost = _estimate_cost_pair(
        byte_length=byte_len, opus_model=model_id
    )
    return {
        "source_id": source_id,
        "manifest_hash": manifest.manifest_hash,
        "model_id": model_id,
        "prompt_content_hash": p_hash,
        "transcript_byte_length": byte_len,
        "estimated_opus_cost_usd": str(opus_cost),
        "estimated_haiku_cost_usd": str(haiku_cost),
        "raw_path": raw_path,
    }


__all__ = [
    "ARTIFACT_TYPE",
    "ARTIFACT_SCHEMA_VERSION",
    "BASELINE_OPUS_WRITTEN",
    "BASELINE_SOURCE_UNKNOWN",
    "BaselineOpusError",
    "BaselineOpusOutcome",
    "BaselineOpusRunSummary",
    "CONFIRM_COST_DECLINED",
    "CONFIRM_COST_REQUIRED",
    "LLM_TRANSPORT_ERROR",
    "MALFORMED_LLM_RESPONSE",
    "MODEL_REGISTRY_ERROR",
    "OPUS_PROMPT_NOT_FOUND",
    "PRODUCED_BY",
    "SOURCE_RECORD_INVALID",
    "SOURCE_RECORD_NOT_FOUND",
    "STUB_ENV",
    "TRANSCRIPT_NOT_FOUND",
    "TRANSCRIPT_UNREADABLE",
    "count_extracted_items",
    "estimate_dry_run",
    "format_all_mode_confirmation",
    "format_summary_table",
    "load_opus_prompt",
    "prompt_content_hash",
    "resolve_opus_model",
    "run_baseline_opus",
]
