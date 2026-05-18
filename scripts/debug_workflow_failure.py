#!/usr/bin/env python3
"""Structured workflow-failure diagnostics.

Invoked by ``.github/workflows/create-opus-reference-baselines.yml`` and
``.github/workflows/compare-opus-haiku.yml`` on ``if: failure()``. It
emits a GitHub Step Summary with enough signal to root-cause a failure
without needing log access or operator input.

Fail-safe by contract: every section is independently guarded and a
section error degrades to a warning line — this script never raises and
always exits 0. A crashing debug step is worse than no debug step.

Runnable locally (no data-lake required — sections degrade cleanly):

    python scripts/debug_workflow_failure.py \\
        --workflow create-opus-reference-baselines \\
        --job create-opus-reference-baselines
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Tokens that mark an actionable failure line in run_history.jsonl.
FAIL_TOKENS = (
    "FAIL:",
    "ERROR:",
    "exit code",
    "Traceback",
    "missing_source_record",
    "invalid_source_record",
    "source_record_ingest_failed",
)

# Signals collected by the sections, read by the decision table.
SIGNALS: dict[str, object] = {
    "source_record_missing": False,
    "anthropic_set": False,
    "data_lake_exists": False,
    "validator_failure": False,
}


def _emit(lines: list[str], text: str = "") -> None:
    lines.append(text)


def _section(lines: list[str], title: str, fn) -> None:
    """Run one section under a guard. Never raises."""
    _emit(lines, f"### {title}")
    _emit(lines)
    try:
        fn(lines)
    except Exception as exc:  # noqa: BLE001 - fail-safe is the contract
        _emit(
            lines,
            f"> warning: section `{title}` failed: "
            f"`{type(exc).__name__}: {exc}` (continuing)",
        )
    _emit(lines)


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a command, capture combined output. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}: {exc}"


def _resolve_data_lake(arg_path: str | None) -> tuple[Path, bool, bool]:
    """Return (path, configured, exists).

    ``configured`` is True when the path came from an explicit
    ``--data-lake`` arg or the ``DATA_LAKE_PATH`` env var (the
    create-opus workflow sets it; the compare workflow does not, so
    fall back to the clone-data-lake default of ``<workspace>/data-lake``).
    """
    env = os.environ.get("DATA_LAKE_PATH")
    configured = bool(arg_path or env)
    raw = arg_path or env or str(REPO_ROOT / "data-lake")
    p = Path(raw)
    return p, configured, p.is_dir()


# --------------------------------------------------------------------------- #
# Section 1 — failure context
# --------------------------------------------------------------------------- #
def _section_context(workflow: str, job: str, lake: Path, configured: bool,
                      exists: bool):
    def fn(lines: list[str]) -> None:
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _emit(lines, f"- workflow: `{workflow}`")
        _emit(lines, f"- job: `{job}`")
        _emit(lines, f"- run id: `{os.environ.get('GITHUB_RUN_ID', 'n/a')}`")
        _emit(
            lines,
            f"- run attempt: "
            f"`{os.environ.get('GITHUB_RUN_ATTEMPT', 'n/a')}`",
        )
        _emit(lines, f"- repository: `{os.environ.get('GITHUB_REPOSITORY', 'n/a')}`")
        _emit(lines, f"- ref: `{os.environ.get('GITHUB_REF', 'n/a')}`")
        _emit(lines, f"- sha: `{os.environ.get('GITHUB_SHA', 'n/a')}`")
        _emit(lines, f"- timestamp (UTC): `{ts}`")
        _emit(lines, f"- python: `{sys.version.split()[0]}`")

        anthropic_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
        SIGNALS["anthropic_set"] = anthropic_set
        _emit(lines, f"- ANTHROPIC_API_KEY set: **{'yes' if anthropic_set else 'no'}**")

        SIGNALS["data_lake_exists"] = exists
        _emit(
            lines,
            f"- data-lake path configured: "
            f"**{'yes' if configured else 'no'}**",
        )
        _emit(
            lines,
            f"- data-lake path exists: **{'yes' if exists else 'no'}** "
            f"(`{lake}`)",
        )
        _emit(lines)
        _emit(lines, "pip freeze (top 20 by name):")
        _emit(lines)
        _emit(lines, "```")
        rc, out = _run([sys.executable, "-m", "pip", "freeze"])
        pkgs: list[str] = []
        if rc == 0 and out:
            pkgs = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not pkgs:
            try:
                import importlib.metadata as md

                pkgs = sorted(
                    f"{d.metadata['Name']}=={d.version}"
                    for d in md.distributions()
                )
            except Exception:  # noqa: BLE001
                pkgs = []
        if pkgs:
            pkgs = sorted(pkgs, key=lambda s: s.lower())[:20]
            for line in pkgs:
                _emit(lines, line)
        else:
            _emit(lines, "(package list unavailable)")
        _emit(lines, "```")

    return fn


# --------------------------------------------------------------------------- #
# Section 2 — data-lake state
# --------------------------------------------------------------------------- #
def _section_data_lake(lake: Path, exists: bool):
    def fn(lines: list[str]) -> None:
        if not exists:
            _emit(lines, "Data-lake directory not present — state checks skipped.")
            return

        raw_dir = lake / "store" / "raw" / "transcripts"
        proc_dir = lake / "store" / "processed" / "meetings"

        transcripts = []
        if raw_dir.is_dir():
            transcripts = sorted(p.name for p in raw_dir.iterdir())
        _emit(
            lines,
            f"- transcripts under `store/raw/transcripts/`: "
            f"**{len(transcripts)}**",
        )
        for name in transcripts[:50]:
            _emit(lines, f"  - `{name}`")
        if len(transcripts) > 50:
            _emit(lines, f"  - … (+{len(transcripts) - 50} more)")

        meeting_dirs = []
        if proc_dir.is_dir():
            meeting_dirs = sorted(
                p for p in proc_dir.iterdir() if p.is_dir()
            )
        _emit(lines)
        _emit(
            lines,
            f"- processed meeting dirs under "
            f"`store/processed/meetings/`: **{len(meeting_dirs)}**",
        )
        missing_any = False
        for d in meeting_dirs[:50]:
            has_sr = (d / "source_record.json").is_file()
            if not has_sr:
                missing_any = True
            _emit(
                lines,
                f"  - `{d.name}` | source_record.json: "
                f"{'present' if has_sr else 'MISSING'}",
            )
        if len(meeting_dirs) > 50:
            _emit(lines, f"  - … (+{len(meeting_dirs) - 50} more)")

        # A transcript present in raw but with zero processed meetings is
        # also a "never ingested" signal.
        if transcripts and not meeting_dirs:
            missing_any = True
        SIGNALS["source_record_missing"] = missing_any

        baselines = list(
            (lake / "store" / "processed").rglob(
                "reference_baselines/opus_reference_minutes.jsonl"
            )
        )
        _emit(lines)
        _emit(
            lines,
            f"- existing Opus baseline JSONL files: **{len(baselines)}**",
        )

    return fn


# --------------------------------------------------------------------------- #
# Section 3 — artifact / governance validator output
# --------------------------------------------------------------------------- #
def _section_validators():
    def fn(lines: list[str]) -> None:
        _emit(lines, "`scripts/_gitignore_audit.py`:")
        _emit(lines)
        _emit(lines, "```")
        rc, out = _run([sys.executable, "scripts/_gitignore_audit.py"])
        _emit(lines, out or "(no output)")
        _emit(lines, f"exit code: {rc}")
        _emit(lines, "```")
        if rc != 0:
            SIGNALS["validator_failure"] = True

        _emit(lines)
        _emit(lines, "`scripts/_artifact_validator.py --check-latest`:")
        _emit(lines)
        _emit(lines, "```")
        validator = REPO_ROOT / "scripts" / "_artifact_validator.py"
        supports_check = False
        try:
            supports_check = "--check-latest" in validator.read_text(
                encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            supports_check = False
        if not supports_check:
            _emit(
                lines,
                "skipped: scripts/_artifact_validator.py exposes no "
                "--check-latest CLI (library module).",
            )
        else:
            rc2, out2 = _run(
                [sys.executable, "scripts/_artifact_validator.py",
                 "--check-latest"]
            )
            _emit(lines, out2 or "(no output)")
            _emit(lines, f"exit code: {rc2}")
            if rc2 != 0:
                SIGNALS["validator_failure"] = True
        _emit(lines, "```")

    return fn


# --------------------------------------------------------------------------- #
# Section 4 — recent FAIL/ERROR lines from run_history.jsonl
# --------------------------------------------------------------------------- #
def _section_run_history(lake: Path, exists: bool):
    def fn(lines: list[str]) -> None:
        if not exists:
            _emit(lines, "Data-lake not present — run_history scan skipped.")
            return
        proc_dir = lake / "store" / "processed" / "meetings"
        history_files = (
            sorted(proc_dir.rglob("run_history.jsonl"))
            if proc_dir.is_dir()
            else []
        )
        if not history_files:
            _emit(lines, "No run_history.jsonl found under the data-lake.")
            return
        matched: list[str] = []
        for hf in history_files:
            try:
                text = hf.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            for raw in text.splitlines():
                if any(tok in raw for tok in FAIL_TOKENS):
                    line = raw.strip()
                    if len(line) > 500:
                        line = line[:500] + " …(truncated)"
                    matched.append(f"{hf.parent.name}: {line}")
        _emit(
            lines,
            f"Scanned **{len(history_files)}** run_history.jsonl file(s); "
            f"**{len(matched)}** matching line(s). Showing last 50:",
        )
        _emit(lines)
        _emit(lines, "```")
        if matched:
            for line in matched[-50:]:
                _emit(lines, line)
        else:
            _emit(lines, "(no FAIL/ERROR lines found)")
        _emit(lines, "```")

    return fn


# --------------------------------------------------------------------------- #
# Section 5 — proposed next action (decision table)
# --------------------------------------------------------------------------- #
def _section_next_action():
    def fn(lines: list[str]) -> None:
        if SIGNALS["source_record_missing"]:
            action = (
                "source_record missing — re-run should now self-heal "
                "(PR #175 merged)"
            )
        elif not SIGNALS["anthropic_set"]:
            action = "Add ANTHROPIC_API_KEY as a repository secret"
        elif not SIGNALS["data_lake_exists"]:
            action = (
                "Data-lake not cloned — check DATA_LAKE_PATH secret and "
                "clone step"
            )
        elif SIGNALS["validator_failure"]:
            action = "Schema violation — read the validator output above"
        else:
            action = "Unknown — paste this summary into Claude for diagnosis"
        _emit(lines, f"**{action}**")

    return fn


def build_summary(workflow: str, job: str, data_lake_arg: str | None) -> str:
    lake, configured, exists = _resolve_data_lake(data_lake_arg)
    lines: list[str] = []
    _emit(lines, f"## Workflow failure debug — {workflow} / {job}")
    _emit(lines)
    _section(
        lines,
        "1. Failure context",
        _section_context(workflow, job, lake, configured, exists),
    )
    _section(lines, "2. Data-lake state", _section_data_lake(lake, exists))
    _section(lines, "3. Artifact validator output", _section_validators())
    _section(
        lines,
        "4. Recent FAIL/ERROR lines (run_history.jsonl)",
        _section_run_history(lake, exists),
    )
    # Section 5 must run last: it reads SIGNALS the earlier sections set.
    _section(lines, "5. Proposed next action", _section_next_action())
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(
            description="Emit a structured workflow-failure Step Summary."
        )
        parser.add_argument("--workflow", required=True)
        parser.add_argument("--job", required=True)
        parser.add_argument(
            "--data-lake",
            default=None,
            help="Override data-lake root (defaults to DATA_LAKE_PATH "
            "env or <repo>/data-lake).",
        )
        args = parser.parse_args(argv)
        summary = build_summary(args.workflow, args.job, args.data_lake)
    except SystemExit:
        # argparse error: still emit a minimal note, never hard-fail.
        summary = (
            "## Workflow failure debug\n\n"
            "> warning: could not parse arguments; emitting minimal summary.\n"
        )
    except Exception as exc:  # noqa: BLE001 - top-level fail-safe
        summary = (
            "## Workflow failure debug\n\n"
            f"> warning: debug script error "
            f"`{type(exc).__name__}: {exc}` (continuing).\n"
        )

    # Always print to stdout (shows in the CI log too); append to the
    # Step Summary when running under Actions.
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write(summary)
        except Exception as exc:  # noqa: BLE001
            print(f"> warning: could not write GITHUB_STEP_SUMMARY: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
