"""Phase 2R — validator unit tests.

One test per gate the spec enumerates. Each test ASSERTS the rejection
fires on a fixture that would have passed before the gate existed.
"""
from __future__ import annotations

from spectrum_systems_core.transcript_quality import validate
from spectrum_systems_core.transcript_quality.checks import CHECKS

from . import fixtures as F


def _check(report, name: str):
    matches = [c for c in report.checks if c.check_name == name]
    assert len(matches) == 1, f"expected exactly one check {name!r}, got {matches!r}"
    return matches[0]


def test_validator_emits_one_result_per_check() -> None:
    report = validate(F.valid_transcript())
    declared = {c["name"] for c in CHECKS}
    emitted = {c.check_name for c in report.checks}
    assert declared == emitted


def test_valid_transcript_passes_all_gates() -> None:
    report = validate(F.valid_transcript())
    assert not report.has_errors, [c for c in report.checks if not c.passed]


def test_encoding_corruption_rejected() -> None:
    report = validate(F.encoding_corrupted_transcript())
    enc = _check(report, "encoding_utf8")
    assert not enc.passed
    assert enc.reason_code == "encoding_not_utf8"
    assert report.has_errors


def test_too_short_warns_not_errors_for_length() -> None:
    report = validate(F.too_short_transcript())
    short = _check(report, "length_above_min")
    assert not short.passed
    assert short.reason_code == "transcript_below_min_length"
    assert short.severity == "warning"


def test_too_large_advisory_warns() -> None:
    report = validate(F.too_large_transcript())
    adv = _check(report, "length_below_advisory_max")
    assert not adv.passed
    assert adv.reason_code == "transcript_above_advisory_max_length"
    assert adv.severity == "warning"


def test_hard_max_blocks() -> None:
    report = validate(F.hard_max_transcript())
    hard = _check(report, "length_below_hard_max")
    assert not hard.passed
    assert hard.reason_code == "transcript_above_hard_max_length"
    assert hard.severity == "error"
    assert report.has_errors


def test_single_speaker_long_passes() -> None:
    """100+ words even with one speaker → sufficient_total_content passes."""
    report = validate(F.single_speaker_long_transcript())
    cnt = _check(report, "sufficient_total_content")
    assert cnt.passed, cnt.detail


def test_single_speaker_too_few_words_blocks() -> None:
    """< 100 words AND < 2 speakers → insufficient_total_content blocks."""
    report = validate(F.single_speaker_too_few_words())
    cnt = _check(report, "sufficient_total_content")
    assert not cnt.passed
    assert cnt.reason_code == "insufficient_total_content"
    assert cnt.severity == "error"
    assert report.has_errors


def test_two_speakers_few_words_passes() -> None:
    """2+ speakers → sufficient_total_content passes even if word count
    is low."""
    report = validate(F.two_speakers_few_words())
    cnt = _check(report, "sufficient_total_content")
    assert cnt.passed, cnt.detail


def test_insufficient_turn_count_warns() -> None:
    """A single-line transcript fires the warning."""
    report = validate(
        "Alice Smith: " + " ".join(["solo"] * 200) + ".\n"
    )
    turns = _check(report, "turn_count_above_min")
    assert not turns.passed
    assert turns.severity == "warning"
    assert turns.reason_code == "insufficient_turn_count"


def test_no_speaker_format_blocks() -> None:
    report = validate(F.no_format_transcript())
    fmt = _check(report, "format_detected")
    assert not fmt.passed
    assert fmt.reason_code == "no_speaker_format_detected"
    assert fmt.severity == "error"
    assert report.detected_format == "unknown"


def test_duplicate_turn_id_blocks() -> None:
    report = validate(F.duplicate_turn_ids_transcript())
    turns = _check(report, "unique_turn_ids")
    assert not turns.passed
    assert turns.reason_code == "duplicate_turn_id"
    assert turns.severity == "error"


def test_speaker_dash_only_detected() -> None:
    report = validate(F.speaker_dash_only_transcript())
    assert report.detected_format == "speaker_dash"


def test_speaker_colon_only_detected() -> None:
    report = validate(F.valid_transcript())
    assert report.detected_format == "speaker_colon"


def test_tied_format_resolves_to_colon() -> None:
    """Alphabetical tiebreaker pins the deterministic resolution."""
    report = validate(F.tied_format_transcript())
    assert report.detected_format == "speaker_colon"


def test_none_input_produces_report_with_errors() -> None:
    report = validate(None)
    assert report.has_errors
    assert any(
        c.reason_code == "no_speaker_format_detected" for c in report.checks
    )


def test_empty_string_input_produces_report_with_errors() -> None:
    report = validate("")
    assert report.has_errors


def test_every_failed_check_has_detail_with_numbers() -> None:
    """Red team Pass 1 #3: a new engineer must be able to read the
    diagnostic. Every failed check must include numeric context in
    detail, not just the reason code."""
    report = validate(F.too_short_transcript())
    for c in report.checks:
        if not c.passed:
            assert c.detail is not None and c.detail.strip(), (
                f"failed check {c.check_name!r} missing detail"
            )


def test_format_detection_mutation_only_colon() -> None:
    """speaker_colon-only fixture detects speaker_colon."""
    transcript = "\n".join(
        f"Alice Smith: turn {i} content padding here." for i in range(5)
    )
    report = validate(transcript)
    assert report.detected_format == "speaker_colon"


def test_format_detection_mutation_only_dash() -> None:
    transcript = "\n".join(
        f"Alice Smith — turn {i} content padding here." for i in range(5)
    )
    report = validate(transcript)
    assert report.detected_format == "speaker_dash"


def test_format_detection_mutation_tie_breaker() -> None:
    transcript = (
        "Alice Smith: one.\nBob Jones: two.\n"
        "Alice Smith — one.\nBob Jones — two.\n"
    )
    report = validate(transcript)
    assert report.detected_format == "speaker_colon"


def test_format_detection_mutation_none() -> None:
    report = validate("just a blob of words with no speaker structure")
    assert report.detected_format == "unknown"


def test_word_count_99_one_speaker_blocks() -> None:
    # 99 distinct words, 1 speaker → insufficient_total_content fires.
    body = "Alice Smith: " + " ".join(f"w{i}" for i in range(99)) + ".\n"
    report = validate(body)
    cnt = _check(report, "sufficient_total_content")
    assert not cnt.passed


def test_word_count_99_two_speakers_passes() -> None:
    body = (
        "Alice Smith: " + " ".join(f"w{i}" for i in range(50)) + ".\n"
        + "Bob Jones: " + " ".join(f"w{i}" for i in range(49)) + ".\n"
    )
    report = validate(body)
    cnt = _check(report, "sufficient_total_content")
    assert cnt.passed


def test_word_count_100_one_speaker_passes() -> None:
    body = "Alice Smith: " + " ".join(f"w{i}" for i in range(100)) + ".\n"
    report = validate(body)
    cnt = _check(report, "sufficient_total_content")
    assert cnt.passed


def test_word_count_100_two_speakers_passes() -> None:
    body = (
        "Alice Smith: " + " ".join(f"w{i}" for i in range(50)) + ".\n"
        + "Bob Jones: " + " ".join(f"w{i}" for i in range(50)) + ".\n"
    )
    report = validate(body)
    cnt = _check(report, "sufficient_total_content")
    assert cnt.passed


def test_validator_is_pure_no_datetime_now_or_uuid_inside_body() -> None:
    """Red team Pass 1 #6: the validator's source must not import or
    call `uuid.uuid4` / `os.environ` / file I/O inside its body. The
    one sanctioned non-determinism (`generated_at`) is computed by
    `_now_iso` at the start of the function; it is the only
    `datetime` use."""
    import inspect

    from spectrum_systems_core.transcript_quality import validate as v

    src = inspect.getsource(v)
    forbidden = ("uuid.uuid4", "os.environ", "Path.read_text", "open(")
    for needle in forbidden:
        assert needle not in src, (
            f"validate() body must not contain {needle!r}; the function "
            "is pure by contract."
        )
