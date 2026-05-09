"""RunManifest: open and close a synthesis run, write totals.

open_run() initialises synthesis/<run_id>/run_manifest.json with empty
totals. close_run() reads cost.jsonl and any artifacts written under the
run dir, sums tokens and cost, and rewrites the manifest with
completed_at set. Also writes the human-facing review_summary.md
projection.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict

import jsonschema

from ._paths import synthesis_run_dir, synthesis_schema_path
from .cost_recorder import read_cost_records


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


VALID_AUDIENCES = ("technical", "policy", "executive", "public")
VALID_PURPOSES = ("report", "keynote", "both")


class RunManifest:
    """Open and close synthesis run manifests."""

    def open_run(
        self,
        run_id: str,
        audience: str,
        purpose: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        if audience not in VALID_AUDIENCES:
            raise ValueError(f"invalid_audience: {audience}")
        if purpose not in VALID_PURPOSES:
            raise ValueError(f"invalid_purpose: {purpose}")
        manifest = {
            "run_id": run_id,
            "audience": audience,
            "purpose": purpose,
            "source_ids_included": [],
            "story_ids_included": [],
            "claim_ids_included": [],
            "theme_ids_included": [],
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_estimated_cost_usd": 0.0,
            "started_at": _now_iso(),
            "completed_at": None,
        }
        schema = json.loads(
            synthesis_schema_path("synthesis_run_manifest")
            .read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(manifest)
        run_dir = synthesis_run_dir(Path(repo_root).resolve(), run_id, create=True)
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def close_run(self, run_id: str, repo_root: str) -> Dict[str, Any]:
        run_dir = synthesis_run_dir(Path(repo_root).resolve(), run_id, create=True)
        manifest_path = run_dir / "run_manifest.json"
        if not manifest_path.is_file():
            return {"status": "failure", "total_cost_usd": 0.0, "reason": "no_manifest"}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Aggregate cost across all cost.jsonl entries for this run.
        cost_records = read_cost_records(run_id, str(repo_root))
        total_input = sum(int(r.get("input_tokens", 0)) for r in cost_records)
        total_output = sum(int(r.get("output_tokens", 0)) for r in cost_records)
        total_cost = float(
            sum(float(r.get("estimated_cost_usd", 0.0)) for r in cost_records)
        )

        # Pull in source/story/claim/theme ids from the artifacts in this run.
        bundle_path = run_dir / "context_bundle.json"
        source_ids: list = []
        story_ids: list = []
        claim_ids: list = []
        theme_ids: list = []
        if bundle_path.is_file():
            try:
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                for item in bundle.get("items", []):
                    sid = item.get("source_id")
                    if sid and sid not in source_ids:
                        source_ids.append(sid)
                    aid = item.get("artifact_id")
                    if not aid:
                        continue
                    atype = item.get("artifact_type", "")
                    if atype == "story_candidate" and aid not in story_ids:
                        story_ids.append(aid)
                    elif atype == "technical_claim" and aid not in claim_ids:
                        claim_ids.append(aid)
                    elif atype == "theme_record" and aid not in theme_ids:
                        theme_ids.append(aid)
            except (OSError, json.JSONDecodeError):
                pass

        themes_path = run_dir / "themes.jsonl"
        if themes_path.is_file():
            try:
                with themes_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        sid = rec.get("synthesis_id")
                        if sid and sid not in theme_ids:
                            theme_ids.append(sid)
            except (OSError, json.JSONDecodeError):
                pass

        manifest.update(
            {
                "source_ids_included": sorted(source_ids),
                "story_ids_included": sorted(story_ids),
                "claim_ids_included": sorted(claim_ids),
                "theme_ids_included": sorted(theme_ids),
                "total_input_tokens": int(total_input),
                "total_output_tokens": int(total_output),
                "total_estimated_cost_usd": round(total_cost, 6),
                "completed_at": _now_iso(),
            }
        )

        schema = json.loads(
            synthesis_schema_path("synthesis_run_manifest")
            .read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema).validate(manifest)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # View-only projection.
        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            ObsidianProjection().write_review_summary_projection(
                run_id, str(repo_root)
            )
        except (FileNotFoundError, OSError, AttributeError):
            pass

        return {"status": "success", "total_cost_usd": total_cost, "reason": ""}
