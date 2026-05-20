"""Phase 2R — pure transcript quality validator.

:func:`validate` accepts a transcript string and a config dict and returns
a :class:`QualityReport`. It is intentionally pure:

* No LLM calls.
* No network access.
* No environment-variable lookups.
* No file I/O. The caller is responsible for reading the file from disk
  (the CLI layer in :mod:`spectrum_systems_core.data_lake.cli` does that).
* No mutable module-level state. The only side effect is producing the
  return value.

The single sanctioned non-determinism inside :func:`validate` is the
``generated_at`` timestamp, computed once at the start and threaded into
the report. The idempotency test
(``tests/transcript_quality/test_idempotency.py``) excludes that field
when comparing two reports for byte-equivalence.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Literal

from .checks import CHECKS

# Speaker line regexes. ``speaker_colon`` matches ``Alice Smith: ...``
# and ``speaker_dash`` matches ``Alice Smith — ...`` (em dash, U+2014).
# Both anchor at the start of a line so a colon mid-utterance does not
# falsely register as a new turn.
_SPEAKER_NAME = r"[A-Z][a-z]+(?: [A-Z][a-z]+)*"
_SPEAKER_COLON_RE = re.compile(rf"^{_SPEAKER_NAME}: ", re.MULTILINE)
_SPEAKER_DASH_RE = re.compile(rf"^{_SPEAKER_NAME} — ", re.MULTILINE)
_TURN_ID_RE = re.compile(r"\[t\d+\]")

_DEFAULTS: dict[str, int] = {
    "min_byte_length": 500,
    "advisory_max_byte_length": 1_000_000,
    "hard_max_byte_length": 10_000_000,
    "min_turn_count": 2,
    "min_word_count_when_single_speaker": 100,
}


@dataclass(frozen=True)
class QualityCheckResult:
    check_name: str
    severity: Literal["error", "warning", "info"]
    passed: bool
    reason_code: str | None
    detail: str | None


@dataclass(frozen=True)
class QualityReport:
    transcript_path: str | None
    source_id: str | None
    transcript_byte_length: int
    detected_format: str
    detected_turn_count: int
    detected_word_count: int
    checks: tuple[QualityCheckResult, ...]
    has_errors: bool
    has_warnings: bool
    generated_at: str


def _now_iso() -> str:
    """The single sanctioned non-determinism. Computed once at the start
    of :func:`validate` and threaded into the resulting report. The
    idempotency test excludes this field."""
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resolve_config(config: dict[str, Any] | None) -> dict[str, int]:
    cfg = dict(_DEFAULTS)
    if config is not None:
        for k, v in config.items():
            if k in cfg and isinstance(v, int):
                cfg[k] = v
    return cfg


def _strip_speaker_prefix(line: str, fmt: str) -> str:
    if fmt == "speaker_colon":
        m = re.match(rf"^{_SPEAKER_NAME}: ", line)
    elif fmt == "speaker_dash":
        m = re.match(rf"^{_SPEAKER_NAME} — ", line)
    else:
        m = None
    return line[m.end():] if m else line


def _detect_format(
    transcript: str, format_hint: str
) -> tuple[str, int, int]:
    """Return (detected_format, colon_count, dash_count).

    When ``format_hint`` is ``"auto"`` the format with more matches
    wins; alphabetical (``speaker_colon`` < ``speaker_dash``) is the
    deterministic tiebreaker.
    """
    colon_count = len(_SPEAKER_COLON_RE.findall(transcript))
    dash_count = len(_SPEAKER_DASH_RE.findall(transcript))
    if format_hint == "speaker_colon":
        return ("speaker_colon", colon_count, dash_count)
    if format_hint == "speaker_dash":
        return ("speaker_dash", colon_count, dash_count)
    if colon_count == 0 and dash_count == 0:
        return ("unknown", 0, 0)
    if dash_count > colon_count:
        return ("speaker_dash", colon_count, dash_count)
    # colon wins ties (alphabetical tiebreaker; documented).
    return ("speaker_colon", colon_count, dash_count)


def _count_speakers(transcript: str, fmt: str) -> int:
    if fmt == "speaker_colon":
        rx = re.compile(rf"^({_SPEAKER_NAME}): ", re.MULTILINE)
    elif fmt == "speaker_dash":
        rx = re.compile(rf"^({_SPEAKER_NAME}) — ", re.MULTILINE)
    else:
        return 0
    names = set(rx.findall(transcript))
    return len(names)


def _count_words(transcript: str, fmt: str) -> int:
    """Count whitespace-separated tokens after stripping speaker labels."""
    total = 0
    for line in transcript.splitlines():
        stripped = _strip_speaker_prefix(line, fmt)
        total += len(stripped.split())
    return total


def _find_duplicate_turn_ids(transcript: str) -> list[str]:
    seen: dict[str, int] = {}
    for token in _TURN_ID_RE.findall(transcript):
        seen[token] = seen.get(token, 0) + 1
    return sorted(t for t, n in seen.items() if n > 1)


def _result(
    name: str,
    *,
    passed: bool,
    detail: str | None,
) -> QualityCheckResult:
    spec = next(c for c in CHECKS if c["name"] == name)
    severity = spec["severity"]
    reason_code = None if passed else spec["reason_code_on_fail"]
    return QualityCheckResult(
        check_name=name,
        severity=severity,  # type: ignore[arg-type]
        passed=passed,
        reason_code=reason_code,
        detail=detail,
    )


def validate(
    transcript: str | None,
    *,
    format: Literal["speaker_colon", "speaker_dash", "auto"] = "auto",
    config: dict[str, Any] | None = None,
    transcript_path: str | None = None,
    source_id: str | None = None,
) -> QualityReport:
    """Validate a transcript string against the Phase 2R quality checks.

    The function is pure: given the same ``transcript`` and ``config``
    it produces a report whose fields are identical except for
    ``generated_at``. No file I/O, no LLM calls, no environment access.
    """
    generated_at = _now_iso()
    cfg = _resolve_config(config)

    if transcript is None or transcript == "":
        # Missing-input bypass: produce a report that names the failure
        # rather than crashing.
        return QualityReport(
            transcript_path=transcript_path,
            source_id=source_id,
            transcript_byte_length=0,
            detected_format="unknown",
            detected_turn_count=0,
            detected_word_count=0,
            checks=(
                _result(
                    "length_above_min",
                    passed=False,
                    detail=(
                        f"Transcript is empty (0 bytes); minimum is "
                        f"{cfg['min_byte_length']}."
                    ),
                ),
                _result(
                    "sufficient_total_content",
                    passed=False,
                    detail="Transcript is empty; no words and no speakers.",
                ),
                _result(
                    "format_detected",
                    passed=False,
                    detail="Transcript is empty; no speaker format detectable.",
                ),
            ),
            has_errors=True,
            has_warnings=True,
            generated_at=generated_at,
        )

    byte_length = len(transcript.encode("utf-8"))
    detected_format, _colon, _dash = _detect_format(transcript, format)
    fmt_for_counts = detected_format if detected_format != "unknown" else "speaker_colon"

    if detected_format == "speaker_colon":
        turn_count = len(_SPEAKER_COLON_RE.findall(transcript))
    elif detected_format == "speaker_dash":
        turn_count = len(_SPEAKER_DASH_RE.findall(transcript))
    else:
        turn_count = 0

    word_count = _count_words(transcript, fmt_for_counts)
    speaker_count = _count_speakers(transcript, fmt_for_counts)

    results: list[QualityCheckResult] = []

    # encoding_utf8 — the caller already decoded to str. The remaining
    # signal is whether the string contains the U+FFFD replacement char.
    bad_codepoints = transcript.count("�")
    results.append(
        _result(
            "encoding_utf8",
            passed=bad_codepoints == 0,
            detail=(
                f"Detected {bad_codepoints} U+FFFD replacement codepoints; "
                "this signals upstream encoding corruption."
                if bad_codepoints
                else "No U+FFFD replacement codepoints detected."
            ),
        )
    )

    results.append(
        _result(
            "length_above_min",
            passed=byte_length >= cfg["min_byte_length"],
            detail=(
                f"Transcript is {byte_length} bytes; minimum is "
                f"{cfg['min_byte_length']}."
            ),
        )
    )

    results.append(
        _result(
            "length_below_advisory_max",
            passed=byte_length <= cfg["advisory_max_byte_length"],
            detail=(
                f"Transcript is {byte_length} bytes; advisory maximum is "
                f"{cfg['advisory_max_byte_length']}."
            ),
        )
    )

    results.append(
        _result(
            "length_below_hard_max",
            passed=byte_length <= cfg["hard_max_byte_length"],
            detail=(
                f"Transcript is {byte_length} bytes; hard maximum is "
                f"{cfg['hard_max_byte_length']}."
            ),
        )
    )

    results.append(
        _result(
            "turn_count_above_min",
            passed=turn_count >= cfg["min_turn_count"],
            detail=(
                f"Detected {turn_count} speaker turns "
                f"(format={detected_format}); minimum is "
                f"{cfg['min_turn_count']}."
            ),
        )
    )

    sufficient = (
        word_count >= cfg["min_word_count_when_single_speaker"]
        or speaker_count >= 2
    )
    results.append(
        _result(
            "sufficient_total_content",
            passed=sufficient,
            detail=(
                f"Detected {word_count} words and {speaker_count} distinct "
                f"speakers; need >= "
                f"{cfg['min_word_count_when_single_speaker']} words OR "
                ">= 2 speakers."
            ),
        )
    )

    results.append(
        _result(
            "format_detected",
            passed=detected_format != "unknown",
            detail=(
                f"Detected speaker format: {detected_format} "
                f"(speaker_colon matches={_colon}, "
                f"speaker_dash matches={_dash})."
            ),
        )
    )

    duplicates = _find_duplicate_turn_ids(transcript)
    results.append(
        _result(
            "unique_turn_ids",
            passed=not duplicates,
            detail=(
                f"Duplicate turn ids detected: {duplicates}"
                if duplicates
                else "No duplicate turn ids detected."
            ),
        )
    )

    has_errors = any(
        (not r.passed) and r.severity == "error" for r in results
    )
    has_warnings = any(
        (not r.passed) and r.severity == "warning" for r in results
    )

    return QualityReport(
        transcript_path=transcript_path,
        source_id=source_id,
        transcript_byte_length=byte_length,
        detected_format=detected_format,
        detected_turn_count=turn_count,
        detected_word_count=word_count,
        checks=tuple(results),
        has_errors=has_errors,
        has_warnings=has_warnings,
        generated_at=generated_at,
    )


def report_to_dict(report: QualityReport) -> dict[str, Any]:
    """Serialise a :class:`QualityReport` to a JSON-safe dict matching
    ``transcript_quality_report.schema.json``."""
    return {
        "artifact_type": "transcript_quality_report",
        "schema_version": "1.0.0",
        "transcript_path": report.transcript_path,
        "source_id": report.source_id,
        "transcript_byte_length": report.transcript_byte_length,
        "detected_format": report.detected_format,
        "detected_turn_count": report.detected_turn_count,
        "detected_word_count": report.detected_word_count,
        "checks": [
            {
                "check_name": c.check_name,
                "severity": c.severity,
                "passed": c.passed,
                "reason_code": c.reason_code,
                "detail": c.detail,
            }
            for c in report.checks
        ],
        "has_errors": report.has_errors,
        "has_warnings": report.has_warnings,
        "generated_at": report.generated_at,
    }
