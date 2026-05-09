"""I/O helpers for governance stores. Deterministic JSON + JSONL."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def utcnow_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def read_json(path: Path) -> Dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def load_audit_index(repo_root: str | Path) -> Dict[str, Any]:
    from ._paths import audits_index_path

    data = read_json(audits_index_path(repo_root))
    if not isinstance(data, dict) or "audits" not in data:
        return {"audits": [], "last_audit_at": None}
    if not isinstance(data.get("audits"), list):
        return {"audits": [], "last_audit_at": None}
    return data


def find_prior_audit(
    repo_root: str | Path, audit_type: str
) -> Dict[str, Any] | None:
    """Return the most recent audit with the given audit_type, or None."""
    from ._paths import audits_dir

    index = load_audit_index(repo_root)
    matching: List[Dict[str, Any]] = [
        e for e in index.get("audits", []) if e.get("audit_type") == audit_type
    ]
    if not matching:
        return None
    matching.sort(key=lambda e: e.get("generated_at") or "", reverse=True)
    last = matching[0]
    audit_id = last.get("audit_id")
    if not audit_id:
        return None
    return read_json(audits_dir(repo_root) / f"{audit_id}.json")


def write_audit_record(
    record: Dict[str, Any], repo_root: str | Path
) -> None:
    """Write the record to audits/<audit_id>.json and append to index.json."""
    from ._paths import audits_dir, audits_index_path, ensure_governance_tree

    ensure_governance_tree(repo_root)
    audit_id = record["audit_id"]
    target = audits_dir(repo_root) / f"{audit_id}.json"
    write_json(target, record)
    index = load_audit_index(repo_root)
    index["audits"].append(
        {
            "audit_id": audit_id,
            "audit_type": record.get("audit_type"),
            "generated_at": record.get("generated_at"),
            "status": record.get("status"),
            "total_flagged": record.get("total_flagged", 0),
        }
    )
    index["last_audit_at"] = record.get("generated_at")
    write_json(audits_index_path(repo_root), index)
