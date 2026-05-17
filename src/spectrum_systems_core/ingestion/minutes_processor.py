"""MinutesProcessor: meeting-minutes .docx -> minutes_record artifact.

Phase L.2 pre-processor. Reads .docx files from
``$DATA_LAKE_PATH/store/raw/minutes/``, extracts text via the existing
``DocxExtractor`` (REUSED — never reimplement), derives a ``meeting_date``
and ``meeting_name`` for ground-truth matching, and writes a
``minutes_record`` artifact to ``$SDL_ROOT/minutes/<minutes_id>.json``.

Never raises. Always returns a dict from every entry point.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import contracts_root
from .date_utils import (
    COMPACT_DATE_RE as _COMPACT_DATE_RE,
)
from .date_utils import (
    DAY_MONTH_YEAR_RE as _DAY_MONTH_YEAR_RE,
)
from .date_utils import (
    MONTH_DAY_YEAR_RE as _MONTH_DAY_YEAR_RE,
)
from .date_utils import (
    NUMERIC_DATE_RE as _NUMERIC_DATE_RE,
)
from .date_utils import (
    extract_meeting_date as _extract_date_from_string,
)
from .date_utils import (
    extract_prose_date as _extract_prose_date,
)
from .docx_extractor import DocxExtractor

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "MinutesProcessor"

# Patterns to strip from the filename when deriving the meeting_name.
# Order matters: strip the most specific suffixes first.
_NAME_STRIP_PATTERNS = [
    re.compile(r"\s*[-–—]\s*minutes?\b.*$", re.IGNORECASE),
    re.compile(r"\bminutes?\b.*$", re.IGNORECASE),
]


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_meeting_date(filename: str, text: str) -> str | None:
    """Extract a meeting_date as ``YYYY-MM-DD`` from filename, then body text.

    Filename is tried first via the shared ``date_utils.extract_meeting_date``
    (all four regex families). If no filename pattern matches, the first
    500 chars of ``text`` are scanned via ``date_utils.extract_prose_date``,
    which restricts itself to prose-style ``Month D, YYYY`` / ``D Mon YYYY``
    patterns to avoid mis-reading numeric references in minutes bodies.
    """
    fname = Path(filename).stem if filename else ""
    found = _extract_date_from_string(fname)
    if found is not None:
        return found
    return _extract_prose_date((text or "")[:500])


def extract_meeting_name(filename: str) -> str:
    """Strip date patterns and ``- minutes ...`` suffixes from the filename."""
    stem = Path(filename).stem if filename else ""
    name = stem
    name = _COMPACT_DATE_RE.sub(" ", name)
    name = _DAY_MONTH_YEAR_RE.sub(" ", name)
    name = _NUMERIC_DATE_RE.sub(" ", name)
    name = _MONTH_DAY_YEAR_RE.sub(" ", name)
    for pat in _NAME_STRIP_PATTERNS:
        name = pat.sub("", name)
    name = re.sub(r"[-–—_]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -–—_.")
    return name or stem or "untitled_minutes"


class MinutesProcessor:
    """Process meeting-minutes .docx files into minutes_record artifacts."""

    def __init__(self, docx_extractor: DocxExtractor | None = None) -> None:
        self._extractor = docx_extractor or DocxExtractor()

    def process(self, docx_path: str, data_lake_path: str) -> dict[str, Any]:
        try:
            return self._process(docx_path, data_lake_path)
        except Exception as exc:  # defensive: never raise
            return _failure(
                docx_path=docx_path,
                reason=f"unexpected_error:{exc}",
            )

    def process_directory(self, data_lake_path: str) -> list[dict[str, Any]]:
        """Process every .docx under ``store/raw/minutes/``.

        Returns ``[]`` when the directory is missing or empty — that is a
        valid pre-state, not an error.

        The result list mixes ``status="success"`` (newly written),
        ``status="skipped"`` (idempotent re-run with matching ``raw_hash``),
        and ``status="failure"`` / ``status="blocked"`` entries. Skipped
        entries carry ``skipped_reason="already_processed"``.
        """
        try:
            base = Path(data_lake_path) / "store" / "raw" / "minutes"
            if not base.is_dir():
                return []
            results: list[dict[str, Any]] = []
            for docx in sorted(base.glob("*.docx")):
                results.append(self.process(str(docx), data_lake_path))
            return results
        except Exception:  # defensive: never raise
            return []

    # -- internals ---------------------------------------------------------

    def _process(self, docx_path: str, data_lake_path: str) -> dict[str, Any]:
        src = Path(docx_path)
        if not src.is_file():
            return _failure(docx_path=docx_path, reason=f"file_not_found:{docx_path}")

        # Reuse DocxExtractor — never reimplement extraction.
        extract = self._extractor.extract(str(src))
        if extract.get("status") != "success":
            return _failure(
                docx_path=docx_path,
                reason=f"extract_failed:{extract.get('reason', '')}",
            )

        txt_path = extract["output_path"]
        try:
            text = Path(txt_path).read_text(encoding="utf-8")
        except OSError as exc:
            return _failure(
                docx_path=docx_path,
                reason=f"txt_read_error:{exc}",
                txt_path=txt_path,
            )

        meeting_date = extract_meeting_date(src.name, text)
        meeting_name = extract_meeting_name(src.name)
        raw_hash = "sha256:" + _sha256_hex(text.encode("utf-8"))

        # Idempotency: if a minutes_record with this raw_hash already
        # exists under SDL_ROOT/minutes/ (non-recursive — retired/ subdir
        # excluded), skip and return the existing artifact's metadata.
        # Failed runs never write an artifact, so there is no on-disk
        # "failed" record that could mask a re-run; failed inputs are
        # naturally reprocessed.
        sdl_root_lookup = _resolve_sdl_root(data_lake_path)
        if sdl_root_lookup is not None:
            existing = _find_existing_minutes_by_hash(sdl_root_lookup, raw_hash)
            if existing is not None:
                return _skipped(
                    docx_path=str(src),
                    txt_path=str(txt_path),
                    existing=existing,
                )

        minutes_id = str(uuid.uuid4())

        record: dict[str, Any] = {
            "minutes_id": minutes_id,
            "docx_path": str(src),
            "txt_path": str(txt_path),
            "meeting_date": meeting_date,
            "meeting_name": meeting_name,
            "text_unit_count": int(extract.get("paragraph_count", 0)),
            "character_count": int(extract.get("character_count", 0)),
            "table_count": int(extract.get("table_count", 0)),
            "raw_hash": raw_hash,
            "created_at": _now_iso(),
            "schema_version": SCHEMA_VERSION,
            "provenance": {"produced_by": PRODUCED_BY},
        }

        # Validate before any artifact write.
        try:
            schema = _load_schema()
        except (FileNotFoundError, OSError) as exc:
            return _failure(
                docx_path=docx_path,
                reason=f"schema_unreadable:{exc}",
                txt_path=txt_path,
                meeting_date=meeting_date,
                meeting_name=meeting_name,
            )
        try:
            jsonschema.Draft202012Validator(schema).validate(record)
        except jsonschema.ValidationError as exc:
            return _failure(
                docx_path=docx_path,
                reason=f"schema_violation:{exc.message}",
                txt_path=txt_path,
                meeting_date=meeting_date,
                meeting_name=meeting_name,
            )

        # Resolve the SDL_ROOT and write the artifact.
        sdl_root = _resolve_sdl_root(data_lake_path)
        if sdl_root is None:
            return _blocked(
                docx_path=docx_path,
                reason="sdl_root_unresolved:set SDL_ROOT or pass a valid data_lake_path",
                txt_path=txt_path,
                meeting_date=meeting_date,
                meeting_name=meeting_name,
                text_unit_count=record["text_unit_count"],
                character_count=record["character_count"],
                table_count=record["table_count"],
            )
        try:
            minutes_dir = sdl_root / "minutes"
            minutes_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = minutes_dir / f"{minutes_id}.json"
            artifact_path.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return _failure(
                docx_path=docx_path,
                reason=f"write_error:{exc}",
                txt_path=txt_path,
                meeting_date=meeting_date,
                meeting_name=meeting_name,
                text_unit_count=record["text_unit_count"],
                character_count=record["character_count"],
                table_count=record["table_count"],
            )

        return {
            "status": "success",
            "minutes_id": minutes_id,
            "artifact_path": str(artifact_path),
            "docx_path": str(src),
            "txt_path": str(txt_path),
            "meeting_date": meeting_date,
            "meeting_name": meeting_name,
            "text_unit_count": record["text_unit_count"],
            "character_count": record["character_count"],
            "table_count": record["table_count"],
            "reason": "",
        }


def _resolve_sdl_root(data_lake_path: str) -> Path | None:
    """Resolve the SDL_ROOT.

    Precedence: ``SDL_ROOT`` env var, else ``<data_lake_path>/store/artifacts``
    (matches ``orchestration._resolve_sdl_root``). Returns ``None`` if the
    parent path does not exist on disk.
    """
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            return None
    if not data_lake_path:
        return None
    base = Path(data_lake_path)
    if not base.exists():
        return None
    candidate = base / "store" / "artifacts"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return candidate


def _load_schema() -> dict[str, Any]:
    schema_file = (
        contracts_root() / "schemas" / "ingestion" / "minutes_record.schema.json"
    )
    return json.loads(schema_file.read_text(encoding="utf-8"))


def _failure(
    *,
    docx_path: str,
    reason: str,
    txt_path: str | None = None,
    meeting_date: str | None = None,
    meeting_name: str | None = None,
    text_unit_count: int = 0,
    character_count: int = 0,
    table_count: int = 0,
) -> dict[str, Any]:
    return {
        "status": "failure",
        "minutes_id": "",
        "artifact_path": None,
        "docx_path": docx_path,
        "txt_path": txt_path,
        "meeting_date": meeting_date,
        "meeting_name": meeting_name,
        "text_unit_count": text_unit_count,
        "character_count": character_count,
        "table_count": table_count,
        "reason": reason,
    }


def _blocked(**kwargs: Any) -> dict[str, Any]:
    result = _failure(**kwargs)
    result["status"] = "blocked"
    return result


def _find_existing_minutes_by_hash(
    sdl_root: Path, raw_hash: str
) -> dict[str, Any] | None:
    """Return summary of an existing minutes_record whose raw_hash matches.

    Reads ``<sdl_root>/minutes/*.json`` non-recursively (so files under
    ``minutes/retired/`` are excluded). Returns ``None`` if no match. A
    corrupt or unparseable JSON file is silently skipped (treated as not
    matching, which routes the caller to re-process — the safe direction).
    """
    minutes_dir = sdl_root / "minutes"
    if not minutes_dir.is_dir():
        return None
    for path in sorted(minutes_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("raw_hash") != raw_hash:
            continue
        return {
            "minutes_id": rec.get("minutes_id", "") or "",
            "artifact_path": str(path),
            "meeting_date": rec.get("meeting_date"),
            "meeting_name": rec.get("meeting_name") or "",
            "text_unit_count": int(rec.get("text_unit_count", 0) or 0),
            "character_count": int(rec.get("character_count", 0) or 0),
            "table_count": int(rec.get("table_count", 0) or 0),
        }
    return None


def _skipped(
    *,
    docx_path: str,
    txt_path: str,
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Result dict for an idempotent skip (already_processed).

    Carries the existing record's identifiers so callers can tell which
    artifact was matched without re-reading the file.
    """
    return {
        "status": "skipped",
        "minutes_id": existing["minutes_id"],
        "artifact_path": existing["artifact_path"],
        "docx_path": docx_path,
        "txt_path": txt_path,
        "meeting_date": existing["meeting_date"],
        "meeting_name": existing["meeting_name"],
        "text_unit_count": existing["text_unit_count"],
        "character_count": existing["character_count"],
        "table_count": existing["table_count"],
        "skipped_reason": "already_processed",
        "reason": "",
    }
