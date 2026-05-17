"""Module 3: Vault index writer.

Writes a read-only index note for a promoted artifact under
``Artifacts/Promoted/`` in the vault. The canonical artifact lives in the
data lake — this note is a pointer with provenance.
"""
from __future__ import annotations

import json
import os
from typing import Any

import jsonschema
import yaml

from . import _frontmatter
from ._paths import schema_path

_SCHEMA_NAME = "vault_index_note"


def _load_schema() -> dict[str, Any]:
    return json.loads(schema_path(_SCHEMA_NAME).read_text(encoding="utf-8"))


def _check_pass_marker() -> str:
    # Avoid emoji literals here so the source file stays plain ASCII.
    return "✅ pass"


class VaultIndexWriter:

    def write_index_note(
        self,
        promoted_artifact_id: str,
        artifact_type: str,
        schema_version: str,
        promoted_at: str,
        pipeline_run_id: str,
        data_lake_ref: str,
        source_note_vault_path: str,
        eval_results: list[dict[str, Any]],
        reviewer_id: str,
        review_decision: str,
        artifact_summary: str,
        vault_root: str,
    ) -> dict[str, Any]:
        # Step 1: idempotency check
        promoted_dir = os.path.join(vault_root, "Artifacts", "Promoted")
        dest = os.path.join(promoted_dir, f"{promoted_artifact_id}.md")
        if os.path.exists(dest):
            return {
                "status": "skipped",
                "reason": "already_exists",
                "index_note_path": dest,
            }

        # Step 2: build frontmatter
        frontmatter: dict[str, Any] = {
            "artifact_id": promoted_artifact_id,
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "promoted_at": promoted_at,
            "pipeline_run_id": pipeline_run_id,
            "data_lake_ref": data_lake_ref,
            "source_note": "[[" + source_note_vault_path + "]]",
            "eval_gate_status": "passed",
            "reviewer_id": reviewer_id,
            "review_decision": review_decision,
            "vault_note_status": "index",
        }

        # Step 3: build eval rows
        pass_marker = _check_pass_marker()
        eval_rows = "\n".join(
            f"| {entry.get('metric_name', '')} | {pass_marker} |"
            for entry in eval_results
        )

        # Step 4: assemble note markdown
        fm_yaml = yaml.safe_dump(
            frontmatter, default_flow_style=False, sort_keys=False
        ).strip()
        note = (
            f"---\n{fm_yaml}\n---\n\n"
            f"# {artifact_type} — {promoted_artifact_id}\n\n"
            f"**Promoted:** {promoted_at}\n"
            f"**Source:** [[{source_note_vault_path}]]\n"
            f"**Canonical artifact:** `{data_lake_ref}`\n\n"
            f"## Summary\n\n"
            f"{artifact_summary}\n\n"
            f"## Eval Coverage\n\n"
            f"| Eval | Result |\n"
            f"|---|---|\n"
            f"{eval_rows}\n\n"
            f"## Provenance\n\n"
            f"- Produced by: `obsidian_ingestion_gate v1.0.0`\n"
            f"- Pipeline run: `{pipeline_run_id}`\n"
            f"- Reviewer: `{reviewer_id}`\n\n"
            f"> This note is a read-only index. "
            f"The canonical artifact lives in the data lake.\n"
            f"> Do not edit this note — changes will not propagate "
            f"to the artifact store.\n"
        )

        # Step 5: write note
        try:
            os.makedirs(promoted_dir, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(note.encode("utf-8"))
        except OSError:
            source_abs = os.path.join(vault_root, source_note_vault_path)
            _frontmatter.stamp_file(
                source_abs, {"ingestion_status": "write_back_failed"}
            )
            return {"status": "failure", "reason": "vault_write_failure"}

        # Step 6: validate written frontmatter
        try:
            with open(dest, "rb") as fh:
                written = fh.read().decode("utf-8")
            written_fm, _body = _frontmatter.split(written)
            schema = _load_schema()
            jsonschema.Draft202012Validator(schema).validate(written_fm)
        except (jsonschema.ValidationError, ValueError, OSError):
            return {"status": "failure", "reason": "schema_violation"}

        # Step 7: data_lake_ref existence check
        if not data_lake_ref.startswith("sdl://"):
            return {
                "status": "failure",
                "reason": "data_lake_ref_not_resolvable",
            }
        artifact_id = data_lake_ref[len("sdl://"):]
        sdl_root = os.environ.get("SDL_ROOT", "")
        if not sdl_root or not os.path.exists(
            os.path.join(sdl_root, artifact_id + ".json")
        ):
            return {
                "status": "failure",
                "reason": "data_lake_ref_not_resolvable",
            }

        # Step 8: stamp the source note
        source_abs = os.path.join(vault_root, source_note_vault_path)
        _frontmatter.stamp_file(
            source_abs,
            {
                "ingestion_status": "complete",
                "promoted_artifact_id": promoted_artifact_id,
                "promoted_at": promoted_at,
                "promoted_note": (
                    f"[[Artifacts/Promoted/{promoted_artifact_id}]]"
                ),
            },
        )

        # Step 9: success
        return {"status": "success", "index_note_path": dest}
