"""IngestionEval: post-extraction quality self-test.

Compares a .docx file to its produced source_record artifact and emits a
``source_eval_result``. Three required checks plus one advisory check.
Eval failures are visible (status == "failed") but never block — the
artifact is durable and a human can review.

Stdlib + jsonschema + python-docx. No LLM calls. Never raises.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import contracts_root
from .docx_extractor import DocxExtractor

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "IngestionEval"

# Thresholds. Tuned against the production evidence:
# - Header-only TIG meeting minutes (50KB .docx, ~11 short heading-only
#   text units): chars/bytes ~= 0.004 — far below 0.02.
# - Rich teams transcript (.docx full of paragraphs): chars/bytes ~= 0.15.
# - Header-only short-unit ratio: 1.0 (every unit is a section heading).
MIN_CHARS_PER_BYTE = 0.02
# Absolute character floor catches the small-but-hollow case (e.g. an 8KB
# header-only .docx whose ratio coincidentally clears 0.02). Real meeting
# transcripts run in the thousands of chars; 200 is far below any
# substantive minutes file.
MIN_TOTAL_CHARS = 200
MAX_SHORT_UNIT_RATIO = 0.8
# Only flag short_ratio>=0.8 as header-only when the *total* content is
# also small. A dialogue-heavy transcript with many short speaker turns
# can legitimately exceed the ratio while having plenty of substance.
MAX_TOTAL_CHARS_FOR_HEADER_FAIL = 2000
SHORT_UNIT_CHAR_THRESHOLD = 50


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check(name: str, *, passed: bool, required: bool, detail: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "required": required,
        "detail": detail,
    }


def _resolve_eval_dir(sdl_root_arg: str | None) -> Path | None:
    if sdl_root_arg:
        return Path(sdl_root_arg) / "evals"
    env = os.environ.get("SDL_ROOT", "").strip()
    if not env:
        return None
    return Path(env) / "evals"


class IngestionEval:
    """Run a self-test over (.docx, source_record) and emit a result dict."""

    def evaluate(
        self,
        docx_path: str,
        source_record: dict[str, Any],
        text_units: list[dict[str, Any]] | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        """Compare a .docx file to its produced source_record artifact.

        Args:
            docx_path: absolute path to the original .docx file.
            source_record: the source_record artifact dict produced by
                SourceLoader.
            text_units: optional already-loaded list of text_unit dicts.
                If omitted, units are loaded from
                ``<repo_root>/<processed_path>/text_units.jsonl``.
            repo_root: store-root used to resolve ``processed_path``. If
                omitted the current working directory is used.

        Never raises. Always returns a source_eval_result dict.
        """
        try:
            return self._evaluate(
                docx_path, source_record, text_units, repo_root
            )
        except Exception as exc:  # defensive: never raise
            return self._error_result(docx_path, source_record, exc)

    def _evaluate(
        self,
        docx_path: str,
        source_record: dict[str, Any],
        text_units_arg: list[dict[str, Any]] | None,
        repo_root: str | None,
    ) -> dict[str, Any]:
        docx = Path(docx_path) if isinstance(docx_path, str) and docx_path else None

        try:
            size_bytes = (
                docx.stat().st_size if docx is not None and docx.is_file() else 0
            )
        except OSError:
            size_bytes = 0

        payload = (
            source_record.get("payload", {})
            if isinstance(source_record, dict)
            else {}
        )
        if not isinstance(payload, dict):
            payload = {}

        recorded_unit_count = payload.get("text_unit_count", 0)
        if not isinstance(recorded_unit_count, int) or recorded_unit_count < 0:
            recorded_unit_count = 0

        text_units: list[dict[str, Any]] = []
        text_units_unloadable = False
        if text_units_arg is not None:
            for u in text_units_arg:
                if isinstance(u, dict):
                    text_units.append(u)
        else:
            text_units = self._load_text_units(payload, repo_root)
            # If the source_record claims units exist but we couldn't load
            # any from disk, distinguish that from "extraction was sparse".
            if recorded_unit_count > 0 and not text_units:
                text_units_unloadable = True

        # Authoritative count for ratios is the recorded count from the
        # source_record (the artifact is what we are evaluating). When
        # the producer didn't record a count we fall back to whatever we
        # could load.
        text_unit_count = (
            recorded_unit_count if recorded_unit_count > 0 else len(text_units)
        )

        character_count = sum(
            len(u.get("text", "")) if isinstance(u, dict) else 0
            for u in text_units
        )

        # Re-extract once: gives us both table_count for the artifact and
        # the recomputed text for the deterministic_extraction check.
        recomputed_text, recomputed_table_count = self._re_extract(docx)

        ratio = (character_count / size_bytes) if size_bytes > 0 else 0.0

        checks: list[dict[str, Any]] = []
        failure_reasons: list[str] = []
        warning_reasons: list[str] = []

        # CHECK-1: text_units_present (required)
        c1_passed = text_unit_count > 0
        checks.append(
            _check(
                "text_units_present",
                passed=c1_passed,
                required=True,
                detail=f"text_unit_count={text_unit_count}",
            )
        )
        if not c1_passed:
            failure_reasons.append("no_text_units_extracted")

        # CHECK-2: minimum_content_ratio (required). Pass requires both
        # ratio>=0.02 AND an absolute character floor — otherwise a tiny
        # header-only .docx whose XML overhead is small could slip past
        # the ratio gate alone.
        ratio_ok = ratio >= MIN_CHARS_PER_BYTE
        chars_ok = character_count >= MIN_TOTAL_CHARS
        c2_passed = ratio_ok and chars_ok
        c2_detail = (
            f"ratio={ratio:.6f} threshold={MIN_CHARS_PER_BYTE} "
            f"chars={character_count} min_chars={MIN_TOTAL_CHARS} "
            f"bytes={size_bytes}"
        )
        checks.append(
            _check(
                "minimum_content_ratio",
                passed=c2_passed,
                required=True,
                detail=c2_detail,
            )
        )
        if not c2_passed:
            if text_units_unloadable:
                failure_reasons.append(
                    "text_units_unloadable:processed_path_missing_or_unreadable"
                )
            else:
                failure_reasons.append(
                    f"extraction_too_sparse:ratio={ratio:.6f}"
                    f":threshold={MIN_CHARS_PER_BYTE}"
                    f":chars={character_count}"
                    f":min_chars={MIN_TOTAL_CHARS}"
                )

        # CHECK-3: not_header_only (required). Only fails when the
        # short-unit ratio is high AND the total content is small —
        # this avoids false-failing dialogue-heavy transcripts that
        # legitimately have many short speaker turns.
        if text_unit_count > 0:
            short_units = self._count_short_units(text_units, text_unit_count)
            short_ratio = short_units / text_unit_count
            ratio_high = short_ratio >= MAX_SHORT_UNIT_RATIO
            content_small = character_count < MAX_TOTAL_CHARS_FOR_HEADER_FAIL
            c3_passed = not (ratio_high and content_small)
            c3_detail = (
                f"short_ratio={short_ratio:.6f} threshold={MAX_SHORT_UNIT_RATIO} "
                f"chars={character_count} max_for_fail={MAX_TOTAL_CHARS_FOR_HEADER_FAIL}"
            )
        else:
            short_ratio = 1.0
            c3_passed = False
            c3_detail = "no_text_units_to_inspect"
        checks.append(
            _check(
                "not_header_only",
                passed=c3_passed,
                required=True,
                detail=c3_detail,
            )
        )
        if not c3_passed and text_unit_count > 0:
            failure_reasons.append(
                f"likely_header_only:short_ratio={short_ratio:.6f}"
                f":chars={character_count}"
            )

        # CHECK-4: deterministic_extraction (advisory)
        stored_raw_hash = payload.get("raw_hash")
        if not isinstance(stored_raw_hash, str):
            stored_raw_hash = ""
        if recomputed_text:
            recomputed_hash = "sha256:" + _sha256_hex(
                recomputed_text.encode("utf-8")
            )
        else:
            recomputed_hash = ""

        # Surface BOTH signals when both fail rather than letting the
        # "no_stored_raw_hash" branch silence a recompute failure.
        if not stored_raw_hash and not recomputed_hash:
            c4_passed = False
            c4_detail = "no_stored_raw_hash_and_recompute_failed"
        elif not stored_raw_hash:
            c4_passed = False
            c4_detail = "no_stored_raw_hash_in_source_record"
        elif not recomputed_hash:
            c4_passed = False
            c4_detail = f"recompute_failed:stored={stored_raw_hash}"
        else:
            c4_passed = recomputed_hash == stored_raw_hash
            c4_detail = (
                "match"
                if c4_passed
                else (
                    f"hash_mismatch:stored={stored_raw_hash}"
                    f":recomputed={recomputed_hash}"
                )
            )
        checks.append(
            _check(
                "deterministic_extraction",
                passed=c4_passed,
                required=False,
                detail=c4_detail,
            )
        )
        if not c4_passed:
            warning_reasons.append(c4_detail)

        eval_passed = not failure_reasons
        if failure_reasons:
            status = "failed"
        elif warning_reasons:
            status = "warning"
        else:
            status = "passed"

        source_artifact_id = ""
        if isinstance(source_record, dict):
            sid = source_record.get("artifact_id", "")
            if isinstance(sid, str):
                source_artifact_id = sid

        result_partial: dict[str, Any] = {
            "eval_id": str(uuid.uuid4()),
            "source_artifact_id": source_artifact_id,
            "docx_path": str(docx_path) if docx_path else "",
            "docx_file_size_bytes": int(size_bytes),
            "text_unit_count": int(text_unit_count),
            "character_count": int(character_count),
            "table_count": int(recomputed_table_count),
            "ratio_chars_per_byte": float(ratio),
            "checks": checks,
            "status": status,
            "eval_passed": bool(eval_passed),
            "failure_reasons": failure_reasons,
            "created_at": _now_iso(),
            "schema_version": SCHEMA_VERSION,
            "provenance": {"produced_by": PRODUCED_BY},
        }
        result_partial["content_hash"] = self._content_hash(result_partial)
        return result_partial

    # -- helpers -----------------------------------------------------------

    def _load_text_units(
        self,
        payload: dict[str, Any],
        repo_root: str | None,
    ) -> list[dict[str, Any]]:
        processed = payload.get("processed_path")
        if not isinstance(processed, str) or not processed:
            return []
        base = Path(processed)
        if not base.is_absolute():
            root = Path(repo_root) if repo_root else Path.cwd()
            base = root / base
        path = base / "text_units.jsonl"
        units: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        units.append(obj)
        except OSError:
            return []
        return units

    def _re_extract(self, docx: Path | None) -> tuple[str, int]:
        """Re-extract via DocxExtractor's body walker.

        Returns ``(text, table_count)``. ``text`` is empty on any failure.
        """
        if docx is None or not docx.is_file():
            return "", 0
        try:
            from docx import Document  # local import keeps test mocking cheap.

            document = Document(str(docx))
            text, _chunks, table_count, _rows = (
                DocxExtractor()._extract_body_text(document)
            )
        except Exception:
            return "", 0
        return text, int(table_count)

    def _count_short_units(
        self,
        text_units: list[dict[str, Any]],
        text_unit_count: int,
    ) -> int:
        """Return the number of text units shorter than the threshold.

        Falls back to ``text_unit_count`` when the units list is empty:
        we cannot prove the units are *not* header-only without seeing
        them, so the conservative answer is "all short". This makes the
        check fail-closed when the producer didn't write text_units.
        """
        if not text_units:
            return text_unit_count
        short = 0
        for unit in text_units:
            text = unit.get("text", "") if isinstance(unit, dict) else ""
            if not isinstance(text, str):
                text = ""
            if len(text.strip()) < SHORT_UNIT_CHAR_THRESHOLD:
                short += 1
        # If we have fewer units than the recorded count, treat the rest
        # as short (fail-closed).
        if len(text_units) < text_unit_count:
            short += text_unit_count - len(text_units)
        return short

    def _content_hash(self, partial: dict[str, Any]) -> str:
        canonical = json.dumps(
            {k: v for k, v in partial.items() if k != "content_hash"},
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + _sha256_hex(canonical.encode("utf-8"))

    def _error_result(
        self,
        docx_path: str,
        source_record: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any]:
        source_artifact_id = ""
        if isinstance(source_record, dict):
            sid = source_record.get("artifact_id", "")
            if isinstance(sid, str):
                source_artifact_id = sid
        partial: dict[str, Any] = {
            "eval_id": str(uuid.uuid4()),
            "source_artifact_id": source_artifact_id,
            "docx_path": str(docx_path) if docx_path else "",
            "docx_file_size_bytes": 0,
            "text_unit_count": 0,
            "character_count": 0,
            "table_count": 0,
            "ratio_chars_per_byte": 0.0,
            "checks": [
                _check(
                    "text_units_present",
                    passed=False,
                    required=True,
                    detail=f"unexpected_error:{exc}",
                ),
            ],
            "status": "failed",
            "eval_passed": False,
            "failure_reasons": [f"unexpected_error:{exc}"],
            "created_at": _now_iso(),
            "schema_version": SCHEMA_VERSION,
            "provenance": {"produced_by": PRODUCED_BY},
        }
        partial["content_hash"] = self._content_hash(partial)
        return partial

    # -- I/O ---------------------------------------------------------------

    def write_eval_result(
        self,
        eval_result: dict[str, Any],
        sdl_root: str | None = None,
    ) -> str:
        """Write the eval result to ``$SDL_ROOT/evals/<sid>_ingestion_eval.json``.

        Returns the absolute path written. Returns "" when SDL_ROOT
        cannot be resolved or any I/O fails. Emits a one-line stderr
        warning on the SDL_ROOT-unset path so a misconfigured deploy
        doesn't silently lose every eval result. Never raises.
        """
        import sys
        sid = ""
        eid = ""
        if isinstance(eval_result, dict):
            sid = eval_result.get("source_artifact_id", "") or ""
            eid = eval_result.get("eval_id", "") or ""
        try:
            base = _resolve_eval_dir(sdl_root)
            if base is None:
                print(
                    "[IngestionEval] warning: SDL_ROOT unset; eval result "
                    f"not persisted (source_artifact_id={sid!r} "
                    f"eval_id={eid!r})",
                    file=sys.stderr,
                )
                return ""
            base.mkdir(parents=True, exist_ok=True)
            name_part = sid or eid
            target = base / f"{name_part}_ingestion_eval.json"
            target.write_text(
                json.dumps(eval_result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return str(target)
        except Exception as exc:  # defensive: never raise
            try:
                print(
                    f"[IngestionEval] warning: write failed: {exc} "
                    f"(source_artifact_id={sid!r} eval_id={eid!r})",
                    file=sys.stderr,
                )
            except Exception:
                pass
            return ""

    def schema_validate(self, eval_result: dict[str, Any]) -> bool:
        try:
            schema_file = (
                contracts_root()
                / "schemas"
                / "ingestion"
                / "source_eval_result.schema.json"
            )
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(eval_result)
            return True
        except (FileNotFoundError, OSError, jsonschema.ValidationError):
            return False
