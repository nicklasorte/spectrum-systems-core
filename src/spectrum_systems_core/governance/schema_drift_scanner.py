"""SchemaDriftScanner — broken refs, unused schemas, baseline drift.

FINDING-I-001: every governance audit carries prior_value and delta.
First run sets baseline (prior_value=null). Subsequent runs detect drift.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema

from ._io import (
    find_prior_audit,
    read_json,
    utcnow_iso,
    write_audit_record,
)
from ._schema import validate_governance_artifact


_LOG = logging.getLogger(__name__)


def _walk_schemas(repo_root: Path) -> List[Path]:
    schemas_root = repo_root / "contracts" / "schemas"
    if not schemas_root.is_dir():
        return []
    return sorted(schemas_root.rglob("*.schema.json"))


def _resolve_ref(ref: str, schema_path: Path, repo_root: Path) -> bool:
    """Return True if a $ref resolves to an existing path. Skips fragment-only refs."""
    if not ref:
        return True
    if ref.startswith("#"):
        return True
    if ref.startswith("http://") or ref.startswith("https://"):
        # External URLs: not validated here. Treated as resolved.
        return True
    target = (schema_path.parent / ref).resolve()
    if target.is_file():
        return True
    return False


def _collect_refs(node: Any) -> List[str]:
    refs: List[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                refs.append(v)
            else:
                refs.extend(_collect_refs(v))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_collect_refs(item))
    return refs


def _list_python_files(repo_root: Path) -> List[Path]:
    src = repo_root / "src"
    if not src.is_dir():
        return []
    return [p for p in src.rglob("*.py") if "__pycache__" not in p.parts]


def _list_eval_definitions(repo_root: Path) -> List[Dict[str, Any]]:
    eval_dir = repo_root / "contracts" / "evals"
    if not eval_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(eval_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
    return out


class SchemaDriftScanner:
    """Scan contracts/schemas/ for drift signals. Recommendation only."""

    def scan(self, repo_root: str | Path) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: List[Dict[str, Any]] = []
        broken_refs = 0
        unused = 0
        unparseable = 0

        schemas = _walk_schemas(repo_root_path)
        py_blob = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in _list_python_files(repo_root_path)
        )

        eval_definitions = _list_eval_definitions(repo_root_path)
        eval_target_types = {
            d.get("target_artifact_type")
            for d in eval_definitions
            if d.get("target_artifact_type")
        }

        for schema_path in schemas:
            rel = str(schema_path.relative_to(repo_root_path))
            try:
                schema_doc = json.loads(schema_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                unparseable += 1
                flagged.append(
                    {
                        "item_type": "schema_unparseable",
                        "item_id": rel,
                        "detail": f"Schema is not valid JSON: {exc}",
                        "severity": "high",
                        "recommended_action": (
                            "Fix JSON syntax in schema file "
                            f"{rel}"
                        ),
                    }
                )
                continue

            try:
                jsonschema.Draft202012Validator.check_schema(schema_doc)
            except jsonschema.SchemaError as exc:
                unparseable += 1
                flagged.append(
                    {
                        "item_type": "schema_invalid",
                        "item_id": rel,
                        "detail": f"Not a valid Draft 2020-12 schema: {exc.message}",
                        "severity": "high",
                        "recommended_action": (
                            "Repair schema definition at " + rel
                        ),
                    }
                )

            for ref in _collect_refs(schema_doc):
                if not _resolve_ref(ref, schema_path, repo_root_path):
                    broken_refs += 1
                    flagged.append(
                        {
                            "item_type": "broken_ref",
                            "item_id": f"{rel}::{ref}",
                            "detail": (
                                f"$ref '{ref}' in {rel} does not resolve "
                                "to an existing schema path"
                            ),
                            "severity": "high",
                            "recommended_action": (
                                f"Fix or remove broken $ref '{ref}' in {rel}"
                            ),
                        }
                    )

            schema_filename = schema_path.name
            schema_stem = schema_path.stem.replace(".schema", "")
            if schema_filename not in py_blob and schema_stem not in py_blob:
                unused += 1
                flagged.append(
                    {
                        "item_type": "unused_schema",
                        "item_id": rel,
                        "detail": (
                            f"Schema {schema_filename} is not referenced by "
                            "any *.py file under src/"
                        ),
                        "severity": "medium",
                        "recommended_action": (
                            "Wire schema into a validator path or "
                            "consider deprecation"
                        ),
                    }
                )

            title = schema_doc.get("title") if isinstance(schema_doc, dict) else None
            if (
                isinstance(title, str)
                and title
                and title not in eval_target_types
            ):
                if title.endswith("_record") or title.endswith("_artifact"):
                    flagged.append(
                        {
                            "item_type": "schema_without_eval",
                            "item_id": rel,
                            "detail": (
                                f"Schema title '{title}' has no eval_case in "
                                "contracts/evals/"
                            ),
                            "severity": "medium",
                            "recommended_action": (
                                "Add an eval_case for artifact_type "
                                f"'{title}' in contracts/evals/"
                            ),
                        }
                    )

        prior_audit = find_prior_audit(repo_root_path, "schema_drift")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value: Dict[str, Any] = {
            "total_schemas": len(schemas),
            "broken_refs": broken_refs,
            "unused": unused,
            "unparseable": unparseable,
        }
        delta: Dict[str, Any] | None = None
        status = "clean"
        if prior_value is not None:
            delta = {
                k: int(current_value.get(k, 0)) - int(prior_value.get(k, 0))
                for k in current_value
            }
            if any(v > 0 for v in delta.values()) and flagged:
                status = "drift_detected"
        if flagged and status == "clean":
            status = "drift_detected"

        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "schema_drift",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged,
            "total_scanned": len(schemas),
            "total_flagged": len(flagged),
            "status": status,
        }
        ok, err = validate_governance_artifact(record, "governance_audit_record")
        if not ok:
            _LOG.warning("schema_drift audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
        write_audit_record(record, repo_root_path)
        return record
