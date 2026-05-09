"""Generic source ingestion layer (Phase A).

raw source file -> source_record (JSON) -> text_units.jsonl -> eval/control
-> promotion -> Obsidian projection (view only).

All modules here are deterministic and fail-closed. No LLM calls.
"""
from .source_loader import SourceLoader
from .grounding import GroundingHelper
from .source_eval import SourceEval
from .obsidian_projection import ObsidianProjection
from .promoter import Promoter
from .pdf_guard import PDFAdmissionGuard
from .pdf_extractor import PDFExtractor

__all__ = [
    "SourceLoader",
    "GroundingHelper",
    "SourceEval",
    "ObsidianProjection",
    "Promoter",
    "PDFAdmissionGuard",
    "PDFExtractor",
]
