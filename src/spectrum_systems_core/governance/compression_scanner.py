"""CompressionScanner — find unused classes, schemas, eval cases, CLI commands.

FINDING-I-006: NEVER deletes or modifies anything. Writes
compression_candidate artifacts to governance/candidates/. Humans actuate
via apply-compression CLI.

Inactivity threshold: COMPRESSION_INACTIVITY_DAYS = 60.
"""
from __future__ import annotations

import ast
import datetime
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from ..harness._io import read_jsonl
from ..harness._paths import evals_dir
from ..harness.run_history import RunHistoryStore
from . import COMPRESSION_INACTIVITY_DAYS
from ._io import (
    find_prior_audit,
    parse_iso,
    utcnow_iso,
    write_audit_record,
    write_json,
)
from ._paths import candidates_dir, ensure_governance_tree
from ._schema import validate_governance_artifact

_LOG = logging.getLogger(__name__)


def _python_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.is_file():
            out.append(path)
    return out


def _classes_in(path: Path) -> list[tuple[str, int]]:
    """Return (class_name, line_no) tuples."""
    out: list[tuple[str, int]] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out.append((node.name, node.lineno))
    return out


def _all_text_blob(repo_root: Path, exclude: list[str]) -> str:
    parts: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo_root))
        if any(rel.startswith(x) for x in exclude):
            continue
        if path.suffix not in {".py", ".json", ".jsonl"}:
            continue
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


def _make_candidate(
    candidate_type: str,
    candidate_path: str,
    candidate_name: str,
    reason: str,
    evidence: dict[str, Any],
    recommended_action: str,
) -> dict[str, Any]:
    return {
        "candidate_id": str(uuid.uuid4()),
        "candidate_type": candidate_type,
        "candidate_path": candidate_path,
        "candidate_name": candidate_name,
        "reason": reason,
        "evidence": evidence,
        "recommended_action": recommended_action,
        "status": "proposed",
        "proposed_at": utcnow_iso(),
        "applied_at": None,
        "applied_by": None,
        "applied_action_detail": "",
    }


class CompressionScanner:
    """Find inactivity-based candidates. Recommendation only."""

    def scan(self, repo_root: str | Path) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        ensure_governance_tree(repo_root_path)
        candidates: list[dict[str, Any]] = []
        files_scanned = 0

        # Build a haystack of all *.py + *.json text outside the defining file.
        src_root = repo_root_path / "src"
        py_files = _python_files(src_root) if src_root.is_dir() else []
        # Map file -> text for individual exclusion when checking class usage.
        py_texts: dict[Path, str] = {}
        for path in py_files:
            try:
                py_texts[path] = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

        # 1. Classes
        for path, text in py_texts.items():
            files_scanned += 1
            classes = _classes_in(path)
            for class_name, line_no in classes:
                refs_outside = 0
                for other, other_text in py_texts.items():
                    if other == path:
                        continue
                    if "test" in str(other.relative_to(repo_root_path)).lower():
                        continue
                    if class_name in other_text:
                        refs_outside += 1
                if refs_outside == 0:
                    rel = str(path.relative_to(repo_root_path))
                    candidates.append(
                        _make_candidate(
                            candidate_type="class",
                            candidate_path=rel,
                            candidate_name=class_name,
                            reason=(
                                f"Class '{class_name}' is defined in {rel} "
                                "but has no references outside its defining "
                                "file or tests"
                            ),
                            evidence={
                                "defined_in": rel,
                                "line_no": line_no,
                                "refs_outside_module": refs_outside,
                            },
                            recommended_action="investigate",
                        )
                    )

        # 2. Schemas
        schemas_root = repo_root_path / "contracts" / "schemas"
        py_blob_outside_contracts = "\n".join(
            text for path, text in py_texts.items()
        )
        if schemas_root.is_dir():
            for schema_path in sorted(schemas_root.rglob("*.schema.json")):
                rel = str(schema_path.relative_to(repo_root_path))
                stem = schema_path.stem.replace(".schema", "")
                hit = (
                    schema_path.name in py_blob_outside_contracts
                    or stem in py_blob_outside_contracts
                )
                if not hit:
                    candidates.append(
                        _make_candidate(
                            candidate_type="schema",
                            candidate_path=rel,
                            candidate_name=stem,
                            reason=(
                                f"Schema {schema_path.name} is not "
                                "referenced by any *.py file under src/"
                            ),
                            evidence={
                                "schema_path": rel,
                                "py_references": 0,
                            },
                            recommended_action="investigate",
                        )
                    )

        # 3. Eval cases (no history in last COMPRESSION_INACTIVITY_DAYS)
        evals_root = repo_root_path / "contracts" / "evals"
        history_dir = evals_dir(repo_root_path)
        cutoff = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(days=COMPRESSION_INACTIVITY_DAYS)
        if evals_root.is_dir():
            for eval_file in sorted(evals_root.glob("*.json")):
                try:
                    data = json.loads(eval_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, list):
                    continue
                for definition in data:
                    if not isinstance(definition, dict):
                        continue
                    artifact_type = definition.get("target_artifact_type")
                    metric_name = (
                        definition.get("metric_name") or definition.get("name")
                    )
                    if not artifact_type or not metric_name:
                        continue
                    history_path = (
                        history_dir / f"{artifact_type}_history.jsonl"
                    )
                    history_records = read_jsonl(history_path)
                    recent_for_metric = []
                    for record in history_records:
                        if record.get("eval_name") != metric_name:
                            continue
                        ts = parse_iso(record.get("recorded_at"))
                        if ts is not None and ts >= cutoff:
                            recent_for_metric.append(record)
                    if not recent_for_metric:
                        rel = str(eval_file.relative_to(repo_root_path))
                        candidates.append(
                            _make_candidate(
                                candidate_type="eval_case",
                                candidate_path=rel,
                                candidate_name=str(metric_name),
                                reason=(
                                    f"Eval '{metric_name}' on artifact_type "
                                    f"'{artifact_type}' has no history in "
                                    f"the last {COMPRESSION_INACTIVITY_DAYS} "
                                    "days"
                                ),
                                evidence={
                                    "eval_file": rel,
                                    "artifact_type": artifact_type,
                                    "days_since_use": (
                                        COMPRESSION_INACTIVITY_DAYS
                                    ),
                                },
                                recommended_action="investigate",
                            )
                        )

        # 4. CLI commands (no usage in 60 days)
        cli_path = src_root / "spectrum_systems_core" / "cli.py"
        cli_commands: list[str] = []
        if cli_path.is_file():
            try:
                cli_text = cli_path.read_text(encoding="utf-8")
            except OSError:
                cli_text = ""
            for match in re.finditer(
                r"sub\.add_parser\(\s*['\"]([\w-]+)['\"]", cli_text
            ):
                cli_commands.append(match.group(1))

        runs = RunHistoryStore().get_recent_runs(repo_root_path, n=10_000)
        recent_runs_text = json.dumps(runs)
        for cmd in cli_commands:
            if cmd in {"audit-governance", "apply-compression"}:
                continue
            if cmd in recent_runs_text:
                continue
            candidates.append(
                _make_candidate(
                    candidate_type="cli_command",
                    candidate_path="src/spectrum_systems_core/cli.py",
                    candidate_name=cmd,
                    reason=(
                        f"CLI command '{cmd}' has no observed usage in the "
                        f"last {COMPRESSION_INACTIVITY_DAYS} days of run "
                        "history"
                    ),
                    evidence={
                        "command_name": cmd,
                        "days_since_use": COMPRESSION_INACTIVITY_DAYS,
                    },
                    recommended_action="investigate",
                )
            )

        # Validate + persist each candidate.
        for candidate in candidates:
            ok, err = validate_governance_artifact(
                candidate, "compression_candidate"
            )
            if not ok:
                _LOG.warning("compression_candidate failed validation: %s", err)
                continue
            target = candidates_dir(repo_root_path) / f"{candidate['candidate_id']}.json"
            write_json(target, candidate)

        flagged_items: list[dict[str, Any]] = []
        for candidate in candidates:
            flagged_items.append(
                {
                    "item_type": candidate["candidate_type"],
                    "item_id": candidate["candidate_id"],
                    "detail": candidate["reason"][:200],
                    "severity": "low",
                    "recommended_action": (
                        f"apply-compression --candidate-id "
                        f"{candidate['candidate_id']} --action "
                        f"{candidate['recommended_action']}"
                    ),
                }
            )

        prior_audit = find_prior_audit(repo_root_path, "compression_scan")
        prior_value = prior_audit.get("current_value") if prior_audit else None
        current_value: dict[str, Any] = {
            "total_candidates": len(candidates),
            "by_type": {
                t: sum(1 for c in candidates if c["candidate_type"] == t)
                for t in ("class", "schema", "eval_case", "cli_command")
            },
        }
        delta = None
        if prior_value is not None:
            delta = {
                "total_candidates": int(len(candidates))
                - int(prior_value.get("total_candidates", 0)),
            }

        status = "drift_detected" if candidates else "clean"
        record = {
            "audit_id": str(uuid.uuid4()),
            "audit_type": "compression_scan",
            "scope": "system_wide",
            "generated_at": utcnow_iso(),
            "current_value": current_value,
            "prior_value": prior_value,
            "delta": delta,
            "flagged_items": flagged_items,
            "total_scanned": files_scanned,
            "total_flagged": len(flagged_items),
            "status": status,
        }
        record["_candidates"] = candidates
        ok, err = validate_governance_artifact(
            {k: v for k, v in record.items() if not k.startswith("_")},
            "governance_audit_record",
        )
        if not ok:
            _LOG.warning("compression_scan audit failed validation: %s", err)
            record["status"] = "error"
            record["flagged_items"] = []
            record["total_flagged"] = 0
            record["_candidates"] = []
        persisted = {k: v for k, v in record.items() if not k.startswith("_")}
        write_audit_record(persisted, repo_root_path)
        return record
