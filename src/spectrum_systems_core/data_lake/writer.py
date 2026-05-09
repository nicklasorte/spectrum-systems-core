"""Writer for promoted product artifacts and learning artifacts.

Contract: docs/contracts/data_lake_contract.md sections 6 and 6A.

Only artifacts with `status == "promoted"` may be written through
`write_promoted_artifact`. eval_result, control_decision, context_bundle,
and other run-internal artifacts are not product artifacts and are
rejected here.

Learning artifacts (`failure_record`, `eval_case_candidate`,
`reviewed_eval_case`) are written through `write_learning_artifact` into
their dedicated subdirectories. Learning artifacts are not promoted
products and are not subject to the `status == "promoted"` rule.
"""
from __future__ import annotations

from pathlib import Path

from ..artifacts import Artifact
from .paths import (
    EVAL_CANDIDATES_SUBDIR,
    FAILURES_SUBDIR,
    REVIEWED_EVALS_SUBDIR,
    eval_candidates_dir,
    failures_dir,
    processed_meeting_dir,
    reviewed_evals_dir,
)
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


# Learning artifact persistence -------------------------------------------------

FAILURE_RECORD_TYPE = "failure_record"
EVAL_CASE_CANDIDATE_TYPE = "eval_case_candidate"
REVIEWED_EVAL_CASE_TYPE = "reviewed_eval_case"

_LEARNING_TYPE_TO_SUBDIR: dict[str, str] = {
    FAILURE_RECORD_TYPE: FAILURES_SUBDIR,
    EVAL_CASE_CANDIDATE_TYPE: EVAL_CANDIDATES_SUBDIR,
    REVIEWED_EVAL_CASE_TYPE: REVIEWED_EVALS_SUBDIR,
}

LEARNING_ARTIFACT_TYPES: frozenset[str] = frozenset(_LEARNING_TYPE_TO_SUBDIR)


def _learning_meeting_id(artifact: Artifact, override: str | None) -> str:
    if override is not None:
        return override
    meeting_id = artifact.payload.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        raise WriterError(
            f"learning artifact {artifact.artifact_id} payload missing "
            "meeting_id; learning artifacts must carry meeting_id for routing"
        )
    return meeting_id


def _learning_dir(
    lake_root: Path | str, artifact_type: str, meeting_id: str
) -> Path:
    if artifact_type == FAILURE_RECORD_TYPE:
        return failures_dir(lake_root, meeting_id)
    if artifact_type == EVAL_CASE_CANDIDATE_TYPE:
        return eval_candidates_dir(lake_root, meeting_id)
    if artifact_type == REVIEWED_EVAL_CASE_TYPE:
        return reviewed_evals_dir(lake_root, meeting_id)
    raise WriterError(
        f"unknown learning artifact type {artifact_type!r}; allowed: "
        f"{sorted(LEARNING_ARTIFACT_TYPES)}"
    )


def write_learning_artifact(
    lake_root: Path | str,
    artifact: Artifact,
    *,
    meeting_id: str | None = None,
) -> Path:
    """Persist a learning artifact under its dedicated subdirectory.

    Path is `processed/meetings/<meeting_id>/<subdir>/<artifact_id>.json`,
    where `<subdir>` is `failures`, `eval_candidates`, or `reviewed_evals`
    depending on `artifact.artifact_type`. The full envelope is written
    as canonical JSON. Two writes of the same artifact produce a
    byte-identical file.

    Learning artifacts are not products and are not subject to the
    `status == 'promoted'` rule. Promoted-product artifact_types are
    rejected here so the two writers cannot accidentally cross.
    """
    if artifact.artifact_type not in LEARNING_ARTIFACT_TYPES:
        raise WriterError(
            f"refused to write artifact_type {artifact.artifact_type!r} as a "
            f"learning artifact; allowed: {sorted(LEARNING_ARTIFACT_TYPES)}"
        )

    target_meeting_id = _learning_meeting_id(artifact, meeting_id)
    target_dir = _learning_dir(lake_root, artifact.artifact_type, target_meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    if not artifact.artifact_id or "/" in artifact.artifact_id or "\\" in artifact.artifact_id:
        raise WriterError(
            f"learning artifact has unsafe artifact_id {artifact.artifact_id!r}"
        )

    target_path = target_dir / f"{artifact.artifact_id}.json"
    data = canonical_json(artifact_to_dict(artifact))
    target_path.write_text(data, encoding="utf-8")
    return target_path
