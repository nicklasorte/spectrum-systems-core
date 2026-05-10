"""Phase L.1: PipelineOrchestrator.

Scans a flat directory of transcript files (.docx and .txt) and runs the
existing Phase A pipeline only on transcripts that have no on-disk
"processed" evidence. Writes one orchestration_run_record artifact per
invocation. Never raises.
"""
from .pipeline_orchestrator import PipelineOrchestrator

__all__ = ["PipelineOrchestrator"]
