"""Phase O.0 — verify-pipeline-state.

Scan ``$SDL_ROOT`` and the data-lake's ``store/`` tree, classify every
JSON artifact by ``artifact_type`` (or ``artifact_kind`` as a legacy
fallback), validate against the matching contract schema, and emit a
``pipeline_state_record`` artifact.

The module is intentionally read-only over the rest of the data lake. The
only path it writes to is ``$SDL_ROOT/verifications/`` (or its
data-lake-relative fallback).

Never raises. Empty SDL_ROOT is a finding, not a silent success.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "verify-pipeline-state"

# Files that are explicitly NOT pipeline artifacts and must be skipped
# during the scan. They live inside the data lake / repo but represent
# config or harness state, not artifacts produced by the loop. These
# names match exactly (case-insensitive).
_CONFIG_FILENAMES = {
    "settings.json",
    "settings.local.json",
    "config.json",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "manifest.json",
    "eval_run_count.json",
    "version.json",
}

# Directories we skip entirely when scanning under SDL_ROOT or the lake.
# Hidden directories (".claude", ".github", ".git") are always skipped.
_SKIP_DIR_NAMES = {
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}

# Filename suffixes that indicate forensics / debug sidecars produced by
# the runner itself. We never validate these against schemas.
_SIDECAR_SUFFIXES = (".invalid.json",)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _contracts_root() -> Path:
    """Locate the contracts/ directory by walking up from this file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts"
        if (candidate / "schemas").is_dir():
            return candidate
    # Fallback to package install location.
    return Path(__file__).resolve().parents[3] / "contracts"


def _resolve_sdl_root(data_lake_path: Optional[str]) -> Optional[Path]:
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        return Path(env)
    if data_lake_path:
        return Path(data_lake_path) / "store" / "artifacts"
    return None


def _resolve_store_root(data_lake_path: Optional[str]) -> Optional[Path]:
    if not data_lake_path:
        return None
    base = Path(data_lake_path) / "store"
    return base if base.exists() else None


def _is_config_or_sidecar(path: Path) -> bool:
    if path.name.lower() in _CONFIG_FILENAMES:
        return True
    for suffix in _SIDECAR_SUFFIXES:
        if path.name.endswith(suffix):
            return True
    return False


def _iter_json_files(root: Path) -> List[Path]:
    """Walk ``root`` and yield JSON files we should consider as artifacts.

    Skips hidden directories, build/cache dirs, and known non-artifact
    config files. Order is deterministic (sorted by full path).
    """
    if not root.is_dir():
        return []
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate in place so os.walk honors the skip.
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in _SKIP_DIR_NAMES
        )
        for fname in sorted(filenames):
            if not fname.endswith(".json"):
                continue
            p = Path(dirpath) / fname
            if _is_config_or_sidecar(p):
                continue
            out.append(p)
    return sorted(out)


def _index_schemas(contracts_root: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index schemas by (artifact_type_const, schema_version_const).

    We rely on each schema having an ``artifact_type`` property with a
    ``const`` and a ``schema_version`` property with a ``const``.
    Schemas that don't match this shape are skipped — they were either
    written before artifact_type was introduced or describe non-artifact
    payloads, and we don't auto-validate them.
    """
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    schemas_root = contracts_root / "schemas"
    if not schemas_root.is_dir():
        return out
    for schema_path in sorted(schemas_root.rglob("*.schema.json")):
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(schema, dict):
            continue
        props = schema.get("properties") or {}
        at_prop = props.get("artifact_type")
        sv_prop = props.get("schema_version")
        if not isinstance(at_prop, dict) or not isinstance(sv_prop, dict):
            continue
        at = at_prop.get("const")
        sv = sv_prop.get("const")
        if not isinstance(at, str) or not isinstance(sv, str):
            continue
        # First wins on ties (we don't expect any since artifact_type
        # uniquely identifies a schema; this is defensive).
        out.setdefault((at, sv), schema)
    return out


def _classify_artifact(
    obj: Any,
) -> Tuple[Optional[str], Optional[str], str, str]:
    """Return (artifact_type, schema_version, kind_flag, raw_kind).

    ``kind_flag`` is one of:
      - "type_only"  — only artifact_type present
      - "kind_only"  — only artifact_kind present (legacy)
      - "both"       — both fields present
      - "neither"    — neither present (not a pipeline artifact)

    The returned ``artifact_type`` prefers ``artifact_type`` and falls
    back to ``artifact_kind`` only when the new field is missing. The
    ``raw_kind`` is the literal ``artifact_kind`` value (for surfacing
    in mismatch warnings).
    """
    if not isinstance(obj, dict):
        return (None, None, "neither", "")
    at = obj.get("artifact_type")
    ak = obj.get("artifact_kind")
    sv = obj.get("schema_version")
    sv_norm = sv if isinstance(sv, str) else None

    has_type = isinstance(at, str) and bool(at)
    has_kind = isinstance(ak, str) and bool(ak)

    if has_type and has_kind:
        flag = "both"
        artifact_type = at
    elif has_type:
        flag = "type_only"
        artifact_type = at
    elif has_kind:
        flag = "kind_only"
        artifact_type = ak
    else:
        flag = "neither"
        artifact_type = None

    return (artifact_type, sv_norm, flag, ak if isinstance(ak, str) else "")


def _validate_against_schema(
    schema: Optional[Dict[str, Any]], obj: Dict[str, Any]
) -> str:
    """Return 'valid', 'invalid', or 'schema_not_found'."""
    if schema is None:
        return "schema_not_found"
    try:
        jsonschema.Draft202012Validator(schema).validate(obj)
        return "valid"
    except jsonschema.ValidationError:
        return "invalid"
    except jsonschema.SchemaError:
        # The schema file itself is broken — surface as not_found so it
        # shows up in next_required_actions without crashing the scan.
        return "schema_not_found"


def _is_chunks_jsonl_path(p: Path) -> bool:
    return p.name == "chunks.jsonl"


def _count_chunks_jsonl(store_root: Optional[Path]) -> int:
    if store_root is None:
        return 0
    processed = store_root / "processed"
    if not processed.is_dir():
        return 0
    n = 0
    for p in processed.rglob("chunks.jsonl"):
        if p.is_file():
            n += 1
    return n


def _list_raw_dir(store_root: Optional[Path], subdir: str) -> List[Path]:
    if store_root is None:
        return []
    p = store_root / "raw" / subdir
    if not p.is_dir():
        return []
    return sorted(c for c in p.iterdir() if c.is_dir())


def _compute_next_required_actions(
    artifacts_by_type: Dict[str, int],
    artifacts_with_artifact_kind_only: int,
    expected: Dict[str, Any],
    total_artifacts_scanned: int,
) -> List[str]:
    """Order matters: actions are presented in the suggested execution order."""
    actions: List[str] = []

    if artifacts_with_artifact_kind_only > 0:
        actions.append("run migrate-artifact-kind workflow")

    confirmed_pairs = int(expected.get("confirmed_pair_count", 0))
    extractions = int(expected.get("meeting_extraction_count", 0))
    alignments = int(expected.get("alignment_result_count", 0))
    eval_results = int(expected.get("eval_result_count", 0))
    baseline_present = bool(expected.get("baseline_eval_summary_present", False))

    if confirmed_pairs > 0 and extractions < confirmed_pairs:
        actions.append("run pipeline with force=true on missing source_ids")

    if extractions > 0 and alignments < extractions:
        actions.append("run eval-ground-truth workflow")

    if eval_results >= 1 and not baseline_present:
        actions.append(
            "run eval-ground-truth --set-baseline after human review"
        )

    return actions


def scan_pipeline_state(
    *,
    data_lake_path: Optional[str],
    validate_schemas: bool = True,
    sdl_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect SDL_ROOT (and the store/ tree) and return the state record.

    The returned dict is the unwritten ``pipeline_state_record`` body —
    suitable for schema validation, JSON serialization, or direct use by
    the findings compiler.

    Never raises. Empty SDL_ROOT yields a warning ("sdl_root_empty") and
    ``total_artifacts_scanned == 0``. The caller decides what to do.
    """
    warnings: List[str] = []
    record_id = str(uuid.uuid4())
    created_at = _now_iso()

    # Resolve roots. SDL_ROOT may live inside or outside the data lake.
    resolved_sdl = (
        Path(sdl_root) if sdl_root else _resolve_sdl_root(data_lake_path)
    )
    store_root = _resolve_store_root(data_lake_path)

    artifacts_by_type: Dict[str, int] = {}
    artifacts_by_schema_version: Dict[str, int] = {}
    validation_failures_by_type: Dict[str, int] = {}
    artifacts_with_artifact_kind_only = 0
    artifacts_with_both_fields = 0
    artifacts_with_artifact_type_only = 0
    total = 0

    schemas_index = _index_schemas(_contracts_root())

    scan_roots: List[Path] = []
    if resolved_sdl is not None and resolved_sdl.is_dir():
        scan_roots.append(resolved_sdl)
    # Also scan the store/processed/ tree because some artifacts (like
    # source_record.json) live there rather than under SDL_ROOT.
    if store_root is not None and (store_root / "processed").is_dir():
        scan_roots.append(store_root / "processed")
    if store_root is not None and (store_root / "raw" / "minutes").is_dir():
        scan_roots.append(store_root / "raw" / "minutes")

    if not scan_roots:
        warnings.append("sdl_root_unresolvable_or_missing")

    seen: set[str] = set()
    for root in scan_roots:
        for path in _iter_json_files(root):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)

            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # A malformed JSON file shows up as a generic warning
                # rather than counted as an artifact.
                warnings.append(f"unreadable_json:{path}")
                continue

            artifact_type, schema_version, kind_flag, _raw_kind = (
                _classify_artifact(obj)
            )
            if artifact_type is None:
                # Not a pipeline artifact (no artifact_type/kind). Skip.
                continue

            total += 1
            artifacts_by_type[artifact_type] = (
                artifacts_by_type.get(artifact_type, 0) + 1
            )
            sv_key = schema_version or "unknown"
            artifacts_by_schema_version[sv_key] = (
                artifacts_by_schema_version.get(sv_key, 0) + 1
            )
            if kind_flag == "kind_only":
                artifacts_with_artifact_kind_only += 1
            elif kind_flag == "both":
                artifacts_with_both_fields += 1
            elif kind_flag == "type_only":
                artifacts_with_artifact_type_only += 1

            if validate_schemas:
                schema = (
                    schemas_index.get((artifact_type, schema_version))
                    if schema_version
                    else None
                )
                status = _validate_against_schema(schema, obj)
                if status != "valid":
                    # schema_not_found is recorded as a failure since
                    # the user can't tell from a count alone whether
                    # the schema is missing or the artifact is malformed.
                    validation_failures_by_type[artifact_type] = (
                        validation_failures_by_type.get(artifact_type, 0) + 1
                    )

    if total == 0:
        warnings.append("sdl_root_empty")

    # Cross-check expected artifact counts. These reflect the spec's
    # promised 13-transcript baseline; we surface the deltas via
    # next_required_actions rather than failing the scan.
    confirmed_pair_count = 0
    confirmed_pair_count = _count_confirmed_pairs(resolved_sdl)
    expected_artifacts: Dict[str, Any] = {
        "source_record_count": _count_source_records_on_disk(store_root),
        "minutes_record_count": artifacts_by_type.get("minutes_record", 0),
        "confirmed_pair_count": confirmed_pair_count,
        "chunks_files_present": _count_chunks_jsonl(store_root),
        "meeting_extraction_count": artifacts_by_type.get(
            "meeting_extraction", 0
        ),
        "alignment_result_count": artifacts_by_type.get(
            "alignment_result", 0
        ),
        "eval_result_count": artifacts_by_type.get("eval_result", 0),
        "baseline_eval_summary_present": _baseline_present(resolved_sdl),
        "glossary_term_count": artifacts_by_type.get("glossary_term", 0),
    }

    next_required_actions = _compute_next_required_actions(
        artifacts_by_type=artifacts_by_type,
        artifacts_with_artifact_kind_only=artifacts_with_artifact_kind_only,
        expected=expected_artifacts,
        total_artifacts_scanned=total,
    )

    record: Dict[str, Any] = {
        "pipeline_state_record_id": record_id,
        "artifact_type": "pipeline_state_record",
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "data_lake_path": str(data_lake_path or ""),
        "sdl_root": str(resolved_sdl) if resolved_sdl else "",
        "total_artifacts_scanned": total,
        "artifacts_by_type": artifacts_by_type,
        "artifacts_by_schema_version": artifacts_by_schema_version,
        "validation_failures_by_type": validation_failures_by_type,
        "artifacts_with_artifact_kind_only": artifacts_with_artifact_kind_only,
        "artifacts_with_both_fields": artifacts_with_both_fields,
        "artifacts_with_artifact_type_only": artifacts_with_artifact_type_only,
        "expected_artifacts": expected_artifacts,
        "next_required_actions": next_required_actions,
        "warnings": warnings,
        "provenance": {"produced_by": PRODUCED_BY},
    }
    return record


def _count_source_records_on_disk(store_root: Optional[Path]) -> int:
    """Count source_record.json files under store/processed/<family>/<sid>/."""
    if store_root is None:
        return 0
    processed = store_root / "processed"
    if not processed.is_dir():
        return 0
    n = 0
    for family_dir in sorted(processed.iterdir()):
        if not family_dir.is_dir():
            continue
        for sid_dir in sorted(family_dir.iterdir()):
            if not sid_dir.is_dir():
                continue
            if (sid_dir / "source_record.json").is_file():
                n += 1
    return n


def _count_confirmed_pairs(sdl_root: Optional[Path]) -> int:
    if sdl_root is None or not sdl_root.is_dir():
        return 0
    pairs_dir = sdl_root / "ground_truth"
    if not pairs_dir.is_dir():
        return 0
    n = 0
    for path in pairs_dir.glob("*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("status") == "confirmed":
            n += 1
    return n


def _baseline_present(sdl_root: Optional[Path]) -> bool:
    if sdl_root is None:
        return False
    return (sdl_root / "evals" / "baseline_eval_summary.json").is_file()


def write_pipeline_state_record(
    record: Dict[str, Any], *, sdl_root: Path
) -> Optional[Path]:
    """Write the record under ``$SDL_ROOT/verifications/<id>.json``.

    Validates against the contract schema first. If validation fails,
    writes a sibling ``.invalid.json`` so a human can inspect, and
    returns ``None``.
    """
    schema_path = (
        _contracts_root()
        / "schemas"
        / "verification"
        / "pipeline_state_record.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        schema = None

    target_dir = sdl_root / "verifications"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    target = target_dir / f"{record['pipeline_state_record_id']}.json"
    if schema is not None:
        try:
            jsonschema.Draft202012Validator(schema).validate(record)
        except jsonschema.ValidationError:
            invalid = target.with_suffix(".invalid.json")
            try:
                invalid.write_text(
                    json.dumps(record, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            return None
    try:
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return target


def emit_actions_summary(record: Dict[str, Any]) -> str:
    """Render a Markdown summary for the GitHub Actions step output."""
    lines: List[str] = []
    lines.append("## verify-pipeline-state")
    lines.append("")
    lines.append(f"- SDL root: `{record.get('sdl_root', '')}`")
    lines.append(
        f"- Total artifacts scanned: **{record.get('total_artifacts_scanned', 0)}**"
    )
    lines.append("")

    counts = record.get("artifacts_by_type", {}) or {}
    if counts:
        lines.append("### Artifacts by type")
        lines.append("")
        lines.append("| Type | Count |")
        lines.append("|------|------:|")
        for key in sorted(counts):
            lines.append(f"| `{key}` | {counts[key]} |")
        lines.append("")

    expected = record.get("expected_artifacts", {}) or {}
    if expected:
        lines.append("### Expected artifact cross-check")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|------:|")
        for key in sorted(expected):
            lines.append(f"| `{key}` | {expected[key]} |")
        lines.append("")

    actions = record.get("next_required_actions", []) or []
    if actions:
        lines.append("### Next required actions")
        lines.append("")
        for a in actions:
            lines.append(f"- [ ] {a}")
        lines.append("")
    else:
        lines.append("### Next required actions")
        lines.append("")
        lines.append("_None._")
        lines.append("")

    warnings = record.get("warnings", []) or []
    if warnings:
        lines.append("### Warnings")
        lines.append("")
        for w in warnings:
            lines.append(f"- `{w}`")
        lines.append("")

    return "\n".join(lines) + "\n"
