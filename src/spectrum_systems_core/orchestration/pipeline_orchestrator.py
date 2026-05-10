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
SCHEMA_VERSION = "1.1.0"
PRODUCED_BY = "PipelineOrchestrator"

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


class PipelineOrchestrator:
    """Detect and run unprocessed transcripts under a data-lake root."""

    def __init__(
        self,
        *,
        transcript_runner: Optional[TranscriptRunner] = None,
        docx_extractor: Optional[DocxExtractor] = None,
        ingestion_eval: Optional[IngestionEval] = None,
    ) -> None:
        self._transcript_runner = transcript_runner or self._default_runner
        self._docx_extractor = docx_extractor or DocxExtractor()
        self._ingestion_eval = ingestion_eval or IngestionEval()

    # -- public API --------------------------------------------------------

    def scan(self, data_lake_path: str) -> Dict[str, Any]:
        try:
            return self._scan(data_lake_path)
        except Exception as exc:  # defensive: never raise
            return {
                "status": "failure",
                "unprocessed": [],
                "already_processed": [],
                "total_raw": 0,
                "total_processed": 0,
                "total_unprocessed": 0,
                "reason": f"unexpected_error:{exc}",
            }

    def run(
        self,
        data_lake_path: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        try:
            return self._run(data_lake_path, dry_run=dry_run, run_id=run_id)
        except Exception as exc:  # defensive: never raise
            return {
                "status": "failure",
                "dry_run": bool(dry_run),
                "run_id": run_id,
                "processed_this_run": [],
                "skipped_already_done": [],
                "failed_this_run": [],
                "total_attempted": 0,
                "total_succeeded": 0,
                "total_failed": 0,
                "orchestration_record_path": "",
                "reason": f"unexpected_error:{exc}",
            }

    # -- scan --------------------------------------------------------------

    def _scan(self, data_lake_path: str) -> Dict[str, Any]:
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
        files = sorted(
            [p for p in transcripts_dir.iterdir() if p.is_file()],
            key=lambda p: p.name,
        )
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
    ) -> Dict[str, Any]:
        started_at = _now_iso()
        scan_result = self._scan(data_lake_path)
        if scan_result["status"] != "success":
            completed_at = _now_iso()
            return {
                "status": "failure",
                "dry_run": dry_run,
                "run_id": run_id,
                "processed_this_run": [],
                "skipped_already_done": [],
                "failed_this_run": [],
                "total_attempted": 0,
                "total_succeeded": 0,
                "total_failed": 0,
                "orchestration_record_path": "",
                "reason": f"scan_failed:{scan_result.get('reason', '')}",
            }

        store_root = Path(data_lake_path) / "store"
        unprocessed = scan_result["unprocessed"]
        already = scan_result["already_processed"]

        skipped_already_done = [
            {"filename": e["filename"], "artifact_id": e["artifact_id"]}
            for e in already
        ]

        processed_this_run: List[Dict[str, Any]] = []
        failed_this_run: List[Dict[str, Any]] = []
        results_for_record: List[Dict[str, Any]] = []

        for entry in unprocessed:
            filename = entry["filename"]
            path = Path(entry["path"])
            source_id = _slugify(path.stem)
            scan_reason = entry.get("reason", "")

            if dry_run:
                results_for_record.append(
                    {
                        "filename": filename,
                        "status": "would_run",
                        "artifact_id": "",
                        "reason": scan_reason or "dry_run",
                        "eval_status": "not_run",
                    }
                )
                continue

            # source_id collisions never run — silent overwrite would
            # destroy data in raw/meetings/<sid>/. Fail explicitly.
            if scan_reason.startswith("source_id_collision_with:"):
                failed_this_run.append(
                    {"filename": filename, "reason": scan_reason}
                )
                results_for_record.append(
                    {
                        "filename": filename,
                        "status": "failure",
                        "artifact_id": "",
                        "reason": scan_reason,
                        "eval_status": "not_run",
                    }
                )
                continue

            run_result = self._run_one(path, source_id, store_root)
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
                    # Advisory check failed (e.g., raw_hash drift). Still
                    # success — but the operator should see it.
                    print(
                        f"[orchestrator] ingestion_eval_warning: {filename}"
                    )
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
                    }
                )
            else:
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
                    }
                )

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

        record_path = ""
        if not dry_run:
            record_path = self._write_run_record(
                store_root=store_root,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                data_lake_path=data_lake_path,
                transcripts_found=len(unprocessed) + len(already),
                already_processed=len(already),
                attempted_this_run=len(unprocessed),
                succeeded_this_run=len(processed_this_run),
                failed_this_run=len(failed_this_run),
                results=results_for_record,
                status=overall_status,
            )

        return {
            "status": overall_status,
            "dry_run": dry_run,
            "run_id": run_id,
            "processed_this_run": processed_this_run,
            "skipped_already_done": skipped_already_done,
            "failed_this_run": failed_this_run,
            "total_attempted": len(unprocessed),
            "total_succeeded": len(processed_this_run),
            "total_failed": len(failed_this_run),
            "orchestration_record_path": record_path,
            "reason": "",
        }

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
        status: str,
    ) -> str:
        record = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "dry_run": False,
            "data_lake_path": data_lake_path,
            "transcripts_found": transcripts_found,
            "already_processed": already_processed,
            "attempted_this_run": attempted_this_run,
            "succeeded_this_run": succeeded_this_run,
            "failed_this_run": failed_this_run,
            "results": results,
            "status": status,
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
                "data_lake_path": data_lake_path or "unknown",
                "transcripts_found": int(transcripts_found),
                "already_processed": int(already_processed),
                "attempted_this_run": int(attempted_this_run),
                "succeeded_this_run": int(succeeded_this_run),
                "failed_this_run": int(failed_this_run),
                "results": [],
                "status": "failure",
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


def _scan_failure(reason: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "unprocessed": [],
        "already_processed": [],
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
