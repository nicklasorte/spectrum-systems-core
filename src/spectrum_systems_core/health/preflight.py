"""Pipeline preflight — feature-flag presence check (Class 3).

Runs before any pipeline stage. Halts the pipeline if a required
feature-flag artifact is missing from the data-lake. Warns if a flag
is present but disabled.

Rollback: ``PREFLIGHT_ENABLED=false`` skips the check entirely. A
warning is logged so the deliberate bypass appears in CI logs.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .finding import HealthFinding, write_finding

_LOG = logging.getLogger(__name__)

PREFLIGHT_ENABLED_ENV_VAR: str = "PREFLIGHT_ENABLED"
_DISABLED_VALUES: frozenset[str] = frozenset({"false", "0", "no", "off"})

# Required feature flags. Add new flags here as phases ship. The file
# layout matches the seed-feature-flags workflow:
# <data_lake>/store/artifacts/config/<flag_name>.json
REQUIRED_FEATURE_FLAGS: tuple[str, ...] = (
    "phase_v_post_hoc_verification_enabled",
    "phase_w_agenda_detection_enabled",
)

# Flags that are expected to be ``enabled: false`` until a paired
# "merged" sentinel flag exists. The warn for these is suppressed
# until the sentinel appears.
SUPPRESS_DISABLED_WARN_UNTIL: dict[str, str] = {
    "phase_w_agenda_detection_enabled": "phase_w_merged",
}


def _preflight_enabled() -> bool:
    raw = os.environ.get(PREFLIGHT_ENABLED_ENV_VAR, "")
    if raw.strip().lower() in _DISABLED_VALUES:
        return False
    return True


def _config_dir(data_lake_path: str | Path) -> Path:
    return Path(data_lake_path) / "store" / "artifacts" / "config"


def _flag_path(data_lake_path: str | Path, flag_name: str) -> Path:
    return _config_dir(data_lake_path) / f"{flag_name}.json"


def _sentinel_present(
    data_lake_path: str | Path, sentinel_name: str
) -> bool:
    """A sentinel flag is "present" if its JSON exists and parses.

    Its ``enabled`` value is not used here — its mere presence flips
    the suppression rule.
    """
    path = _flag_path(data_lake_path, sentinel_name)
    if not path.is_file():
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return True


def _read_flag(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON dict or None on any failure.

    Per Red Team Pass 1: a malformed flag file is treated as missing
    (the flag is effectively absent), not a crash.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def check_feature_flags(
    data_lake_path: str | Path,
    *,
    required: Iterable[str] = REQUIRED_FEATURE_FLAGS,
    pipeline_run_id: str | None = None,
) -> list[HealthFinding]:
    """Audit required feature flags. Returns one finding per problem."""
    findings: list[HealthFinding] = []
    for flag_name in required:
        path = _flag_path(data_lake_path, flag_name)
        flag = _read_flag(path) if path.exists() else None
        if flag is None:
            findings.append(
                HealthFinding(
                    finding_code="feature_flag_missing",
                    severity="halt",
                    pipeline_run_id=pipeline_run_id,
                    context={
                        "flag_name": flag_name,
                        "expected_path": str(path),
                    },
                    remediation=(
                        "Run the 'Seed feature flags' workflow in "
                        "GitHub Actions to materialise this flag."
                    ),
                )
            )
            continue
        enabled = bool(flag.get("enabled", False))
        if enabled:
            continue
        sentinel = SUPPRESS_DISABLED_WARN_UNTIL.get(flag_name)
        if sentinel and not _sentinel_present(data_lake_path, sentinel):
            # Expected to be disabled until sentinel exists; suppress warn.
            continue
        findings.append(
            HealthFinding(
                finding_code="feature_flag_disabled",
                severity="warn",
                pipeline_run_id=pipeline_run_id,
                context={"flag_name": flag_name, "enabled": False},
                remediation=(
                    f"Set enabled:true in {flag_name}.json to activate "
                    "this phase."
                ),
            )
        )
    return findings


def run_preflight(
    data_lake_path: str | Path,
    *,
    pipeline_run_id: str | None = None,
    out_stream=None,
) -> int:
    """Entry point. Writes findings and returns an exit code.

    Returns 0 on clean preflight, 1 on any halt finding. Warn / info
    findings are written but do not affect the exit code.
    """
    out = out_stream if out_stream is not None else sys.stdout

    if not _preflight_enabled():
        _LOG.warning(
            "preflight_disabled: %s=false -- skipping feature-flag presence "
            "check. This is a deliberate bypass; restore the env var to "
            "re-enable.",
            PREFLIGHT_ENABLED_ENV_VAR,
        )
        print(
            f"warning: preflight bypassed via {PREFLIGHT_ENABLED_ENV_VAR}=false",
            file=out,
        )
        return 0

    findings = check_feature_flags(
        data_lake_path, pipeline_run_id=pipeline_run_id
    )
    halt = False
    for f in findings:
        try:
            write_finding(f, data_lake_path=data_lake_path)
        except Exception as exc:  # noqa: BLE001
            # A write failure must not silently swallow a halt: keep
            # the halt signal even if the artifact didn't land.
            _LOG.warning("health_finding_write_failed: %s", exc)
        if f.is_halt():
            halt = True

    _write_github_summary(findings)
    _print_text_summary(findings, out)

    if halt:
        return 1
    return 0


def _print_text_summary(
    findings: list[HealthFinding], out
) -> None:
    if not findings:
        print("preflight: all checks passed.", file=out)
        return
    print("preflight findings:", file=out)
    for f in findings:
        print(
            f"  [{f.severity}] {f.finding_code}: "
            f"{f.context} -- {f.remediation}",
            file=out,
        )


def _write_github_summary(findings: list[HealthFinding]) -> None:
    """Append a Markdown table to ``$GITHUB_STEP_SUMMARY`` if set."""
    gh_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not gh_path:
        return
    try:
        with open(gh_path, "a", encoding="utf-8") as fh:
            fh.write("## Health Check Results\n\n")
            if not findings:
                fh.write("OK All health checks passed.\n")
                return
            fh.write("| Finding | Severity | Remediation |\n")
            fh.write("|---------|----------|-------------|\n")
            for f in findings:
                icon = {"halt": "HALT", "warn": "WARN", "info": "INFO"}[
                    f.severity
                ]
                # Compact context into a single line.
                ctx = f.context.get("flag_name") or f.context.get(
                    "artifact_id"
                ) or ""
                label = f"{f.finding_code}: {ctx}" if ctx else f.finding_code
                fh.write(
                    f"| {label} | {icon} | "
                    f"{f.remediation.replace('|', '/')} |\n"
                )
    except OSError as exc:
        _LOG.warning("github_step_summary_write_failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m spectrum_systems_core.health.preflight``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m spectrum_systems_core.health.preflight",
        description="Run pipeline preflight (feature flag presence check).",
    )
    parser.add_argument(
        "--data-lake",
        required=False,
        default=os.environ.get("DATA_LAKE_PATH", ""),
        help="Path to data-lake root (defaults to $DATA_LAKE_PATH).",
    )
    parser.add_argument(
        "--pipeline-run-id",
        required=False,
        default=os.environ.get("PIPELINE_RUN_ID") or None,
    )
    args = parser.parse_args(argv)
    if not args.data_lake:
        print(
            "error: --data-lake not supplied and DATA_LAKE_PATH unset",
            file=sys.stderr,
        )
        return 2
    return run_preflight(
        args.data_lake, pipeline_run_id=args.pipeline_run_id
    )


if __name__ == "__main__":
    raise SystemExit(main())
