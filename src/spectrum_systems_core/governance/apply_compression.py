"""apply-compression action handler.

FINDING-I-006: NEVER auto-deletes. For action="remove", prints the git rm
command for the human to run manually. For "merge", prints suggested merge
target. For "deprecate", renames file with a suffix or adds a warning.
For "investigate", records only.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from ._io import read_json, utcnow_iso, write_json
from ._paths import (
    candidates_archive_dir,
    candidates_dir,
    ensure_governance_tree,
)
from ._schema import validate_governance_artifact

_LOG = logging.getLogger(__name__)


def _find_candidate_path(
    candidate_id: str, repo_root: Path
) -> tuple[Path | None, dict[str, Any] | None]:
    proposed = candidates_dir(repo_root) / f"{candidate_id}.json"
    if proposed.is_file():
        return proposed, read_json(proposed)
    archived = candidates_archive_dir(repo_root) / f"{candidate_id}.json"
    if archived.is_file():
        return archived, read_json(archived)
    return None, None


def apply_compression(
    *,
    candidate_id: str,
    action: str,
    human_id: str,
    note: str = "",
    auto_confirm: bool = False,
    repo_root: str | Path | None = None,
    out_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Apply a compression candidate. NEVER auto-deletes (FINDING-I-006)."""
    repo_root_path = Path(repo_root or Path.cwd()).resolve()
    ensure_governance_tree(repo_root_path)

    if out_lines is None:
        out_lines = []

    if action not in {"remove", "merge", "deprecate", "investigate"}:
        return {
            "status": "failure",
            "reason": f"unknown_action: {action}",
        }

    path, candidate = _find_candidate_path(candidate_id, repo_root_path)
    if path is None or candidate is None:
        return {
            "status": "failure",
            "reason": f"candidate_not_found: {candidate_id}",
        }

    if candidate.get("status") != "proposed":
        return {
            "status": "failure",
            "reason": f"candidate_already_{candidate.get('status', 'unknown')}",
        }

    recommended = candidate.get("recommended_action")
    if action != "investigate" and recommended != action:
        return {
            "status": "failure",
            "reason": (
                f"action_mismatch: candidate recommends '{recommended}' "
                f"but '{action}' was requested"
            ),
        }

    candidate_path_str = str(candidate.get("candidate_path") or "")
    target_file = repo_root_path / candidate_path_str
    applied_detail = ""

    if action == "investigate":
        applied_detail = (
            f"investigate_only: human review noted by {human_id}. {note}".strip()
        )
    elif action == "deprecate":
        if (
            target_file.is_file()
            and candidate.get("candidate_type") in {"class", "file"}
        ):
            new_name = target_file.with_suffix(".deprecated.py")
            try:
                shutil.move(str(target_file), str(new_name))
                applied_detail = (
                    f"renamed: {target_file.relative_to(repo_root_path)} -> "
                    f"{new_name.relative_to(repo_root_path)}"
                )
            except OSError as exc:
                applied_detail = f"rename_failed: {exc}"
        else:
            applied_detail = (
                "deprecation noted. Human should add DeprecationWarning at "
                f"the head of {candidate_path_str}"
            )
        out_lines.append(applied_detail)
    elif action == "remove":
        out_lines.append(
            f"REMOVE candidate '{candidate_id}' is recommendation-only — "
            "this CLI does NOT auto-delete (FINDING-I-006)."
        )
        out_lines.append(
            f"Run manually: git rm {candidate_path_str}"
        )
        applied_detail = (
            f"remove_recorded_only_no_files_modified by {human_id}. {note}"
        ).strip()
    elif action == "merge":
        out_lines.append(
            f"MERGE candidate '{candidate_id}' is recommendation-only — "
            "this CLI does NOT auto-merge (FINDING-I-006)."
        )
        out_lines.append(
            "Manual steps: identify the merge target, copy unique behaviour, "
            f"then git rm {candidate_path_str}"
        )
        applied_detail = (
            f"merge_recorded_only_no_files_modified by {human_id}. {note}"
        ).strip()

    candidate["status"] = "applied"
    candidate["applied_at"] = utcnow_iso()
    candidate["applied_by"] = human_id
    candidate["applied_action_detail"] = applied_detail
    ok, err = validate_governance_artifact(candidate, "compression_candidate")
    if not ok:
        return {
            "status": "failure",
            "reason": f"schema_violation_after_update: {err}",
        }

    archive_target = candidates_archive_dir(repo_root_path) / f"{candidate_id}.json"
    write_json(archive_target, candidate)
    try:
        path.unlink()
    except OSError as exc:  # pragma: no cover
        _LOG.warning("apply_compression unlink failed: %s", exc)

    return {
        "status": "success",
        "candidate_id": candidate_id,
        "action": action,
        "applied_action_detail": applied_detail,
    }
