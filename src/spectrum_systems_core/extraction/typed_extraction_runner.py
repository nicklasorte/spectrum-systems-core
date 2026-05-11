"""Run the typed extraction pipeline for a single source.

Phase M3.0 + M3.1. Glue code that:
1. Loads chunks.jsonl for ``source_id`` from the processed tree.
2. Classifies each chunk via ``ChunkClassifier`` (with regulatory-verb
   fallback).
3. Routes classified chunks to the three typed extractors.
4. Merges results into a ``meeting_extraction`` artifact and writes
   it atomically under ``<SDL_ROOT>/extractions/``.

This module is invoked from both the CLI (``spectrum-core extract-typed``)
and ``PipelineOrchestrator.run_typed_extraction``. It is deliberately
side-effect-bounded: it reads chunks + glossary, writes one artifact, and
returns a summary dict. Failures degrade to a ``status="failure"`` dict
with a ``reason`` field; never raises.
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from .action_item_extractor import ActionItemExtractor
from .chunk_classifier import ChunkClassifier
from .claim_extractor import ClaimExtractor
from .decision_extractor import DecisionExtractor
from .extraction_merger import ExtractionMerger
from .glossary_manager import GlossaryManager


_SOURCE_FAMILIES = (
    "meetings", "books", "comments", "working_papers", "notes",
)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_store_root(data_lake: Optional[str] = None) -> Optional[Path]:
    raw = data_lake or os.environ.get("DATA_LAKE_PATH") or ""
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        return None
    return p / "store"


def _resolve_sdl_root(data_lake: Optional[str] = None) -> Optional[Path]:
    env_sdl = os.environ.get("SDL_ROOT", "").strip()
    if env_sdl:
        return Path(env_sdl)
    store = _resolve_store_root(data_lake)
    if store is None:
        return None
    return store / "artifacts"


def _resolve_glossary_root(sdl_root: Optional[Path]) -> Optional[Path]:
    env_glossary = os.environ.get("SDL_GLOSSARY", "").strip()
    if env_glossary:
        return Path(env_glossary)
    if sdl_root is not None:
        return sdl_root.parent / "glossary" if sdl_root.name == "artifacts" else sdl_root / "glossary"
    return None


def _find_chunks_path(store_root: Path, source_id: str) -> Optional[Path]:
    for family in _SOURCE_FAMILIES:
        p = store_root / "processed" / family / source_id / "stories" / "chunks.jsonl"
        if p.is_file():
            return p
    return None


def _find_source_artifact_id(store_root: Path, source_id: str) -> Optional[str]:
    for family in _SOURCE_FAMILIES:
        sr_path = store_root / "processed" / family / source_id / "source_record.json"
        if sr_path.is_file():
            try:
                data = json.loads(sr_path.read_text(encoding="utf-8"))
                aid = data.get("artifact_id")
                if isinstance(aid, str) and aid:
                    return aid
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _load_chunks(path: Path) -> List[Dict[str, Any]]:
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


def _meeting_extraction_path(sdl_root: Path, source_artifact_id: str) -> Path:
    return sdl_root / "extractions" / f"{source_artifact_id}_meeting_extraction.json"


def run_typed_extraction(
    source_id: str,
    *,
    data_lake: Optional[str] = None,
    force: bool = False,
    api_callers: Optional[Dict[str, Callable[[str], Dict[str, Any]]]] = None,
    glossary_manager: Optional[GlossaryManager] = None,
) -> Dict[str, Any]:
    """Run the typed-extraction pipeline for one source_id.

    Returns ``{"status": "success"|"skipped"|"failure", ...}``.
    Never raises.
    """
    if not source_id:
        return {"status": "failure", "reason": "source_id_required"}

    store_root = _resolve_store_root(data_lake)
    if store_root is None:
        return {"status": "failure", "reason": "data_lake_not_found"}

    chunks_path = _find_chunks_path(store_root, source_id)
    if chunks_path is None:
        return {
            "status": "failure",
            "reason": f"chunks_jsonl_not_found:source_id={source_id}",
        }

    source_artifact_id = _find_source_artifact_id(store_root, source_id)
    if not source_artifact_id:
        # No source_record artifact_id found; fabricate a deterministic id
        # so the output path is still stable. Track this in the reason so
        # downstream tools can flag it.
        source_artifact_id = str(uuid.UUID(bytes=(source_id + "x" * 16).encode("utf-8")[:16]))

    sdl_root = _resolve_sdl_root(data_lake)
    if sdl_root is None:
        return {"status": "failure", "reason": "sdl_root_not_found"}

    out_path = _meeting_extraction_path(sdl_root, source_artifact_id)
    if out_path.exists() and not force:
        return {
            "status": "skipped",
            "reason": "meeting_extraction_exists",
            "path": str(out_path),
        }

    chunks = _load_chunks(chunks_path)
    if not chunks:
        return {
            "status": "failure",
            "reason": f"chunks_jsonl_empty:source_id={source_id}",
        }

    available_turn_ids: Set[str] = set()
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id")
        if isinstance(cid, str) and cid:
            available_turn_ids.add(cid)

    # Glossary
    if glossary_manager is None:
        glossary_root = _resolve_glossary_root(sdl_root)
        glossary_manager = GlossaryManager(
            str(glossary_root) if glossary_root else None
        )

    api_callers = api_callers or {}
    classifier = ChunkClassifier(api_caller=api_callers.get("classifier"))
    decision_x = DecisionExtractor(api_caller=api_callers.get("decision"))
    claim_x = ClaimExtractor(api_caller=api_callers.get("claim"))
    action_x = ActionItemExtractor(api_caller=api_callers.get("action_item"))

    classifications: List[Dict[str, Any]] = []
    bucket: Dict[str, List[Dict[str, Any]]] = {
        "decision": [], "claim": [], "action_item": [], "off_topic": [],
    }
    for chunk in chunks:
        cls = classifier.classify(chunk, source_id)
        classifications.append(cls)
        bucket[cls["classification"]].append(chunk)

    # Glossary context block is rebuilt per group (cheap; one call per
    # extractor here). Concatenate texts from this group to pick relevant
    # terms.
    def _block_for(group: Sequence[Dict[str, Any]]) -> str:
        text = " ".join((c.get("text") or "") for c in group)
        terms = glossary_manager.retrieve_for_chunk(text)
        return glossary_manager.format_for_prompt(terms)

    decisions = decision_x.extract(
        bucket["decision"], _block_for(bucket["decision"]), available_turn_ids,
    )
    claims = claim_x.extract(
        bucket["claim"], _block_for(bucket["claim"]), available_turn_ids,
    )
    actions = action_x.extract(
        bucket["action_item"], _block_for(bucket["action_item"]),
        available_turn_ids,
    )

    extraction_run_id = "tex-" + uuid.uuid4().hex[:16]
    artifact = ExtractionMerger().merge(
        source_artifact_id=source_artifact_id,
        extraction_run_id=extraction_run_id,
        classifications=classifications,
        decisions=decisions,
        claims=claims,
        action_items=actions,
    )

    try:
        ExtractionMerger.write_to(artifact, out_path)
    except OSError as exc:
        return {
            "status": "failure",
            "reason": f"write_error:{exc}",
        }

    return {
        "status": "success",
        "source_id": source_id,
        "source_artifact_id": source_artifact_id,
        "path": str(out_path),
        "decisions": len(artifact["decisions"]),
        "claims": len(artifact["claims"]),
        "action_items": len(artifact["action_items"]),
        "total_chunks_classified": artifact["total_chunks_classified"],
        "off_topic_count": artifact["off_topic_count"],
        "regulatory_verb_fallback_count": artifact["regulatory_verb_fallback_count"],
        "routing_quality_warning": artifact["routing_quality_warning"],
        "requires_human_dedup_count": artifact["requires_human_dedup_count"],
        "extraction_run_id": extraction_run_id,
    }


def find_meeting_extraction(
    source_artifact_id: str,
    data_lake: Optional[str] = None,
) -> Optional[Path]:
    """Return the path to ``<source_artifact_id>_meeting_extraction.json``
    if it exists, else None. Used by EvalAligner integration to decide
    whether to use typed-extraction output as alignment input.
    """
    sdl_root = _resolve_sdl_root(data_lake)
    if sdl_root is None:
        return None
    p = _meeting_extraction_path(sdl_root, source_artifact_id)
    return p if p.is_file() else None
