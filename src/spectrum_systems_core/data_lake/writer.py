"""Writer for promoted product artifacts.

Contract: docs/contracts/data_lake_contract.md section 6.

Only artifacts with `status == "promoted"` may be written through
`write_promoted_artifact`. eval_result, control_decision, context_bundle,
and other run-internal artifacts are not product artifacts and are
rejected here.
"""
from __future__ import annotations

from pathlib import Path

from ..artifacts import Artifact
from .paths import processed_meeting_dir
from .serialize import artifact_to_dict, canonical_json, slugify

_RUN_INTERNAL_TYPES: frozenset[str] = frozenset(
    {"context_bundle", "eval_result", "control_decision"}
)


class WriterError(ValueError):
    """Raised when a write would violate the data lake contract."""


def _meeting_id_from_artifact(artifact: Artifact) -> str:
    meeting_id = artifact.payload.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        raise WriterError(
            f"artifact {artifact.artifact_id} payload missing meeting_id; "
            "promoted artifacts must carry meeting_id for routing"
        )
    return meeting_id


def _slug_for(artifact: Artifact, slug: str | None) -> str:
    if slug is not None:
        cleaned = slugify(slug)
        if "__" in slug:
            raise WriterError(
                f"slug must not contain '__'; got {slug!r}"
            )
        return cleaned
    short_hash = artifact.content_hash[:12]
    title = artifact.payload.get("title")
    if isinstance(title, str) and title.strip():
        return f"{slugify(title)}-{short_hash}"
    return short_hash


def write_promoted_artifact(
    lake_root: Path | str,
    artifact: Artifact,
    *,
    slug: str | None = None,
    meeting_id: str | None = None,
) -> Path:
    """Write one promoted artifact under processed/meetings/<meeting_id>/.

    Returns the path written. Two calls with identical inputs produce a
    byte-identical file.
    """
    if artifact.status != "promoted":
        raise WriterError(
            f"refused to write artifact with status {artifact.status!r}; "
            "only promoted artifacts may be written as products"
        )
    if artifact.artifact_type in _RUN_INTERNAL_TYPES:
        raise WriterError(
            f"refused to write run-internal artifact_type "
            f"{artifact.artifact_type!r} as a product"
        )

    target_meeting_id = meeting_id or _meeting_id_from_artifact(artifact)
    target_dir = processed_meeting_dir(lake_root, target_meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    slug_value = _slug_for(artifact, slug)
    if "__" in artifact.artifact_type:
        raise WriterError(
            f"artifact_type {artifact.artifact_type!r} contains the "
            "reserved '__' separator"
        )
    filename = f"{artifact.artifact_type}__{slug_value}.json"
    target_path = target_dir / filename

    data = canonical_json(artifact_to_dict(artifact))
    target_path.write_text(data, encoding="utf-8")
    return target_path
