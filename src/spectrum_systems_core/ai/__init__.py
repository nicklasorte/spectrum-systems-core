"""Phase H: governed AI over operational memory.

One loop: question -> retrieve from governed memory -> assemble context bundle
-> AI generation -> grounding eval -> advisory output.

All AI outputs are advisory only. No autonomous writes outside ai/.
The PromptRegistry is the only source of prompts. The AIAdapter is the only
call site for AI memory queries.
"""
from .adapter import AIAdapter
from .grounding_eval import (
    MAX_QUERY_COST_USD,
    MAX_QUERY_TOKENS,
    UUID_PATTERN,
    AIGroundingEval,
)
from .memory_context_builder import MemoryContextBuilder
from .prompt_registry import PromptRegistry

__all__ = [
    "AIAdapter",
    "AIGroundingEval",
    "MAX_QUERY_COST_USD",
    "MAX_QUERY_TOKENS",
    "MemoryContextBuilder",
    "PromptRegistry",
    "UUID_PATTERN",
]
