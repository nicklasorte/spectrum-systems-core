"""Phase V pipeline-wiring helpers.

The wiring lives in this module so ``typed_extraction_runner`` only needs
a single, thin call site. Tests exercise the helper directly without
mocking the runner.

Single source of control: ``apply_phase_v_if_enabled`` consults the
feature flag and either short-circuits or runs the verifier, annotates
the meeting_extraction, and writes the verification artifact. On
incomplete coverage the helper raises ``VerificationIncompleteError`` so
the runner fails closed (no half-verified artifact lands on disk).
"""
from __future__ import annotations

import datetime
import json
import logging
import pathlib
import uuid
from collections.abc import Callable
from typing import Any

from ..config.feature_flag import PHASE_V_FLAG_NAME, FeatureFlag
from ._schemas import (
    validate_meeting_extraction_v2,
    validate_source_verification_result,
)
from .model_registry import ModelRegistry
from .post_hoc_verifier import PostHocVerifier, _coerce_item_id

_LOG = logging.getLogger(__name__)


class VerificationIncompleteError(RuntimeError):
    """Raised when the verifier produced fewer entries than the artifact has items."""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def apply_phase_v_if_enabled(
    meeting_extraction: dict[str, Any],
    chunks_by_id: dict[str, dict[str, Any]],
    *,
    data_lake_path: str | pathlib.Path,
    sdl_root: str | pathlib.Path,
    pipeline_run_id: str | None = None,
    api_caller: Callable[[str], dict[str, Any]] | None = None,
    model_registry: ModelRegistry | None = None,
    flag_reader: FeatureFlag | None = None,
) -> dict[str, Any] | None:
    """If the Phase V flag is enabled, run the verifier and annotate.

    Returns the source_verification_result artifact (a dict) when the
    pass runs to completion, or ``None`` when the flag is disabled.

    Raises ``VerificationIncompleteError`` if completeness fails. The
    caller is expected to fail-closed (abort the pipeline) on raise.

    Side effects:
      * mutates ``meeting_extraction`` in place: bumps schema_version to
        ``"2.0.0"``, links ``verification_artifact_id``, stamps
        ``verification_status`` + appends ``"post_hoc_*"`` to
        ``exclusion_reasons`` on each item, and bumps provenance.phase.
      * writes the source_verification_result JSON under
        ``<sdl_root>/verifications/``.
    """
    flag = flag_reader or FeatureFlag(data_lake_path)
    enabled = flag.is_enabled(PHASE_V_FLAG_NAME)
    # Phase S.4: surface the gate state in the orchestrator log so an
    # operator sees "Phase V enabled: True/False" without needing to grep
    # the on-disk flag JSON. The verifier itself is silent on a clean run.
    _LOG.info("Phase V enabled: %s", enabled)
    if not enabled:
        return None

    registry = model_registry or ModelRegistry(sdl_root)
    verifier = PostHocVerifier(
        registry, sdl_root=str(sdl_root), api_caller=api_caller,
    )
    run_id = pipeline_run_id or str(uuid.uuid4())
    verification_result = verifier.verify_extraction(
        meeting_extraction, chunks_by_id, run_id,
    )

    # Completeness check (also enforced again by the gate, but doing it
    # here means we never write a half-verified meeting_extraction).
    summary = verification_result.get("summary") or {}
    halted = summary.get("status") == "halted_sanity_check"

    items_with_verification = len(verification_result["item_verifications"])
    total_items = sum(
        len(meeting_extraction.get(k, []) or [])
        for k in ("decisions", "claims", "action_items")
    )
    if not halted and items_with_verification != total_items:
        raise VerificationIncompleteError(
            f"verification_incomplete: items_with_verification="
            f"{items_with_verification} != total_items={total_items}"
        )
    # Halted runs are expected to be partial. They still must have
    # processed at least EARLY_HALT_SAMPLE_SIZE items -- a halt with
    # 0 entries means the verifier reported halted before producing
    # any output, which the operator must investigate. Fail-closed
    # (RT1 Sev-2 fix).
    if halted and items_with_verification < PostHocVerifier.EARLY_HALT_SAMPLE_SIZE:
        raise VerificationIncompleteError(
            f"verification_halted_with_insufficient_sample: "
            f"items_with_verification={items_with_verification} < "
            f"EARLY_HALT_SAMPLE_SIZE={PostHocVerifier.EARLY_HALT_SAMPLE_SIZE}"
        )

    # Annotate each item with verification_status + exclusion_reasons.
    by_item_id: dict[str, dict[str, Any]] = {
        v["item_id"]: v for v in verification_result["item_verifications"]
    }
    for key in ("decisions", "claims", "action_items"):
        for item in meeting_extraction.get(key, []) or []:
            item_id = _coerce_item_id(item)
            entry = by_item_id.get(item_id)
            if entry is None:
                # When halted, items past the halt point have no entry;
                # mark them verification_failed so the gate blocks.
                item["verification_status"] = "verification_failed"
            else:
                item["verification_status"] = entry["verification_status"]
            _append_post_hoc_exclusion_reason(item)

    # Bump schema metadata for v2.
    meeting_extraction["schema_version"] = "2.0.0"
    meeting_extraction["verification_artifact_id"] = (
        verification_result["source_verification_result_id"]
    )
    if isinstance(meeting_extraction.get("provenance"), dict):
        meeting_extraction["provenance"]["phase"] = "V"

    # Validate before write: catch schema drifts at runtime.
    validate_meeting_extraction_v2(meeting_extraction)
    validate_source_verification_result(verification_result)

    # Write verification artifact.
    write_verification_result(verification_result, sdl_root=sdl_root)
    return verification_result


def _append_post_hoc_exclusion_reason(item: dict[str, Any]) -> None:
    """When an item failed verification, append the matching reason to
    ``exclusion_reasons``. Coexists with existing ``low_confidence``
    reasons; never duplicates.
    """
    status = item.get("verification_status")
    if status == "verified" or status is None:
        return
    code = {
        "unsupported": "post_hoc_unsupported",
        "contradicted": "post_hoc_contradicted",
        "insufficient_evidence": "post_hoc_insufficient_evidence",
        "verification_failed": "post_hoc_verification_failed",
    }.get(status)
    if code is None:
        return
    reasons = item.setdefault("exclusion_reasons", [])
    if not isinstance(reasons, list):
        reasons = []
        item["exclusion_reasons"] = reasons
    if code not in reasons:
        reasons.append(code)

    # Defensive: also flag for HITL so the legacy low_confidence
    # reviewer queue picks it up even when downstream consumers don't
    # know about exclusion_reasons yet.
    item["items_requiring_review"] = True
    existing_reason = item.get("review_reason")
    if not existing_reason:
        item["review_reason"] = code
    elif existing_reason == "low_confidence" and "low_confidence" not in reasons:
        # Preserve both reasons explicitly when both apply.
        reasons.insert(0, "low_confidence")


def write_verification_result(
    verification_result: dict[str, Any],
    *,
    sdl_root: str | pathlib.Path,
) -> pathlib.Path:
    """Atomic write under ``<sdl_root>/verifications/``."""
    root = pathlib.Path(sdl_root) / "verifications"
    root.mkdir(parents=True, exist_ok=True)
    name = (
        f"{verification_result['source_verification_result_id']}_"
        f"source_verification_result.json"
    )
    target = root / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(verification_result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return target
