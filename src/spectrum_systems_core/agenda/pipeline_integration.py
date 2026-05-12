"""Phase W pipeline integration glue.

Single entry point: ``apply_phase_w_if_enabled(...)``. The caller
(``run_typed_extraction``) invokes this after loading chunks and before
classification. The function:

  1. Short-circuits if the Phase W feature flag is disabled.
  2. Constructs an :class:`AgendaDetector`, runs detection.
  3. Writes every produced ``agenda_item`` artifact to disk **before**
     annotating chunks (Attack 12: synchronous order).
  4. Annotates each chunk dict in place with ``agenda_item_id``.
  5. Runs the pre-flight check; raises :class:`AgendaReferenceError`
     if any chunk references a non-existent artifact.
  6. Returns a metrics dict carrying detection outcome + duration so
     the smoke test and RegressionGate have a single source of truth.

The metrics dict shape::

    {
      "agenda_detection_attempted": bool,
      "agenda_detection_succeeded": bool,
      "agenda_items_detected_count": int,
      "detection_method": str,
      "detection_duration_seconds": float,
      "detector_model_used": str,
    }

The companion factory ``make_phase_w_agenda_resolver`` returns a
``chunk -> Optional[str]`` callable that the ChunkClassifier injects
into its prompt. The resolver:

  * returns None when the flag is off (no prompt change),
  * returns None when ``chunk["agenda_item_id"]`` is None or missing
    (and logs a warning when the flag IS on, Attack 2 distinction),
  * returns None when the referenced label is the undetected fallback
    (per task spec: classifier behaves as before in that case),
  * otherwise returns the label string from the on-disk artifact.
"""
from __future__ import annotations

import json
import logging
import pathlib
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ..config import FeatureFlag, PHASE_W_FLAG_NAME
from ..verification.model_registry import ModelRegistry
from .agenda_detector import (
    AgendaDetector,
    AgendaReferenceError,
    UNCATEGORIZED_LABEL,
    build_chunk_to_agenda_mapping,
    validate_agenda_references,
)

_LOG = logging.getLogger(__name__)


def _disabled_metrics() -> Dict[str, Any]:
    return {
        "agenda_detection_attempted": False,
        "agenda_detection_succeeded": False,
        "agenda_items_detected_count": 0,
        "detection_method": "disabled",
        "detection_duration_seconds": 0.0,
        "detector_model_used": "",
    }


def _agenda_dir(sdl_root: pathlib.Path, source_id: str) -> pathlib.Path:
    return sdl_root / "agenda" / source_id


def write_agenda_artifact(
    artifact: Dict[str, Any],
    sdl_root: pathlib.Path,
    source_id: str,
) -> pathlib.Path:
    """Persist one agenda_item artifact deterministically by id.

    File path is ``<sdl_root>/agenda/<source_id>/<agenda_item_id>.json``.
    Idempotent: re-running the pipeline with the same artifact id is a
    plain overwrite.
    """
    target_dir = _agenda_dir(sdl_root, source_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    item_id = artifact.get("agenda_item_id") or str(uuid.uuid4())
    target = target_dir / f"{item_id}.json"
    target.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def apply_phase_w_if_enabled(
    chunks: List[Dict[str, Any]],
    source_id: str,
    *,
    data_lake_path: Union[str, pathlib.Path],
    sdl_root: Union[str, pathlib.Path],
    pipeline_run_id: str,
    model_registry: Optional[ModelRegistry] = None,
    api_caller: Optional[Callable[[str], Dict[str, Any]]] = None,
    flag_name: str = PHASE_W_FLAG_NAME,
) -> Dict[str, Any]:
    """Annotate ``chunks`` with agenda_item_id when the flag is on.

    Mutates ``chunks`` in place to set ``agenda_item_id`` on each. When
    the flag is off the chunks list is left exactly as it was passed in
    (no field added, no field cleared) -- this is the bit that lets
    rollback be a single flag flip.

    Returns the metrics dict described in the module docstring.
    """
    if not FeatureFlag(data_lake_path).is_enabled(flag_name):
        return _disabled_metrics()

    sdl_root_path = pathlib.Path(sdl_root)
    registry = model_registry or ModelRegistry(sdl_root_path)
    detector = AgendaDetector(
        registry, sdl_root=str(sdl_root_path), api_caller=api_caller,
    )
    detection_result = detector.detect(
        chunks=chunks,
        source_id=source_id,
        pipeline_run_id=pipeline_run_id,
    )

    agenda_items: Sequence[Dict[str, Any]] = (
        detection_result.get("agenda_items") or []
    )

    # Attack 12: write every artifact BEFORE annotating chunks. If a
    # write fails we surface immediately rather than leave chunks
    # pointing at a non-existent file. We do not catch OSError here:
    # the data lake is local-filesystem; a write failure is fatal.
    for item in agenda_items:
        write_agenda_artifact(item, sdl_root_path, source_id)

    mapping = build_chunk_to_agenda_mapping(chunks, agenda_items)
    for chunk in chunks:
        cid = chunk.get("chunk_id") or chunk.get("id")
        if isinstance(cid, str) and cid in mapping:
            chunk["agenda_item_id"] = mapping[cid]
        else:
            chunk["agenda_item_id"] = None

    # Attack 12 pre-flight: every non-null agenda_item_id on a chunk
    # must resolve to an on-disk artifact. We re-check against disk so
    # this catches both "detector forgot to emit the artifact" and
    # "write_agenda_artifact failed for one of them".
    _validate_against_disk(chunks, sdl_root_path, source_id)

    return {
        "agenda_detection_attempted": True,
        "agenda_detection_succeeded": bool(
            detection_result.get("detection_succeeded")
        ),
        "agenda_items_detected_count": int(
            detection_result.get("items_count") or 0
        ),
        "detection_method": detection_result.get("detection_method") or "",
        "detection_duration_seconds": float(
            detection_result.get("detection_duration_seconds") or 0.0
        ),
        "detector_model_used": detection_result.get(
            "detector_model_used"
        ) or "",
        "detection_failure_reason": (
            detection_result.get("detection_failure_reason")
        ),
    }


def make_phase_w_agenda_resolver(
    data_lake_path: Union[str, pathlib.Path],
    sdl_root: Union[str, pathlib.Path],
    source_id: str,
    *,
    flag_name: str = PHASE_W_FLAG_NAME,
) -> Callable[[Dict[str, Any]], Optional[str]]:
    """Build the ``chunk -> Optional[str]`` resolver used by ChunkClassifier.

    Resolution rules (RT1 attacks 2 + 7):

      * Flag off: return None for every chunk -- classifier prompt is
        identical to pre-Phase W behaviour.
      * Flag on, chunk has no ``agenda_item_id`` (or it is None): log
        a warning and return None. The warning makes the
        "missing field with flag on" state visible (Attack 2 vs. an
        empty-string fallback that silently degrades).
      * Flag on, agenda_item artifact missing on disk: log a warning
        and return None. The pre-flight should have caught this -- the
        resolver is defensive in case it ran before pre-flight (e.g.
        unit tests).
      * Flag on, label == UNCATEGORIZED_LABEL: return None so the
        prompt is unchanged for undetected transcripts.
      * Otherwise: return the label string.

    Cache key is the resolved ``agenda_item_id`` so a re-run with the
    same id is cached -- but the cache is per-resolver-instance so the
    pipeline gets a fresh resolver per run (Attack 7 cache-invalidation:
    we never need to invalidate because resolvers are short-lived).
    """
    flag_on = FeatureFlag(data_lake_path).is_enabled(flag_name)
    sdl_root_path = pathlib.Path(sdl_root)
    cache: Dict[str, Optional[str]] = {}

    def _resolve(chunk: Dict[str, Any]) -> Optional[str]:
        if not flag_on:
            return None
        if not isinstance(chunk, dict):
            return None
        aid = chunk.get("agenda_item_id")
        if not isinstance(aid, str) or not aid:
            cid = chunk.get("chunk_id") or chunk.get("id") or "?"
            _LOG.warning(
                "agenda_item_id missing on chunk %s while "
                "phase_w_enabled=true", cid,
            )
            return None
        if aid in cache:
            return cache[aid]
        artifact_path = _agenda_dir(sdl_root_path, source_id) / f"{aid}.json"
        if not artifact_path.is_file():
            _LOG.warning(
                "agenda_resolver_artifact_missing: %s "
                "(falling back to no-context prompt)", artifact_path,
            )
            cache[aid] = None
            return None
        try:
            data = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "agenda_resolver_artifact_unreadable: %s: %s", artifact_path, exc,
            )
            cache[aid] = None
            return None
        label = data.get("label")
        if not isinstance(label, str) or not label.strip():
            cache[aid] = None
            return None
        if label.strip() == UNCATEGORIZED_LABEL:
            cache[aid] = None
            return None
        cache[aid] = label.strip()
        return cache[aid]

    return _resolve


def _validate_against_disk(
    chunks: Sequence[Dict[str, Any]],
    sdl_root: pathlib.Path,
    source_id: str,
) -> None:
    """Re-read the agenda dir and check every chunk's agenda_item_id
    resolves to a file on disk.

    Attack 12: the in-memory list is not enough. Catching this against
    disk catches "wrote 4 of 5 items then crashed; chunks still
    annotated".
    """
    referenced = {
        c.get("agenda_item_id") for c in chunks
        if isinstance(c.get("agenda_item_id"), str)
    }
    if not referenced:
        return
    agenda_dir = _agenda_dir(sdl_root, source_id)
    existing = {p.stem for p in agenda_dir.glob("*.json")} if (
        agenda_dir.is_dir()
    ) else set()
    missing = referenced - existing
    if missing:
        raise AgendaReferenceError(
            f"Pipeline pre-flight: chunks reference agenda_item_ids "
            f"that have no artifact on disk under "
            f"{agenda_dir}: {sorted(missing)}"
        )
