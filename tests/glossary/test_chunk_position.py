"""Phase V.4 tests: chunk position + attention-direction block."""
from __future__ import annotations

import pytest

from spectrum_systems_core.glossary.chunk_position import (
    ATTENTION_DIRECTION_BLOCK,
    POSITION_CLOSING,
    POSITION_MIDDLE,
    POSITION_OPENING,
    assign_chunk_positions,
    attention_block_for_position,
    compute_chunk_position,
)


def test_three_chunks_positions() -> None:
    out = assign_chunk_positions([{}, {}, {}])
    positions = [c["chunk_position"] for c in out]
    assert positions == [POSITION_OPENING, POSITION_MIDDLE, POSITION_CLOSING]


def test_ten_chunks_distribution() -> None:
    out = assign_chunk_positions([{"i": i} for i in range(10)])
    positions = [c["chunk_position"] for c in out]
    # ratio < 0.33 -> opening: i/10 in {0, 0.1, 0.2, 0.3} = i in 0..3
    # 0.33 <= ratio <= 0.67 -> middle: i in 4..6
    # ratio > 0.67 -> closing: i in 7..9
    assert positions[:4] == [POSITION_OPENING] * 4
    assert positions[4:7] == [POSITION_MIDDLE] * 3
    assert positions[7:] == [POSITION_CLOSING] * 3


def test_twenty_six_chunks_middle_is_proportional() -> None:
    out = assign_chunk_positions([{} for _ in range(26)])
    positions = [c["chunk_position"] for c in out]
    # Middle ratios: 0.33 <= i/26 <= 0.67  -> i in roughly 9..17
    middles = [i for i, p in enumerate(positions) if p == POSITION_MIDDLE]
    assert min(middles) >= 8 and min(middles) <= 9
    assert max(middles) >= 16 and max(middles) <= 17


def test_single_chunk_is_opening() -> None:
    out = assign_chunk_positions([{"only": True}])
    assert out[0]["chunk_position"] == POSITION_OPENING


def test_empty_input_returns_empty_list() -> None:
    assert assign_chunk_positions([]) == []


def test_does_not_mutate_input() -> None:
    inp = [{"i": 0}, {"i": 1}, {"i": 2}]
    out = assign_chunk_positions(inp)
    assert "chunk_position" not in inp[0]
    assert out[0]["chunk_position"] == POSITION_OPENING
    # And the dicts are distinct objects.
    assert out[0] is not inp[0]


def test_attention_block_for_middle_position() -> None:
    block = attention_block_for_position(POSITION_MIDDLE)
    assert "ATTENTION DIRECTION" in block
    assert block == ATTENTION_DIRECTION_BLOCK


def test_attention_block_for_opening_is_empty() -> None:
    assert attention_block_for_position(POSITION_OPENING) == ""


def test_attention_block_for_closing_is_empty() -> None:
    assert attention_block_for_position(POSITION_CLOSING) == ""


def test_attention_block_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("POSITION_AWARE_PROMPTING_ENABLED", "false")
    assert attention_block_for_position(POSITION_MIDDLE) == ""


def test_recomputed_each_run_not_cached() -> None:
    """Same chunk dict, different total -> different position. The
    position is computed from the CURRENT list length on every call."""
    base = {"i": 1}
    out_3 = assign_chunk_positions([base, base, base])
    out_26 = assign_chunk_positions([base] * 26)
    # Index 1 in a 3-element list: ratio 0.33 -> middle
    assert out_3[1]["chunk_position"] == POSITION_MIDDLE
    # Index 1 in a 26-element list: ratio 0.038 -> opening
    assert out_26[1]["chunk_position"] == POSITION_OPENING


def test_compute_chunk_position_clamps_out_of_range() -> None:
    # Out-of-range index is clamped.
    assert compute_chunk_position(-5, 10) == POSITION_OPENING
    assert compute_chunk_position(99, 10) == POSITION_CLOSING


def test_zero_total_chunks_is_opening() -> None:
    assert compute_chunk_position(0, 0) == POSITION_OPENING
