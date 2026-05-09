from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ArtifactStatus = str

ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"draft", "evaluated", "promoted", "rejected"}
)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_content_hash(payload: Any) -> str:
    canonical = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class Artifact:
    artifact_type: str
    schema_version: int
    status: ArtifactStatus
    payload: dict[str, Any]
    trace_id: str
    input_refs: list[str] = field(default_factory=list)
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    content_hash: str = ""

    def __post_init__(self) -> None:
        from .validation import ensure_valid_status

        ensure_valid_status(self.status)
        if not self.content_hash:
            self.content_hash = compute_content_hash(self.payload)


def new_artifact(
    artifact_type: str,
    payload: dict[str, Any],
    trace_id: str,
    *,
    schema_version: int = 1,
    status: ArtifactStatus = "draft",
    input_refs: list[str] | None = None,
) -> Artifact:
    return Artifact(
        artifact_type=artifact_type,
        schema_version=schema_version,
        status=status,
        payload=payload,
        trace_id=trace_id,
        input_refs=list(input_refs or []),
    )
