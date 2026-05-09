"""Module 1: Obsidian ingestion gate.

Converts a Markdown note in the Obsidian vault into a deterministic
``obsidian_input_artifact`` envelope. Fail-closed: any step that fails
stamps the source note and returns a failure dict — never raises.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import uuid
from typing import Any, Dict

import jsonschema
import yaml

from . import _frontmatter
from ._paths import schema_digest, schema_path


_SCHEMA_NAME = "obsidian_input_artifact"
_COMPONENT_VERSION = "1.0.0"


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure(reason: str, path: str, detail: str) -> Dict[str, Any]:
    timestamp = _now_iso()
    _frontmatter.stamp_file(
        path,
        {
            "ingestion_status": "failed",
            "ingestion_failure_reason": reason,
            "ingestion_at": timestamp,
        },
    )
    return {
        "status": "failure",
        "artifact": {
            "artifact_kind": "obsidian_ingestion_failure",
            "artifact_id": str(uuid.uuid4()),
            "created_at": timestamp,
            "reason_codes": [reason],
            "vault_note_path": path,
            "detail": detail,
        },
    }


class ObsidianIngestionGate:
    """Deterministic gate from a vault note to obsidian_input_artifact."""

    def run(
        self,
        vault_note_path: str,
        vault_root: str,
        pipeline_trigger_tag: str = "#pending-pipeline",
    ) -> Dict[str, Any]:
        # Step 1: read + decode
        try:
            with open(vault_note_path, "rb") as fh:
                raw_bytes = fh.read()
        except FileNotFoundError as exc:
            return _failure("unreadable_file", vault_note_path, str(exc))
        except OSError as exc:
            return _failure("unreadable_file", vault_note_path, str(exc))

        try:
            raw_content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _failure("encoding_error", vault_note_path, str(exc))

        # Step 2: parse frontmatter
        try:
            frontmatter, _body = _frontmatter.split(raw_content)
        except (ValueError, yaml.YAMLError) as exc:
            return _failure("schema_violation", vault_note_path, str(exc))

        # Step 3: trigger tag check
        tags = frontmatter.get("tags") or []
        if not isinstance(tags, list) or pipeline_trigger_tag not in tags:
            return _failure(
                "missing_trigger_tag",
                vault_note_path,
                f"trigger tag {pipeline_trigger_tag!r} not in tags={tags!r}",
            )

        # Step 4: hashes
        content_hash = "sha256:" + hashlib.sha256(
            raw_content.encode("utf-8")
        ).hexdigest()
        fingerprint_src = vault_note_path + content_hash + _COMPONENT_VERSION
        execution_fingerprint_hash = "sha256:" + hashlib.sha256(
            fingerprint_src.encode("utf-8")
        ).hexdigest()

        # Step 5: identifiers
        artifact_id = str(uuid.uuid4())
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        created_at = _now_iso()
        vault_relative_path = os.path.relpath(vault_note_path, vault_root)

        # Step 6: schema digest
        try:
            digest = schema_digest(_SCHEMA_NAME)
        except (FileNotFoundError, OSError) as exc:
            return _failure("schema_violation", vault_note_path, str(exc))

        # Step 7: assemble artifact
        artifact: Dict[str, Any] = {
            "artifact_kind": "obsidian_input_artifact",
            "artifact_id": artifact_id,
            "created_at": created_at,
            "schema_ref": {
                "name": _SCHEMA_NAME,
                "version": "1.0.0",
                "digest": digest,
            },
            "trace": {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": None,
            },
            "provenance": {
                "produced_by": {
                    "component": "obsidian_ingestion_gate",
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [],
                "execution_fingerprint_hash": execution_fingerprint_hash,
            },
            "payload": {
                "vault_relative_path": vault_relative_path,
                "content_hash": content_hash,
                "raw_content": raw_content,
                "frontmatter": frontmatter,
                "byte_length": len(raw_content.encode("utf-8")),
                "encoding": "utf-8",
            },
            "ingestion_status": "success",
        }

        # Step 8: schema validation
        try:
            schema = _load_schema()
            jsonschema.Draft202012Validator(schema).validate(artifact)
        except jsonschema.ValidationError as exc:
            return _failure("schema_violation", vault_note_path, exc.message)
        except (FileNotFoundError, OSError) as exc:
            return _failure("schema_violation", vault_note_path, str(exc))

        # Step 9: replay consistency
        recomputed = "sha256:" + hashlib.sha256(
            raw_content.encode("utf-8")
        ).hexdigest()
        if recomputed != content_hash:
            return _failure(
                "replay_inconsistency",
                vault_note_path,
                "content hash mismatch on recompute",
            )

        # Step 10: stamp the vault note
        _frontmatter.stamp_file(
            vault_note_path,
            {
                "ingestion_artifact_id": artifact_id,
                "ingestion_status": "submitted",
                "ingestion_at": created_at,
            },
        )

        # Step 11: success
        return {"status": "success", "artifact": artifact}


def _load_schema() -> Dict[str, Any]:
    import json
    return json.loads(schema_path(_SCHEMA_NAME).read_text(encoding="utf-8"))
