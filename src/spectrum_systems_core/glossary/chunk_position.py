"""Phase V.4: chunk position labelling + attention-direction block.

Computes ``chunk_position`` proportionally over the current chunk list
(not from absolute indices) so the "middle" of a 26-chunk transcript
is chunks ~9-17 and the "middle" of a 10-chunk transcript is ~3-6.

The attention block is injected ONLY for ``middle`` chunks. Opening
and closing receive no extra block, mirroring the research finding
that LLMs naturally attend to the first and last positions of a
context window.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Sequence


POSITION_OPENING: str = "opening"
POSITION_MIDDLE: str = "middle"
POSITION_CLOSING: str = "closing"

POSITION_LABELS: tuple[str, ...] = (
    POSITION_OPENING,
    POSITION_MIDDLE,
    POSITION_CLOSING,
)

_LOWER_BOUND: float = 0.33
_UPPER_BOUND: float = 0.67

ATTENTION_DIRECTION_BLOCK: str = (
    "ATTENTION DIRECTION\n"
    "===================\n"
    "This content is from the middle section of the meeting transcript.\n"
    "Apply the same extraction rigor as the opening and closing sections.\n"
    "Middle-section content often contains key technical decisions and\n"
    "working group assignments -- do not underweight it.\n"
)

_ATTENTION_ENV: str = "POSITION_AWARE_PROMPTING_ENABLED"


def _attention_enabled() -> bool:
    raw = os.environ.get(_ATTENTION_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def compute_chunk_position(index: int, total_chunks: int) -> str:
    """Return ``opening`` | ``middle`` | ``closing`` for a chunk.

    Edge cases:
    - ``total_chunks <= 1``: ``opening`` (single-chunk transcript).
    - ``index`` out of range: clamped.

    The cut-offs are proportional thirds of the chunk list:
    ``i < n/3`` -> opening; ``n/3 <= i < 2n/3`` -> middle; rest -> closing.
    Index-based rather than ratio-based so that ``n=3`` produces
    ``[opening, middle, closing]`` exactly.
    """
    if total_chunks <= 1:
        return POSITION_OPENING
    i = max(0, min(int(index), total_chunks - 1))
    # Ceiling boundaries: each third contains at least one chunk,
    # so 3 chunks split [opening, middle, closing] exactly.
    lower = max(1, math.ceil(total_chunks / 3.0))
    upper = max(lower + 1, math.ceil(total_chunks * 2.0 / 3.0))
    if i < lower:
        return POSITION_OPENING
    if i < upper:
        return POSITION_MIDDLE
    return POSITION_CLOSING


def assign_chunk_positions(
    chunks: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return a new list of chunks with ``chunk_position`` set.

    Does not mutate the input. The position is computed from the
    CURRENT list length on every call -- never from a previously
    stored value -- to guarantee that re-runs with a different total
    chunk count produce fresh positions (Attack 5 / Attack 11 fix).
    """
    out: List[Dict[str, Any]] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        new_chunk = dict(chunk) if isinstance(chunk, dict) else {}
        new_chunk["chunk_position"] = compute_chunk_position(idx, total)
        out.append(new_chunk)
    return out


def attention_block_for_position(position: str) -> str:
    """Return the prompt block for ``position`` or empty string.

    Returns the attention-direction block for ``middle`` chunks when
    the env flag is enabled (default). Returns empty string for
    opening / closing positions or when the feature is disabled.
    """
    if position != POSITION_MIDDLE:
        return ""
    if not _attention_enabled():
        return ""
    return ATTENTION_DIRECTION_BLOCK


__all__ = [
    "ATTENTION_DIRECTION_BLOCK",
    "POSITION_CLOSING",
    "POSITION_LABELS",
    "POSITION_MIDDLE",
    "POSITION_OPENING",
    "assign_chunk_positions",
    "attention_block_for_position",
    "compute_chunk_position",
]
