"""Phase AB.3 — three-point extraction comparison runner.

Compares the three extraction points on ONE meeting's transcript:

  1. regex  — the deterministic labelled-prefix extractor (baseline)
  2. haiku  — structured, schema-shaped LLM extraction
  3. opus   — unconstrained LLM extraction (opaque, ceiling)

It writes three measurement-instrument artifacts plus a Markdown
report. These are run-level records (like ``manifest__`` /
``debug__``), NOT promoted product artifacts: they are written even on
partial failure so a blocked run still explains itself, and they never
enter the artifact index.

Fail-closed order (red-team Pass 1):

  1. pre-flight ANTHROPIC_API_KEY (non-empty) — unless stub extractors
     are injected. Missing → exit 1, NO artifact written.
  2. transcript + ``source_record`` must exist on disk → otherwise
     ``source_record_missing`` BEFORE any API call, NO artifact written.
  3. run the three extractors; capture per-extractor status.
  4. always write extraction_comparison (+ telemetry + markdown);
     extraction_unconstrained only when Opus succeeded.
  5. any extractor failed → exit 1, comparison status ``rejected``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..artifacts import Artifact, new_artifact
from ..data_lake.loader import LoaderError, load_meeting
from ..data_lake.paths import processed_meeting_dir, validate_meeting_id
from ..data_lake.pipeline import (
    _DETERMINISTIC_CREATED_AT,
    _stable_artifact_id,
    source_record_path,
)
from ..data_lake.serialize import artifact_to_dict, canonical_json
from ..workflows.meeting_minutes import _build_base_payload
from . import llm_haiku, llm_opus

COMPARISON_TYPE = "extraction_comparison"
TELEMETRY_TYPE = "extraction_telemetry"
UNCONSTRAINED_TYPE = "extraction_unconstrained"

# Env flag that forces deterministic stub extractors (no network, no
# API key). Used by the stub-mode CLI/integration tests ONLY. Real
# runs never set it; the comparison runner's whole purpose is to make
# real API calls.
STUB_ENV_FLAG = "COMPARE_EXTRACTION_STUB"

_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(filename_stem: str) -> str:
    """Deterministic filename-stem -> meeting_id slug.

    Lowercase; every run of non-[a-z0-9] collapses to a single hyphen;
    leading/trailing hyphens stripped. Defined once here so the
    ``--transcript-file`` meeting_id derivation cannot drift from the
    contract.

    "7 GHz Downlink TIG Meeting Kickoff - transcript 20251218"
      -> "7-ghz-downlink-tig-meeting-kickoff-transcript-20251218"
    """
    s = filename_stem.lower()
    s = _SLUG_NON_ALNUM_RE.sub("-", s)
    s = s.strip("-")
    return s


class _StubOpusResult:
    """Deterministic in-runner Opus stub.

    ``llm_opus`` intentionally ships NO stub (Opus is comparison-only,
    never unit-tested through the adapter). Stub MODE still needs a
    third extractor so the 3-extractor write path is exercisable in CI
    without an API key; that stub lives here, not in the adapter, so
    the never-parse invariant around the adapter stays clean.
    """

    def __init__(self) -> None:
        self.raw_output = (
            "Decisions:\n"
            "- STUB opus decision\n"
            "Action Items:\n"
            "- STUB opus action (owner: stub)\n"
            "Open Questions:\n"
            "- STUB opus question?\n"
        )
        self.cost_usd = 0.0
        self.latency_ms = 0
        self.model = "stub"
        self.prompt = llm_opus.OPUS_EXTRACTION_PROMPT


def _stub_opus_extract(_transcript: str) -> _StubOpusResult:
    return _StubOpusResult()


def _preflight_credentials(
    env: Mapping[str, str], out, *, stub: bool
) -> bool:
    """Return True if it is safe to proceed.

    A missing OR empty/whitespace ANTHROPIC_API_KEY fails closed
    (red-team Pass 1: empty string is not a credential). Stub mode
    skips the check because it never calls the API.
    """
    if stub:
        return True
    key = env.get("ANTHROPIC_API_KEY")
    if not key or not key.strip():
        print("ERROR: missing_credentials:ANTHROPIC_API_KEY", file=out)
        print(
            "Set a non-empty ANTHROPIC_API_KEY in your environment before "
            "running compare-extraction. "
            "|retry: spectrum-core compare-extraction --lake <lake> "
            "--meeting-id <id>",
            file=out,
        )
        return False
    return True


def _load_source_record_chunks(
    lake_root: Path, meeting_id: str
) -> tuple[list[dict] | None, str | None]:
    """Return ``(chunks, error)``. ``error`` is a reason code string
    when the source_record is missing/malformed; ``chunks`` is None
    then. The runner refuses to proceed without it (red-team Pass 1)."""
    sr_path = source_record_path(lake_root, meeting_id)
    if not sr_path.is_file():
        return None, f"source_record_missing:{sr_path}"
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"source_record_unreadable:{exc}"
    if not isinstance(record, dict):
        return None, "source_record_invalid:not_a_json_object"
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None, "source_record_invalid:payload_not_a_dict"
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None, "source_record_invalid:chunks_missing_or_empty"
    return chunks, None


def _transcript_with_turn_ids(chunks: list[dict]) -> str:
    """Render the chunked transcript with explicit turn_ids so the
    Haiku prompt can cite source_turns. Deterministic given chunks."""
    lines: list[str] = []
    for c in chunks:
        turn_id = c.get("turn_id", "")
        speaker = c.get("speaker") or ""
        text = c.get("text", "")
        prefix = f"[{turn_id}]"
        if speaker:
            prefix += f" {speaker}:"
        lines.append(f"{prefix} {text}".rstrip())
    return "\n".join(lines)


def _regex_output(transcript_text: str) -> dict:
    """Existing deterministic labelled-prefix extractor, reshaped into
    the comparison ``{decisions, actions, questions}`` envelope. Reuses
    ``workflows.meeting_minutes._build_base_payload`` so the regex point
    is byte-identical to the production regex path."""
    base = _build_base_payload(transcript_text)
    return {
        "decisions": [{"text": d} for d in base["decisions"]],
        "actions": [{"text": a} for a in base["action_items"]],
        "questions": [{"text": q} for q in base["open_questions"]],
    }


def _new_instrument_artifact(
    artifact_type: str, payload: dict, trace_id: str
) -> Artifact:
    """Build a run-level instrument artifact with a stabilised id /
    created_at (same scheme the pipeline uses) so the structural
    identity is reproducible. Telemetry VALUES are real measurements
    and therefore not byte-stable across runs — documented in the
    artifact manifest."""
    art = new_artifact(
        artifact_type=artifact_type,
        payload=payload,
        trace_id=trace_id,
        status="draft",
        input_refs=[],
    )
    art.artifact_id = _stable_artifact_id(
        kind=artifact_type, trace_id=trace_id, payload=payload
    )
    art.created_at = _DETERMINISTIC_CREATED_AT
    return art


def _write_instrument(lake_root: Path, meeting_id: str, art: Artifact) -> Path:
    """Write a run-level instrument artifact directly (NOT via
    ``write_promoted_artifact``, which is promoted-only). Filename slug
    is the meeting_id so re-runs overwrite rather than accumulate."""
    target_dir = processed_meeting_dir(lake_root, meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{art.artifact_type}__{meeting_id}.json"
    target_path.write_text(
        canonical_json(artifact_to_dict(art)), encoding="utf-8"
    )
    return target_path


def run_compare_extraction(
    *,
    lake_root: Path | str,
    meeting_id: str | None = None,
    transcript_file: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    haiku_extract: Callable[[str], Any] | None = None,
    opus_extract: Callable[[str], Any] | None = None,
    stream=None,
) -> int:
    """Run the three-point comparison for one meeting.

    Source selection is mutually exclusive — provide exactly one of:

    * ``meeting_id`` — read the lake (transcript + chunked
      ``source_record``; fail-closed if either is missing).
    * ``transcript_file`` — read a flat transcript file directly;
      ``meeting_id`` is derived from the slugified filename stem and
      no ``source_record`` is required.

    Returns 0 only when all three extractors succeeded; 1 on any
    usage error, pre-flight failure, or extractor failure. Injected
    extractors are the test seam; production passes none and the real
    adapters are used after the fail-closed pre-flight gate.
    """
    out = stream if stream is not None else sys.stdout
    env = env if env is not None else os.environ
    lake_root = Path(lake_root)

    stub = str(env.get(STUB_ENV_FLAG, "")).strip().lower() in {
        "1", "true", "yes",
    }

    # 0. exactly one source selector. A usage error returns 1 with a
    #    clear message (NOT argparse's exit 2) so every entry point
    #    — direct call, cli.py, data_lake.cli — behaves identically.
    if (meeting_id is None) == (transcript_file is None):
        print(
            "ERROR: source_selector_invalid: provide exactly one of "
            "--meeting-id or --transcript-file",
            file=out,
        )
        return 1

    # 1. pre-flight credentials (skipped only in stub mode). Stays
    #    ahead of any transcript read so a credential-less invocation
    #    fails closed before touching disk in either mode.
    if not _preflight_credentials(env, out, stub=stub):
        return 1

    # 2. resolve the transcript source.
    if transcript_file is not None:
        tf = Path(transcript_file)
        if not tf.is_file():
            print(f"ERROR: transcript_file_not_found:{tf}", file=out)
            return 1
        transcript_bytes = tf.read_bytes()
        if not transcript_bytes.strip():
            print(f"ERROR: transcript_file_empty:{tf}", file=out)
            return 1
        meeting_id = slugify(tf.stem)
        try:
            validate_meeting_id(meeting_id)
        except ValueError as exc:
            print(
                f"ERROR: meeting_id_from_filename_invalid:{exc}", file=out
            )
            return 1
        try:
            transcript_text = transcript_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(f"ERROR: transcript_file_not_utf8:{exc}", file=out)
            return 1
        # No chunked source_record in flat-file mode: the Haiku point
        # sees the raw transcript (no turn ids to cite). Deterministic
        # given the same file bytes.
        transcript_turns = transcript_text
        transcript_hash = hashlib.sha256(transcript_bytes).hexdigest()
    else:
        # Existing --meeting-id path: unchanged. transcript +
        # source_record must exist BEFORE any API call.
        try:
            transcript_input = load_meeting(lake_root, meeting_id)
        except LoaderError as exc:
            print(f"ERROR: meeting_not_loadable:{exc}", file=out)
            return 1

        chunks, sr_error = _load_source_record_chunks(lake_root, meeting_id)
        if sr_error is not None:
            print(f"ERROR: {sr_error}", file=out)
            print(
                "Run `spectrum-core process-meeting --lake <lake> "
                f"--meeting-id {meeting_id}` first to produce source_record. "
                f"|retry: spectrum-core compare-extraction --lake {lake_root} "
                f"--meeting-id {meeting_id}",
                file=out,
            )
            return 1

        assert chunks is not None  # narrowed by sr_error is None
        transcript_text = transcript_input.transcript_text
        transcript_turns = _transcript_with_turn_ids(chunks)
        transcript_hash = transcript_input.transcript_hash

    trace_id = f"cmp-{transcript_hash[:16]}"

    if stub:
        haiku_extract = haiku_extract or llm_haiku.stub_extract
        opus_extract = opus_extract or _stub_opus_extract
    else:
        haiku_extract = haiku_extract or llm_haiku.real_extract
        opus_extract = opus_extract or llm_opus.real_extract

    status: dict[str, str] = {}
    telemetry: dict[str, dict] = {}

    # 3a. regex — deterministic, cannot fail on a loadable transcript.
    regex_out = _regex_output(transcript_text)
    status["regex"] = "ok"
    telemetry["regex"] = {"cost_usd": 0.0, "latency_ms": 0}

    # 3b. haiku.
    haiku_out: dict = {"decisions": [], "actions": [], "questions": []}
    try:
        hr = haiku_extract(transcript_turns)
        haiku_out = hr.output
        status["haiku"] = "ok"
        telemetry["haiku"] = {
            "cost_usd": hr.cost_usd,
            "latency_ms": hr.latency_ms,
            "model": hr.model,
        }
    except Exception as exc:  # noqa: BLE001 — record, never crash the run
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        status["haiku"] = f"failed:{reason}"
        telemetry["haiku"] = {
            "cost_usd": 0.0,
            "latency_ms": 0,
            "error": traceback.format_exc(limit=2),
        }

    # 3c. opus — unconstrained; raw text persisted to its own artifact.
    opus_ref: str | None = None
    opus_unconstrained: Artifact | None = None
    try:
        orr = opus_extract(transcript_text)
        unconstrained_payload = {
            "meeting_id": meeting_id,
            "raw_output": orr.raw_output,
            "model": orr.model,
            "prompt": orr.prompt,
            "cost_usd": orr.cost_usd,
            "latency_ms": orr.latency_ms,
        }
        opus_unconstrained = _new_instrument_artifact(
            UNCONSTRAINED_TYPE, unconstrained_payload, trace_id
        )
        opus_ref = opus_unconstrained.artifact_id
        status["opus"] = "ok"
        telemetry["opus"] = {
            "cost_usd": orr.cost_usd,
            "latency_ms": orr.latency_ms,
            "model": orr.model,
        }
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        status["opus"] = f"failed:{reason}"
        telemetry["opus"] = {
            "cost_usd": 0.0,
            "latency_ms": 0,
            "error": traceback.format_exc(limit=2),
        }

    all_ok = all(v == "ok" for v in status.values())

    # 4. always write the instrument artifacts.
    #
    # ``schema_version`` here is a PAYLOAD-level semantic-version marker
    # (string), distinct from the artifact envelope's integer
    # ``schema_version`` (the system constitution §6 binds the envelope
    # to an integer; it stays 1). Phase AC sets "1.1.0": the gap /
    # per-entity view layered on this raw record now has a per-entity
    # breakdown. An old artifact written before Phase AC carries no
    # payload ``schema_version`` and every reader treats its absence as
    # "1.0.0" and falls back to the aggregate-only view (red-team
    # Pass 1 item 5). The raw extractor outputs themselves are
    # unchanged, so a 1.0.0 reader of a 1.1.0 artifact also still works.
    comparison_payload = {
        "schema_version": "1.1.0",
        "meeting_id": meeting_id,
        "transcript_artifact_id": transcript_hash,
        "extractor_status": status,
        "regex_output": regex_out,
        "haiku_output": haiku_out,
        "opus_output_ref": opus_ref,
    }
    comparison = _new_instrument_artifact(
        COMPARISON_TYPE, comparison_payload, trace_id
    )
    comparison.status = "promoted" if all_ok else "rejected"

    telemetry_payload = {
        "meeting_id": meeting_id,
        "comparison_artifact_id": comparison.artifact_id,
        "regex": telemetry["regex"],
        "haiku": telemetry["haiku"],
        "opus": telemetry["opus"],
    }
    telemetry_art = _new_instrument_artifact(
        TELEMETRY_TYPE, telemetry_payload, trace_id
    )
    telemetry_art.status = comparison.status

    written: list[Path] = []
    if opus_unconstrained is not None:
        written.append(
            _write_instrument(lake_root, meeting_id, opus_unconstrained)
        )
    written.append(_write_instrument(lake_root, meeting_id, comparison))
    written.append(_write_instrument(lake_root, meeting_id, telemetry_art))

    # Markdown report (view-only, deterministic from the artifact +
    # opaque opus text passed verbatim — never parsed here).
    from ..data_lake.markdown_views import write_extraction_comparison_markdown

    opus_raw = (
        opus_unconstrained.payload["raw_output"]
        if opus_unconstrained is not None
        else ""
    )
    md_path = write_extraction_comparison_markdown(
        lake_root,
        meeting_id=meeting_id,
        comparison_payload=comparison.payload,
        telemetry_payload=telemetry_art.payload,
        opus_raw_text=opus_raw,
    )
    written.append(md_path)

    print(f"meeting_id: {meeting_id}", file=out)
    print(f"comparison status: {comparison.status}", file=out)
    for name in ("regex", "haiku", "opus"):
        print(f"  - {name}: {status[name]}", file=out)
    for p in written:
        print(f"wrote: {p}", file=out)

    return 0 if all_ok else 1


__all__ = [
    "COMPARISON_TYPE",
    "TELEMETRY_TYPE",
    "UNCONSTRAINED_TYPE",
    "STUB_ENV_FLAG",
    "slugify",
    "run_compare_extraction",
]
