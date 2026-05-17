"""Generic source ingestion layer (Phase A).

raw source file -> source_record (JSON) -> text_units.jsonl -> eval/control
-> promotion -> Obsidian projection (view only).

All modules here are deterministic and fail-closed. No LLM calls.
"""
from .docx_extractor import DocxExtractor
from .ground_truth_linker import GroundTruthLinker
from .grounding import GroundingHelper
from .ingestion_eval import IngestionEval
from .minutes_deduplicator import deduplicate_minutes
from .minutes_processor import MinutesProcessor
from .obsidian_projection import ObsidianProjection
from .pdf_extractor import PDFExtractor
from .pdf_guard import PDFAdmissionGuard
from .promoter import Promoter
from .source_eval import SourceEval
from .source_loader import SourceLoader

__all__ = [
    "SourceLoader",
    "GroundingHelper",
    "SourceEval",
    "ObsidianProjection",
    "Promoter",
    "PDFAdmissionGuard",
    "PDFExtractor",
    "DocxExtractor",
    "IngestionEval",
    "MinutesProcessor",
    "deduplicate_minutes",
    "GroundTruthLinker",
]
