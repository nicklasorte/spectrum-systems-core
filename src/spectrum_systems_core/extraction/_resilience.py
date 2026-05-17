"""Resilience primitives for the Haiku extraction API call sites.

Phase X-0 + X-1. Provides three small, named helpers used by every
Anthropic call in the extraction path:

- ``call_with_backoff(fn)`` -- retry on RateLimitError with exponential
  backoff plus jitter, re-raising after exhaustion. Non-rate-limit
  exceptions propagate immediately on the first attempt.
- ``guard_empty_response(raw, chunk_id)`` -- raise ``EmptyResponseError``
  when the model returned an empty/whitespace-only body. Must run
  BEFORE ``strip_markdown_fence`` so an empty string cannot slip
  through to ``json.loads``.
- ``strip_markdown_fence(text)`` -- remove a leading ``` (with optional
  language tag) and the trailing ``` so the JSON parser sees plain JSON.

Call order on every chunk is: guard_empty_response -> strip_markdown_fence
-> json.loads. Each helper is named (never inlined) so it can be unit
tested directly and so review can grep for call sites.

X-0 part C: ``MAX_CONCURRENT_HAIKU_CALLS`` is the single source of truth
for per-process Haiku concurrency. 13 transcripts in parallel x 2
concurrent classifier batches = 26 simultaneous, well under the 50/min
org rate limit with retry headroom.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


# X-0 part A: backoff config.
MAX_RETRIES: int = 5
# X-0 part C: per-process concurrency cap for Haiku batch classifier.
MAX_CONCURRENT_HAIKU_CALLS: int = 2


class EmptyResponseError(Exception):
    """Raised when the model returned an empty / whitespace-only body."""


def call_with_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run ``fn`` with retry on ``anthropic.RateLimitError`` only.

    Wait between retries is ``2**attempt + uniform(0, 1)`` seconds.
    After ``max_retries`` attempts the last RateLimitError re-raises so
    the caller can emit the ``api_rate_limit_exhausted`` failure
    artifact. Non-rate-limit exceptions are NOT retried -- they
    propagate on the first attempt so a real bug is not masked.

    ``sleep`` and ``rand`` are injectable for tests so we never have a
    test that actually sleeps for >1s.
    """
    # Lazy import: tests + offline runs that never invoke a real Haiku
    # caller do not require the anthropic SDK at import time.
    try:
        import anthropic
        rate_limit_exc = anthropic.RateLimitError
    except ImportError:
        # Without the SDK there is no RateLimitError to catch. Run once.
        return fn()

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except rate_limit_exc as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            wait = (2 ** attempt) + rand(0, 1)
            sleep(wait)
    # Unreachable; the final attempt either returns or re-raises.
    assert last_exc is not None
    raise last_exc


def guard_empty_response(raw: str, chunk_id: str) -> str:
    """Return ``raw`` unchanged or raise ``EmptyResponseError``.

    An empty / whitespace-only body means the model produced no JSON to
    parse. Letting that fall through to ``json.loads("")`` produces the
    misleading ``Expecting value: line 1 column 1 (char 0)`` error and
    hides the true cause (rate limit, network truncation, etc).

    Must be called BEFORE ``strip_markdown_fence`` so a stripped fence
    cannot accidentally produce "" that then bypasses the guard.
    """
    if not isinstance(raw, str) or not raw or not raw.strip():
        raise EmptyResponseError(
            f"Empty API response for chunk {chunk_id}"
        )
    return raw


def strip_markdown_fence(text: str) -> str:
    """Strip a leading ``` (with optional language tag) and trailing ```.

    Per X-1 spec. Kept as a named function for direct unit testing and
    so its call sites can be grep'd. Idempotent on non-fenced input.
    Does NOT raise on malformed fences; the downstream ``json.loads``
    will surface the parse failure so it can be counted as
    ``block_reason: parse_error``.

    PR #134 parity: the degenerate single-line ` ```{...}``` ` shape
    (opening fence with NO newline after it) drops only the three
    backticks (``text[3:]``), never the body. Returning ``""`` here
    would turn a fenced-but-recoverable Haiku response into an empty
    one, which the extract-typed claims parser then logs as
    ``typed_extraction_llm_json_parse_failed`` and silently treats as
    zero items. A bare ` ``` ` (no body) still collapses to ``""``
    because ``"```"[3:] == ""``, so the X-1 "model wrote only a fence"
    fail-closed case is unchanged. Mirrors
    ``scripts/create_opus_reference_baselines._strip_fence``.
    """
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()
