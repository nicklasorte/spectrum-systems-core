"""PipelineOrchestrator: scan -> compare -> run unprocessed transcripts.

Phase L.1. Replaces the manual "which transcripts have I run?" step. Given
a flat directory of transcript files (.docx and .txt) under
``<data_lake>/store/raw/transcripts/``, the orchestrator:

1. Lists transcripts found in that directory.
2. Detects on-disk evidence that each transcript has already been processed.
3. Runs the existing Phase A pipeline only on the unprocessed transcripts.
4. Writes one ``orchestration_run_record`` artifact per invocation.

Key invariants:

* Never raises. Every public entry point returns a dict.
* Unknown / ambiguous evidence => unprocessed (run again). SourceLoader is
  idempotent by content hash, so re-running is safe.
* ``scan()`` is strictly read-only. It performs zero file-system writes
  anywhere under the data lake.
* ``dry_run=True`` performs zero file-system writes anywhere under the
  data lake (no staging, no .docx extraction, no metadata.json
  regeneration, no orchestration_run_record).
* The orchestration_run_record is written on every non-dry-run invocation,
  including partial-failure runs and runs where every attempt failed. If
  the assembled record fails schema validation, a minimal fallback record
  is written instead so partial-failure evidence is never silently
  dropped.
* "Already processed" requires *content-hash agreement* between the
  current raw transcript and the stored ``source_record.payload.raw_hash``
  whenever such a hash is recorded. A mismatch is ambiguous evidence and
  is therefore treated as unprocessed (the transcript was edited or
  re-uploaded since last run).
* Two raw files that slugify to the same ``source_id`` are flagged as
  collisions and emitted as failures from ``run()`` (never silently
  overwritten).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema

from ..ingestion import (
    DocxExtractor,
    IngestionEval,
    ObsidianProjection,
    Promoter,
    SourceEval,
    SourceLoader,
)

TRANSCRIPTS_SUBDIR = ("raw", "transcripts")
DEFAULT_SOURCE_FAMILY = "meetings"
DEFAULT_SOURCE_TYPE = "meeting_transcript"
DEFAULT_DATE = "1970-01-01"
SCHEMA_VERSION = "1.4.0"
PRODUCED_BY = "PipelineOrchestrator"

# Phase L.3 — full pipeline stage chain.
STAGE_PROCESS_SOURCE = "process_source"
STAGE_EXTRACT_STORIES = "extract_stories"
STAGE_PROMOTE_KNOWLEDGE = "promote_knowledge"
STAGE_EXTRACT_CLAIMS = "extract_claims"
PIPELINE_STAGES = (
    STAGE_PROCESS_SOURCE,
    STAGE_EXTRACT_STORIES,
    STAGE_PROMOTE_KNOWLEDGE,
    STAGE_EXTRACT_CLAIMS,
)
STAGE_STATUS_SUCCESS = "success"
STAGE_STATUS_SKIPPED = "skipped"
STAGE_STATUS_FAILURE = "failure"
STAGE_STATUS_FORCED = "forced"
STAGE_STATUS_NOT_RUN = "not_run"
STAGE_STATUSES = (
    STAGE_STATUS_SUCCESS,
    STAGE_STATUS_SKIPPED,
    STAGE_STATUS_FAILURE,
    STAGE_STATUS_FORCED,
    STAGE_STATUS_NOT_RUN,
)
SYNTHESIZE_STATUS_SUCCESS = "success"
SYNTHESIZE_STATUS_SKIPPED = "skipped"
SYNTHESIZE_STATUS_FAILURE = "failure"
SYNTHESIZE_STATUS_NOT_RUN = "not_run"

_MINUTES_FILTER_REASON = (
    "filename_contains_minutes_keyword — "
    "file may belong in store/raw/minutes/ instead"
)

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slugify(name: str) -> str:
    lowered = name.lower().strip().replace(" ", "-")
    slug = _SLUG_RE.sub("-", lowered).strip("-_")
    return slug or "transcript"


def _hash_text_file(path: Path) -> str:
    """Return ``sha256:<hex>`` of a UTF-8 text file's content, or "" on error.

    Matches SourceLoader's ``raw_hash`` formula: sha256 over the UTF-8
    encoded text content (not raw bytes). Used to compare a current raw
    transcript against ``source_record.payload.raw_hash`` for staleness
    detection.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_docx_extracted(path: Path) -> str:
    """Return ``sha256:<hex>`` over the DocxExtractor's would-be output.

    Mirrors :class:`DocxExtractor`'s projection (paragraphs + table rows in
    document order, joined by ``\\n\\n``), so we can compare a .docx
    against an existing source_record's raw_hash without performing any
    file writes. Returns "" on any error so callers can treat as unknown
    evidence.
    """
    try:
        from docx import Document

        doc = Document(str(path))
        text, _chunks, _tables, _rows = DocxExtractor()._extract_body_text(doc)
    except Exception:
        return ""
    if not text.strip():
        return ""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_sdl_root(store_root: Path) -> Path:
    """Resolve the data-lake artifacts root. SDL_ROOT env var wins; else
    fall back to ``<store_root>/artifacts`` (the repo's conventional
    location used by Promoter's _LocalDataLake fallback).
    """
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        return Path(env)
    return store_root / "artifacts"


def _schema_path() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "contracts" / "schemas" / "orchestration"
        if candidate.is_dir():
            return candidate / "orchestration_run_record.schema.json"
    raise FileNotFoundError("orchestration schema directory not found")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


# Shape of a transcript_runner result:
#   {"status": "success" | "failure", "artifact_id": str, "reason": str}
TranscriptRunner = Callable[[Path, str, Path], Dict[str, Any]]

# Per-source stage runners (Stages 2-4). Each returns:
#   {"status": "success" | "failure", "reason": str}
StageRunner = Callable[[str, Path], Dict[str, Any]]

# Synthesize runner (Stage 5). Runs once per orchestrator invocation.
SynthesizeRunner = Callable[[Path], Dict[str, Any]]


class PipelineOrchestrator:
    """Detect and run unprocessed transcripts through the full pipeline.

    Phase L.3: extends the original Phase L.1 single-stage orchestrator
    to chain Stages 2-5. Each stage is independently injectable for
    testing; defaults wrap the production extractors.
    """

    def __init__(
        self,
        *,
        transcript_runner: Optional[TranscriptRunner] = None,
        docx_extractor: Optional[DocxExtractor] = None,
        ingestion_eval: Optional[IngestionEval] = None,
        extract_stories_runner: Optional[StageRunner] = None,
        promote_knowledge_runner: Optional[StageRunner] = None,
        extract_claims_runner: Optional[StageRunner] = None,
        synthesize_runner: Optional[SynthesizeRunner] = None,
    ) -> None:
        self._transcript_runner = transcript_runner or self._default_runner
        self._docx_extractor = docx_extractor or DocxExtractor()
        self._ingestion_eval = ingestion_eval or IngestionEval()
        self._extract_stories_runner = (
            extract_stories_runner or _default_extract_stories_runner
        )
        self._promote_knowledge_runner = (
            promote_knowledge_runner or _default_promote_knowledge_runner
        )
        self._extract_claims_runner = (
            extract_claims_runner or _default_extract_claims_runner
        )
        self._synthesize_runner = (
            synthesize_runner or _default_synthesize_runner
        )

    # -- public API --------------------------------------------------------

    def run_typed_extraction(
        self,
        source_id: str,
        *,
        data_lake_path: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Phase M3.0+M3.1. Run the typed-extraction pipeline for one source.

        This method is additive: it is NOT called by ``run()`` so existing
        Stage 2-5 behavior is unchanged. Invoke explicitly from the CLI
        (``extract-typed``) once chunks.jsonl exists for the source.

        Never raises.
        """
        try:
            from ..extraction.typed_extraction_runner import run_typed_extraction
            return run_typed_extraction(
                source_id, data_lake=data_lake_path, force=force,
            )
        except Exception as exc:  # defensive: never raise
            return {"status": "failure", "reason": f"unexpected_error:{exc}"}

    def scan(
        self, data_lake_path: str, force: bool = False
    ) -> Dict[str, Any]:
        try:
            return self._scan(data_lake_path, force=force)
        except Exception as exc:  # defensive: never raise
            return {
                "status": "failure",
                "unprocessed": [],
                "already_processed": [],
                "filtered_from_transcripts": [],
                "total_raw": 0,
                "total_processed": 0,
                "total_unprocessed": 0,
                "reason": f"unexpected_error:{exc}",
            }

    def run(
        self,
        data_lake_path: str,
        dry_run: bool = False,
        force: bool = False,
        *,
        force_only_missing: bool = False,
        specific_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the orchestration loop.

        Phase O.3 additions:
        - ``specific_source_id``: process only the named source_id.
        - ``force_only_missing``: when force=True, only reprocess source_ids
          that have no meeting_extraction artifact yet. Has no effect when
          force=False.
        """
        run_id = str(uuid.uuid4())
        try:
            return self._run(
                data_lake_path,
                dry_run=dry_run,
                run_id=run_id,
                force=force,
                force_only_missing=force_only_missing,
                specific_source_id=specific_source_id,
            )
        except Exception as exc:  # defensive: never raise
            return {
                "status": "failure",
                "dry_run": bool(dry_run),
                "force": bool(force),
                "force_only_missing": bool(force_only_missing),
                "specific_source_id": specific_source_id,
                "run_id": run_id,
                "processed_this_run": [],
                "skipped_already_done": [],
                "failed_this_run": [],
                "filtered_from_transcripts": [],
                "total_attempted": 0,
                "total_succeeded": 0,
                "total_failed": 0,
                "synthesize_status": SYNTHESIZE_STATUS_NOT_RUN,
                "total_stages_completed": 0,
                "total_stages_failed": 0,
                "source_ids_processed": [],
                "source_ids_skipped": [],
                "source_ids_failed": [],
                "orchestration_record_path": "",
                "reason": f"unexpected_error:{exc}",
            }

    # -- scan --------------------------------------------------------------

    def _scan(
        self, data_lake_path: str, force: bool = False
    ) -> Dict[str, Any]:
        if not data_lake_path:
            return _scan_failure("data_lake_path_required")
        root = Path(data_lake_path)
        if not root.exists():
            return _scan_failure(
                f"data_lake_path_not_found:{data_lake_path}"
            )

        store_root = root / "store"
        transcripts_dir = store_root.joinpath(*TRANSCRIPTS_SUBDIR)

        unprocessed: List[Dict[str, Any]] = []
        already_processed: List[Dict[str, Any]] = []

        if not transcripts_dir.is_dir():
            # No drop directory yet => nothing to do, but not a failure.
            return {
                "status": "success",
                "unprocessed": [],
                "already_processed": [],
                "filtered_from_transcripts": [],
                "total_raw": 0,
                "total_processed": 0,
                "total_unprocessed": 0,
                "reason": "",
            }

        # Index the existing on-disk evidence once.
        evidence = _build_processed_evidence(store_root)

        # Deterministic order: sorted by filename. .docx are paired with their
        # extracted .txt sibling (if present) so we don't double-count one
        # transcript under two extensions.
        all_files = sorted(
            [p for p in transcripts_dir.iterdir() if p.is_file()],
            key=lambda p: p.name,
        )

        # Filter out .docx/.txt files whose name contains "minutes"
        # (case-insensitive). These almost certainly belong in
        # store/raw/minutes/ — processing them as transcripts would
        # produce wrong artifacts. The filter is advisory only: filtered
        # files are NOT moved or deleted, and never counted as failures.
        files: List[Path] = []
        filtered_from_transcripts: List[Dict[str, Any]] = []
        for p in all_files:
            ext = p.suffix.lower()
            if ext in (".docx", ".txt") and "minutes" in p.name.lower():
                filtered_from_transcripts.append(
                    {
                        "filename": p.name,
                        "reason": _MINUTES_FILTER_REASON,
                    }
                )
                print(
                    f"⚠ Filtered from transcripts (contains 'minutes'): "
                    f"{p.name}"
                )
                continue
            files.append(p)
        seen_stems: set[str] = set()
        ordered: List[Path] = []
        # First pass: prefer .docx (they'll trigger extraction); skip .txt
        # whose stem already has a .docx peer in the directory.
        docx_stems = {p.stem for p in files if p.suffix.lower() == ".docx"}
        for p in files:
            ext = p.suffix.lower()
            if ext == ".docx":
                if p.stem not in seen_stems:
                    ordered.append(p)
                    seen_stems.add(p.stem)
            elif ext == ".txt":
                if p.stem in docx_stems:
                    continue  # extracted sibling of a .docx already counted
                if p.stem not in seen_stems:
                    ordered.append(p)
                    seen_stems.add(p.stem)
            # Other extensions ignored.

        # Detect slugify collisions across raw filenames first. A
        # collision means two distinct raw files would map to the same
        # source_id and therefore overwrite each other when staged. Per
        # Principle 3 (unknown = unprocessed) and to prevent silent data
        # loss, every member of a collision is flagged as unprocessed
        # with an explicit reason; run() will turn each into a failure.
        sid_to_paths: Dict[str, List[Path]] = {}
        for path in ordered:
            sid_to_paths.setdefault(_slugify(path.stem), []).append(path)
        collision_paths: set[str] = set()
        for sid, paths in sid_to_paths.items():
            if len(paths) > 1:
                for p in paths:
                    collision_paths.add(str(p))

        for path in ordered:
            source_id = _slugify(path.stem)
            entry_common = {
                "path": str(path),
                "filename": path.name,
            }

            if str(path) in collision_paths:
                others = [
                    p.name for p in sid_to_paths[source_id] if p != path
                ]
                unprocessed.append(
                    {
                        **entry_common,
                        "reason": (
                            "source_id_collision_with:"
                            + ",".join(sorted(others))
                        ),
                    }
                )
                continue

            ev = evidence.get(source_id)
            if ev is None:
                unprocessed.append(
                    {**entry_common, "reason": "no_processed_evidence"}
                )
                continue

            # Evidence exists. If a raw_hash was recorded, require it to
            # match the current raw transcript's hash. A mismatch
            # (transcript edited or re-uploaded) is ambiguous evidence
            # and routes to unprocessed. Empty stored hash falls back to
            # source_id-only matching (older records lack raw_hash).
            if ev.raw_hash:
                current_hash = _current_raw_hash(path)
                if not current_hash:
                    unprocessed.append(
                        {
                            **entry_common,
                            "reason": "raw_hash_unknown",
                        }
                    )
                    continue
                if current_hash != ev.raw_hash:
                    unprocessed.append(
                        {
                            **entry_common,
                            "reason": "raw_hash_mismatch",
                        }
                    )
                    continue

            # Force mode: Stage 1 evidence exists, but the operator wants
            # everything re-run. Surface as unprocessed with reason="forced"
            # so run() re-invokes Stage 1. Existing artifacts are not
            # deleted; underlying loaders are content-addressed.
            if force:
                unprocessed.append(
                    {
                        **entry_common,
                        "reason": "forced",
                        "prior_artifact_id": ev.artifact_id,
                    }
                )
                continue

            already_processed.append(
                {
                    **entry_common,
                    "artifact_id": ev.artifact_id,
                    "reason": ev.evidence_kind,
                }
            )

        return {
            "status": "success",
            "unprocessed": unprocessed,
            "already_processed": already_processed,
            "filtered_from_transcripts": filtered_from_transcripts,
            "total_raw": len(unprocessed) + len(already_processed),
            "total_processed": len(already_processed),
            "total_unprocessed": len(unprocessed),
            "reason": "",
        }

    # -- run ---------------------------------------------------------------

    def _run(
        self,
        data_lake_path: str,
        *,
        dry_run: bool,
        run_id: str,
        force: bool = False,
        force_only_missing: bool = False,
        specific_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        started_at = _now_iso()
        scan_result = self._scan(data_lake_path, force=force)
        if scan_result["status"] != "success":
            completed_at = _now_iso()
            return {
                "status": "failure",
                "dry_run": dry_run,
                "force": force,
                "force_only_missing": force_only_missing,
                "specific_source_id": specific_source_id,
                "run_id": run_id,
                "processed_this_run": [],
                "skipped_already_done": [],
                "failed_this_run": [],
                "filtered_from_transcripts": [],
                "total_attempted": 0,
                "total_succeeded": 0,
                "total_failed": 0,
                "synthesize_status": SYNTHESIZE_STATUS_NOT_RUN,
                "total_stages_completed": 0,
                "total_stages_failed": 0,
                "source_ids_processed": [],
                "source_ids_skipped": [],
                "source_ids_failed": [],
                "orchestration_record_path": "",
                "reason": f"scan_failed:{scan_result.get('reason', '')}",
            }

        store_root = Path(data_lake_path) / "store"
        unprocessed = scan_result["unprocessed"]
        already = scan_result["already_processed"]
        filtered_from_transcripts = scan_result.get(
            "filtered_from_transcripts", []
        )

        # Phase O.3 — specific_source_id filter wins over every other flag.
        # If provided, drop everything that does not match. The filtered-out
        # entries are NOT surfaced as failures (they are not "in scope").
        if specific_source_id:
            unprocessed = [
                e for e in unprocessed
                if _slugify(Path(e["filename"]).stem) == specific_source_id
            ]
            already = [
                e for e in already
                if _slugify(Path(e["filename"]).stem) == specific_source_id
            ]

        # Phase O.3 — force_only_missing: when force=True, skip transcripts
        # whose meeting_extraction artifact already exists. Without force,
        # the normal idempotency path already skips them — the flag is a
        # no-op. The fail-closed direction here is "process": if we can't
        # determine extraction existence we re-run.
        skipped_by_force_only_missing: List[Dict[str, Any]] = []
        if force and force_only_missing:
            keep_unprocessed: List[Dict[str, Any]] = []
            for entry in unprocessed:
                sid = _slugify(Path(entry["filename"]).stem)
                if _meeting_extraction_exists(store_root, sid):
                    print(
                        f"[orchestrator] meeting_extraction_exists_skipping: "
                        f"{entry['filename']} source_id={sid}",
                        flush=True,
                    )
                    skipped_by_force_only_missing.append(
                        {"filename": entry["filename"], "source_id": sid}
                    )
                else:
                    keep_unprocessed.append(entry)
            unprocessed = keep_unprocessed

        skipped_already_done = [
            {"filename": e["filename"], "artifact_id": e["artifact_id"]}
            for e in already
        ]

        processed_this_run: List[Dict[str, Any]] = []
        failed_this_run: List[Dict[str, Any]] = []
        results_for_record: List[Dict[str, Any]] = []

        # Track which transcripts reached Stage 2 success this run; Stage 5
        # fires iff at least one did (per task spec — "synthesize runs if
        # any stage 2 artifacts exist", verified via the artifact-existence
        # check in _run_one_stage so stale leftovers do NOT count).
        any_stage2_success_this_run = False
        any_stage4_success_this_run = False  # retained for record-keeping
        # Tally across all transcripts for the new aggregate fields.
        total_stages_completed = 0
        total_stages_failed = 0

        # Emit one results-row per filtered file so the run record has a
        # per-file trail (filtered files are also surfaced as the
        # aggregate `filtered_from_transcripts` array). They are NEVER
        # processed and NEVER counted as failures.
        for f in filtered_from_transcripts:
            results_for_record.append(
                {
                    "filename": f["filename"],
                    "status": "filtered",
                    "artifact_id": "",
                    "reason": f["reason"],
                    "eval_status": "not_run",
                    "pipeline_stages": _empty_pipeline_stages(),
                }
            )

        # Process unprocessed transcripts (Stage 1 needs running).
        for entry in unprocessed:
            filename = entry["filename"]
            path = Path(entry["path"])
            source_id = _slugify(path.stem)
            scan_reason = entry.get("reason", "")
            forced_entry = scan_reason == "forced"

            if dry_run:
                results_for_record.append(
                    {
                        "filename": filename,
                        "status": "would_run",
                        "artifact_id": "",
                        "reason": scan_reason or "dry_run",
                        "eval_status": "not_run",
                        "pipeline_stages": _empty_pipeline_stages(),
                    }
                )
                continue

            # source_id collisions never run — silent overwrite would
            # destroy data in raw/meetings/<sid>/. Fail explicitly.
            if scan_reason.startswith("source_id_collision_with:"):
                failed_this_run.append(
                    {"filename": filename, "reason": scan_reason}
                )
                pipeline_stages = _empty_pipeline_stages()
                pipeline_stages[STAGE_PROCESS_SOURCE] = STAGE_STATUS_FAILURE
                total_stages_failed += 1
                results_for_record.append(
                    {
                        "filename": filename,
                        "status": "failure",
                        "artifact_id": "",
                        "reason": scan_reason,
                        "eval_status": "not_run",
                        "pipeline_stages": pipeline_stages,
                    }
                )
                continue

            run_result = self._run_one(path, source_id, store_root)
            pipeline_stages = _empty_pipeline_stages()

            if run_result["status"] == "success":
                eval_status = self._maybe_run_ingestion_eval(
                    docx_path=path if path.suffix.lower() == ".docx" else None,
                    source_record=run_result.get("source_record"),
                    text_units=run_result.get("text_units"),
                    store_root=store_root,
                )
                entry_status = "success"
                if eval_status == "failed":
                    entry_status = "extraction_quality_warning"
                    print(
                        f"[orchestrator] extraction_quality_warning: {filename}"
                    )
                elif eval_status == "warning":
                    print(
                        f"[orchestrator] ingestion_eval_warning: {filename}"
                    )
                pipeline_stages[STAGE_PROCESS_SOURCE] = (
                    STAGE_STATUS_FORCED if forced_entry else STAGE_STATUS_SUCCESS
                )
                total_stages_completed += 1

                # Stages 2-4 chain — never blocks other transcripts.
                stages_2_4 = self._run_stages_2_to_4(
                    source_id=source_id,
                    store_root=store_root,
                    force=force,
                    filename=filename,
                )
                pipeline_stages.update(stages_2_4["pipeline_stages"])
                total_stages_completed += stages_2_4["completed"]
                total_stages_failed += stages_2_4["failed"]
                if stages_2_4["stage2_success"]:
                    any_stage2_success_this_run = True
                if stages_2_4["stage4_success"]:
                    any_stage4_success_this_run = True

                processed_this_run.append(
                    {
                        "filename": filename,
                        "status": entry_status,
                        "artifact_id": run_result["artifact_id"],
                    }
                )
                results_for_record.append(
                    {
                        "filename": filename,
                        "status": entry_status,
                        "artifact_id": run_result["artifact_id"],
                        "reason": (
                            ""
                            if entry_status == "success"
                            else "ingestion_eval_failed"
                        ),
                        "eval_status": eval_status,
                        "pipeline_stages": pipeline_stages,
                    }
                )
            else:
                pipeline_stages[STAGE_PROCESS_SOURCE] = STAGE_STATUS_FAILURE
                total_stages_failed += 1
                failed_this_run.append(
                    {
                        "filename": filename,
                        "reason": run_result["reason"],
                    }
                )
                results_for_record.append(
                    {
                        "filename": filename,
                        "status": "failure",
                        "artifact_id": run_result.get("artifact_id", ""),
                        "reason": run_result["reason"],
                        "eval_status": "not_run",
                        "pipeline_stages": pipeline_stages,
                    }
                )

        # Process already-Stage-1 transcripts. They skip Stage 1 but may
        # still need Stages 2-4 (if their existing artifacts were created
        # before those stages were added, OR if force=True).
        for entry in already:
            if dry_run:
                continue
            filename = entry["filename"]
            source_id = _slugify(Path(entry["filename"]).stem)
            pipeline_stages = _empty_pipeline_stages()
            pipeline_stages[STAGE_PROCESS_SOURCE] = STAGE_STATUS_SKIPPED
            stages_2_4 = self._run_stages_2_to_4(
                source_id=source_id,
                store_root=store_root,
                force=force,
                filename=filename,
            )
            pipeline_stages.update(stages_2_4["pipeline_stages"])
            total_stages_completed += stages_2_4["completed"]
            total_stages_failed += stages_2_4["failed"]
            if stages_2_4["stage2_success"]:
                any_stage2_success_this_run = True
            if stages_2_4["stage4_success"]:
                any_stage4_success_this_run = True

            results_for_record.append(
                {
                    "filename": filename,
                    "status": "skipped_already_done",
                    "artifact_id": entry["artifact_id"],
                    "reason": entry.get("reason", ""),
                    "eval_status": "not_run",
                    "pipeline_stages": pipeline_stages,
                }
            )

        # Stage 5 (synthesize) — runs once per invocation.
        # Sev-1 hazard guard: force alone does NOT trigger synthesize.
        # Synthesize runs iff at least one transcript reached Stage 2
        # success this invocation (per task spec: "synthesize runs if
        # any stage 2 artifacts exist"). The artifact-existence check
        # already filters out stale leftovers, so stage2_success_this_run
        # implies the chunks.jsonl was produced or freshly verified
        # during this run.
        synthesize_status = SYNTHESIZE_STATUS_NOT_RUN
        if not dry_run:
            if any_stage2_success_this_run:
                synth_result = self._run_synthesize(store_root)
                if synth_result["status"] == "success":
                    synthesize_status = SYNTHESIZE_STATUS_SUCCESS
                    total_stages_completed += 1
                else:
                    synthesize_status = SYNTHESIZE_STATUS_FAILURE
                    total_stages_failed += 1
                    print(
                        f"[orchestrator] synthesize_failed: "
                        f"{synth_result.get('reason', '')}"
                    )
            else:
                synthesize_status = SYNTHESIZE_STATUS_SKIPPED

        completed_at = _now_iso()

        if dry_run:
            overall_status = "dry_run"
        elif not unprocessed:
            overall_status = "success"
        elif failed_this_run and not processed_this_run:
            overall_status = "failure"
        elif failed_this_run and processed_this_run:
            overall_status = "partial"
        else:
            overall_status = "success"

        # Phase O.3 — by-source_id rollups for the run record.
        source_ids_processed = sorted({
            _slugify(Path(e["filename"]).stem) for e in processed_this_run
        })
        source_ids_failed = sorted({
            _slugify(Path(e["filename"]).stem) for e in failed_this_run
        })
        source_ids_skipped = sorted({
            *(_slugify(Path(e["filename"]).stem) for e in skipped_already_done),
            *(e["source_id"] for e in skipped_by_force_only_missing),
        })
        # Surface force-only-missing skips in the per-file results so the
        # run record carries one row per file the operator might ask about.
        for skip in skipped_by_force_only_missing:
            results_for_record.append({
                "filename": skip["filename"],
                "status": "skipped_already_done",
                "artifact_id": "",
                "reason": "meeting_extraction_exists_skipping",
                "eval_status": "not_run",
                "pipeline_stages": _empty_pipeline_stages(),
            })

        record_path = ""
        if not dry_run:
            record_path = self._write_run_record(
                store_root=store_root,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                data_lake_path=data_lake_path,
                transcripts_found=(
                    len(unprocessed) + len(already)
                    + len(skipped_by_force_only_missing)
                ),
                already_processed=(
                    len(already) + len(skipped_by_force_only_missing)
                ),
                attempted_this_run=len(unprocessed),
                succeeded_this_run=len(processed_this_run),
                failed_this_run=len(failed_this_run),
                results=results_for_record,
                filtered_from_transcripts=filtered_from_transcripts,
                status=overall_status,
                force=force,
                force_only_missing=force_only_missing,
                specific_source_id=specific_source_id,
                synthesize_status=synthesize_status,
                total_stages_completed=total_stages_completed,
                total_stages_failed=total_stages_failed,
                source_ids_processed=source_ids_processed,
                source_ids_skipped=source_ids_skipped,
                source_ids_failed=source_ids_failed,
            )

        return {
            "status": overall_status,
            "dry_run": dry_run,
            "force": force,
            "force_only_missing": force_only_missing,
            "specific_source_id": specific_source_id,
            "run_id": run_id,
            "processed_this_run": processed_this_run,
            "skipped_already_done": skipped_already_done
            + [
                {"filename": s["filename"], "artifact_id": ""}
                for s in skipped_by_force_only_missing
            ],
            "failed_this_run": failed_this_run,
            "filtered_from_transcripts": filtered_from_transcripts,
            "total_attempted": len(unprocessed),
            "total_succeeded": len(processed_this_run),
            "total_failed": len(failed_this_run),
            "synthesize_status": synthesize_status,
            "total_stages_completed": total_stages_completed,
            "total_stages_failed": total_stages_failed,
            "source_ids_processed": source_ids_processed,
            "source_ids_skipped": source_ids_skipped,
            "source_ids_failed": source_ids_failed,
            "orchestration_record_path": record_path,
            "reason": "",
            "results": results_for_record,
        }

    # -- Stages 2-4 chain --------------------------------------------------

    def _run_stages_2_to_4(
        self,
        *,
        source_id: str,
        store_root: Path,
        force: bool,
        filename: str,
    ) -> Dict[str, Any]:
        """Run Stages 2-4 in sequence with idempotency + dependency rules.

        Rules (per Phase L.3 spec):
        - Stage 2 fail => Stages 3 and 4 are NOT attempted (marked not_run).
          They depend on Stage 2 output (or at least share its precondition).
        - Stage 3 fail does NOT block Stage 4 (Stage 4 reads text_units
          directly, not knowledge artifacts).
        - Skipping (idempotency) is success-equivalent for downstream
          dependency checks: a skipped Stage 2 still allows Stages 3+4.
        - Force re-runs every stage regardless of existing artifacts;
          underlying modules overwrite their own working files (no rm).

        Returns:
          {
            "pipeline_stages": {extract_stories, promote_knowledge, extract_claims},
            "completed": int (stages with success or skipped),
            "failed": int,
            "stage2_success": bool,  # used to gate synthesize
            "stage4_success": bool,
          }
        """
        result_stages: Dict[str, str] = {
            STAGE_EXTRACT_STORIES: STAGE_STATUS_NOT_RUN,
            STAGE_PROMOTE_KNOWLEDGE: STAGE_STATUS_NOT_RUN,
            STAGE_EXTRACT_CLAIMS: STAGE_STATUS_NOT_RUN,
        }
        completed = 0
        failed = 0
        stage2_success = False
        stage4_success = False

        # Stage 2: extract-stories
        stage2 = self._run_one_stage(
            stage_name=STAGE_EXTRACT_STORIES,
            runner=self._extract_stories_runner,
            source_id=source_id,
            store_root=store_root,
            force=force,
            idempotency_check=_stage2_done,
            artifact_check=_stage2_artifact_exists,
            filename=filename,
        )
        result_stages[STAGE_EXTRACT_STORIES] = stage2["status"]
        if stage2["status"] in (STAGE_STATUS_SUCCESS, STAGE_STATUS_FORCED):
            completed += 1
            stage2_success = True
        elif stage2["status"] == STAGE_STATUS_SKIPPED:
            completed += 1
        elif stage2["status"] == STAGE_STATUS_FAILURE:
            failed += 1

        # Per task: Stage 2 failure => Stages 3+4 not attempted for this transcript.
        if stage2["status"] == STAGE_STATUS_FAILURE:
            return {
                "pipeline_stages": result_stages,
                "completed": completed,
                "failed": failed,
                "stage2_success": stage2_success,
                "stage4_success": stage4_success,
            }

        # Stage 3: promote-knowledge (KnowledgeSynthesizer)
        stage3 = self._run_one_stage(
            stage_name=STAGE_PROMOTE_KNOWLEDGE,
            runner=self._promote_knowledge_runner,
            source_id=source_id,
            store_root=store_root,
            force=force,
            idempotency_check=_stage3_done,
            artifact_check=_stage3_done,
            filename=filename,
        )
        result_stages[STAGE_PROMOTE_KNOWLEDGE] = stage3["status"]
        if stage3["status"] in (STAGE_STATUS_SUCCESS, STAGE_STATUS_FORCED, STAGE_STATUS_SKIPPED):
            completed += 1
        elif stage3["status"] == STAGE_STATUS_FAILURE:
            failed += 1

        # Stage 4: extract-claims (independent of Stage 3 — still attempted
        # even if Stage 3 failed).
        stage4 = self._run_one_stage(
            stage_name=STAGE_EXTRACT_CLAIMS,
            runner=self._extract_claims_runner,
            source_id=source_id,
            store_root=store_root,
            force=force,
            idempotency_check=_stage4_done,
            artifact_check=_stage4_done,
            filename=filename,
        )
        result_stages[STAGE_EXTRACT_CLAIMS] = stage4["status"]
        if stage4["status"] in (STAGE_STATUS_SUCCESS, STAGE_STATUS_FORCED):
            completed += 1
            stage4_success = True
        elif stage4["status"] == STAGE_STATUS_SKIPPED:
            completed += 1
        elif stage4["status"] == STAGE_STATUS_FAILURE:
            failed += 1

        return {
            "pipeline_stages": result_stages,
            "completed": completed,
            "failed": failed,
            "stage2_success": stage2_success,
            "stage4_success": stage4_success,
        }

    def _run_one_stage(
        self,
        *,
        stage_name: str,
        runner: StageRunner,
        source_id: str,
        store_root: Path,
        force: bool,
        idempotency_check: Callable[[Path, str], bool],
        artifact_check: Callable[[Path, str], bool],
        filename: str,
    ) -> Dict[str, Any]:
        """Run one stage with idempotency + post-run artifact verification.

        Artifact-as-evidence contract:

        - The on-disk artifact (e.g., ``stories/chunks.jsonl`` for Stage 2)
          is the PRIMARY success signal.
        - The runner's return ``status`` is the SECONDARY signal.
        - When they disagree, log a discrepancy and resolve as follows:

          * runner=success + artifact present → success (normal)
          * runner=success + artifact missing → FAILURE
              (warn: ``cli_success_artifact_missing``)
          * runner=failure + artifact newly produced this run → SUCCESS
              (warn: ``cli_failure_artifact_produced``)
          * runner=failure + artifact stale (pre-existed, no new output)
              → FAILURE (Sev-1 guard: a leftover artifact from a prior
              run is NOT evidence that THIS run succeeded)
          * runner=failure + artifact missing → FAILURE (normal)

        ``idempotency_check`` is the marker used to decide whether to
        skip the stage entirely on a re-run (no runner call, no
        artifact recheck). ``artifact_check`` is what we look for after
        the runner returns. They may differ — Stage 2 uses
        ``candidates.jsonl`` for idempotency (a fully-completed run
        wrote it) but ``chunks.jsonl`` for artifact evidence (deterministic
        Chunker output proves partial success even if StoryExtractor
        fails).

        Never raises.
        """
        already_done = idempotency_check(store_root, source_id)
        if already_done and not force:
            return {"status": STAGE_STATUS_SKIPPED, "reason": "already_done"}

        artifact_pre_existed = artifact_check(store_root, source_id)
        forced_run = force and already_done
        prefix = "[force] " if forced_run else ""
        print(f"  {prefix}{stage_name}: {filename} ...", flush=True)

        runner_ok = False
        runner_reason = ""
        try:
            result = runner(source_id, store_root)
            if not isinstance(result, dict):
                runner_reason = "runner_returned_non_dict"
            else:
                runner_ok = result.get("status") == "success"
                runner_reason = result.get(
                    "reason", "" if runner_ok else "stage_failed"
                )
        except Exception as exc:  # defensive: never raise
            runner_reason = f"unexpected_error:{exc}"

        artifact_post_exists = artifact_check(store_root, source_id)
        artifact_produced_this_run = (
            artifact_post_exists and not artifact_pre_existed
        )

        success_status = STAGE_STATUS_FORCED if forced_run else STAGE_STATUS_SUCCESS

        if runner_ok:
            if artifact_post_exists:
                return {"status": success_status, "reason": runner_reason}
            # CLI success but no artifact — log the discrepancy and fail.
            print(
                f"[orchestrator] {stage_name}_artifact_missing_despite_cli_success: "
                f"filename={filename} source_id={source_id}",
                flush=True,
            )
            return {
                "status": STAGE_STATUS_FAILURE,
                "reason": f"cli_success_artifact_missing:{runner_reason}",
            }

        # Runner failed.
        if artifact_produced_this_run:
            print(
                f"[orchestrator] {stage_name}_artifact_present_despite_cli_failure: "
                f"filename={filename} source_id={source_id} "
                f"runner_reason={runner_reason}",
                flush=True,
            )
            return {
                "status": success_status,
                "reason": f"cli_failure_artifact_produced:{runner_reason}",
            }

        if artifact_pre_existed and artifact_post_exists:
            # Sev-1 guard: a stale artifact from a prior run does NOT
            # count as new evidence. Surface the discrepancy.
            print(
                f"[orchestrator] {stage_name}_stale_artifact_not_treated_as_success: "
                f"filename={filename} source_id={source_id} "
                f"runner_reason={runner_reason}",
                flush=True,
            )
        return {
            "status": STAGE_STATUS_FAILURE,
            "reason": runner_reason or "stage_failed",
        }

    def _run_synthesize(self, store_root: Path) -> Dict[str, Any]:
        """Stage 5. Runs the synthesize runner once. Never raises."""
        print("  synthesize: (audience=technical, purpose=report) ...", flush=True)
        try:
            result = self._synthesize_runner(store_root)
        except Exception as exc:  # defensive
            return {
                "status": "failure",
                "reason": f"unexpected_error:{exc}",
            }
        if not isinstance(result, dict):
            return {
                "status": "failure",
                "reason": "runner_returned_non_dict",
            }
        return result

    def _run_one(
        self,
        path: Path,
        source_id: str,
        store_root: Path,
    ) -> Dict[str, Any]:
        """Run pipeline for one transcript file. Never raises."""
        try:
            txt_path = path
            if path.suffix.lower() == ".docx":
                extract_result = self._docx_extractor.extract(str(path))
                if extract_result.get("status") != "success":
                    return {
                        "status": "failure",
                        "artifact_id": "",
                        "reason": (
                            "docx_extract_failed:"
                            f"{extract_result.get('reason', '')}"
                        ),
                    }
                txt_path = Path(extract_result["output_path"])

            if not txt_path.is_file():
                return {
                    "status": "failure",
                    "artifact_id": "",
                    "reason": f"txt_not_found:{txt_path}",
                }

            return self._transcript_runner(txt_path, source_id, store_root)
        except Exception as exc:  # defensive: never raise
            return {
                "status": "failure",
                "artifact_id": "",
                "reason": f"unexpected_error:{exc}",
            }

    # -- default runner: stage + load + eval + promote ---------------------

    def _default_runner(
        self,
        txt_path: Path,
        source_id: str,
        store_root: Path,
    ) -> Dict[str, Any]:
        try:
            stage_result = _stage_transcript_into_meetings(
                txt_path=txt_path,
                source_id=source_id,
                store_root=store_root,
            )
            if stage_result["status"] != "success":
                return {
                    "status": "failure",
                    "artifact_id": "",
                    "reason": stage_result["reason"],
                }

            loader_result = SourceLoader().load(source_id, str(store_root))
            if loader_result["status"] != "success":
                return {
                    "status": "failure",
                    "artifact_id": "",
                    "reason": (
                        "source_loader_failed:"
                        f"{loader_result.get('reason', '')}"
                    ),
                }

            source_record = loader_result["source_record"]
            text_units = loader_result["text_units"]

            eval_result = SourceEval().run(
                source_record, text_units, repo_root=str(store_root)
            )
            if eval_result.get("decision") == "block":
                reasons = ",".join(eval_result.get("reason_codes", []))
                return {
                    "status": "failure",
                    "artifact_id": source_record.get("artifact_id", ""),
                    "reason": f"source_eval_blocked:{reasons}",
                }

            promote_result = Promoter().promote(source_record)
            if promote_result.get("status") != "success":
                return {
                    "status": "failure",
                    "artifact_id": source_record.get("artifact_id", ""),
                    "reason": (
                        "promote_failed:"
                        f"{promote_result.get('reason', '')}"
                    ),
                }

            try:
                ObsidianProjection().write_source_index(
                    source_record, text_units, str(store_root)
                )
            except Exception:
                # Projection is view-only; do not fail the run on it.
                pass

            return {
                "status": "success",
                "artifact_id": source_record["artifact_id"],
                "reason": "",
                "source_record": source_record,
                "text_units": text_units,
            }
        except Exception as exc:  # defensive: never raise
            return {
                "status": "failure",
                "artifact_id": "",
                "reason": f"unexpected_error:{exc}",
            }

    # -- ingestion eval ----------------------------------------------------

    def _maybe_run_ingestion_eval(
        self,
        *,
        docx_path: Optional[Path],
        source_record: Optional[Dict[str, Any]],
        text_units: Optional[List[Dict[str, Any]]],
        store_root: Path,
    ) -> str:
        """Run IngestionEval and write its result, returning the eval status.

        Returns one of "passed", "warning", "failed", or "not_run".
        Never raises and never blocks.
        """
        if docx_path is None or not isinstance(source_record, dict):
            return "not_run"
        try:
            result = self._ingestion_eval.evaluate(
                str(docx_path),
                source_record,
                text_units=text_units,
                repo_root=str(store_root),
            )
            sdl_root = _resolve_sdl_root(store_root)
            self._ingestion_eval.write_eval_result(
                result, sdl_root=str(sdl_root)
            )
            status = result.get("status", "not_run")
            if status not in ("passed", "warning", "failed"):
                return "not_run"
            return status
        except Exception:  # defensive: never raise
            return "not_run"

    # -- run-record write --------------------------------------------------

    def _write_run_record(
        self,
        *,
        store_root: Path,
        run_id: str,
        started_at: str,
        completed_at: str,
        data_lake_path: str,
        transcripts_found: int,
        already_processed: int,
        attempted_this_run: int,
        succeeded_this_run: int,
        failed_this_run: int,
        results: List[Dict[str, Any]],
        filtered_from_transcripts: List[Dict[str, Any]],
        status: str,
        force: bool = False,
        force_only_missing: bool = False,
        specific_source_id: Optional[str] = None,
        synthesize_status: str = SYNTHESIZE_STATUS_NOT_RUN,
        total_stages_completed: int = 0,
        total_stages_failed: int = 0,
        source_ids_processed: Optional[List[str]] = None,
        source_ids_skipped: Optional[List[str]] = None,
        source_ids_failed: Optional[List[str]] = None,
    ) -> str:
        record = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "dry_run": False,
            "force": force,
            "force_only_missing": bool(force_only_missing),
            "specific_source_id": specific_source_id,
            "data_lake_path": data_lake_path,
            "transcripts_found": transcripts_found,
            "already_processed": already_processed,
            "attempted_this_run": attempted_this_run,
            "succeeded_this_run": succeeded_this_run,
            "failed_this_run": failed_this_run,
            "filtered_from_transcripts": filtered_from_transcripts,
            "results": results,
            "source_ids_processed": list(source_ids_processed or []),
            "source_ids_skipped": list(source_ids_skipped or []),
            "source_ids_failed": list(source_ids_failed or []),
            "status": status,
            "synthesize_status": synthesize_status,
            "total_stages_completed": int(total_stages_completed),
            "total_stages_failed": int(total_stages_failed),
            "schema_version": SCHEMA_VERSION,
            "provenance": {"produced_by": PRODUCED_BY},
        }

        record_to_write = record
        try:
            schema = json.loads(_schema_path().read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(record)
        except (FileNotFoundError, OSError) as exc:
            # Schema unavailable. Continue and write the record anyway —
            # losing all partial-failure evidence is worse than writing an
            # unvalidated but well-formed record.
            record_to_write["reason_internal"] = (
                f"schema_unavailable:{exc}"
            )
        except jsonschema.ValidationError as exc:
            # Schema validation failed. Build a minimal-but-valid fallback
            # so partial-failure evidence is preserved on disk.
            fallback = {
                "run_id": run_id,
                "started_at": started_at,
                "completed_at": completed_at,
                "dry_run": False,
                "force": force,
                "force_only_missing": bool(force_only_missing),
                "specific_source_id": specific_source_id,
                "data_lake_path": data_lake_path or "unknown",
                "transcripts_found": int(transcripts_found),
                "already_processed": int(already_processed),
                "attempted_this_run": int(attempted_this_run),
                "succeeded_this_run": int(succeeded_this_run),
                "failed_this_run": int(failed_this_run),
                "filtered_from_transcripts": filtered_from_transcripts,
                "results": [],
                "source_ids_processed": list(source_ids_processed or []),
                "source_ids_skipped": list(source_ids_skipped or []),
                "source_ids_failed": list(source_ids_failed or []),
                "status": "failure",
                "synthesize_status": synthesize_status,
                "total_stages_completed": int(total_stages_completed),
                "total_stages_failed": int(total_stages_failed),
                "schema_version": SCHEMA_VERSION,
                "provenance": {"produced_by": PRODUCED_BY},
            }
            try:
                jsonschema.Draft202012Validator(schema).validate(fallback)
                record_to_write = fallback
                # Stash the original reason on a sibling file so we don't
                # lose the evidence entirely.
            except jsonschema.ValidationError:
                # Even the fallback failed validation. Skip the write.
                return ""

            sdl_root = _resolve_sdl_root(store_root)
            target_dir = sdl_root / "orchestration"
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{run_id}.json"
                target.write_text(
                    json.dumps(record_to_write, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                # Write the original (invalid) record alongside as
                # .invalid.json for forensics. Valid JSON: the
                # validation error is embedded as a top-level key so
                # downstream tooling can still parse the file.
                invalid_target = target_dir / f"{run_id}.invalid.json"
                forensic = {
                    "_validation_error": str(exc.message),
                    "original_record": record,
                }
                invalid_target.write_text(
                    json.dumps(forensic, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                return str(target)
            except OSError:
                return ""

        sdl_root = _resolve_sdl_root(store_root)
        target_dir = sdl_root / "orchestration"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{run_id}.json"
            target.write_text(
                json.dumps(record_to_write, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return str(target)
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meeting_extraction_exists(store_root: Path, source_id: str) -> bool:
    """Return True iff a meeting_extraction artifact exists for ``source_id``.

    Phase O.3 helper. Two file-naming patterns are accepted:

      1. ``<SDL_ROOT>/extractions/<source_id>_meeting_extraction.json``
         — the spec-level path the test suite seeds.
      2. ``<SDL_ROOT>/extractions/<source_artifact_id>_meeting_extraction.json``
         — what ``typed_extraction_runner`` writes today.

    The (2) path requires resolving source_id -> source_artifact_id via
    the on-disk source_record. If neither path can be checked, return
    False (fail-closed in the *process* direction: when we cannot tell
    whether an extraction exists, we re-run rather than silently skip).
    """
    if not source_id:
        return False
    sdl_root = _resolve_sdl_root(store_root)
    extractions_dir = sdl_root / "extractions"
    if not extractions_dir.is_dir():
        return False

    # Pattern (1) — fast path.
    direct = extractions_dir / f"{source_id}_meeting_extraction.json"
    if direct.is_file():
        return True

    # Pattern (2) — resolve via source_record.
    source_artifact_id = ""
    pd = _processed_dir(store_root, source_id)
    if pd is not None:
        record_path = pd / "source_record.json"
        if record_path.is_file():
            try:
                rec = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                rec = None
            if isinstance(rec, dict):
                aid = rec.get("artifact_id", "")
                if isinstance(aid, str):
                    source_artifact_id = aid

    if source_artifact_id:
        indirect = (
            extractions_dir
            / f"{source_artifact_id}_meeting_extraction.json"
        )
        if indirect.is_file():
            return True

    # Last resort: scan extractions/ and match by loaded source_artifact_id.
    # Cheap because the directory typically has O(N_transcripts) files.
    for path in extractions_dir.glob("*_meeting_extraction.json"):
        if not path.is_file():
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        # A meeting_extraction may store source_id directly (fixture path).
        if obj.get("source_id") == source_id:
            return True
        if (
            source_artifact_id
            and obj.get("source_artifact_id") == source_artifact_id
        ):
            return True
    return False


def _empty_pipeline_stages() -> Dict[str, str]:
    """Initial per-transcript stage map; all stages start as not_run."""
    return {
        STAGE_PROCESS_SOURCE: STAGE_STATUS_NOT_RUN,
        STAGE_EXTRACT_STORIES: STAGE_STATUS_NOT_RUN,
        STAGE_PROMOTE_KNOWLEDGE: STAGE_STATUS_NOT_RUN,
        STAGE_EXTRACT_CLAIMS: STAGE_STATUS_NOT_RUN,
    }


def _processed_dir(store_root: Path, source_id: str) -> Optional[Path]:
    """Locate processed/<family>/<sid>/ for any known family; None if absent.

    Mirrors `extraction._paths.find_processed_dir` without importing
    extraction at module load time (avoids circular imports).
    """
    try:
        from ..ingestion.source_loader import SOURCE_FAMILIES
    except Exception:
        SOURCE_FAMILIES = ("meetings",)
    for family in SOURCE_FAMILIES:
        candidate = store_root / "processed" / family / source_id
        if candidate.is_dir():
            return candidate
    return None


def _stage2_done(store_root: Path, source_id: str) -> bool:
    """Stage 2 (extract-stories) idempotency marker: candidates.jsonl exists.

    Used to decide whether to SKIP the stage on a re-run. A run that
    only produced ``chunks.jsonl`` (StoryExtractor failed before writing
    candidates) is still incomplete and must be retried, so the
    idempotency marker stays at ``candidates.jsonl``. The separate
    artifact-existence check (``_stage2_artifact_exists``) uses
    ``chunks.jsonl`` per the post-run verification contract.
    """
    pd = _processed_dir(store_root, source_id)
    if pd is None:
        return False
    return (pd / "stories" / "candidates.jsonl").is_file()


def _stage2_artifact_exists(store_root: Path, source_id: str) -> bool:
    """Stage 2 post-run artifact-evidence: stories/chunks.jsonl.

    Per the artifact-as-evidence contract: ``chunks.jsonl`` is the
    deterministic Chunker output that proves Stage 2 produced usable
    output even when downstream StoryExtractor / StoryEval steps fail
    (e.g., transient API errors). It is intentionally a weaker signal
    than the idempotency marker.
    """
    pd = _processed_dir(store_root, source_id)
    if pd is None:
        return False
    return (pd / "stories" / "chunks.jsonl").is_file()


def _stage3_done(store_root: Path, source_id: str) -> bool:
    """Stage 3 (knowledge) marker: any of concepts/themes/analogies.jsonl.

    Used as BOTH the idempotency marker and the post-run artifact-evidence
    check — KnowledgeSynthesizer writes all three on a successful run,
    and any one of them is sufficient evidence that the stage produced
    output.
    """
    pd = _processed_dir(store_root, source_id)
    if pd is None:
        return False
    kd = pd / "knowledge"
    return any(
        (kd / f"{t}.jsonl").is_file()
        for t in ("concepts", "themes", "analogies")
    )


def _stage4_done(store_root: Path, source_id: str) -> bool:
    """Stage 4 (extract-claims) marker: paper/claims.jsonl exists.

    Used as BOTH the idempotency marker and the post-run artifact-evidence
    check — ClaimExtractor writes ``claims.jsonl`` on a successful run.
    """
    pd = _processed_dir(store_root, source_id)
    if pd is None:
        return False
    return (pd / "paper" / "claims.jsonl").is_file()


def _default_extract_stories_runner(
    source_id: str, store_root: Path
) -> Dict[str, Any]:
    """Default Stage 2 runner: Chunker + StoryExtractor + StoryEval + Filter.

    Never raises. Returns ``{"status": "success"|"failure", "reason": str}``.
    Imports are lazy so import-time cost is paid only when the stage runs.
    """
    try:
        from ..extraction import (
            Chunker,
            StoryEval,
            StoryExtractor,
            StoryworthyFilter,
        )
        chunk_result = Chunker().chunk(source_id, str(store_root))
        if chunk_result.get("status") != "success":
            return {
                "status": "failure",
                "reason": f"chunker_failed:{chunk_result.get('reason', '')}",
            }
        extractor_result = StoryExtractor().extract_from_source(
            source_id, str(store_root)
        )
        if extractor_result.get("status") != "success":
            return {
                "status": "failure",
                "reason": (
                    "extractor_failed:"
                    f"{extractor_result.get('reason', '')}"
                ),
            }
        all_records = extractor_result.get("all_records", [])
        StoryEval().run(all_records, source_id, str(store_root))
        StoryworthyFilter().run_on_source(source_id, str(store_root))
        return {"status": "success", "reason": ""}
    except Exception as exc:  # defensive: never raise
        return {"status": "failure", "reason": f"unexpected_error:{exc}"}


def _default_promote_knowledge_runner(
    source_id: str, store_root: Path
) -> Dict[str, Any]:
    """Default Stage 3 runner: KnowledgeSynthesizer (concepts+themes+analogies).

    Reads stories/promoted/ (human-gated input). When there are no
    promoted stories, the synthesizer succeeds with zero records — that
    is the expected behavior for an automated pre-promotion run, not a
    failure.
    """
    try:
        from ..extraction import KnowledgeSynthesizer
        ks = KnowledgeSynthesizer()
        for method_name in (
            "synthesize_concepts",
            "synthesize_themes",
            "synthesize_analogies",
        ):
            method = getattr(ks, method_name)
            r = method(source_id, str(store_root))
            if r.get("status") != "success":
                return {
                    "status": "failure",
                    "reason": f"{method_name}:{r.get('reason', '')}",
                }
        return {"status": "success", "reason": ""}
    except Exception as exc:  # defensive
        return {"status": "failure", "reason": f"unexpected_error:{exc}"}


def _default_extract_claims_runner(
    source_id: str, store_root: Path
) -> Dict[str, Any]:
    """Default Stage 4 runner: claims + assumptions + evidence + contradictions."""
    try:
        from ..paper import (
            AssumptionExtractor,
            ClaimEval,
            ClaimExtractor,
            ContradictionDetector,
            EvidenceBuilder,
            EvidenceEval,
        )
        claim_result = ClaimExtractor().extract_from_source(
            source_id, str(store_root)
        )
        if claim_result.get("status") != "success":
            return {
                "status": "failure",
                "reason": (
                    "claim_extraction_failed:"
                    f"{claim_result.get('reason', '')}"
                ),
            }
        assumption_result = AssumptionExtractor().extract_from_source(
            source_id, str(store_root)
        )
        if assumption_result.get("status") != "success":
            return {
                "status": "failure",
                "reason": (
                    "assumption_extraction_failed:"
                    f"{assumption_result.get('reason', '')}"
                ),
            }
        claim_eval = ClaimEval().run(
            claim_result.get("claims", []),
            assumption_result.get("assumptions", []),
            source_id,
            str(store_root),
        )
        if claim_eval.get("decision") == "block":
            return {
                "status": "failure",
                "reason": (
                    "claim_eval_blocked:"
                    + ",".join(claim_eval.get("reason_codes", []))
                ),
            }
        evidence_result = EvidenceBuilder().build_for_source(
            source_id, str(store_root)
        )
        if evidence_result.get("status") != "success":
            return {
                "status": "failure",
                "reason": (
                    "evidence_build_failed:"
                    f"{evidence_result.get('reason', '')}"
                ),
            }
        cd = ContradictionDetector().run_on_source(
            source_id, str(store_root)
        )
        if cd.get("status") != "success":
            return {
                "status": "failure",
                "reason": (
                    "contradiction_detection_failed:"
                    f"{cd.get('reason', '')}"
                ),
            }
        # EvidenceEval is informational; do not block on warn-level eval.
        return {"status": "success", "reason": ""}
    except Exception as exc:  # defensive
        return {"status": "failure", "reason": f"unexpected_error:{exc}"}


def _default_synthesize_runner(store_root: Path) -> Dict[str, Any]:
    """Default Stage 5 runner: cli.synthesize with audience=technical, purpose=report.

    DATA_LAKE_PATH must point to the parent of ``store_root``; the runner
    sets it temporarily and restores the previous value afterwards.
    """
    try:
        from .. import cli as _cli  # lazy import to avoid circular load
        prev = os.environ.get("DATA_LAKE_PATH")
        os.environ["DATA_LAKE_PATH"] = str(store_root.parent)
        try:
            rc = _cli.synthesize(audience="technical", purpose="report")
        finally:
            if prev is None:
                os.environ.pop("DATA_LAKE_PATH", None)
            else:
                os.environ["DATA_LAKE_PATH"] = prev
        if rc != 0:
            return {"status": "failure", "reason": f"synthesize_exit:{rc}"}
        return {"status": "success", "reason": ""}
    except Exception as exc:  # defensive
        return {"status": "failure", "reason": f"unexpected_error:{exc}"}


def _scan_failure(reason: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "unprocessed": [],
        "already_processed": [],
        "filtered_from_transcripts": [],
        "total_raw": 0,
        "total_processed": 0,
        "total_unprocessed": 0,
        "reason": reason,
    }


class _Evidence:
    __slots__ = ("artifact_id", "evidence_kind", "raw_hash")

    def __init__(
        self, artifact_id: str, evidence_kind: str, raw_hash: str
    ) -> None:
        self.artifact_id = artifact_id
        self.evidence_kind = evidence_kind
        self.raw_hash = raw_hash


def _build_processed_evidence(store_root: Path) -> Dict[str, _Evidence]:
    """Index on-disk processed evidence keyed by source_id.

    Two evidence kinds:

    * ``processed_dir``: ``processed/<family>/<source_id>/source_record.json``
    * ``sdl_artifact``: ``<SDL_ROOT>/<artifact_id>.json`` whose
      ``payload.source_id`` matches.

    The recorded ``payload.raw_hash`` is captured (empty string if absent)
    so the caller can verify the evidence still matches the current raw
    transcript. A corrupt or unparseable JSON file is silently skipped at
    this layer (treated as no-evidence, which routes the transcript into
    "unprocessed" — the safe direction per Principle 3).
    """
    found: Dict[str, _Evidence] = {}

    def _record(existing: _Evidence | None, candidate: _Evidence) -> _Evidence:
        # If we have no prior entry, take the candidate.
        if existing is None:
            return candidate
        # If the prior entry already has a raw_hash, keep it. Otherwise
        # prefer a candidate that DOES carry a raw_hash so the staleness
        # check has something to compare against. Sev-2 fix from Gate B.
        if existing.raw_hash:
            return existing
        if candidate.raw_hash:
            return candidate
        return existing

    processed_root = store_root / "processed"
    if processed_root.is_dir():
        for family_dir in sorted(processed_root.iterdir()):
            if not family_dir.is_dir():
                continue
            for sid_dir in sorted(family_dir.iterdir()):
                if not sid_dir.is_dir():
                    continue
                record_path = sid_dir / "source_record.json"
                if not record_path.is_file():
                    continue
                try:
                    record = json.loads(
                        record_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                payload = record.get("payload") or {}
                rec_source_id = payload.get("source_id")
                if not isinstance(rec_source_id, str) or not rec_source_id:
                    continue
                aid = record.get("artifact_id", "")
                rh = payload.get("raw_hash", "")
                candidate = _Evidence(
                    aid if isinstance(aid, str) else "",
                    "processed_dir",
                    rh if isinstance(rh, str) else "",
                )
                found[rec_source_id] = _record(
                    found.get(rec_source_id), candidate
                )

    sdl_root = _resolve_sdl_root(store_root)
    if sdl_root.is_dir():
        for path in sorted(sdl_root.glob("*.json")):
            if not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            payload = record.get("payload") or {}
            rec_source_id = payload.get("source_id")
            if not isinstance(rec_source_id, str) or not rec_source_id:
                continue
            aid = record.get("artifact_id", "")
            rh = payload.get("raw_hash", "")
            candidate = _Evidence(
                aid if isinstance(aid, str) else "",
                "sdl_artifact",
                rh if isinstance(rh, str) else "",
            )
            found[rec_source_id] = _record(
                found.get(rec_source_id), candidate
            )

    return found


def _current_raw_hash(path: Path) -> str:
    """Hash that should match an existing source_record.payload.raw_hash.

    For .txt sources, this is sha256 over the file's UTF-8 content (the
    same formula SourceLoader uses).

    For .docx sources, this is sha256 over DocxExtractor's deterministic
    text projection, since that is what eventually becomes ``source.txt``
    and what SourceLoader hashes.
    """
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _hash_docx_extracted(path)
    return _hash_text_file(path)


def _stage_transcript_into_meetings(
    *,
    txt_path: Path,
    source_id: str,
    store_root: Path,
) -> Dict[str, Any]:
    """Copy a transcript .txt into raw/meetings/<source_id>/ with metadata.

    Idempotent: if the staged source.txt already exists with identical
    content, no rewrite happens. metadata.json is regenerated only if
    missing (so user-provided metadata is preserved).
    """
    try:
        target_dir = store_root / "raw" / DEFAULT_SOURCE_FAMILY / source_id
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            content = txt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return {
                "status": "failure",
                "reason": f"txt_read_error:{exc}",
            }

        target_txt = target_dir / "source.txt"
        existing = ""
        if target_txt.is_file():
            try:
                existing = target_txt.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing = ""
        if existing != content:
            target_txt.write_text(content, encoding="utf-8")

        metadata_path = target_dir / "metadata.json"
        if not metadata_path.is_file():
            metadata = {
                "source_id": source_id,
                "source_family": DEFAULT_SOURCE_FAMILY,
                "source_type": DEFAULT_SOURCE_TYPE,
                "title": txt_path.stem,
                "description": "",
                "date": DEFAULT_DATE,
                "author": "",
                "tags": [],
                "raw_format": "txt",
                "private_use_only": False,
            }
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        return {"status": "success", "reason": ""}
    except Exception as exc:  # defensive
        return {"status": "failure", "reason": f"stage_error:{exc}"}
