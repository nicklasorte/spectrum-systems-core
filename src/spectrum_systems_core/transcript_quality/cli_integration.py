"""Phase 2R — CLI glue for the transcript-quality validator.

Two public entry points:

* :func:`run_check_transcript_cli` — drives ``spectrum-core check-transcript``.
* :func:`run_pre_flight_check` — used by the extraction CLI when
  ``--enable-pre-flight-check`` is set. Returns ``(ok, report_dict,
  reason_code)``; the caller halts extraction when ``ok`` is False.

Both functions encapsulate file I/O and the diagnostics-write path so
the pure validator stays I/O-free.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

from ..reason_codes import (
    TRANSCRIPT_NOT_FOUND,
    TRANSCRIPT_QUALITY_CHECK_FAILED,
    TRANSCRIPT_UNREADABLE,
)
from ._config_loader import (
    TranscriptQualityConfigError,
    load_config,
)
from .validate import QualityReport, report_to_dict, validate


@dataclass(frozen=True)
class CheckTranscriptResult:
    exit_code: int
    report: QualityReport | None
    diagnostics_path: Path | None
    reason_code: str | None
    message: str | None


def _read_transcript_bytes(path: Path) -> tuple[str | None, str | None]:
    """Return (decoded_text, reason_code). reason_code is None on
    success; one of ``transcript_not_found`` / ``transcript_unreadable``
    on failure."""
    if not path.is_file():
        return (None, TRANSCRIPT_NOT_FOUND)
    try:
        raw = path.read_bytes()
    except OSError:
        return (None, TRANSCRIPT_UNREADABLE)
    # Strip a leading UTF-8 BOM so a BOM-tagged transcript still decodes
    # cleanly (the CLI layer enforces UTF-8; the BOM is benign).
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (None, TRANSCRIPT_UNREADABLE)
    return (text, None)


def _now_compact_iso() -> str:
    """A compact, filename-safe ISO-8601 stamp (no colons)."""
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )


def _diagnostics_dir(lake_root: Path, source_id: str) -> Path:
    return (
        Path(lake_root)
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
    )


def _write_report(
    *,
    report: QualityReport,
    lake_root: Path,
    source_id: str,
) -> Path:
    diag = _diagnostics_dir(lake_root, source_id)
    diag.mkdir(parents=True, exist_ok=True)
    timestamp = _now_compact_iso()
    path = diag / f"transcript_quality_report__{timestamp}.json"
    path.write_text(
        json.dumps(report_to_dict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _resolve_transcript_from_source_record(
    lake_root: Path, source_id: str
) -> tuple[Path | None, str | None]:
    """Resolve the transcript file for ``source_id`` via PR #188's
    ``source_record.json::payload.raw_path`` contract.

    Returns (resolved_path, reason_code). When the source_record itself
    is missing or malformed we return ``transcript_unreadable`` because
    the caller has no readable transcript either way.
    """
    sr_path = (
        Path(lake_root)
        / "processed"
        / "meetings"
        / source_id
        / "source_record.json"
    )
    if not sr_path.is_file():
        # Fall back to the data lake contract default layout — that is
        # the production path before the source_record contract is
        # populated. Failure is reported as transcript_not_found.
        candidate = (
            Path(lake_root) / "raw" / "meetings" / source_id / "transcript.txt"
        )
        if candidate.is_file():
            return (candidate, None)
        return (None, TRANSCRIPT_NOT_FOUND)
    try:
        sr = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (None, TRANSCRIPT_UNREADABLE)
    payload = sr.get("payload") if isinstance(sr, dict) else None
    raw_rel = payload.get("raw_path") if isinstance(payload, dict) else None
    if not isinstance(raw_rel, str) or not raw_rel.strip():
        # No raw_path → fall through to the canonical raw path.
        candidate = (
            Path(lake_root) / "raw" / "meetings" / source_id / "transcript.txt"
        )
        if candidate.is_file():
            return (candidate, None)
        return (None, TRANSCRIPT_NOT_FOUND)
    raw_path = (Path(lake_root) / raw_rel).resolve()
    if not raw_path.is_file():
        return (None, TRANSCRIPT_NOT_FOUND)
    return (raw_path, None)


def run_check_transcript_cli(
    *,
    transcript_path: str | None,
    source_id: str | None,
    lake: str | None,
    stream,
    config_path: str | None = None,
) -> CheckTranscriptResult:
    """Drive ``spectrum-core check-transcript``.

    Exit codes (mirrored on the returned result):

    * 0 — report produced, no errors
    * 1 — report produced, errors present
    * 2 — transcript could not be read

    Exactly one of (``transcript_path``) or (``source_id`` + ``lake``)
    must be supplied. Mixing the two modes is rejected with exit 2.
    """
    if transcript_path and source_id:
        msg = (
            "ERROR: --transcript-path and --source-id are mutually "
            "exclusive; provide exactly one."
        )
        stream.write(msg + "\n")
        return CheckTranscriptResult(
            exit_code=2,
            report=None,
            diagnostics_path=None,
            reason_code="usage_error",
            message=msg,
        )
    if not transcript_path and not source_id:
        msg = (
            "ERROR: provide either --transcript-path or "
            "--source-id (with --lake)."
        )
        stream.write(msg + "\n")
        return CheckTranscriptResult(
            exit_code=2,
            report=None,
            diagnostics_path=None,
            reason_code="usage_error",
            message=msg,
        )
    if source_id and not lake:
        msg = "ERROR: --source-id requires --lake."
        stream.write(msg + "\n")
        return CheckTranscriptResult(
            exit_code=2,
            report=None,
            diagnostics_path=None,
            reason_code="usage_error",
            message=msg,
        )

    try:
        config = load_config(config_path) if config_path else load_config()
    except TranscriptQualityConfigError as exc:
        msg = f"ERROR: transcript_quality_config_invalid: {exc}"
        stream.write(msg + "\n")
        return CheckTranscriptResult(
            exit_code=2,
            report=None,
            diagnostics_path=None,
            reason_code="config_error",
            message=msg,
        )

    if transcript_path:
        path_obj = Path(transcript_path)
        text, reason = _read_transcript_bytes(path_obj)
        if reason is not None:
            msg = f"ERROR: {reason}:{path_obj}"
            stream.write(msg + "\n")
            return CheckTranscriptResult(
                exit_code=2,
                report=None,
                diagnostics_path=None,
                reason_code=reason,
                message=msg,
            )
        resolved_source_id = source_id
        resolved_path_str: str | None = str(path_obj)
    else:
        # source_id + lake mode
        assert source_id is not None and lake is not None
        lake_root = Path(lake)
        resolved_path, reason = _resolve_transcript_from_source_record(
            lake_root, source_id
        )
        if reason is not None:
            msg = f"ERROR: {reason}:source_id={source_id}"
            stream.write(msg + "\n")
            return CheckTranscriptResult(
                exit_code=2,
                report=None,
                diagnostics_path=None,
                reason_code=reason,
                message=msg,
            )
        assert resolved_path is not None
        text, read_reason = _read_transcript_bytes(resolved_path)
        if read_reason is not None:
            msg = f"ERROR: {read_reason}:{resolved_path}"
            stream.write(msg + "\n")
            return CheckTranscriptResult(
                exit_code=2,
                report=None,
                diagnostics_path=None,
                reason_code=read_reason,
                message=msg,
            )
        resolved_source_id = source_id
        resolved_path_str = str(resolved_path)

    report = validate(
        text,
        config=config,
        transcript_path=resolved_path_str,
        source_id=resolved_source_id,
    )

    stream.write(
        json.dumps(report_to_dict(report), indent=2, sort_keys=True) + "\n"
    )

    diagnostics_path: Path | None = None
    if source_id and lake:
        diagnostics_path = _write_report(
            report=report,
            lake_root=Path(lake),
            source_id=source_id,
        )
        stream.write(f"diagnostics_path={diagnostics_path}\n")

    exit_code = 1 if report.has_errors else 0
    return CheckTranscriptResult(
        exit_code=exit_code,
        report=report,
        diagnostics_path=diagnostics_path,
        reason_code=None,
        message=None,
    )


@dataclass(frozen=True)
class PreFlightResult:
    ok: bool
    report: QualityReport | None
    diagnostics_path: Path | None
    reason_code: str | None
    message: str | None


def run_pre_flight_check(
    *,
    transcript_text: str,
    lake_root: Path | str,
    source_id: str,
    transcript_path: str | None = None,
    stream=None,
    config_path: str | None = None,
) -> PreFlightResult:
    """Run the validator over a resolved transcript before extraction.

    Called from the extraction CLI when ``--enable-pre-flight-check``
    is set. The caller has already loaded the transcript bytes from
    disk (so this function does not re-decode).
    """
    try:
        config = load_config(config_path) if config_path else load_config()
    except TranscriptQualityConfigError as exc:
        # Treat a malformed config as fail-closed: the operator cannot
        # be sure what the validator would have decided, so block.
        msg = f"transcript_quality_config_invalid:{exc}"
        if stream is not None:
            stream.write(f"ERROR: {msg}\n")
        return PreFlightResult(
            ok=False,
            report=None,
            diagnostics_path=None,
            reason_code="config_error",
            message=msg,
        )

    report = validate(
        transcript_text,
        config=config,
        transcript_path=transcript_path,
        source_id=source_id,
    )
    diagnostics_path = _write_report(
        report=report,
        lake_root=Path(lake_root),
        source_id=source_id,
    )
    if stream is not None:
        if report.has_errors:
            stream.write(
                f"transcript_quality_check_failed: report_path="
                f"{diagnostics_path}\n"
            )
        elif report.has_warnings:
            stream.write(
                f"transcript_quality_check_warnings_only: report_path="
                f"{diagnostics_path}\n"
            )
        else:
            stream.write(
                f"transcript_quality_check_passed: report_path="
                f"{diagnostics_path}\n"
            )
    if report.has_errors:
        return PreFlightResult(
            ok=False,
            report=report,
            diagnostics_path=diagnostics_path,
            reason_code=TRANSCRIPT_QUALITY_CHECK_FAILED,
            message="transcript_quality_check_failed",
        )
    return PreFlightResult(
        ok=True,
        report=report,
        diagnostics_path=diagnostics_path,
        reason_code=None,
        message=None,
    )
