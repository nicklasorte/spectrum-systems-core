"""Phase Z.4 — corpus ingest (all 13 transcripts).

Reads ``config/corpus_manifest.yaml``, and for each transcript:

  1. Validates format — ``character_count >= 1000``, a ``MEETING``
     metadata header is present, and ``>= 10`` speaker turns
     (lines matching ``^[A-Z][A-Z ]+:``).
  2. On any failed check -> emits a ``transcript_ingest_result`` with
     ``status='blocked'``, ``reason='ingest_format_error'`` and a
     ``detail`` naming the failed check, logs it, and CONTINUES to the
     next transcript (one bad transcript never blocks the others).
  3. On pass -> runs the EXISTING deterministic speaker-turn chunker
     (``data_lake.chunker.chunk_transcript`` — reused, not
     re-implemented), emits a ``chunked_transcript`` artifact, and a
     ``transcript_ingest_result`` with ``status='present'`` whose
     ``chunked_transcript_artifact_id`` points at it.

The corpus roll-up (``corpus_ingest_summary``) is built ONLY AFTER
every transcript has been processed (this script is strictly
sequential — no parallel writers — so the last-write-wins race called
out in red-team Pass 1 #4 cannot drop a per-transcript result).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from _phase_z_lake import (  # noqa: E402
    data_lake_store_root,
    write_corpus_instrument,
    write_instrument,
)

from spectrum_systems_core.artifacts import new_artifact  # noqa: E402
from spectrum_systems_core.data_lake.chunker import (  # noqa: E402
    CHUNKER_VERSION_SPEAKER_TURN,
    chunk_transcript,
)

_REPO_ROOT = _SCRIPTS_DIR.parent
DEFAULT_MANIFEST = _REPO_ROOT / "config" / "corpus_manifest.yaml"

MIN_CHARACTER_COUNT = 1000
MIN_SPEAKER_TURNS = 10
SPEAKER_TURN_RE = re.compile(r"^[A-Z][A-Z ]+:", re.MULTILINE)
MEETING_HEADER_RE = re.compile(r"(?im)^\s*MEETING\b")

INGEST_RESULT_TYPE = "transcript_ingest_result"
CHUNKED_TYPE = "chunked_transcript"
SUMMARY_TYPE = "corpus_ingest_summary"
SCHEMA_VERSION = "1.0.0"


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def validate_transcript(
    text: str,
) -> tuple[bool, str | None, int, int]:
    """Return ``(ok, detail, speaker_turn_count, character_count)``.

    Every check runs so the result record carries the real counts
    even when blocked; ``detail`` names the FIRST failing check
    (deterministic order: chars -> header -> speaker turns).
    """
    character_count = len(text)
    speaker_turn_count = len(SPEAKER_TURN_RE.findall(text))
    if character_count < MIN_CHARACTER_COUNT:
        return (
            False,
            f"character_count_below_min:{character_count}<"
            f"{MIN_CHARACTER_COUNT}",
            speaker_turn_count,
            character_count,
        )
    if MEETING_HEADER_RE.search(text) is None:
        return (
            False,
            "missing_meeting_header",
            speaker_turn_count,
            character_count,
        )
    if speaker_turn_count < MIN_SPEAKER_TURNS:
        return (
            False,
            f"speaker_turn_count_below_min:{speaker_turn_count}<"
            f"{MIN_SPEAKER_TURNS}",
            speaker_turn_count,
            character_count,
        )
    return (True, None, speaker_turn_count, character_count)


def _ingest_result_payload(
    *,
    transcript_id: str,
    status: str,
    reason: str | None,
    detail: str | None,
    speaker_turn_count: int | None,
    character_count: int | None,
    chunked_id: str | None,
) -> dict[str, Any]:
    return {
        "artifact_type": INGEST_RESULT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "transcript_id": transcript_id,
        "produced_at": _now(),
        "status": status,
        "reason": reason,
        "detail": detail,
        "speaker_turn_count": speaker_turn_count,
        "character_count": character_count,
        "chunked_transcript_artifact_id": chunked_id,
    }


def _resolve_raw_path(template: str, lake_dir: str) -> Path:
    return Path(template.replace("{data_lake}", lake_dir))


def ingest_one(
    *,
    entry: dict[str, Any],
    lake_dir: str,
    store: Path,
    log: list[str],
) -> dict[str, Any]:
    """Process one manifest entry; return its transcript_ingest_result
    payload (also written to the data-lake)."""
    transcript_id = str(entry.get("id") or "")
    raw_template = str(entry.get("raw_path") or "")

    def _block(detail: str, stc: int | None, cc: int | None) -> dict:
        log.append(f"{transcript_id}: BLOCKED ({detail})")
        return _ingest_result_payload(
            transcript_id=transcript_id,
            status="blocked",
            reason="ingest_format_error",
            detail=detail,
            speaker_turn_count=stc,
            character_count=cc,
            chunked_id=None,
        )

    raw_path = _resolve_raw_path(raw_template, lake_dir)
    if not raw_path.is_file():
        result = _block("raw_transcript_missing", None, None)
        _persist_result(store, transcript_id, result)
        return result
    text = raw_path.read_text(encoding="utf-8")

    ok, detail, stc, cc = validate_transcript(text)
    if not ok:
        result = _block(detail or "ingest_format_error", stc, cc)
        _persist_result(store, transcript_id, result)
        return result

    chunks = chunk_transcript(text)
    chunk_payload = {
        "artifact_type": CHUNKED_TYPE,
        "schema_version": SCHEMA_VERSION,
        "transcript_id": transcript_id,
        "produced_at": _now(),
        "speaker_turn_count": stc,
        "character_count": cc,
        "chunker_version": CHUNKER_VERSION_SPEAKER_TURN,
        "chunks": [
            {
                "turn_id": c["turn_id"],
                "speaker": c.get("speaker"),
                "text": c.get("text", ""),
            }
            for c in chunks
        ],
    }
    validate_artifact(chunk_payload, CHUNKED_TYPE)
    chunk_art = new_artifact(
        artifact_type=CHUNKED_TYPE,
        payload=chunk_payload,
        trace_id=f"chunk-{transcript_id}",
        status="draft",
    )
    write_instrument(store, transcript_id, chunk_art)
    log.append(
        f"{transcript_id}: PRESENT ({len(chunks)} chunks, "
        f"{stc} speaker turns)"
    )
    result = _ingest_result_payload(
        transcript_id=transcript_id,
        status="present",
        reason=None,
        detail=None,
        speaker_turn_count=stc,
        character_count=cc,
        chunked_id=chunk_art.artifact_id,
    )
    _persist_result(store, transcript_id, result)
    return result


def _persist_result(
    store: Path, transcript_id: str, payload: dict[str, Any]
) -> None:
    validate_artifact(payload, INGEST_RESULT_TYPE)
    art = new_artifact(
        artifact_type=INGEST_RESULT_TYPE,
        payload=payload,
        trace_id=f"ingest-{transcript_id}",
        status="draft",
    )
    write_instrument(store, transcript_id, art)


def run_corpus_ingest(
    *, manifest_path: Path, lake_dir: str, store: Path, log: list[str]
) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("transcripts", []) if manifest else []

    # Strictly sequential: collect every per-transcript result FIRST,
    # only THEN build the roll-up (Pass 1 #4 — no concurrent writer
    # can drop a result because there is no concurrency).
    results: list[dict[str, Any]] = []
    for entry in entries:
        results.append(
            ingest_one(
                entry=entry, lake_dir=lake_dir, store=store, log=log
            )
        )

    blocked = [r for r in results if r["status"] == "blocked"]
    present = [r for r in results if r["status"] == "present"]
    summary = {
        "artifact_type": SUMMARY_TYPE,
        "schema_version": SCHEMA_VERSION,
        "produced_at": _now(),
        "total_transcripts": len(results),
        "present": len(present),
        "blocked": len(blocked),
        "blocked_ids": sorted(r["transcript_id"] for r in blocked),
    }
    validate_artifact(summary, SUMMARY_TYPE)
    summary_art = new_artifact(
        artifact_type=SUMMARY_TYPE,
        payload=summary,
        trace_id=f"corpus-ingest-{summary['produced_at']}",
        status="draft",
    )
    write_corpus_instrument(store, summary_art)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase Z.4 corpus ingest over config/corpus_manifest.yaml."
    )
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST),
        help="Corpus manifest path.",
    )
    parser.add_argument(
        "--lake", default=None,
        help="Data-lake root (defaults to $DATA_LAKE_PATH).",
    )
    args = parser.parse_args(argv)

    store = data_lake_store_root(args.lake)
    if store is None:
        print(
            json.dumps(
                {
                    "error": "environment_not_ready",
                    "detail": "data-lake not found at "
                    + (args.lake or os.environ.get("DATA_LAKE_PATH", "")),
                }
            )
        )
        return 1
    # The manifest's {data_lake} placeholder resolves to the data-lake
    # STORE root (the dir that actually holds raw/ and processed/ in
    # this repo's layout — data-lake/store/raw/transcripts/...), so a
    # manifest written as {data_lake}/raw/transcripts/<f>.txt lands at
    # <DATA_LAKE_PATH>/store/raw/transcripts/<f>.txt.
    lake_dir = str(store)

    log: list[str] = []
    try:
        summary = run_corpus_ingest(
            manifest_path=Path(args.manifest),
            lake_dir=lake_dir,
            store=store,
            log=log,
        )
    except (OSError, yaml.YAMLError, ArtifactValidationError) as exc:
        print(json.dumps({"error": "ingest_failed", "detail": str(exc)}))
        return 1

    for line in log:
        print(f"[z4] {line}", file=sys.stderr)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
