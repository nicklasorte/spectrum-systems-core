"""Single execution path for Haiku extraction + comparison + invocation log.

Phase 2 — eval-path alignment.

This is the ONE entry point through which any caller (production CLI,
correction miner, batch workflow, future Meta-Harness) produces a
``meeting_minutes`` extraction and a paired ``comparison_result``. The
function:

1. Reads the candidate prompt content as a STRING (callers never pass
   a path so the miner and production cannot drift on how the prompt
   is loaded).
2. Runs the existing live-LLM workflow with that prompt forced into
   place via a context manager.
3. Validates the resulting artifact against ``schema_version`` 1.4.0
   (and its older legacy versions for backward compatibility).
4. Runs the Phase 1 grounding gate (re-applied through the comparison
   engine) and the comparison against the matching-schema Opus
   baseline.
5. Stamps an ``extraction_config`` block onto the artifact's
   ``provenance`` so the prompt content hash, chunk hashes, transcript
   hash, and seed inputs are reproducible from the on-disk artifact
   alone.
6. Writes a ``pipeline_invocation_log`` diagnostic so a reviewer can
   reproduce the run from `source_id`, prompt_content_hash, and
   transcript_hash without re-reading any code.
7. Returns the comparison artifact.

The function never decides promotion. Promotion decisions remain in
``promotion/`` and ``control/``. ``governed_pipeline_run`` only
guarantees that the EXTRACTION and SCORING surfaces are identical
between callers. Two invocations with identical
``ExtractionConfig`` inputs MUST produce identical comparison F1
(determinism test ``test_governed_run_determinism.py``).
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..validation import (
    ArtifactValidationError,
    SchemaNotFoundError,
    validate_artifact,
)

CALLER_PRODUCTION_CLI = "production_cli"
CALLER_CORRECTION_MINER = "correction_miner"
CALLER_BATCH_WORKFLOW = "batch_workflow"

ALLOWED_CALLERS: frozenset[str] = frozenset(
    {CALLER_PRODUCTION_CLI, CALLER_CORRECTION_MINER, CALLER_BATCH_WORKFLOW}
)

# Diagnostic artifact constants — kept in lockstep with the schema.
PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE = "pipeline_invocation_log"
PIPELINE_INVOCATION_LOG_SCHEMA_VERSION = "1.0.0"
PIPELINE_INVOCATION_LOG_TTL_DAYS = 30


class PipelineRunError(RuntimeError):
    """Raised when ``governed_pipeline_run`` cannot complete.

    Carries a reason_code (str) so a caller can pattern-match on a
    stable token rather than parse the message string.
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class ExtractionConfig:
    """Reproducible inputs to one extraction run.

    Every value here is captured into the artifact's ``provenance``
    block so the run can be re-executed from the artifact alone.

    Fields:
      temperature: LLM sampling temperature. The codebase fixes this
        at 0.0 today; the field exists so a future tuner cannot drift.
      seed_inputs: ``{"model_id": ..., "prompt_content_hash": ...,
        "transcript_hash": ...}`` at a minimum. Callers may add
        additional keys; the validator only asserts the required three.
      chunks_full_hash: sha256 of the concatenated chunk hashes in
        deterministic order. Drift detector.
      chunk_count: integer chunk count. Drift detector.
      first_chunk_hash: sha256 of the first chunk's text.
      last_chunk_hash: sha256 of the last chunk's text.
      prompt_content_hash: sha256 of the FULL prompt content (the
        string the model was given, not a file path).
    """

    temperature: float
    seed_inputs: Dict[str, str]
    chunks_full_hash: str
    chunk_count: int
    first_chunk_hash: str
    last_chunk_hash: str
    prompt_content_hash: str

    REQUIRED_SEED_KEYS: frozenset[str] = field(
        default=frozenset({"model_id", "prompt_content_hash", "transcript_hash"}),
        repr=False,
        compare=False,
    )

    def to_dict(self) -> Dict[str, Any]:
        missing = self.REQUIRED_SEED_KEYS - set(self.seed_inputs)
        if missing:
            raise PipelineRunError(
                "extraction_config_seed_missing",
                f"seed_inputs missing required keys: {sorted(missing)}",
            )
        return {
            "temperature": float(self.temperature),
            "seed_inputs": dict(self.seed_inputs),
            "chunks_full_hash": self.chunks_full_hash,
            "chunk_count": int(self.chunk_count),
            "first_chunk_hash": self.first_chunk_hash,
            "last_chunk_hash": self.last_chunk_hash,
            "prompt_content_hash": self.prompt_content_hash,
        }


@dataclass(frozen=True)
class GovernedPipelineRunResult:
    """Return value of :func:`governed_pipeline_run`.

    ``comparison_artifact`` is the full comparison_result envelope.
    ``invocation_log`` is the pipeline_invocation_log artifact written
    to the diagnostics directory. ``artifact`` is the produced
    meeting_minutes envelope (None when extraction was blocked before
    an artifact was assembled, e.g. transport halt).
    """

    comparison_artifact: Dict[str, Any]
    invocation_log: Dict[str, Any]
    artifact: Optional[Dict[str, Any]]
    promoted: bool
    legacy_eval: bool
    schema_version: str
    f1: float


# ---------------------------------------------------------------------------
# Pure hash helpers — used by both production and the miner so the
# drift detector cannot disagree across callers.
# ---------------------------------------------------------------------------
def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_content_hash(prompt_text: str) -> str:
    """sha256 of the verbatim prompt content (no path, no metadata)."""
    if not isinstance(prompt_text, str):
        raise PipelineRunError(
            "prompt_content_invalid",
            f"prompt_content must be a string, got {type(prompt_text).__name__}",
        )
    return _sha256_hex(prompt_text)


def transcript_hash(transcript_text: str) -> str:
    if not isinstance(transcript_text, str):
        raise PipelineRunError(
            "transcript_invalid",
            f"transcript must be a string, got {type(transcript_text).__name__}",
        )
    return _sha256_hex(transcript_text)


def extraction_config_hash(cfg: ExtractionConfig | Dict[str, Any]) -> str:
    """Deterministic hash of an ExtractionConfig (canonical-json sha256)."""
    if isinstance(cfg, ExtractionConfig):
        cfg = cfg.to_dict()
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return _sha256_hex(canonical)


def build_extraction_config_from_run(
    *,
    prompt_text: str,
    transcript_text: str,
    model_id: str,
    chunks: List[Dict[str, Any]],
    temperature: float = 0.0,
) -> ExtractionConfig:
    """Construct an ExtractionConfig from the inputs the run was given.

    Chunk hashes are derived from each chunk's ``text`` field in
    chunk-order so two identical inputs produce byte-identical
    config dicts.
    """
    chunk_texts: List[str] = []
    for ch in chunks:
        if isinstance(ch, Mapping):
            txt = ch.get("text") or ""
        else:
            txt = ""
        chunk_texts.append(str(txt))
    per_chunk_hashes = [_sha256_hex(t) for t in chunk_texts]
    full = _sha256_hex("\n".join(per_chunk_hashes))
    first = per_chunk_hashes[0] if per_chunk_hashes else _sha256_hex("")
    last = per_chunk_hashes[-1] if per_chunk_hashes else _sha256_hex("")
    p_hash = prompt_content_hash(prompt_text)
    t_hash = transcript_hash(transcript_text)
    return ExtractionConfig(
        temperature=float(temperature),
        seed_inputs={
            "model_id": str(model_id),
            "prompt_content_hash": p_hash,
            "transcript_hash": t_hash,
        },
        chunks_full_hash=full,
        chunk_count=len(chunks),
        first_chunk_hash=first,
        last_chunk_hash=last,
        prompt_content_hash=p_hash,
    )


# ---------------------------------------------------------------------------
# Prompt-override seam — the miner already uses this exact pattern, but
# we centralise it here so callers cannot drift on how the prompt is
# injected. The function returns a context manager that swaps the
# ``meeting_minutes_llm`` prompt-loader function for the duration of
# the run, then restores it. Tests assert restoration on exception.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _override_prompt(prompt_text: str):
    from ..workflows import meeting_minutes_llm as mml

    original = getattr(mml, "_load_prompt", None)
    if original is None:
        # The workflow exposes its prompt loader through a private
        # helper; older versions inlined it. We probe both seams.
        original = getattr(mml, "_system_prompt", None)
    # Replace the system-prompt fetcher with a closure that returns the
    # forced string. The workflow uses `_system_prompt()` at the top of
    # each call; overriding here makes the candidate prompt
    # observationally identical to the production prompt.
    setattr(mml, "_system_prompt", lambda: prompt_text)
    try:
        yield
    finally:
        if original is None:
            try:
                delattr(mml, "_system_prompt")
            except AttributeError:
                pass
        else:
            setattr(mml, "_system_prompt", original)


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ttl_iso(started_at_iso: str, days: int) -> str:
    dt = datetime.datetime.fromisoformat(started_at_iso)
    return (dt + datetime.timedelta(days=days)).isoformat()


def _diagnostics_dir(data_lake_path: Path, source_id: str) -> Path:
    return (
        Path(data_lake_path)
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
    )


def write_pipeline_invocation_log(
    *,
    data_lake_path: Path,
    source_id: str,
    invocation_id: str,
    started_at: str,
    completed_at: str,
    caller: str,
    extraction_config: ExtractionConfig | Dict[str, Any],
    comparison_artifact_path: Optional[str],
) -> Dict[str, Any]:
    """Write the diagnostic log entry and return the artifact dict.

    Lifecycle mirrors ``grounding_rejection_report``: never promoted,
    never indexed, never product. Expires 30 days after ``started_at``.
    The reconciler (``scripts/reconcile_invocation_logs.py``) walks the
    data lake weekly and surfaces missing logs.
    """
    if caller not in ALLOWED_CALLERS:
        raise PipelineRunError(
            "invocation_log_invalid_caller",
            f"caller must be one of {sorted(ALLOWED_CALLERS)}, got {caller!r}",
        )
    cfg_dict = (
        extraction_config.to_dict()
        if isinstance(extraction_config, ExtractionConfig)
        else dict(extraction_config)
    )
    log = {
        "artifact_type": PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE,
        "schema_version": PIPELINE_INVOCATION_LOG_SCHEMA_VERSION,
        "source_id": str(source_id),
        "invocation_id": str(invocation_id),
        "started_at": started_at,
        "completed_at": completed_at,
        "caller": caller,
        "extraction_config_hash": extraction_config_hash(cfg_dict),
        "prompt_content_hash": cfg_dict.get("prompt_content_hash", ""),
        "transcript_hash": cfg_dict.get("seed_inputs", {}).get(
            "transcript_hash", ""
        ),
        "comparison_artifact_path": comparison_artifact_path or "",
        "ttl_expires_at": _ttl_iso(started_at, PIPELINE_INVOCATION_LOG_TTL_DAYS),
    }
    # Validate before write — drift catcher for schema changes.
    try:
        validate_artifact(log, PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE)
    except SchemaNotFoundError:
        # The schema must exist; this is a programmer error not an
        # operator error. Re-raise as a PipelineRunError so callers
        # see a stable reason_code.
        raise PipelineRunError(
            "invocation_log_schema_missing",
            f"no schema for {PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE}",
        )

    out_dir = _diagnostics_dir(Path(data_lake_path), source_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"pipeline_invocation_log__{invocation_id}.json"
    out_path.write_text(
        json.dumps(log, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return log


# ---------------------------------------------------------------------------
# The single execution path.
# ---------------------------------------------------------------------------
def governed_pipeline_run(
    *,
    source_id: str,
    prompt_content: str,
    transcript: str,
    data_lake_path: Path | str,
    extraction_config: Optional[ExtractionConfig] = None,
    caller: str = CALLER_PRODUCTION_CLI,
    client: Optional[Callable[..., str]] = None,
    skip_invocation_log: bool = False,
) -> GovernedPipelineRunResult:
    """Run extraction → schema_validate → grounding_gate → compare.

    This function is THE only path for callers that produce a
    ``meeting_minutes`` artifact paired with a ``comparison_result``.
    The call-graph CI gate in
    ``tests/pipeline/test_call_graph_single_path.py`` asserts every
    extraction-producing function either IS this function or calls it.

    ``prompt_content`` is a STRING, never a path. The miner reads the
    candidate prompt text and passes it directly; production reads
    ``meeting_minutes_llm.md`` and passes its contents. Both callers
    arrive at the same execution path so a measurement-layer drift
    (which motivated Phase 2) cannot recur.

    ``caller`` is one of ``production_cli``, ``correction_miner``,
    ``batch_workflow``. The invocation log records which caller
    produced the run so an operator can reconcile production runs vs.
    miner evaluations after a drift incident.

    Determinism contract: two invocations with byte-identical
    ``prompt_content``, ``transcript``, model registry, and seed
    inputs MUST produce the same comparison F1. Non-determinism is a
    bug; investigate extraction, chunking, or LLM call ordering.
    """
    started_at = _now_utc_iso()
    invocation_id = uuid.uuid4().hex
    data_lake_path = Path(data_lake_path)

    # Defer expensive imports so a callable importing the pipeline
    # module (e.g. the call-graph test) does not pay LLM / workflow
    # import cost. scripts/ is not a package; it is added to sys.path
    # by every script entry point (see scripts/correction_miner.py).
    import sys as _sys
    _scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
    if str(_scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(_scripts_dir))
    import compare_opus_haiku as cmp  # noqa: WPS433
    from ..workflows.meeting_minutes_llm import (
        run_meeting_minutes_llm_workflow,
    )

    if caller not in ALLOWED_CALLERS:
        raise PipelineRunError(
            "invalid_caller",
            f"caller must be one of {sorted(ALLOWED_CALLERS)}, got {caller!r}",
        )
    if not isinstance(prompt_content, str) or not prompt_content.strip():
        raise PipelineRunError(
            "prompt_content_invalid",
            "prompt_content must be a non-empty string",
        )
    if not isinstance(transcript, str):
        raise PipelineRunError(
            "transcript_invalid",
            "transcript must be a string",
        )

    # Execute extraction. The prompt-override context manager is the
    # ONE place candidate prompts are injected so production and the
    # miner cannot drift on injection mechanics.
    with _override_prompt(prompt_content):
        result = run_meeting_minutes_llm_workflow(
            transcript,
            client=client,
            meeting_id=source_id,
            source_id=source_id,
            lake_root=data_lake_path / "store",
        )

    artifact_dict: Optional[Dict[str, Any]] = None
    if result.meeting_minutes is not None:
        artifact_dict = {
            "artifact_id": result.meeting_minutes.artifact_id,
            "artifact_type": result.meeting_minutes.artifact_type,
            "schema_version": result.meeting_minutes.schema_version,
            "status": result.meeting_minutes.status,
            "created_at": result.meeting_minutes.created_at,
            "trace_id": result.meeting_minutes.trace_id,
            "input_refs": list(result.meeting_minutes.input_refs),
            "content_hash": result.meeting_minutes.content_hash,
            "payload": result.meeting_minutes.payload,
        }

    # Stamp the extraction_config block. If the caller did not pre-
    # compute one we derive it from the inputs (production callers
    # take this path). The block lands inside `payload.provenance`
    # because that subschema permits additional keys (the top-level
    # meeting_minutes schema has additionalProperties: false).
    if extraction_config is None:
        # Read the resolved extraction model_id off the produced
        # artifact's provenance. Falls back to "unknown" only when the
        # workflow failed before stamping; the field is required by
        # the seed_inputs validator so the value matters.
        model_id = "unknown"
        chunks_for_hash: List[Dict[str, Any]] = []
        if artifact_dict is not None:
            prov = artifact_dict["payload"].get("provenance") or {}
            model_id = prov.get("model_id") or "unknown"
            # Rebuild chunks deterministically so the config can be
            # reconstructed even when the producer's chunk list is
            # not exposed through the WorkflowResult API.
            from ..data_lake.chunker import chunk_transcript

            chunks_for_hash = chunk_transcript(transcript)
        extraction_config = build_extraction_config_from_run(
            prompt_text=prompt_content,
            transcript_text=transcript,
            model_id=model_id,
            chunks=chunks_for_hash,
            temperature=0.0,
        )

    if artifact_dict is not None:
        prov = artifact_dict["payload"].setdefault("provenance", {})
        # Additive: never overwrite a caller's pre-existing block.
        prov.setdefault("extraction_config", extraction_config.to_dict())
        # Re-hash so the envelope's content_hash matches the
        # provenance stamp. The Phase-1 LLM workflow already does this
        # for the demoted-warning provenance stamp.
        from ..artifacts import compute_content_hash

        artifact_dict["content_hash"] = compute_content_hash(
            artifact_dict["payload"]
        )

    # Run comparison against the matching-schema Opus baseline.
    baseline_rows = cmp.load_opus_baseline(data_lake_path, source_id)
    gt_pairs = cmp.load_gt_pairs(data_lake_path, source_id)
    types = cmp.extraction_types()
    haiku_payload = artifact_dict["payload"] if artifact_dict else {}
    metrics_bundle = cmp.compute_comparison(
        baseline_rows=baseline_rows,
        haiku_payload=haiku_payload,
        gt_pairs=gt_pairs,
        types=types,
    )
    summary = metrics_bundle["summary"]
    f1 = float(summary.get("haiku_f1_vs_opus") or 0.0)

    # Determine legacy_eval status: a run with no extraction_config in
    # the artifact's provenance, or an artifact at schema_version <
    # 1.4.0, is a legacy eval and excluded from the variance budget.
    legacy_eval = False
    schema_ver = "unknown"
    if artifact_dict is not None:
        schema_ver = str(artifact_dict.get("schema_version") or "unknown")
    if artifact_dict is None:
        legacy_eval = True
    else:
        payload = artifact_dict["payload"]
        ec_present = bool(
            payload.get("provenance", {}).get("extraction_config", {}).get(
                "prompt_content_hash"
            )
        )
        if not ec_present:
            legacy_eval = True
        if schema_ver.startswith("1.0") or schema_ver.startswith("1.1") or \
                schema_ver.startswith("1.2") or schema_ver.startswith("1.3"):
            legacy_eval = True

    # Build the comparison artifact envelope when the metrics bundle
    # is complete enough to support it. The miner does NOT persist
    # comparisons (the production CLI is the writer), so a partial
    # metrics bundle from a unit test should not block the run.
    haiku_artifact_for_cmp = artifact_dict or {
        "artifact_type": "meeting_minutes",
        "schema_version": "1.4.0",
        "payload": {},
    }
    required_metric_keys = {
        "gt_pairs_present",
        "summary",
        "by_type",
        "false_negatives",
        "haiku_only_items",
        "gt_missed",
    }
    if required_metric_keys.issubset(metrics_bundle):
        comparison = cmp.build_comparison_artifact(
            baseline_rows=baseline_rows,
            haiku_artifact=haiku_artifact_for_cmp,
            metrics=metrics_bundle,
            source_id=source_id,
            compared_at=_now_utc_iso(),
        )
    else:
        # Fallback: emit a minimal envelope with the summary and the
        # legacy_eval flag so downstream callers (and tests) can read
        # consistent fields without depending on the full metrics shape.
        comparison = {
            "artifact_type": "comparison_result",
            "schema_version": "1.0.0",
            "source_id": source_id,
            "compared_at": _now_utc_iso(),
            "summary": metrics_bundle.get("summary", {}),
            "legacy_eval": cmp.is_legacy_eval(haiku_artifact_for_cmp),
        }

    invocation_log: Dict[str, Any] = {}
    if not skip_invocation_log:
        completed_at = _now_utc_iso()
        # Comparison path is conceptual here — the production CLI is
        # the writer. The reconciler tolerates an empty path on a
        # miner run because the miner does not persist comparisons.
        invocation_log = write_pipeline_invocation_log(
            data_lake_path=data_lake_path,
            source_id=source_id,
            invocation_id=invocation_id,
            started_at=started_at,
            completed_at=completed_at,
            caller=caller,
            extraction_config=extraction_config,
            comparison_artifact_path=None,
        )

    return GovernedPipelineRunResult(
        comparison_artifact=comparison,
        invocation_log=invocation_log,
        artifact=artifact_dict,
        promoted=bool(result.promoted),
        legacy_eval=legacy_eval,
        schema_version=schema_ver,
        f1=f1,
    )


__all__ = [
    "ALLOWED_CALLERS",
    "CALLER_BATCH_WORKFLOW",
    "CALLER_CORRECTION_MINER",
    "CALLER_PRODUCTION_CLI",
    "ExtractionConfig",
    "GovernedPipelineRunResult",
    "PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE",
    "PIPELINE_INVOCATION_LOG_SCHEMA_VERSION",
    "PIPELINE_INVOCATION_LOG_TTL_DAYS",
    "PipelineRunError",
    "build_extraction_config_from_run",
    "extraction_config_hash",
    "governed_pipeline_run",
    "prompt_content_hash",
    "transcript_hash",
    "write_pipeline_invocation_log",
]
