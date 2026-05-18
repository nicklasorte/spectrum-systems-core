"""Phase Z — shared data-lake locator / instrument reader & writer.

Phase Y instrument artifacts (``opus_ceiling``,
``extraction_alignment_comparison``, ``false_negative_set``,
``improvement_cycle_result``, ``candidate_evaluation``,
``correction_candidate``) and the Phase Z report artifacts
(``dec18_run_report``, ``transcript_ingest_result``,
``corpus_ingest_summary``, ``corpus_improvement_summary``) are NOT
promoted product artifacts — they are draft-status run-level records.
They are written directly with ``serialize.canonical_json`` to
``<store>/processed/meetings/<meeting_id>/<artifact_type>__<artifact_id>.json``
(the same write path the Phase AB comparison_runner uses), never
through ``write_promoted_artifact`` (which refuses non-promoted
status).

This module is the single place Phase Z scripts resolve the data-lake
root, enumerate those instrument files, pick the most recent one, and
write a new one — so the path convention and the "most recent by
produced_at" rule are defined once, not copy-pasted into four scripts.
Underscore-prefixed so the CLAUDE.md integration-compliance scan
treats it as infra (covered transitively by the four script
integration tests), not as a standalone artifact-reading script.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from spectrum_systems_core.data_lake.paths import (
    processed_corpus_dir,
    processed_meeting_dir,
)
from spectrum_systems_core.data_lake.serialize import (
    artifact_to_dict,
    canonical_json,
)

# The single corpus the Phase Z multi-transcript machinery operates
# over. Corpus-level instruments (corpus_ingest_summary,
# corpus_improvement_summary) live under
# processed/corpus/<CORPUS_ID>/ — a sibling of processed/meetings/,
# per data_lake_contract.md, so a corpus roll-up never collides with
# a single meeting.
CORPUS_ID = "corpus-main"


def data_lake_store_root(explicit: str | None = None) -> Path | None:
    """Return ``<DATA_LAKE_PATH>/store`` (or ``<explicit>/store``), or
    ``None`` when the path is unset / does not exist.

    Fail-closed: a missing data-lake is ``None`` here and the caller
    halts with ``environment_not_ready`` — never a silent empty run.
    """
    raw = explicit if explicit is not None else os.environ.get(
        "DATA_LAKE_PATH", ""
    )
    if not raw:
        return None
    base = Path(raw)
    if not base.exists():
        return None
    return base / "store"


def produced_at_of(envelope: dict[str, Any]) -> str:
    """The recency key for an instrument envelope.

    ``payload.produced_at`` (Phase Z report artifacts carry it) wins;
    otherwise the envelope ``created_at`` (every Phase Y artifact has
    one). Empty string when neither is present so ``max(...)`` is
    still total and deterministic.
    """
    payload = envelope.get("payload")
    if isinstance(payload, dict):
        pa = payload.get("produced_at")
        if isinstance(pa, str) and pa:
            return pa
    ca = envelope.get("created_at")
    return ca if isinstance(ca, str) else ""


def _instrument_files(
    store: Path, meeting_id: str, artifact_type: str
) -> list[Path]:
    meeting_dir = processed_meeting_dir(store, meeting_id)
    if not meeting_dir.is_dir():
        return []
    return sorted(meeting_dir.glob(f"{artifact_type}__*.json"))


def iter_instruments(
    store: Path, meeting_id: str, artifact_type: str
) -> list[dict[str, Any]]:
    """Every readable instrument envelope of ``artifact_type`` for
    ``meeting_id``, sorted ascending by ``produced_at`` then path.

    Unreadable / non-JSON files are skipped (a corrupt sibling never
    crashes a read-only dashboard) — the caller decides whether an
    empty result is itself a halt.
    """
    out: list[tuple[str, str, dict[str, Any]]] = []
    for path in _instrument_files(store, meeting_id, artifact_type):
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(env, dict):
            continue
        out.append((produced_at_of(env), path.name, env))
    out.sort(key=lambda t: (t[0], t[1]))
    return [env for _pa, _name, env in out]


def latest_instrument(
    store: Path, meeting_id: str, artifact_type: str
) -> dict[str, Any] | None:
    """The most recently produced instrument envelope, or ``None``."""
    envs = iter_instruments(store, meeting_id, artifact_type)
    return envs[-1] if envs else None


def write_instrument(store: Path, meeting_id: str, artifact: Any) -> Path:
    """Write a draft instrument artifact to its canonical path.

    ``<store>/processed/meetings/<meeting_id>/<artifact_type>__<artifact_id>.json``
    with deterministic ``canonical_json``. Two writes of the same
    artifact produce a byte-identical file (the Phase AB precedent).
    """
    meeting_dir = processed_meeting_dir(store, meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{artifact.artifact_type}__{artifact.artifact_id}.json"
    target = meeting_dir / filename
    target.write_text(
        canonical_json(artifact_to_dict(artifact)), encoding="utf-8"
    )
    return target


def write_corpus_instrument(store: Path, artifact: Any) -> Path:
    """Write a corpus-level instrument under
    ``<store>/processed/corpus/<CORPUS_ID>/<type>__<id>.json``."""
    corpus_dir = processed_corpus_dir(store, CORPUS_ID)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{artifact.artifact_type}__{artifact.artifact_id}.json"
    target = corpus_dir / filename
    target.write_text(
        canonical_json(artifact_to_dict(artifact)), encoding="utf-8"
    )
    return target


def latest_corpus_instrument(
    store: Path, artifact_type: str
) -> dict[str, Any] | None:
    """Most recently produced corpus-level instrument of a type."""
    corpus_dir = processed_corpus_dir(store, CORPUS_ID)
    if not corpus_dir.is_dir():
        return None
    out: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(corpus_dir.glob(f"{artifact_type}__*.json")):
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(env, dict):
            out.append((produced_at_of(env), path.name, env))
    out.sort(key=lambda t: (t[0], t[1]))
    return out[-1][2] if out else None
