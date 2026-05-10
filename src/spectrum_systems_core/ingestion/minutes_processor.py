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
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ._paths import contracts_root
from .docx_extractor import DocxExtractor

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "MinutesProcessor"

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Numeric date in filenames: M-D-YY, M-D-YYYY, M_D_YY, M.D.YYYY, etc.
# Captures: month, day, year. Year may be 2-digit (treated 20xx) or 4-digit.
_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[-_./](\d{1,2})[-_./](\d{2}|\d{4})(?!\d)"
)
# Compact ISO-ish: YYYYMMDD.
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
# Day + month + year:  22Jan2026 / 22-Jan-2026 / 22 Jan 2026
_DAY_MONTH_YEAR_RE = re.compile(
    r"(?<![A-Za-z\d])(\d{1,2})[-_.\s]?([A-Za-z]{3,9})[-_.\s]?(\d{4})(?![A-Za-z\d])"
)
# Month + day + year in text: "January 22, 2026" / "January 22 2026"
_MONTH_DAY_YEAR_RE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)"
)

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


def _two_digit_to_full_year(y: int) -> int:
    # 00-79 -> 2000-2079, 80-99 -> 1980-1999. Conservative pivot for
    # working-paper history; 2026-era minutes always land in the 20xx half.
    if y < 100:
        return 2000 + y if y < 80 else 1900 + y
    return y


def _safe_iso_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return datetime.date(year, month, day).isoformat()
    except (ValueError, TypeError):
        return None


def extract_meeting_date(filename: str, text: str) -> Optional[str]:
    """Extract a meeting_date as YYYY-MM-DD from filename, then text.

    Priority:
      1. Filename: numeric (M-D-YY[YY]), compact (YYYYMMDD), day-month-year.
      2. First 500 chars of text: "Month D, YYYY" / "D Month YYYY".
      3. Otherwise None — caller treats as unmatched.

    Month-only patterns (e.g. ``Jan2026``) are intentionally NOT matched:
    fabricating a day-of-month would risk false matches in the linker.
    """
    fname = Path(filename).stem if filename else ""

    # Strict: compact YYYYMMDD wins on filenames.
    m = _COMPACT_DATE_RE.search(fname)
    if m:
        d = _safe_iso_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is not None:
            return d

    # Day-month-year style on filename.
    m = _DAY_MONTH_YEAR_RE.search(fname)
    if m:
        month_name = m.group(2).lower()
        if month_name in _MONTHS:
            d = _safe_iso_date(int(m.group(3)), _MONTHS[month_name], int(m.group(1)))
            if d is not None:
                return d

    # Numeric M-D-YY[YY] on filename.
    m = _NUMERIC_DATE_RE.search(fname)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = _two_digit_to_full_year(year)
        d = _safe_iso_date(year, month, day)
        if d is not None:
            return d

    # Fallback: first 500 chars of body text.
    head = (text or "")[:500]
    m = _MONTH_DAY_YEAR_RE.search(head)
    if m:
        month_name = m.group(1).lower()
        if month_name in _MONTHS:
            d = _safe_iso_date(int(m.group(3)), _MONTHS[month_name], int(m.group(2)))
            if d is not None:
                return d
    m = _DAY_MONTH_YEAR_RE.search(head)
    if m:
        month_name = m.group(2).lower()
        if month_name in _MONTHS:
            d = _safe_iso_date(int(m.group(3)), _MONTHS[month_name], int(m.group(1)))
            if d is not None:
                return d

    return None


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

    def __init__(self, docx_extractor: Optional[DocxExtractor] = None) -> None:
        self._extractor = docx_extractor or DocxExtractor()

    def process(self, docx_path: str, data_lake_path: str) -> Dict[str, Any]:
        try:
            return self._process(docx_path, data_lake_path)
        except Exception as exc:  # defensive: never raise
            return _failure(
                docx_path=docx_path,
                reason=f"unexpected_error:{exc}",
            )

    def process_directory(self, data_lake_path: str) -> List[Dict[str, Any]]:
        """Process every .docx under ``store/raw/minutes/``.

        Returns ``[]`` when the directory is missing or empty — that is a
        valid pre-state, not an error.
        """
        try:
            base = Path(data_lake_path) / "store" / "raw" / "minutes"
            if not base.is_dir():
                return []
            results: List[Dict[str, Any]] = []
            for docx in sorted(base.glob("*.docx")):
                results.append(self.process(str(docx), data_lake_path))
            return results
        except Exception:  # defensive: never raise
            return []

    # -- internals ---------------------------------------------------------

    def _process(self, docx_path: str, data_lake_path: str) -> Dict[str, Any]:
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
        minutes_id = str(uuid.uuid4())

        record: Dict[str, Any] = {
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


def _resolve_sdl_root(data_lake_path: str) -> Optional[Path]:
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


def _load_schema() -> Dict[str, Any]:
    schema_file = (
        contracts_root() / "schemas" / "ingestion" / "minutes_record.schema.json"
    )
    return json.loads(schema_file.read_text(encoding="utf-8"))


def _failure(
    *,
    docx_path: str,
    reason: str,
    txt_path: Optional[str] = None,
    meeting_date: Optional[str] = None,
    meeting_name: Optional[str] = None,
    text_unit_count: int = 0,
    character_count: int = 0,
    table_count: int = 0,
) -> Dict[str, Any]:
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


def _blocked(**kwargs: Any) -> Dict[str, Any]:
    result = _failure(**kwargs)
    result["status"] = "blocked"
    return result
