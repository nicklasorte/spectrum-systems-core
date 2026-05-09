"""MarkdownAuthorityScanner — automated form of the markdown-authority-leak
manual check from prior phases.

FINDING-I-004: Markdown is view-only. Reading a .md file as a source of
authority violates the system rule. Allowed read paths are projection
writers + ingestion gateways that explicitly treat .md as input.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ._io import find_prior_audit, utcnow_iso, write_audit_record
from ._schema import validate_governance_artifact


_LOG = logging.getLogger(__name__)


ALLOWED_MD_READ_PATHS: List[str] = [
    "src/spectrum_systems_core/ingestion/obsidian_projection.py",
    "src/spectrum_systems_core/extraction/story_review_gateway.py",
    "src/spectrum_systems_core/paper/",
    "src/spectrum_systems_core/synthesis/synthesis_review_gateway.py",
    "src/spectrum_systems_core/agency/profile_store.py",
    "src/spectrum_systems_core/cli.py",
    "src/spectrum_systems_core/obsidian_bridge/",
    "src/spectrum_systems_core/governance/markdown_authority_scanner.py",
    "tests/",
]


MD_OPEN_PATTERN = re.compile(
    r"open\s*\(\s*([^,)\n]*\.md['\"][^,)\n]*)"
)


def _is_write_mode(text: str, match_start: int, match_end: int) -> bool:
    """Look at 5 lines surrounding the match for write/append mode flags."""
    line_starts: List[int] = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(idx + 1)
    line_no = 0
    for i, start in enumerate(line_starts):
        if start > match_start:
            break
        line_no = i
    lo = max(0, line_no - 5)
    hi = min(len(line_starts) - 1, line_no + 5)
    start_offset = line_starts[lo]
    end_offset = (
        line_starts[hi + 1] if (hi + 1) < len(line_starts) else len(text)
    )
    window = text[start_offset:end_offset]
    if re.search(r"['\"](w|wb|wt|w\+|a|ab|at|x)['\"]", window):
        return True
    if re.search(r"write_text\s*\(", window):
        return True
    return False


def _python_files(repo_root: Path) -> List[Path]:
    out: List[Path] = []
    for path in sorted(repo_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.is_file():
            out.append(path)
    return out


def _path_starts_with_any(rel_path: str, prefixes: List[str]) -> bool:
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


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


class MarkdownAuthorityScanner:
    """Find disallowed .md read sites — view-only rule (FINDING-I-004)."""

    def scan(self, repo_root: str | Path) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        flagged: List[Dict[str, Any]] = []
        files_scanned = 0

        for path in _python_files(repo_root_path):
            try:
                rel_path = str(path.relative_to(repo_root_path))
            except ValueError:
                continue
            files_scanned += 1
            if _path_starts_with_any(rel_path, ALLOWED_MD_READ_PATHS):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in MD_OPEN_PATTERN.finditer(text):
                if _is_write_mode(text, match.start(), match.end()):
                    continue
                line_no = _line_for_offset(text, match.start())
                snippet = match.group(0)
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                flagged.append(
                    {
                        "item_type": "markdown_read_as_authority",
                        "item_id": f"{rel_path}:{line_no}",
                        "detail": (
                            "Markdown read as authority — violates view-only "
                            f"rule: {snippet}"
                        ),
                        "severity": "high",
                        "recommended_action": (
                            "Move data into a JSON artifact and read that, "
                            "or move the file under an allowed projection "
                            "writer path"
                        ),
                    }
                )

        prior_audit = find_prior_audit(repo_root_path, "markdown_authority")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value: Dict[str, Any] = {
            "files_scanned": files_scanned,
            "total_flags": len(flagged),
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
            "audit_type": "markdown_authority",
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
            _LOG.warning("markdown_authority audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
        write_audit_record(record, repo_root_path)
        return record
