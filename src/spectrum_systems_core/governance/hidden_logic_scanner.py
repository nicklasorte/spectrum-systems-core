"""HiddenLogicScanner — three named anti-patterns, no fuzzy matching.

FINDING-I-003: this scanner is exact and reviewable. It targets a fixed
list of named anti-patterns. Vague "decision logic" is out of scope.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from ._io import find_prior_audit, utcnow_iso, write_audit_record
from ._schema import validate_governance_artifact

_LOG = logging.getLogger(__name__)


UUID_LITERAL_PATTERN = re.compile(
    r"['\"]([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})['\"]"
)

EVAL_STATUS_BRANCH_PATTERN = re.compile(
    r"if\s+\w*eval_result.?\w*\s*==\s*['\"](pass|fail|warn|block)['\"]"
)

PROMPT_LIKE_PATTERN = re.compile(
    r"['\"](?:You are|Return ONLY|Return only|return only|return ONLY)",
    re.MULTILINE,
)


ANTI_PATTERNS: dict[str, dict[str, Any]] = {
    "uuid_literal_in_source": {
        "pattern": UUID_LITERAL_PATTERN,
        "include_paths": ["src/spectrum_systems_core/"],
        "exclude_paths": [
            "tests/",
            "tests/fixtures/",
        ],
        "severity": "medium",
        "reason": (
            "Hard-coded UUID in source — should be in fixtures or generated"
        ),
        "recommended_action": (
            "Move UUID to fixtures or replace with str(uuid.uuid4())"
        ),
    },
    "eval_status_branch_outside_eval_module": {
        "pattern": EVAL_STATUS_BRANCH_PATTERN,
        "include_paths": ["src/spectrum_systems_core/"],
        "exclude_paths": [
            "src/spectrum_systems_core/synthesis/bundle_eval.py",
            "src/spectrum_systems_core/paper/",
            "src/spectrum_systems_core/agency/",
            "src/spectrum_systems_core/ai/grounding_eval.py",
            "src/spectrum_systems_core/extraction/",
        ],
        "severity": "high",
        "reason": (
            "Eval result branching outside eval module — decision logic creep"
        ),
        "recommended_action": (
            "Move branching into the appropriate eval module"
        ),
    },
    "prompt_like_string_outside_registry": {
        "pattern": PROMPT_LIKE_PATTERN,
        "include_paths": [],  # entire repo unless excluded
        "exclude_paths": [
            "ai/registry/",
            "tests/fixtures/ai/",
            "src/spectrum_systems_core/ai/prompt_registry.py",
            "src/spectrum_systems_core/ai/adapter.py",
            "tests/",
        ],
        "severity": "high",
        "reason": "Prompt-like string outside registry — bypass risk",
        "recommended_action": "Move prompt to ai/registry/prompts.json",
    },
}


def _path_starts_with_any(rel_path: str, prefixes: list[str]) -> bool:
    """Path-prefix match (FINDING-I-003 RT3-006). Not substring."""
    normalized = rel_path.replace("\\", "/")
    for prefix in prefixes:
        prefix_norm = prefix.replace("\\", "/")
        if normalized == prefix_norm.rstrip("/"):
            return True
        if normalized.startswith(prefix_norm) and (
            prefix_norm.endswith("/") or prefix_norm.endswith(".py")
        ):
            return True
    return False


def _python_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(repo_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.is_file():
            out.append(path)
    return out


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


class HiddenLogicScanner:
    """Scan all .py files for ANTI_PATTERNS — fixed and exact."""

    def scan(self, repo_root: str | Path) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: list[dict[str, Any]] = []
        files_scanned = 0

        files = _python_files(repo_root_path)
        for path in files:
            try:
                rel_path = str(path.relative_to(repo_root_path))
            except ValueError:
                continue
            files_scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern_name, definition in ANTI_PATTERNS.items():
                include_paths: list[str] = list(definition.get("include_paths") or [])
                exclude_paths: list[str] = list(definition.get("exclude_paths") or [])
                if include_paths and not _path_starts_with_any(rel_path, include_paths):
                    continue
                if _path_starts_with_any(rel_path, exclude_paths):
                    continue
                regex: re.Pattern = definition["pattern"]
                for match in regex.finditer(text):
                    line_no = _line_for_offset(text, match.start())
                    snippet = match.group(0)
                    if len(snippet) > 80:
                        snippet = snippet[:77] + "..."
                    flagged.append(
                        {
                            "item_type": pattern_name,
                            "item_id": f"{rel_path}:{line_no}",
                            "detail": (
                                f"{definition['reason']}: {snippet}"
                            ),
                            "severity": definition["severity"],
                            "recommended_action": definition["recommended_action"],
                        }
                    )

        prior_audit = find_prior_audit(repo_root_path, "hidden_logic_creep")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value: dict[str, Any] = {
            "files_scanned": files_scanned,
            "total_flags": len(flagged),
            "high_severity": sum(1 for f in flagged if f["severity"] == "high"),
            "medium_severity": sum(1 for f in flagged if f["severity"] == "medium"),
        }
        delta = None
        if prior_value is not None:
            delta = {
                k: int(current_value.get(k, 0)) - int(prior_value.get(k, 0))
                for k in current_value
            }

        status = "drift_detected" if flagged else "clean"
        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "hidden_logic_creep",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged,
            "total_scanned": files_scanned,
            "total_flagged": len(flagged),
            "status": status,
        }
        ok, err = validate_governance_artifact(record, "governance_audit_record")
        if not ok:
            _LOG.warning("hidden_logic_creep audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
        write_audit_record(record, repo_root_path)
        return record
