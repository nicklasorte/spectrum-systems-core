"""Phase X-0 + X-1 unit tests: backoff, empty-response guard, fence strip.

Covers the resilience primitives in
``spectrum_systems_core.extraction._resilience`` and their integration
with the orchestrator chunk counter / failure-artifact emission path.

These tests deliberately do NOT exercise the live Anthropic SDK -- the
``call_with_backoff`` helper accepts injected ``sleep`` and ``rand``
callbacks so we never burn wall-clock seconds in CI, and a stub
``anthropic.RateLimitError`` is used where needed.
"""
from __future__ import annotations

import json
import unittest

import anthropic

from spectrum_systems_core.extraction import _failure_artifacts as fa
from spectrum_systems_core.extraction._chunk_counters import (
    BLOCK_REASON_EMPTY_RESPONSE,
    BLOCK_REASON_OTHER,
    BLOCK_REASON_PARSE_ERROR,
    BLOCK_REASON_RATE_LIMIT,
    STAGE_FAILED,
    STAGE_OK,
    STAGE_PARTIAL,
    ChunkCounters,
)
from spectrum_systems_core.extraction._resilience import (
    MAX_CONCURRENT_HAIKU_CALLS,
    MAX_RETRIES,
    EmptyResponseError,
    call_with_backoff,
    guard_empty_response,
    strip_markdown_fence,
)

# ---------------------------------------------------------------------------
# X-0 part A: call_with_backoff
# ---------------------------------------------------------------------------


def _make_rate_limit_error() -> anthropic.RateLimitError:
    """Build a RateLimitError without going through the real SDK constructor.

    The SDK constructor wants a real httpx.Response (with .request etc.)
    which we do not have in a unit test. Building one via __new__ +
    __init__ on the base ``Exception`` skips that path while still
    producing an instance that ``isinstance(exc, RateLimitError)``
    returns True on -- which is all ``call_with_backoff`` needs.
    """
    exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
    Exception.__init__(exc, "rate_limit")
    return exc


class CallWithBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []

    def _sleep(self, secs: float) -> None:
        self.sleeps.append(secs)

    def _rand(self, lo: float, hi: float) -> float:  # noqa: ARG002
        return 0.0  # deterministic jitter for assertions

    def test_returns_value_on_first_success(self) -> None:
        calls = {"n": 0}

        def fn() -> int:
            calls["n"] += 1
            return 42

        out = call_with_backoff(fn, sleep=self._sleep, rand=self._rand)

        self.assertEqual(out, 42)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.sleeps, [])

    def test_retries_on_rate_limit_error_up_to_max(self) -> None:
        calls = {"n": 0}

        def fn() -> int:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _make_rate_limit_error()
            return 7

        out = call_with_backoff(
            fn, max_retries=5, sleep=self._sleep, rand=self._rand,
        )

        self.assertEqual(out, 7)
        self.assertEqual(calls["n"], 3)
        # Two retries: first wait = 2**0 = 1, second wait = 2**1 = 2.
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_reraises_after_max_retries_exhausted(self) -> None:
        calls = {"n": 0}

        def fn() -> int:
            calls["n"] += 1
            raise _make_rate_limit_error()

        with self.assertRaises(anthropic.RateLimitError):
            call_with_backoff(
                fn, max_retries=3, sleep=self._sleep, rand=self._rand,
            )
        self.assertEqual(calls["n"], 3)
        # 3 attempts total: two sleeps between attempts.
        self.assertEqual(len(self.sleeps), 2)

    def test_does_not_retry_on_non_rate_limit_exceptions(self) -> None:
        calls = {"n": 0}

        def fn() -> int:
            calls["n"] += 1
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            call_with_backoff(
                fn, max_retries=5, sleep=self._sleep, rand=self._rand,
            )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.sleeps, [])


# ---------------------------------------------------------------------------
# X-0 part B: guard_empty_response
# ---------------------------------------------------------------------------


class GuardEmptyResponseTests(unittest.TestCase):
    def test_raises_on_empty_string(self) -> None:
        with self.assertRaises(EmptyResponseError):
            guard_empty_response("", "chunk-1")

    def test_raises_on_whitespace_only(self) -> None:
        with self.assertRaises(EmptyResponseError):
            guard_empty_response("   \n  ", "chunk-2")

    def test_passes_through_non_empty_content(self) -> None:
        out = guard_empty_response('{"items": []}', "chunk-3")
        self.assertEqual(out, '{"items": []}')

    def test_call_order_guard_then_fence_strip(self) -> None:
        # X-1 invariant: guard runs BEFORE fence strip so an empty
        # string cannot reach json.loads("").
        with self.assertRaises(EmptyResponseError):
            guard_empty_response("", "chunk-x")
        # If reversed (strip first), the empty fence would strip to ""
        # and then json.loads("") raises the misleading char-0 error.


# ---------------------------------------------------------------------------
# X-0 part C: concurrency constant
# ---------------------------------------------------------------------------


class ConcurrencyConstantTests(unittest.TestCase):
    def test_max_concurrent_is_two(self) -> None:
        # 13 jobs * 2 concurrent = 26 simultaneous, under 50/min limit.
        self.assertEqual(MAX_CONCURRENT_HAIKU_CALLS, 2)

    def test_max_retries_is_five(self) -> None:
        self.assertEqual(MAX_RETRIES, 5)


# ---------------------------------------------------------------------------
# X-1: strip_markdown_fence
# ---------------------------------------------------------------------------


class StripMarkdownFenceTests(unittest.TestCase):
    def test_fenced_with_json_tag(self) -> None:
        text = '```json\n{"items": [{"x": 1}]}\n```'
        self.assertEqual(strip_markdown_fence(text), '{"items": [{"x": 1}]}')

    def test_fenced_without_language_tag(self) -> None:
        text = '```\n{"a": 1}\n```'
        self.assertEqual(strip_markdown_fence(text), '{"a": 1}')

    def test_non_fenced_passes_through(self) -> None:
        text = '{"a": 1}'
        self.assertEqual(strip_markdown_fence(text), '{"a": 1}')

    def test_trailing_whitespace_after_close_fence(self) -> None:
        text = '```json\n{"a": 1}\n```   \n  '
        self.assertEqual(strip_markdown_fence(text), '{"a": 1}')

    def test_non_string_input_returns_empty(self) -> None:
        self.assertEqual(strip_markdown_fence(None), "")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# X-0 part D: ChunkCounters rollup
# ---------------------------------------------------------------------------


class ChunkCountersTests(unittest.TestCase):
    def test_zero_blocked_is_ok(self) -> None:
        c = ChunkCounters()
        c.record_attempt(10)
        c.record_success(10)
        self.assertEqual(c.stage_status(), STAGE_OK)

    def test_minority_blocked_is_partial(self) -> None:
        c = ChunkCounters()
        c.record_attempt(10)
        c.record_success(7)
        c.record_block(BLOCK_REASON_RATE_LIMIT, n=3)
        self.assertEqual(c.stage_status(), STAGE_PARTIAL)
        self.assertEqual(c.chunks_blocked, 3)

    def test_majority_blocked_is_failed(self) -> None:
        c = ChunkCounters()
        c.record_attempt(10)
        c.record_success(4)
        c.record_block(BLOCK_REASON_RATE_LIMIT, n=6)
        self.assertEqual(c.stage_status(), STAGE_FAILED)

    def test_exactly_half_is_partial(self) -> None:
        c = ChunkCounters()
        c.record_attempt(10)
        c.record_block(BLOCK_REASON_PARSE_ERROR, n=5)
        # 5/10 == 0.5; rule is `> 0.5` -> failed, so exactly half stays
        # at partial.
        self.assertEqual(c.stage_status(), STAGE_PARTIAL)

    def test_block_reasons_breakout(self) -> None:
        c = ChunkCounters()
        c.record_attempt(10)
        c.record_block(BLOCK_REASON_RATE_LIMIT, n=1)
        c.record_block(BLOCK_REASON_EMPTY_RESPONSE, n=2)
        c.record_block(BLOCK_REASON_PARSE_ERROR, n=3)
        c.record_block(BLOCK_REASON_OTHER, n=4)
        d = c.as_dict()
        self.assertEqual(d["chunks_blocked"], 10)
        self.assertEqual(
            d["block_reasons"],
            {
                "rate_limit_exhausted": 1,
                "empty_response": 2,
                "parse_error": 3,
                "other": 4,
            },
        )

    def test_unknown_reason_tallies_under_other(self) -> None:
        c = ChunkCounters()
        c.record_attempt(1)
        c.record_block("gremlin", n=1)
        self.assertEqual(c.block_reasons["other"], 1)


# ---------------------------------------------------------------------------
# Failure artifact emission + counter bumps must be atomic
# ---------------------------------------------------------------------------


class FailureArtifactEmissionTests(unittest.TestCase):
    def test_emit_rate_limit_bumps_counter(self) -> None:
        c = ChunkCounters()
        c.record_attempt(1)
        art = fa.emit_rate_limit_exhausted(
            c,
            chunk_id="chunk-1",
            source_id="src",
            component="caller",
            detail="boom",
        )
        self.assertEqual(art["artifact_type"], fa.ARTIFACT_RATE_LIMIT_EXHAUSTED)
        self.assertEqual(c.chunks_blocked, 1)
        self.assertEqual(c.block_reasons["rate_limit_exhausted"], 1)

    def test_emit_empty_response_bumps_counter(self) -> None:
        c = ChunkCounters()
        c.record_attempt(1)
        art = fa.emit_empty_response(
            c, chunk_id="x", source_id="s", component="caller", detail="",
        )
        self.assertEqual(art["artifact_type"], fa.ARTIFACT_EMPTY_RESPONSE)
        self.assertEqual(c.block_reasons["empty_response"], 1)

    def test_emit_parse_error_bumps_counter(self) -> None:
        c = ChunkCounters()
        c.record_attempt(1)
        art = fa.emit_json_parse_failed(
            c, chunk_id="x", source_id="s", component="caller", detail="",
        )
        self.assertEqual(art["artifact_type"], fa.ARTIFACT_JSON_PARSE_FAILED)
        self.assertEqual(c.block_reasons["parse_error"], 1)

    def test_emit_empty_result_bumps_other(self) -> None:
        c = ChunkCounters()
        c.record_attempt(1)
        art = fa.emit_empty_result(
            c, chunk_id="x", source_id="s", component="caller", detail="",
        )
        self.assertEqual(art["artifact_type"], fa.ARTIFACT_EMPTY_RESULT)
        self.assertEqual(c.block_reasons["other"], 1)

    def test_emit_persists_artifact_when_sdl_root_provided(self, tmp_path=None) -> None:
        # unittest does not inject tmp_path -- use a temp dir manually.
        import tempfile
        from pathlib import Path

        c = ChunkCounters()
        with tempfile.TemporaryDirectory() as tmp:
            sdl_root = Path(tmp)
            fa.emit_rate_limit_exhausted(
                c,
                chunk_id="cid",
                source_id="src",
                component="cmp",
                detail="oops",
                sdl_root=sdl_root,
            )
            failures = list((sdl_root / "failures").glob("*.json"))
            self.assertEqual(len(failures), 1)
            doc = json.loads(failures[0].read_text())
            self.assertEqual(doc["artifact_type"], fa.ARTIFACT_RATE_LIMIT_EXHAUSTED)
            self.assertEqual(doc["chunk_id"], "cid")


# ---------------------------------------------------------------------------
# X-1 fence-strip integration with strict parser
# ---------------------------------------------------------------------------


class ParseJsonStrictTests(unittest.TestCase):
    def test_fenced_input_parses_correctly(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        out = _parse_json_response_strict(
            '```json\n{"items": [{"a": 1}]}\n```',
            chunk_id="c1",
        )
        self.assertEqual(out, {"items": [{"a": 1}]})

    def test_fenced_input_without_language_tag(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        out = _parse_json_response_strict(
            '```\n{"a": 1}\n```',
            chunk_id="c1",
        )
        self.assertEqual(out, {"a": 1})

    def test_non_fenced_input_parses(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        out = _parse_json_response_strict('{"a": 1}', chunk_id="c1")
        self.assertEqual(out, {"a": 1})

    def test_trailing_whitespace_after_close_fence(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        out = _parse_json_response_strict(
            '```json\n{"a": 1}\n```   \n',
            chunk_id="c1",
        )
        self.assertEqual(out, {"a": 1})

    def test_malformed_json_after_stripping_raises(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        with self.assertRaises(json.JSONDecodeError):
            _parse_json_response_strict(
                '```json\n{not valid}\n```',
                chunk_id="c1",
            )

    def test_empty_text_raises_empty_response_error(self) -> None:
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        with self.assertRaises(EmptyResponseError):
            _parse_json_response_strict("", chunk_id="c1")

    def test_fence_only_no_content_raises_empty_response_error(self) -> None:
        # `_parse_json_response_strict` must NOT pass an empty fenced
        # body to json.loads -- that would surface as a parse_error
        # when the real failure is an empty response.
        from spectrum_systems_core.extraction.typed_extraction_runner import (
            _parse_json_response_strict,
        )
        with self.assertRaises(EmptyResponseError):
            _parse_json_response_strict(
                "```json\n```",
                chunk_id="c1",
            )


if __name__ == "__main__":
    unittest.main()
