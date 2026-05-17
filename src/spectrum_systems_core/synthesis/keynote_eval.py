"""KeynoteEval: deterministic evals on a keynote_scaffold.

EVAL-KEY-001..007. Required: 001..006 block on failure. EVAL-KEY-007
(bundle_hash equality with the report draft) only warns
(FINDING-F-005) — a mismatch is a review-time signal, not an automatic
block, because the two artifacts may legitimately be from separate runs
when only one of (report, keynote) is produced.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import synthesis_schema_path

REQUIRED_BEAT_TYPES = {"opener", "call_to_action"}
MIN_ARC_BEATS = 3


class KeynoteEval:
    """Run schema + reference + arc-shape checks on a keynote_scaffold."""

    def run(
        self,
        scaffold: dict[str, Any],
        bundle: dict[str, Any],
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        eval_results: list[dict[str, Any]] = []
        reason_codes: list[str] = []
        warn_codes: list[str] = []

        # EVAL-KEY-001: schema_conformance
        try:
            schema = json.loads(
                synthesis_schema_path("keynote_scaffold")
                .read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(scaffold)
            eval_results.append(
                {"name": "EVAL-KEY-001", "status": "pass", "reason": ""}
            )
        except (FileNotFoundError, OSError) as exc:
            return {
                "decision": "block",
                "eval_results": [
                    {
                        "name": "schema_load",
                        "status": "fail",
                        "reason": f"schema_unreadable: {exc}",
                    }
                ],
                "reason_codes": ["schema_unreadable"],
                "warn_codes": [],
            }
        except jsonschema.ValidationError as exc:
            eval_results.append(
                {
                    "name": "EVAL-KEY-001",
                    "status": "fail",
                    "reason": f"schema_violation: {exc.message}",
                }
            )
            reason_codes.append("EVAL-KEY-001:schema_conformance")

        bundle_artifact_ids = {
            item.get("artifact_id", "")
            for item in (bundle or {}).get("items", []) or []
        }

        # EVAL-KEY-002: opener_story_in_bundle
        opener_story_id = (
            (scaffold.get("opener") or {}).get("story_id") or ""
        )
        if opener_story_id and opener_story_id not in bundle_artifact_ids:
            eval_results.append(
                {
                    "name": "EVAL-KEY-002",
                    "status": "fail",
                    "reason": f"opener_story_not_in_bundle: {opener_story_id}",
                }
            )
            reason_codes.append("EVAL-KEY-002:opener_story_not_in_bundle")
        else:
            eval_results.append(
                {"name": "EVAL-KEY-002", "status": "pass", "reason": ""}
            )

        arc = scaffold.get("arc") or []

        # EVAL-KEY-003: arc_minimum_beats
        if len(arc) < MIN_ARC_BEATS:
            eval_results.append(
                {
                    "name": "EVAL-KEY-003",
                    "status": "fail",
                    "reason": f"arc_has_{len(arc)}_beats_min_{MIN_ARC_BEATS}",
                }
            )
            reason_codes.append("EVAL-KEY-003:arc_minimum_beats")
        else:
            eval_results.append(
                {"name": "EVAL-KEY-003", "status": "pass", "reason": ""}
            )

        # EVAL-KEY-004: arc_has_required_beat_types
        beat_types = {str(b.get("beat_type") or "") for b in arc}
        missing = REQUIRED_BEAT_TYPES - beat_types
        if missing:
            eval_results.append(
                {
                    "name": "EVAL-KEY-004",
                    "status": "fail",
                    "reason": "missing_beat_types: " + ", ".join(sorted(missing)),
                }
            )
            reason_codes.append("EVAL-KEY-004:missing_beat_types")
        else:
            eval_results.append(
                {"name": "EVAL-KEY-004", "status": "pass", "reason": ""}
            )

        # EVAL-KEY-005: claim_ids_in_bundle
        offenders: list[str] = []
        for beat in arc:
            for cid in beat.get("claim_ids") or []:
                if cid not in bundle_artifact_ids:
                    offenders.append(cid)
        if offenders:
            eval_results.append(
                {
                    "name": "EVAL-KEY-005",
                    "status": "fail",
                    "reason": "fabricated_claim_ids_in_arc: "
                    + ", ".join(sorted(set(offenders))),
                }
            )
            reason_codes.append("EVAL-KEY-005:fabricated_claim_ids_in_arc")
        else:
            eval_results.append(
                {"name": "EVAL-KEY-005", "status": "pass", "reason": ""}
            )

        # EVAL-KEY-006: temperature_zero
        if scaffold.get("generation_temperature") != 0:
            eval_results.append(
                {
                    "name": "EVAL-KEY-006",
                    "status": "fail",
                    "reason": "non_zero_temperature",
                }
            )
            reason_codes.append("EVAL-KEY-006:temperature_zero")
        else:
            eval_results.append(
                {"name": "EVAL-KEY-006", "status": "pass", "reason": ""}
            )

        # EVAL-KEY-007: bundle_hash_matches_report  (FINDING-F-005)
        run_id = scaffold.get("run_id") or ""
        report_paths: list[Path] = []
        if repo_root:
            report_paths.append(
                Path(repo_root) / "synthesis" / run_id / "report_draft.json"
            )
        report_paths.append(
            Path.cwd() / "synthesis" / run_id / "report_draft.json"
        )
        warned_007 = False
        passed_007 = False
        for candidate in report_paths:
            if not candidate.is_file():
                continue
            try:
                report = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            report_hash = report.get("bundle_hash")
            if (
                isinstance(report_hash, str)
                and report_hash
                and report_hash != scaffold.get("bundle_hash")
            ):
                eval_results.append(
                    {
                        "name": "EVAL-KEY-007",
                        "status": "warn",
                        "reason": (
                            "bundle_hash_mismatch: "
                            f"report={report_hash} "
                            f"scaffold={scaffold.get('bundle_hash')}"
                        ),
                    }
                )
                warn_codes.append("EVAL-KEY-007:bundle_hash_mismatch")
                warned_007 = True
            else:
                passed_007 = True
            break
        if not warned_007 and not passed_007:
            # No report draft to compare with — pass with neutral note.
            eval_results.append(
                {
                    "name": "EVAL-KEY-007",
                    "status": "pass",
                    "reason": "no_report_draft_to_compare",
                }
            )
        elif passed_007 and not warned_007:
            eval_results.append(
                {"name": "EVAL-KEY-007", "status": "pass", "reason": ""}
            )

        decision = "block" if reason_codes else ("warn" if warn_codes else "allow")
        return {
            "decision": decision,
            "eval_results": eval_results,
            "reason_codes": reason_codes,
            "warn_codes": warn_codes,
        }
