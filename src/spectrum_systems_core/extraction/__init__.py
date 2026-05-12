"""Phase C: story and knowledge extraction.

chunk → extract → eval (grounding) → filter → human review → promote.
All artifacts are candidate status until a human promotes them.
No Markdown read-back. No auto-promotion. Structured retrieval only.
"""
from .chunker import (
    CHUNK_MERGE_ENABLED_ENV,
    Chunker,
    MIN_CHUNK_CHARS,
    MIN_CHUNK_CHARS_ENV,
    merge_short_chunks,
)
from .story_extractor import StoryExtractor
from .story_eval import StoryEval
from .storyworthy_filter import StoryworthyFilter
from .story_review_gateway import StoryReviewGateway
from .knowledge_synthesizer import KnowledgeSynthesizer
from .connection_engine import ConnectionEngine

__all__ = [
    "CHUNK_MERGE_ENABLED_ENV",
    "Chunker",
    "MIN_CHUNK_CHARS",
    "MIN_CHUNK_CHARS_ENV",
    "merge_short_chunks",
    "StoryExtractor",
    "StoryEval",
    "StoryworthyFilter",
    "StoryReviewGateway",
    "KnowledgeSynthesizer",
    "ConnectionEngine",
]
