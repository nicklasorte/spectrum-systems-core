"""SourceLoader: raw source file -> source_record + text_units.jsonl.

Deterministic. Fail-closed. No LLM calls. Stdlib + jsonschema only.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import jsonschema

from ._paths import contracts_root, schema_digest, schema_path

SOURCE_FAMILIES: Tuple[str, ...] = (
    "meetings",
    "books",
    "comments",
    "working_papers",
    "notes",
)

_COMPONENT_NAME = "source_loader"
_COMPONENT_VERSION = "1.0.0"

_SPEAKER_TURN_RE = re.compile(r"^[A-Z][A-Z\s]{1,40}:\s")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failure(reason: str, detail: str = "") -> Dict[str, Any]:
    msg = reason if not detail else f"{reason}: {detail}"
    return {
        "status": "failure",
        "source_record": None,
        "text_units": [],
        "reason": msg,
    }


class SourceLoader:
    """Ingest a raw source from raw/<family>/<source_id>/ into a source_record."""

    def load(self, source_id: str, repo_root: str) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()

        # 1. Locate the source directory under raw/<family>/<source_id>/
        source_dir, source_family = self._find_source_dir(repo_root_path, source_id)
        if source_dir is None:
            return _failure(
                "source_not_found",
                f"no raw/<family>/{source_id}/ directory under {repo_root_path}",
            )

        # 2. Read metadata.json
        metadata_path = source_dir / "metadata.json"
        if not metadata_path.is_file():
            return _failure(
                "metadata_unreadable",
                f"missing metadata.json at {metadata_path}",
            )
        try:
            metadata_bytes = metadata_path.read_bytes()
            metadata = json.loads(metadata_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return _failure("metadata_unreadable", str(exc))
        except json.JSONDecodeError as exc:
            return _failure("metadata_unreadable", f"invalid JSON: {exc}")
        if not isinstance(metadata, dict):
            return _failure(
                "metadata_unreadable", "metadata.json must be a JSON object"
            )

        # 4. Reject PDF in Phase A (checked before schema so the dedicated
        #    error code wins over the enum mismatch on raw_format).
        if metadata.get("raw_format") == "pdf":
            return _failure(
                "pdf_not_supported",
                "PDF sources are not supported in Phase A. Use Phase B PDF loader.",
            )

        # 3. Validate metadata schema
        try:
            metadata_schema = self._load_schema("source_metadata")
        except (FileNotFoundError, OSError) as exc:
            return _failure("metadata_schema_violation", str(exc))
        try:
            jsonschema.Draft202012Validator(metadata_schema).validate(metadata)
        except jsonschema.ValidationError as exc:
            return _failure("metadata_schema_violation", exc.message)

        # Cross-check: directory family must match metadata family.
        if metadata["source_family"] != source_family:
            return _failure(
                "metadata_schema_violation",
                f"source_family {metadata['source_family']!r} does not match "
                f"directory family {source_family!r}",
            )
        if metadata["source_id"] != source_id:
            return _failure(
                "metadata_schema_violation",
                f"metadata.source_id {metadata['source_id']!r} does not match "
                f"directory name {source_id!r}",
            )

        # 5. Locate the raw source file
        raw_format = metadata["raw_format"]
        candidates = []
        if raw_format == "txt":
            candidates = [source_dir / "source.txt", source_dir / "source.md"]
        elif raw_format == "md":
            candidates = [source_dir / "source.md", source_dir / "source.txt"]
        else:  # defensive: schema already restricts to txt/md (pdf rejected above)
            candidates = [source_dir / "source.txt", source_dir / "source.md"]

        raw_path: Path | None = None
        for candidate in candidates:
            if candidate.is_file():
                raw_path = candidate
                break
        if raw_path is None:
            return _failure(
                "source_file_not_found",
                f"neither source.txt nor source.md found in {source_dir}",
            )

        # 6. Read content as UTF-8
        try:
            raw_bytes = raw_path.read_bytes()
        except OSError as exc:
            return _failure("source_encoding_error", str(exc))
        try:
            raw_content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _failure("source_encoding_error", str(exc))
        if not raw_content.strip():
            return _failure(
                "source_empty",
                f"source file at {raw_path} is empty or whitespace-only",
            )

        # 7. raw_hash
        raw_hash = "sha256:" + _sha256_hex(raw_content.encode("utf-8"))

        # 8. Split into text units
        text_units = self._split_text_units(raw_content, source_family, source_id)

        # 9. execution_fingerprint_hash
        fingerprint_seed = (
            source_id + raw_hash + f"{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
        ).encode("utf-8")
        execution_fingerprint_hash = "sha256:" + _sha256_hex(fingerprint_seed)

        # 10. Assemble source_record
        try:
            schema_d = schema_digest("source_record")
        except (FileNotFoundError, OSError) as exc:
            return _failure("source_record_schema_violation", str(exc))

        raw_path_rel = self._rel(repo_root_path, raw_path)
        processed_dir = (
            repo_root_path / "processed" / source_family / source_id
        )
        processed_path_rel = self._rel(repo_root_path, processed_dir)

        source_record: Dict[str, Any] = {
            "artifact_kind": "source_record",
            "artifact_id": str(uuid.uuid4()),
            "created_at": _now_iso(),
            "schema_ref": {
                "name": "source_record",
                "version": "1.0.0",
                "digest": schema_d,
            },
            "trace": {
                "trace_id": uuid.uuid4().hex,
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": None,
            },
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [],
                "execution_fingerprint_hash": execution_fingerprint_hash,
            },
            "payload": {
                "source_id": source_id,
                "source_family": source_family,
                "source_type": metadata["source_type"],
                "title": metadata["title"],
                "metadata": metadata,
                "raw_path": raw_path_rel,
                "raw_hash": raw_hash,
                "text_unit_count": len(text_units),
                "processed_path": processed_path_rel,
            },
        }

        # 11. Validate source_record schema
        try:
            sr_schema = self._load_schema("source_record")
        except (FileNotFoundError, OSError) as exc:
            return _failure("source_record_schema_violation", str(exc))
        try:
            jsonschema.Draft202012Validator(sr_schema).validate(source_record)
        except jsonschema.ValidationError as exc:
            return _failure("source_record_schema_violation", exc.message)

        # 13. Validate every text_unit before any write.
        try:
            tu_schema = self._load_schema("text_unit")
        except (FileNotFoundError, OSError) as exc:
            return _failure("text_unit_schema_violation", str(exc))
        validator = jsonschema.Draft202012Validator(tu_schema)
        for unit in text_units:
            try:
                validator.validate(unit)
            except jsonschema.ValidationError as exc:
                return _failure(
                    "text_unit_schema_violation",
                    f"unit ordinal={unit.get('ordinal')}: {exc.message}",
                )

        # 12. Write source_record.json (after all validations pass).
        try:
            processed_dir.mkdir(parents=True, exist_ok=True)
            (processed_dir / "source_record.json").write_text(
                json.dumps(source_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # 13b. Write text_units.jsonl
            with (processed_dir / "text_units.jsonl").open(
                "w", encoding="utf-8"
            ) as fh:
                for unit in text_units:
                    fh.write(
                        json.dumps(unit, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
        except OSError as exc:
            return _failure("write_error", str(exc))

        return {
            "status": "success",
            "source_record": source_record,
            "text_units": text_units,
        }

    # ---------- helpers ----------

    def _find_source_dir(
        self, repo_root: Path, source_id: str
    ) -> Tuple[Path | None, str | None]:
        for family in SOURCE_FAMILIES:
            candidate = repo_root / "raw" / family / source_id
            if candidate.is_dir():
                return candidate, family
        return None, None

    def _rel(self, repo_root: Path, target: Path) -> str:
        try:
            return str(target.resolve().relative_to(repo_root)).replace(os.sep, "/")
        except ValueError:
            return str(target).replace(os.sep, "/")

    def _load_schema(self, name: str) -> Dict[str, Any]:
        return json.loads(schema_path(name).read_text(encoding="utf-8"))

    def _split_text_units(
        self, content: str, source_family: str, source_id: str
    ) -> List[Dict[str, Any]]:
        """Deterministic split. Meetings → speaker_turns, others → paragraphs."""
        if source_family == "meetings":
            units = self._split_speaker_turns(content, source_id)
            if units:
                return units
            # Fall back to paragraphs when no speaker turns are detected.
        return self._split_paragraphs(content, source_id)

    def _split_speaker_turns(
        self, content: str, source_id: str
    ) -> List[Dict[str, Any]]:
        lines = content.splitlines(keepends=True)
        # Collect (line_index_0_based, char_offset_0_based) for each line.
        line_offsets: List[int] = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line)

        # Find speaker-turn boundary lines.
        boundary_indices = [
            i for i, line in enumerate(lines) if _SPEAKER_TURN_RE.match(line)
        ]
        if not boundary_indices:
            return []

        units: List[Dict[str, Any]] = []
        ordinal = 0
        for idx, start_line in enumerate(boundary_indices):
            end_line = (
                boundary_indices[idx + 1] - 1
                if idx + 1 < len(boundary_indices)
                else len(lines) - 1
            )
            char_start = line_offsets[start_line]
            if end_line + 1 < len(lines):
                char_end = line_offsets[end_line + 1]
            else:
                char_end = len(content)
            chunk = content[char_start:char_end]
            stripped = chunk.strip()
            if not stripped:
                continue
            unit = self._build_unit(
                source_id=source_id,
                ordinal=ordinal,
                unit_type="speaker_turn",
                text=stripped,
                line_start=start_line,
                line_end=end_line,
                char_start=char_start,
                char_end=char_end,
            )
            units.append(unit)
            ordinal += 1
        return units

    def _split_paragraphs(
        self, content: str, source_id: str
    ) -> List[Dict[str, Any]]:
        lines = content.splitlines(keepends=True)
        line_offsets: List[int] = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line)

        # Group consecutive non-blank lines into paragraphs.
        units: List[Dict[str, Any]] = []
        ordinal = 0
        i = 0
        n = len(lines)
        while i < n:
            # Skip leading blank lines.
            if lines[i].strip() == "":
                i += 1
                continue
            start_line = i
            while i < n and lines[i].strip() != "":
                i += 1
            end_line = i - 1
            char_start = line_offsets[start_line]
            if end_line + 1 < n:
                char_end = line_offsets[end_line + 1]
            else:
                char_end = len(content)
            chunk = content[char_start:char_end]
            stripped = chunk.strip()
            if not stripped:
                continue
            unit = self._build_unit(
                source_id=source_id,
                ordinal=ordinal,
                unit_type="paragraph",
                text=stripped,
                line_start=start_line,
                line_end=end_line,
                char_start=char_start,
                char_end=char_end,
            )
            units.append(unit)
            ordinal += 1
        return units

    def _build_unit(
        self,
        *,
        source_id: str,
        ordinal: int,
        unit_type: str,
        text: str,
        line_start: int,
        line_end: int,
        char_start: int,
        char_end: int,
    ) -> Dict[str, Any]:
        return {
            "unit_id": str(uuid.uuid4()),
            "source_id": source_id,
            "unit_type": unit_type,
            "ordinal": ordinal,
            "text": text,
            "text_hash": "sha256:" + _sha256_hex(text.encode("utf-8")),
            "locator": {
                "line_start": line_start,
                "line_end": line_end,
                "char_start": char_start,
                "char_end": char_end,
            },
        }
