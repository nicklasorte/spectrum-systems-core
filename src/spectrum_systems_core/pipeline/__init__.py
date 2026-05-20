"""Phase 2 — eval-path alignment.

This package provides the SINGLE execution path for any code that
produces a Haiku extraction artifact and a paired comparison against
the Opus baseline. It exists so the correction miner cannot evaluate a
candidate prompt with one code path while production runs it with
another (the 4.1-point F1 gap between miner-measured and live runs
that motivated Phase 2 was that exact drift).

The public entry point is :func:`governed_run.governed_pipeline_run`.
A call-graph CI gate
(``tests/pipeline/test_call_graph_single_path.py``) asserts every
extraction-producing function either IS ``governed_pipeline_run`` or
calls it; ``git grep`` is not sufficient — the test walks the AST.
"""
from .governed_run import (
    ALLOWED_CALLERS,
    CALLER_BATCH_WORKFLOW,
    CALLER_CORRECTION_MINER,
    CALLER_PRODUCTION_CLI,
    ExtractionConfig,
    GovernedPipelineRunResult,
    PIPELINE_INVOCATION_LOG_ARTIFACT_TYPE,
    PIPELINE_INVOCATION_LOG_SCHEMA_VERSION,
    PIPELINE_INVOCATION_LOG_TTL_DAYS,
    PipelineRunError,
    build_extraction_config_from_run,
    extraction_config_hash,
    governed_pipeline_run,
    prompt_content_hash,
    transcript_hash,
    write_pipeline_invocation_log,
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
