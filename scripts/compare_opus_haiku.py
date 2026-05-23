"""Deterministic Haiku-vs-Opus extraction comparison (System 1).

Reads the Opus reference baseline and the promoted Haiku
``meeting_minutes`` artifact for one ``source_id`` and emits a
``comparison_result`` artifact with recall / precision / F1 by
extraction type.

ZERO LLM CALLS. The comparison is pure, case-insensitive,
whitespace-normalized substring matching — the SAME match rule the
``extraction_within_source_required`` eval uses. This module never
imports ``anthropic`` and never imports the LLM client, so the
"no model call in the comparison" property is verifiable by static
scan (``tests/test_compare_opus_haiku.py`` asserts it). Comparing a
regex-extractor artifact against an Opus baseline is meaningless, so
the Haiku artifact's ``provenance.produced_by`` MUST be
``meeting_minutes_llm`` or the script halts fail-closed.

Fail-closed reason codes:

* ``missing_opus_baseline``       — no opus_reference_minutes.jsonl
* ``missing_haiku_llm_output``    — no promoted meeting_minutes artifact
                                    with provenance produced_by ==
                                    "meeting_minutes_llm"
* ``empty_haiku_artifact``        — every LLM meeting_minutes artifact
                                    for the source has all extraction
                                    arrays empty (would emit a lying
                                    0-item diff)
* ``invalid_haiku_artifact``      — artifact present but fails the
                                    meeting_minutes schema
* ``missing_candidate_artifact``  — three-way mode: no populated LLM
                                    meeting_minutes artifact whose
                                    provenance.model_id contains the
                                    requested model token (e.g. Sonnet)
* ``empty_candidate_artifact``    — three-way mode: every matching
                                    candidate artifact is all-empty
* ``data_lake_not_a_directory``   — --data-lake is not a directory

Three-way mode (``--include-sonnet``): a second candidate
(``meeting_minutes`` produced by ``meeting_minutes_llm`` whose
``provenance.model_id`` contains ``"sonnet"``) is diffed against the
SAME Opus baseline as Haiku and both results are merged into one
``comparison_result`` with ``comparison_mode == "three_way"``. It is
written to a DISTINCT path (``comparisons/three_way_<ts>.json``) so the
two-way artifact is never overwritten. Default (no ``--include-sonnet``)
output is byte-for-byte the legacy two-way shape.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# scripts/ on sys.path so the artifact validator import works whether
# this file is run as a script or imported as a module by tests / by
# the correction miner.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from spectrum_systems_core.promotion.gate import (  # noqa: E402
    GROUNDING_BINDING_SCHEMA_VERSION,
)

_REPO_ROOT = _SCRIPTS_DIR.parent
_MEETING_MINUTES_SCHEMA = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "schemas"
    / "meeting_minutes.schema.json"
)

COMPARISON_ARTIFACT_TYPE = "comparison_result"
COMPARISON_SCHEMA_VERSION = "1.0.0"
HAIKU_LLM_PROVENANCE = "meeting_minutes_llm"

# Phase 1: schema_version at which verbatim span grounding became
# binding. Comparisons against a 1.4.0+ Haiku artifact re-run the gate
# on the artifact's promoted items so tampering or transcript mutation
# is caught BEFORE the comparison's metrics are written. The value is
# imported from the SINGLE canonical source on the gate module so a
# schema bump there flows through the comparison engine, the cascade
# synthetic envelope below, and the Opus reference-baseline writer in
# one edit. Re-aliased to a private name to keep the existing read
# sites local-scope as before.
_GROUNDING_BINDING_SCHEMA_VERSION = GROUNDING_BINDING_SCHEMA_VERSION
_MIXED_SCHEMA_REASON = "schema_version_mixed"
_PROMPT_DRIFT_REASON = "prompt_drift_post_merge"

# Phase 2.B: chunking-strategy cross-check halt. Fires when the two
# artifacts under comparison declare different chunking strategies
# (e.g. one was produced with CHUNK_OVERLAP_TURNS=0, the other with
# CHUNK_OVERLAP_TURNS=2). A cross-strategy F1 number is misleading
# because the input to the model was structurally different; the
# operator must produce a matched baseline before measuring.
_CHUNKING_STRATEGY_MISMATCH_REASON = "chunking_strategy_mismatch"
# Pre-Phase-2.B artifacts omit the provenance field entirely; the
# reader treats a missing value as this default so legacy artifacts
# remain comparable with default-off Phase-2.B artifacts (both
# "speaker_turn_v1") and the halt does not fire on no-op rolls.
_DEFAULT_CHUNKING_STRATEGY_VERSION = "speaker_turn_v1"

# The model token that a candidate with NO ``provenance.model_id`` is
# treated as. Historically every ``produced_by == "meeting_minutes_llm"``
# artifact was the default Haiku extraction (the registry default IS
# Haiku) and legacy artifacts did not stamp ``provenance.model_id``.
# ``find_candidate_artifact(..., "haiku")`` must therefore still accept
# such an artifact so the refactor changes NO existing behaviour. A
# Sonnet (or any non-default) run, by contrast, ALWAYS stamps an
# explicit ``provenance.model_id`` (the real workflow resolves it from
# the registry), so absence-of-model_id never silently counts as a
# non-default model — selecting Sonnet is fail-closed.
_DEFAULT_CANDIDATE_MODEL_TOKEN = "haiku"

# Text fields tried, in priority order, when a Haiku payload item is a
# structured object. This is a LOCAL, byte-identical copy of
# ``scripts/create_opus_reference_baselines._GROUND_TRUTH_TEXT_FIELDS``
# so ``_item_text`` resolves a structured item to the EXACT same string
# the Opus baseline producer's ``extract_ground_truth_text`` resolved it
# to — an asymmetric reader would make the Haiku-vs-Opus diff lie (the
# exact bug that read 0 Haiku items off an artifact whose object-form
# ``decisions`` had been extracted and grounded). The canonical
# extraction prompt lets the model return a structured object for ANY
# type (``decisions`` in particular arrive as
# ``{"text","verb","stakeholders","confidence","rationale"}``), so the
# reader must NOT be keyed on a per-type field. Kept LOCAL (never
# imported) so this module never transitively imports the LLM client and
# the zero-LLM property stays a static fact;
# ``tests/test_compare_opus_haiku.py`` asserts this tuple stays
# byte-identical to the producer's so they cannot drift.
_GROUND_TRUTH_TEXT_FIELDS = (
    "text",
    "question_text",
    "commitment_text",
    "risk_text",
    "reference_text",
    "parameter_name",
    "position_text",
    "objection_text",
    "input_text",
    "ruling_text",
    "term",
    "name",
    "title",
    "phase_name",
    "reference",
)

# Per-type maps retained ONLY as the documented cross-script mirror that
# ``tests/test_compare_opus_haiku.py`` asserts stays in sync with
# ``create_opus_reference_baselines``. They are NOT the text-resolution
# authority any more: ``_item_text`` reads structured items through the
# shared tolerant ``_GROUND_TRUTH_TEXT_FIELDS`` resolver above, exactly
# as the baseline producer's ``extract_ground_truth_text`` does, so the
# two readers are symmetric by construction. The producer treats its own
# ``_LEGACY_OBJECT_TEXT_FIELD`` the same way (retained-but-unused).
_PRIMARY_TEXT_FIELD: Dict[str, Optional[str]] = {
    "decisions": None,
    "action_items": None,
    "open_questions": None,
    "commitments": "commitment_text",
    "risks": "risk_text",
    "cross_references": "ref_text",
    "attendees": "name",
    "topics": "title",
    "regulatory_references": "reference_text",
    "technical_parameters": "value",
    "named_artifacts": "name",
    "scheduled_events": "title",
    "claims": "claim_text",
    "sentiment_indicators": "text_preview",
    "meeting_phases": "phase_name",
    # 1.3.0 additions — MUST stay byte-equal to the baseline producer's
    # map (asserted by tests/test_compare_opus_haiku.py); an asymmetric
    # reader would make the Haiku-vs-Opus diff lie.
    "issue_registry_entry": "title",
    "position_statement": "position_text",
    "dissent_or_objection": "objection_text",
    "agenda_item": "title",
    "precedent_reference": "reference_text",
    "external_stakeholder_input": "input_text",
    "glossary_definition": "term",
    "procedural_ruling": "ruling_text",
}

# Vestigial cross-script mirror (see the block comment above
# ``_PRIMARY_TEXT_FIELD``): kept byte-equal to the producer's map so the
# sync assertion holds; no longer consulted by ``_item_text``.
_LEGACY_OBJECT_TEXT_FIELD: Dict[str, str] = {
    "action_items": "action",
    "open_questions": "question_text",
}


class ComparisonError(RuntimeError):
    """Fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Match function — deterministic and SYMMETRIC by construction.
# --------------------------------------------------------------------------
def text_match(a: str, b: str) -> bool:
    """Case-insensitive, whitespace-normalized substring match.

    Symmetric: ``a in b or b in a`` is invariant under swapping ``a``
    and ``b`` (the disjunction is commutative and each operand is the
    mirror of the other). Same rule as the
    ``extraction_within_source_required`` eval. No embeddings, no fuzzy
    similarity — deterministic text only.
    """
    a_norm = " ".join((a or "").lower().split())
    b_norm = " ".join((b or "").lower().split())
    if not a_norm or not b_norm:
        return False
    return a_norm in b_norm or b_norm in a_norm


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def extraction_types() -> List[str]:
    """The extraction types, derived from the meeting_minutes schema.

    Every array property except ``grounding`` (Phase Y meta, not a
    content category). Deriving from the schema means a new type added
    there is automatically compared — no parallel list to drift.
    """
    try:
        schema = json.loads(
            _MEETING_MINUTES_SCHEMA.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(
            "missing_extraction_schema",
            f"cannot read meeting_minutes schema at "
            f"{_MEETING_MINUTES_SCHEMA}: {exc}",
        ) from exc
    props = schema.get("properties", {})
    types: List[str] = []
    for key, spec in props.items():
        if key == "grounding":
            continue
        if isinstance(spec, dict) and spec.get("type") == "array":
            types.append(key)
    return types


def _item_text(etype: str, item: Any) -> str:
    """Comparable string for one Haiku payload item.

    Mirrors ``create_opus_reference_baselines.extract_ground_truth_text``
    EXACTLY (type-agnostic tolerant resolution) so the Haiku reader and
    the Opus baseline producer can never read the same item differently
    — an asymmetric reader makes the diff lie. Resolution order:

    1. A plain string is returned as-is (whitespace-stripped; the Opus
       side is stripped by ``opus_items_by_type`` and ``text_match``
       whitespace-normalizes, so the strip is immaterial to matching).
    2. For a dict, the first present, non-empty *string* field from
       ``_GROUND_TRUTH_TEXT_FIELDS`` (priority order) wins — so an
       object-form ``decisions`` item resolves on ``text`` exactly like
       the producer, instead of being dropped as ``''``.
    3. Else the first non-empty string value anywhere in the dict.
    4. Else (no string content / a non-dict, non-string item) ``str()``
       of the item — the producer's never-drop fallback; mirrored so a
       pathological item is read identically on both sides rather than
       being silently dropped on only the Haiku side (which would itself
       be the asymmetry this fix removes).

    ``etype`` is accepted for call-site symmetry and a future per-type
    override seam; the resolution is deliberately type-agnostic because
    the canonical extraction prompt's object form is.
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for field in _GROUND_TRUTH_TEXT_FIELDS:
            val = item.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in item.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    return str(item).strip()


# --------------------------------------------------------------------------
# Loaders.
# --------------------------------------------------------------------------
def _meeting_dir(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake / "store" / "processed" / "meetings" / source_id
    )


def _opus_baseline_path(data_lake: Path, source_id: str) -> Path:
    """Path to the Opus reference baseline JSONL for one source.

    Single source of truth for the path so the ``--print-inputs`` debug
    readout cannot drift from the loader that actually reads it. Pure
    path construction — not comparison logic.
    """
    return (
        _meeting_dir(data_lake, source_id)
        / "reference_baselines"
        / "opus_reference_minutes.jsonl"
    )


def load_opus_baseline(
    data_lake: Path, source_id: str
) -> List[Dict[str, Any]]:
    """Read opus_reference_minutes.jsonl, or HALT missing_opus_baseline."""
    path = _opus_baseline_path(data_lake, source_id)
    if not path.is_file():
        raise ComparisonError(
            "missing_opus_baseline",
            f"no Opus reference baseline at {path}",
        )
    rows: List[Dict[str, Any]] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ComparisonError(
                "invalid_opus_baseline",
                f"non-JSON line {lineno} in {path}: {exc}",
            ) from exc
        if not isinstance(rec, dict):
            raise ComparisonError(
                "invalid_opus_baseline",
                f"line {lineno} in {path} is "
                f"{type(rec).__name__}, expected an object",
            )
        # Fail-closed: a baseline row missing extraction_type or
        # ground_truth_text would silently shrink the recall
        # denominator (making Haiku look better than it is). A drifted
        # baseline halts rather than inflating the metric.
        etype = rec.get("extraction_type")
        gtext = rec.get("ground_truth_text")
        if not isinstance(etype, str) or not etype.strip():
            raise ComparisonError(
                "invalid_opus_baseline",
                f"line {lineno} in {path} has no usable "
                f"extraction_type",
            )
        if not isinstance(gtext, str) or not gtext.strip():
            raise ComparisonError(
                "invalid_opus_baseline",
                f"line {lineno} in {path} has no usable "
                f"ground_truth_text",
            )
        rows.append(rec)
    return rows


def _haiku_recency_key(path: Path) -> Tuple[float, str]:
    """Order Haiku candidates oldest → newest (``max()`` picks newest).

    The selector must NOT order by filename: the on-disk filename is
    ``meeting_minutes__<artifact_id>.json`` and ``artifact_id`` is a
    content hash, so a stale all-empty run from an earlier extraction
    can sort BEFORE the current real one — the exact bug, where a
    0-array artifact named ``...67ccaa13dda9.json`` shadowed the real
    ``...eecbe9e2de04.json`` and halted the comparison at
    ``haiku_item_count == 0``. The envelope ``created_at`` is no help
    either: ``data_lake/pipeline.py`` freezes it to
    ``1970-01-01T00:00:00+00:00`` for determinism, and the
    meeting_minutes schema's ``provenance`` object declares only
    ``produced_by`` / ``phase`` (no ``created_at``), so the file's
    modification time is the only recency signal actually present.

    Key: ``(st_mtime, filename)`` — the most recently written artifact
    wins; the filename is the final, deterministic tiebreaker so two
    artifacts sharing an mtime tick still order total-deterministically
    rather than by glob/dict iteration order.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.name)


def _extracted_item_count(
    artifact: Dict[str, Any], types: List[str]
) -> int:
    """Total items across the arrays ``compute_comparison`` reads.

    ``types`` is ``extraction_types()`` (``grounding`` excluded).
    Keying the populated-ness test to the SAME arrays the diff consumes
    means the selector's "this run extracted something" signal can never
    drift from what the comparison measures: a stale all-empty run
    returns 0 here even if it still carries a non-empty ``grounding``
    array (grounding is Phase-Y meta, never compared), and a real run
    returns its true item count. Pure and deterministic — the artifact
    is already parsed in ``llm_candidates``, so no re-read / no second
    failure surface.
    """
    payload = artifact.get("payload")
    if not isinstance(payload, dict):
        return 0
    total = 0
    for etype in types:
        value = payload.get(etype)
        if isinstance(value, list):
            total += len(value)
    return total


def _candidate_model_matches(
    payload: Dict[str, Any], model_id_substring: str
) -> bool:
    """True when this artifact's model identity matches the token.

    Match rule (case-insensitive ``contains``):

    * ``provenance.model_id`` is present and non-empty → match iff the
      substring is in it. The real workflow ALWAYS stamps
      ``provenance.model_id`` from the registry, so a Haiku run carries
      ``claude-haiku-…`` and a Sonnet run carries ``claude-sonnet-…`` —
      ``"haiku"`` / ``"sonnet"`` discriminate them cleanly. Selecting
      Sonnet is fail-closed: a Haiku-model artifact never matches
      ``"sonnet"``.
    * ``provenance.model_id`` absent/blank → fall back to
      ``provenance.produced_by``; if the substring is still not found,
      treat the artifact as the DEFAULT model (Haiku) — see
      ``_DEFAULT_CANDIDATE_MODEL_TOKEN``. This is the clause that keeps
      every legacy / synthetic ``meeting_minutes_llm`` artifact (no
      stamped model_id) selectable by ``find_haiku_artifact`` so the
      refactor changes NO existing behaviour. Absence never counts as a
      non-default model.
    """
    sub = (model_id_substring or "").strip().lower()
    if not sub:
        return False
    prov = payload.get("provenance")
    prov = prov if isinstance(prov, dict) else {}
    model_id = prov.get("model_id")
    if isinstance(model_id, str) and model_id.strip():
        return sub in model_id.lower()
    produced_by = prov.get("produced_by")
    if isinstance(produced_by, str) and sub in produced_by.lower():
        return True
    # No explicit model_id: the artifact is the default (Haiku) run.
    return sub == _DEFAULT_CANDIDATE_MODEL_TOKEN


def find_candidate_artifact(
    data_lake: Path,
    source_id: str,
    model_id_substring: str,
    *,
    missing_reason: str = "missing_candidate_artifact",
    empty_reason: str = "empty_candidate_artifact",
    invalid_reason: str = "invalid_candidate_artifact",
    target_chunking_strategy_version: Optional[str] = None,
    no_strategy_match_reason: str = "no_haiku_artifact_matching_strategy",
) -> Tuple[Dict[str, Any], Path]:
    """Locate the promoted LLM ``meeting_minutes`` artifact for a model.

    Scans ``meeting_minutes__*.json`` for the source whose
    ``payload.provenance.produced_by`` is ``meeting_minutes_llm`` AND
    whose model identity contains ``model_id_substring`` (see
    ``_candidate_model_matches``). When
    ``target_chunking_strategy_version`` is not None, candidates are
    ALSO filtered to those whose ``chunking_strategy_version`` matches
    the target (with absent/null treated as ``speaker_turn_v1`` per
    Phase 2.B). Recency (file mtime — see ``_haiku_recency_key``) only
    ORDERS the remaining candidates; a CONTENT check picks the winner.
    Walking newest → oldest, the first artifact that actually extracted
    something (≥1 item across the arrays ``compute_comparison`` reads)
    is selected.

    Why content, not pure recency: PR #183 made selection mtime-based
    so a stale all-empty earlier run could not shadow the real
    extraction. But the runner reaches this code only via
    ``clone-data-lake`` (``git clone``), and git stamps EVERY
    checked-out file's mtime with the single clone time. Both artifacts
    then share an mtime, the ``(st_mtime, filename)`` key ties, and
    selection collapses onto the content-blind filename
    (``artifact_id`` is a content hash) — re-introducing the exact
    pre-#183 bug, picking the stale empty file. The content check makes
    the selection robust regardless of mtime collisions.

    Fail-closed: if EVERY matching LLM artifact for the source is
    all-empty the script halts ``empty_reason`` rather than silently
    emitting a meaningless 0.0-recall diff. A regex-extractor artifact
    (``produced_by == "meeting_minutes"``) is NOT comparable against an
    Opus baseline, so its presence does not satisfy the requirement —
    if no matching LLM artifact exists at all the script halts
    ``missing_reason``. The reason codes are parameterised so the
    Haiku call keeps emitting the legacy ``missing_haiku_llm_output`` /
    ``empty_haiku_artifact`` / ``invalid_haiku_artifact`` codes (no
    behavioural change) while the Sonnet call emits the
    ``*_candidate_artifact`` codes. Only the SELECTED envelope is
    validated against the meeting_minutes schema before any field is
    read (CLAUDE.md read-path co-requirement); a stale earlier run must
    never be able to block the current real extraction by failing
    schema.
    """
    mdir = _meeting_dir(data_lake, source_id)
    candidates = sorted(mdir.glob("meeting_minutes__*.json"))
    saw_non_llm = False
    saw_other_model = False
    saw_other_strategy: List[str] = []
    llm_candidates: List[
        Tuple[Tuple[float, str], Dict[str, Any], Path]
    ] = []
    for path in candidates:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ComparisonError(
                invalid_reason,
                f"meeting_minutes artifact at {path} unreadable/!json: "
                f"{exc}",
            ) from exc
        if not isinstance(artifact, dict):
            continue
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            continue
        produced_by = (
            (payload.get("provenance") or {}).get("produced_by")
            if isinstance(payload.get("provenance"), dict)
            else None
        )
        if produced_by != HAIKU_LLM_PROVENANCE:
            saw_non_llm = True
            continue
        if not _candidate_model_matches(payload, model_id_substring):
            # An LLM artifact for a DIFFERENT model. Not an error by
            # itself (the other model's artifact legitimately lives in
            # the same directory); it just does not satisfy THIS
            # selector. Tracked only to make the halt detail precise.
            saw_other_model = True
            continue
        if target_chunking_strategy_version is not None:
            candidate_strategy = _chunking_strategy_version_of(artifact)
            if candidate_strategy != target_chunking_strategy_version:
                # Right model, wrong chunking strategy. Track it so the
                # halt detail names what was on disk; never silently
                # select a wrong-strategy artifact and produce a
                # cross-strategy F1 number.
                saw_other_strategy.append(candidate_strategy)
                continue
        llm_candidates.append(
            (_haiku_recency_key(path), artifact, path)
        )

    if llm_candidates:
        # Recency only ORDERS; a content check picks the winner. mtime
        # ties under git clone (every file gets the clone timestamp),
        # so picking purely by the (mtime, filename) key would collapse
        # back onto the content-blind filename and re-pick a stale
        # all-empty run. Walk newest → oldest, take the first artifact
        # that actually extracted something.
        types = extraction_types()
        ordered = sorted(
            llm_candidates, key=lambda c: c[0], reverse=True
        )
        selected: Optional[Tuple[Dict[str, Any], Path]] = None
        for _key, candidate_art, candidate_path in ordered:
            if _extracted_item_count(candidate_art, types) > 0:
                selected = (candidate_art, candidate_path)
                break
        if selected is None:
            # Every matching LLM artifact for this source is all-empty.
            # Returning one would emit a meaningless 0.0-recall diff AND
            # overwrite eval_history.jsonl with a false signal (and
            # spuriously trip the F1<0.70 correction-miner dispatch).
            # Fail closed.
            newest_path = ordered[0][2]
            raise ComparisonError(
                empty_reason,
                f"every meeting_minutes LLM artifact under {mdir} "
                f"matching model token {model_id_substring!r} has all "
                f"{len(types)} extraction arrays empty (newest: "
                f"{newest_path}); refusing to emit a 0-item comparison",
            )
        artifact, path = selected
        payload = artifact["payload"]
        # meeting_minutes.schema.json describes the FLAT
        # ``{"artifact_type": "meeting_minutes", **payload}`` shape
        # (the exact object the in-loop strict-schema eval validates),
        # NOT the on-disk envelope. Validate that form before reading
        # any extraction field off the payload (CLAUDE.md read-path
        # co-requirement) so a drifted/garbage payload is refused here
        # instead of silently producing a meaningless diff.
        flat = {"artifact_type": "meeting_minutes", **payload}
        try:
            validate_artifact(flat, "meeting_minutes", str(path))
        except ArtifactValidationError as exc:
            raise ComparisonError(
                invalid_reason,
                f"meeting_minutes artifact at {path} failed schema: "
                f"{exc}",
            ) from exc
        return artifact, path

    # Wrong-strategy is a DIFFERENT halt than missing-artifact: a
    # matching-model artifact IS present on disk, it just declares a
    # different chunking_strategy_version than the target. Surfacing it
    # as `no_*_matching_strategy` (not as generic `missing_*`) is what
    # tells the operator the fix is to re-run / re-baseline at the
    # matched strategy, not to extract anew from scratch. The check is
    # ordered BEFORE missing_reason because saw_other_strategy implies
    # there was at least one model-matching artifact on disk.
    if target_chunking_strategy_version is not None and saw_other_strategy:
        seen_versions = sorted(set(saw_other_strategy))
        raise ComparisonError(
            no_strategy_match_reason,
            (
                f"no meeting_minutes_llm artifact at "
                f"chunking_strategy_version="
                f"{target_chunking_strategy_version!r} found under "
                f"{mdir} (model token {model_id_substring!r}); "
                f"on-disk strategies for matching-model artifacts: "
                f"{seen_versions}. Re-run extraction at the matched "
                f"strategy, or pass --chunking-strategy to override."
            ),
        )

    detail = (
        f"no promoted meeting_minutes artifact with "
        f"provenance.produced_by == {HAIKU_LLM_PROVENANCE!r} and model "
        f"token {model_id_substring!r} under {mdir}"
    )
    if target_chunking_strategy_version is not None:
        detail += (
            f" at chunking_strategy_version="
            f"{target_chunking_strategy_version!r}"
        )
    if saw_other_model:
        detail += (
            " (a meeting_minutes_llm artifact for a different model "
            "was found but its provenance.model_id does not contain "
            f"{model_id_substring!r})"
        )
    if saw_non_llm:
        detail += (
            " (a regex-extractor meeting_minutes artifact was found "
            "but comparing it against the Opus baseline is meaningless)"
        )
    raise ComparisonError(missing_reason, detail)


def find_haiku_artifact(
    data_lake: Path,
    source_id: str,
    *,
    target_chunking_strategy_version: Optional[str] = None,
) -> Tuple[Dict[str, Any], Path]:
    """Locate the promoted Haiku ``meeting_minutes`` artifact.

    Thin wrapper over :func:`find_candidate_artifact` with the default
    Haiku model token and the LEGACY reason codes, so every existing
    caller / test sees byte-identical behaviour and the same
    ``missing_haiku_llm_output`` / ``empty_haiku_artifact`` /
    ``invalid_haiku_artifact`` halts. A ``meeting_minutes_llm`` artifact
    with no stamped ``provenance.model_id`` (legacy / synthetic) still
    matches ``"haiku"`` via the default-token clause in
    ``_candidate_model_matches`` — that is what preserves the prior
    contract exactly.

    ``target_chunking_strategy_version`` (Phase 2.B follow-up) filters
    candidates to the matching strategy before recency / content
    ordering, so a haiku artifact at a wrong strategy can never silently
    win when one at the matching strategy exists. When no candidate
    matches the target strategy the wrapper halts
    ``no_haiku_artifact_matching_strategy`` rather than falling back to
    the wrong artifact.
    """
    return find_candidate_artifact(
        data_lake,
        source_id,
        _DEFAULT_CANDIDATE_MODEL_TOKEN,
        missing_reason="missing_haiku_llm_output",
        empty_reason="empty_haiku_artifact",
        invalid_reason="invalid_haiku_artifact",
        target_chunking_strategy_version=target_chunking_strategy_version,
        no_strategy_match_reason="no_haiku_artifact_matching_strategy",
    )


# --------------------------------------------------------------------------
# Phase 6 — Stage 2 cascade output loader. Selects the most recent
# meeting_minutes_filtered artifact for a source and reshapes it into the
# same {payload: {extraction arrays}} envelope shape the comparison core
# already expects, so compute_comparison can score it with no branching.
# --------------------------------------------------------------------------
CASCADE_FILTERED_ARTIFACT_TYPE = "meeting_minutes_filtered"


def find_cascade_filtered_artifact(
    data_lake: Path, source_id: str
) -> Tuple[Dict[str, Any], Path]:
    """Locate the most recent meeting_minutes_filtered artifact.

    Selection rule: pick the candidate with the most recent mtime that
    actually carries items (mirrors :func:`find_candidate_artifact`'s
    content-then-recency tiebreak so a stale empty filter does not
    shadow a real one). Fail-closed: when no cascade artifact exists
    for the source, raise ``cascade_artifact_not_found`` so the
    operator sees a clear halt rather than a silent fall-through to
    the raw Haiku artifact.

    Returns a SYNTHETIC dict shaped like the regular meeting_minutes
    envelope so :func:`compute_comparison` can score it without
    branching: ``{"artifact_type": "meeting_minutes", "schema_version":
    "1.4.0", "payload": <filtered_items + provenance>}``. The original
    `meeting_minutes_filtered` envelope is preserved on the returned
    dict under `_cascade_envelope` so the caller can echo
    `prompt_variant=production_haiku_with_cascade_filter` on the
    comparison_result without re-reading the file.
    """
    mdir = _meeting_dir(data_lake, source_id)
    candidates = sorted(mdir.glob("meeting_minutes_filtered__*.json"))
    if not candidates:
        raise ComparisonError(
            "cascade_artifact_not_found",
            f"no meeting_minutes_filtered__*.json under {mdir}; run the "
            f"cascade with --enable-cascade-filter before "
            f"--use-cascade-output (Phase 6).",
        )

    parsed: List[Tuple[Tuple[float, str], Dict[str, Any], Path]] = []
    for path in candidates:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ComparisonError(
                "invalid_cascade_artifact",
                f"meeting_minutes_filtered at {path} unreadable/!json: "
                f"{exc}",
            ) from exc
        if not isinstance(envelope, dict):
            continue
        if envelope.get("artifact_type") != CASCADE_FILTERED_ARTIFACT_TYPE:
            continue
        parsed.append((_haiku_recency_key(path), envelope, path))

    if not parsed:
        raise ComparisonError(
            "cascade_artifact_not_found",
            f"no readable meeting_minutes_filtered under {mdir}",
        )

    # Walk newest → oldest, take the first that has at least one item
    # in any of the 23 filtered arrays. An all-empty cascade is real
    # (the filter dropped everything), but we still surface a fail-
    # closed halt because comparing 0 filtered items vs Opus would
    # emit a meaningless 0-recall diff.
    ordered = sorted(parsed, key=lambda c: c[0], reverse=True)
    selected: Optional[Tuple[Dict[str, Any], Path]] = None
    for _key, env, path in ordered:
        filtered = env.get("filtered_items") or {}
        total = sum(
            len(v) for v in filtered.values()
            if isinstance(v, list)
        )
        if total > 0:
            selected = (env, path)
            break

    if selected is None:
        newest_path = ordered[0][2]
        raise ComparisonError(
            "empty_cascade_artifact",
            f"every meeting_minutes_filtered under {mdir} has all 23 "
            f"filtered arrays empty (newest: {newest_path}); refusing "
            f"to emit a 0-item cascade comparison",
        )

    envelope, path = selected
    filtered_items = envelope.get("filtered_items") or {}
    extraction_config = envelope.get("extraction_config") or {}
    # Reshape to the meeting_minutes envelope the comparison core
    # already understands. The Phase 1 grounding re-verification is
    # skipped for cascade artifacts (their schema_version reflects the
    # filtered shape, not 1.4.0); a future Phase 7 may add a parallel
    # gate. The artifact carries enough provenance to identify itself
    # as the Stage 2 output.
    synthetic_payload: Dict[str, Any] = {}
    for k, v in filtered_items.items():
        synthetic_payload[k] = list(v) if isinstance(v, list) else v
    synthetic_payload["provenance"] = {
        "produced_by": HAIKU_LLM_PROVENANCE,
        "model_id": (
            (extraction_config.get("seed_inputs") or {}).get("model_id")
            or extraction_config.get("model_id")
            or ""
        ),
        "extraction_config": extraction_config,
    }
    synthetic_payload.setdefault("title", "cascade filtered output")
    synthetic_payload.setdefault("summary", "")
    synthetic_payload.setdefault("decisions", filtered_items.get("decisions") or [])
    synthetic_payload.setdefault(
        "action_items", filtered_items.get("action_items") or []
    )
    synthetic_payload.setdefault(
        "open_questions", filtered_items.get("open_questions") or []
    )

    synthetic_envelope: Dict[str, Any] = {
        "artifact_type": "meeting_minutes",
        # Read from the canonical source so the cascade synthetic
        # envelope advances automatically when the gate's binding
        # version bumps. A literal here is the exact foot-gun that
        # produced the schema_version_mixed halt on the Opus
        # baseline writer.
        "schema_version": GROUNDING_BINDING_SCHEMA_VERSION,
        "payload": synthetic_payload,
        "_cascade_envelope": envelope,
    }
    return synthetic_envelope, path


def load_gt_pairs(
    data_lake: Path, source_id: str
) -> Optional[List[Dict[str, Any]]]:
    """Read human_minutes_gt_pairs.jsonl, or None when absent.

    Absent GT is NOT a halt: the task says log and continue with GT
    metrics skipped (set to 0 with a presence flag).
    """
    path = (
        _meeting_dir(data_lake, source_id)
        / "ground_truth"
        / "human_minutes_gt_pairs.jsonl"
    )
    if not path.is_file():
        return None
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


# --------------------------------------------------------------------------
# Pure comparison core (imported and reused by the correction miner —
# NEVER reimplemented there).
# --------------------------------------------------------------------------
def opus_items_by_type(
    baseline_rows: List[Dict[str, Any]], types: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Group Opus baseline rows by extraction_type.

    Each item keeps its full record plus a ``_text`` key (the
    baseline's ``ground_truth_text``) used for matching.
    """
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in types}
    for rec in baseline_rows:
        etype = rec.get("extraction_type")
        if etype not in out:
            out.setdefault(etype, [])
        text = rec.get("ground_truth_text")
        if not isinstance(text, str) or not text.strip():
            continue
        item = dict(rec)
        item["_text"] = text.strip()
        out[etype].append(item)
    return out


def haiku_items_by_type(
    payload: Dict[str, Any], types: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Group Haiku artifact payload items by extraction_type.

    Each item is ``{"_text": <comparable string>, "item": <raw>}``.
    Items with no readable text are dropped (they cannot match and are
    not real extracted content for diff purposes).
    """
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in types}
    for etype in types:
        value = payload.get(etype)
        if not isinstance(value, list):
            continue
        for raw in value:
            text = _item_text(etype, raw)
            if not text:
                continue
            out[etype].append({"_text": text, "item": raw})
    return out


def _match_one_type(
    opus: List[Dict[str, Any]], haiku: List[Dict[str, Any]]
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Greedy one-to-one match for one extraction type.

    Deterministic (list order). Each Opus item is matched to AT MOST
    one Haiku item and each Haiku item is consumed AT MOST once, so a
    single Opus item cannot inflate TP by matching many Haiku items
    (and vice versa). Returns ``(true_positives, false_negatives,
    haiku_only)``.
    """
    matched_haiku: set[int] = set()
    true_positives = 0
    false_negatives: List[Dict[str, Any]] = []
    for o in opus:
        hit = None
        for idx, h in enumerate(haiku):
            if idx in matched_haiku:
                continue
            if text_match(o["_text"], h["_text"]):
                hit = idx
                break
        if hit is None:
            false_negatives.append(o)
        else:
            matched_haiku.add(hit)
            true_positives += 1
    haiku_only = [
        h for idx, h in enumerate(haiku) if idx not in matched_haiku
    ]
    return true_positives, false_negatives, haiku_only


def _f1(recall: float, precision: float) -> float:
    if recall + precision == 0.0:
        return 0.0
    return 2.0 * recall * precision / (recall + precision)


def _gt_recall(
    gt_pairs: List[Dict[str, Any]], candidate_texts: List[str]
) -> Tuple[int, int]:
    """(covered, total) — a GT pair is covered if any candidate text
    matches its ground_truth_text (cross-type, per the spec)."""
    total = 0
    covered = 0
    for pair in gt_pairs:
        gt_text = pair.get("ground_truth_text")
        if not isinstance(gt_text, str) or not gt_text.strip():
            continue
        total += 1
        if any(text_match(gt_text, ct) for ct in candidate_texts):
            covered += 1
    return covered, total


def compute_comparison(
    *,
    baseline_rows: List[Dict[str, Any]],
    haiku_payload: Dict[str, Any],
    gt_pairs: Optional[List[Dict[str, Any]]],
    types: List[str],
) -> Dict[str, Any]:
    """Pure metric computation. No I/O. Reused by the correction miner.

    Returns the ``summary`` / ``by_type`` / ``false_negatives`` /
    ``haiku_only_items`` / ``gt_missed`` building blocks.
    """
    opus_by_type = opus_items_by_type(baseline_rows, types)
    haiku_by_type = haiku_items_by_type(haiku_payload, types)

    by_type: Dict[str, Any] = {}
    total_tp = 0
    total_opus = 0
    total_haiku = 0
    fn_full: List[Dict[str, Any]] = []
    haiku_only_full: List[Dict[str, Any]] = []

    all_types = list(dict.fromkeys(list(types) + list(opus_by_type)))
    for etype in all_types:
        opus = opus_by_type.get(etype, [])
        haiku = haiku_by_type.get(etype, [])
        tp, fns, h_only = _match_one_type(opus, haiku)
        total_tp += tp
        total_opus += len(opus)
        total_haiku += len(haiku)
        by_type[etype] = {
            "opus_count": len(opus),
            "haiku_count": len(haiku),
            "true_positives": tp,
            "false_negatives": [
                {
                    "text_preview": o["_text"][:200],
                    "extraction_type": etype,
                }
                for o in fns
            ],
            "haiku_only": [
                {
                    "text_preview": h["_text"][:200],
                    "extraction_type": etype,
                }
                for h in h_only
            ],
        }
        for o in fns:
            full = {k: v for k, v in o.items() if k != "_text"}
            full["extraction_type"] = etype
            full["text_preview"] = o["_text"][:200]
            fn_full.append(full)
        for h in h_only:
            haiku_only_full.append(
                {
                    "extraction_type": etype,
                    "text_preview": h["_text"][:200],
                    "item": h["item"],
                }
            )

    recall = total_tp / total_opus if total_opus else 0.0
    precision = total_tp / total_haiku if total_haiku else 0.0
    f1 = _f1(recall, precision)

    gt_present = gt_pairs is not None
    gt_pairs = gt_pairs or []
    haiku_texts = [
        h["_text"] for items in haiku_by_type.values() for h in items
    ]
    opus_texts = [
        o["_text"] for items in opus_by_type.values() for o in items
    ]
    gt_cov_haiku, gt_total = _gt_recall(gt_pairs, haiku_texts)
    gt_cov_opus, _ = _gt_recall(gt_pairs, opus_texts)
    gt_missed: List[Dict[str, Any]] = []
    for pair in gt_pairs:
        gt_text = pair.get("ground_truth_text")
        if not isinstance(gt_text, str) or not gt_text.strip():
            continue
        if not any(text_match(gt_text, ht) for ht in haiku_texts):
            gt_missed.append(pair)

    gt_recall_haiku = (
        gt_cov_haiku / gt_total if gt_total else 0.0
    )
    gt_recall_opus = gt_cov_opus / gt_total if gt_total else 0.0

    summary = {
        "total_opus_items": total_opus,
        "total_haiku_items": total_haiku,
        "true_positives": total_tp,
        "false_negatives": len(fn_full),
        "haiku_only": len(haiku_only_full),
        "gt_covered_by_haiku": gt_cov_haiku,
        "gt_missed_by_haiku": len(gt_missed),
        "gt_covered_by_opus": gt_cov_opus,
        "haiku_recall_vs_opus": recall,
        "haiku_precision_vs_opus": precision,
        "haiku_f1_vs_opus": f1,
        "gt_recall_haiku": gt_recall_haiku,
        "gt_recall_opus": gt_recall_opus,
    }
    return {
        "summary": summary,
        "by_type": by_type,
        "false_negatives": fn_full,
        "haiku_only_items": haiku_only_full,
        "gt_missed": gt_missed,
        "gt_pairs_present": gt_present,
    }


# --------------------------------------------------------------------------
# Artifact + eval_history + summary table.
# --------------------------------------------------------------------------
def _run_id_of(artifact: Dict[str, Any]) -> str:
    """Stable run id for one ``meeting_minutes`` artifact.

    Model-agnostic: the same resolution order works for the Haiku and
    the Sonnet candidate (provenance.run_id → provenance.trace_id →
    envelope.trace_id → "").
    """
    payload = artifact.get("payload") or {}
    prov = payload.get("provenance") or {}
    for key in ("run_id", "trace_id"):
        v = prov.get(key)
        if isinstance(v, str) and v:
            return v
    v = artifact.get("trace_id")
    return v if isinstance(v, str) and v else ""


def _haiku_run_id(artifact: Dict[str, Any]) -> str:
    return _run_id_of(artifact)


def _opus_model_id(baseline_rows: List[Dict[str, Any]]) -> str:
    for rec in baseline_rows:
        mid = rec.get("model_id")
        if isinstance(mid, str) and mid:
            return mid
    return ""


def build_comparison_artifact(
    *,
    source_id: str,
    haiku_artifact: Dict[str, Any],
    baseline_rows: List[Dict[str, Any]],
    metrics: Dict[str, Any],
    compared_at: str,
) -> Dict[str, Any]:
    return {
        "artifact_type": COMPARISON_ARTIFACT_TYPE,
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "source_id": source_id,
        "haiku_run_id": _haiku_run_id(haiku_artifact),
        "opus_model_id": _opus_model_id(baseline_rows),
        "compared_at": compared_at,
        "gt_pairs_present": metrics["gt_pairs_present"],
        "legacy_eval": is_legacy_eval(haiku_artifact),
        "summary": metrics["summary"],
        "by_type": metrics["by_type"],
        "false_negatives": metrics["false_negatives"],
        "haiku_only_items": metrics["haiku_only_items"],
        "gt_missed": metrics["gt_missed"],
        # Phase 5 — additive prompt_variant readout. Defaults to
        # `production_haiku` for pre-Phase-5 artifacts so the
        # comparison engine treats the two-way path identically
        # whether the candidate carries the field or not.
        "haiku_prompt_variant": _prompt_variant_of(haiku_artifact),
    }


def is_legacy_eval(haiku_artifact: Dict[str, Any]) -> bool:
    """Phase 2: True when a comparison should be excluded from the
    tolerance-budget per-source variance computation.

    A run is "legacy" when ANY of these hold:

    * The artifact has no ``extraction_config`` block in
      ``payload.provenance``.
    * The block is present but ``prompt_content_hash`` is missing.
    * The artifact's schema_version predates 1.4.0.
    """
    sv = _artifact_schema_version(haiku_artifact) or ""
    if sv and sv < _GROUNDING_BINDING_SCHEMA_VERSION:
        return True
    ec = _extraction_config_from_artifact(haiku_artifact)
    if not isinstance(ec, dict):
        return True
    if not isinstance(ec.get("prompt_content_hash"), str):
        return True
    if not ec["prompt_content_hash"].strip():
        return True
    return False


def _merge_three_way_by_type(
    haiku_metrics: Dict[str, Any], sonnet_metrics: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge the two per-type diffs into the three-way ``by_type`` shape.

    ``opus_count`` is taken from the Haiku result (both diffs run
    against the SAME Opus baseline, so the per-type Opus count is
    identical; asserting equality would only add a failure surface for
    a value that cannot diverge by construction). Every per-type key is
    unioned across both results so a type present in only one side
    still appears with zeroed counts on the other.
    """
    h_by = haiku_metrics["by_type"]
    s_by = sonnet_metrics["by_type"]
    merged: Dict[str, Any] = {}
    for etype in dict.fromkeys(list(h_by) + list(s_by)):
        h = h_by.get(etype) or {}
        s = s_by.get(etype) or {}
        merged[etype] = {
            "opus_count": h.get("opus_count", s.get("opus_count", 0)),
            "haiku_count": h.get("haiku_count", 0),
            "haiku_tp": h.get("true_positives", 0),
            "haiku_fn": h.get("false_negatives", []),
            "haiku_only": h.get("haiku_only", []),
            "sonnet_count": s.get("haiku_count", 0),
            "sonnet_tp": s.get("true_positives", 0),
            "sonnet_fn": s.get("false_negatives", []),
            "sonnet_only": s.get("haiku_only", []),
        }
    return merged


def build_three_way_comparison_artifact(
    *,
    source_id: str,
    haiku_artifact: Dict[str, Any],
    sonnet_artifact: Dict[str, Any],
    baseline_rows: List[Dict[str, Any]],
    haiku_metrics: Dict[str, Any],
    sonnet_metrics: Dict[str, Any],
    compared_at: str,
) -> Dict[str, Any]:
    """Merge the Haiku and Sonnet diffs into one three-way artifact.

    ``haiku_summary`` / ``sonnet_summary`` reuse the exact summary
    field names ``compute_comparison`` emits (``haiku_*`` keys) — the
    distinction is the TOP-LEVEL key, not the inner field names — so the
    pure comparison core is reused verbatim with zero logic
    duplication. The two-way top-level keys (``summary``,
    ``false_negatives``, ``haiku_only_items``, ``gt_missed``) are
    deliberately ABSENT: the comparison_result schema's ``three_way``
    branch forbids them so a three-way artifact can never be misread as
    a two-way one (and vice versa).
    """
    artifact_out: Dict[str, Any] = {
        "artifact_type": COMPARISON_ARTIFACT_TYPE,
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "comparison_mode": "three_way",
        "source_id": source_id,
        "haiku_run_id": _run_id_of(haiku_artifact),
        "sonnet_run_id": _run_id_of(sonnet_artifact),
        "opus_model_id": _opus_model_id(baseline_rows),
        "compared_at": compared_at,
        "gt_pairs_present": (
            haiku_metrics["gt_pairs_present"]
            or sonnet_metrics["gt_pairs_present"]
        ),
        "haiku_summary": haiku_metrics["summary"],
        "sonnet_summary": sonnet_metrics["summary"],
        "by_type": _merge_three_way_by_type(
            haiku_metrics, sonnet_metrics
        ),
    }
    # Phase 5 — additive prompt_variant readout. Defaults to
    # `production_haiku` when extraction_config.prompt_variant is
    # absent (pre-Phase-5 artifacts). The comparison schema's
    # additionalProperties is open at the comparison_result top level
    # (only the two-way fields are forbidden in three_way), so adding
    # these keys is schema-additive. Tests in
    # tests/comparison/test_three_way_audit.py assert this stamp on
    # both legacy and Phase-5 artifacts.
    artifact_out["haiku_prompt_variant"] = _prompt_variant_of(haiku_artifact)
    artifact_out["sonnet_prompt_variant"] = _prompt_variant_of(sonnet_artifact)
    return artifact_out


def _comparison_out_path(
    data_lake: Path, source_id: str, timestamp: str
) -> Path:
    safe_ts = timestamp.replace(":", "").replace("+", "")
    return (
        _meeting_dir(data_lake, source_id)
        / "comparisons"
        / f"haiku_vs_opus_{safe_ts}.json"
    )


def _three_way_out_path(
    data_lake: Path, source_id: str, timestamp: str
) -> Path:
    """DISTINCT path for the three-way artifact.

    A different filename prefix in the SAME ``comparisons/`` directory
    so the append-only data-lake never overwrites the two-way
    ``haiku_vs_opus_<ts>.json``. ``correction_miner.load_comparison_results``
    globs ``haiku_vs_opus_*.json`` only, so a ``three_way_*.json`` file
    is invisible to System 2 and its different ``by_type`` shape can
    never reach the miner's two-way reader.
    """
    safe_ts = timestamp.replace(":", "").replace("+", "")
    return (
        _meeting_dir(data_lake, source_id)
        / "comparisons"
        / f"three_way_{safe_ts}.json"
    )


def _append_eval_history(
    data_lake: Path, source_id: str, row: Dict[str, Any]
) -> Path:
    """APPEND one row to eval_history.jsonl — existing rows untouched.

    Opened in append mode so no prior byte is rewritten; the comparison
    row is purely additive to whatever the LLM workflow projection or a
    prior comparison already wrote.
    """
    path = _meeting_dir(data_lake, source_id) / "eval_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def render_summary_table(
    metrics: Dict[str, Any], types: List[str]
) -> str:
    by_type = metrics["by_type"]
    rows = [
        "Type              | Opus | Haiku | TP | FN | Haiku-only",
        "------------------+------+-------+----+----+-----------",
    ]
    seen = list(
        dict.fromkeys(list(types) + list(by_type.keys()))
    )
    tot_o = tot_h = tot_tp = tot_fn = tot_ho = 0
    for etype in seen:
        bt = by_type.get(etype)
        if not bt:
            continue
        o = bt["opus_count"]
        h = bt["haiku_count"]
        tp = bt["true_positives"]
        fn = len(bt["false_negatives"])
        ho = len(bt["haiku_only"])
        if o == 0 and h == 0:
            continue
        tot_o += o
        tot_h += h
        tot_tp += tp
        tot_fn += fn
        tot_ho += ho
        rows.append(
            f"{etype:<17} | {o:<4} | {h:<5} | {tp:<2} | {fn:<2} | {ho}"
        )
    rows.append(
        "------------------+------+-------+----+----+-----------"
    )
    rows.append(
        f"{'TOTAL':<17} | {tot_o:<4} | {tot_h:<5} | {tot_tp:<2} | "
        f"{tot_fn:<2} | {tot_ho}"
    )
    s = metrics["summary"]
    rows.append("")
    rows.append(
        f"Haiku recall vs Opus:    "
        f"{s['haiku_recall_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"Haiku precision vs Opus: "
        f"{s['haiku_precision_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"Haiku F1 vs Opus:        "
        f"{s['haiku_f1_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"GT recall (Haiku):       "
        f"{s['gt_recall_haiku'] * 100:.1f}%"
    )
    rows.append(
        f"GT recall (Opus):        "
        f"{s['gt_recall_opus'] * 100:.1f}%"
    )
    return "\n".join(rows)


def render_three_way_table(
    three_way_artifact: Dict[str, Any], types: List[str]
) -> str:
    """Human-readable Opus / Haiku / Sonnet table (STDERR only).

    Reads the merged ``by_type`` and the two summaries off the
    three-way artifact so the table can never drift from what was
    written to disk.
    """
    by_type = three_way_artifact["by_type"]
    rows = [
        "Type              | Opus | Haiku | H-TP | H-FN | Sonnet | "
        "S-TP | S-FN",
        "------------------+------+-------+------+------+--------+"
        "------+-----",
    ]
    seen = list(dict.fromkeys(list(types) + list(by_type.keys())))
    tot_o = tot_h = tot_htp = tot_hfn = 0
    tot_s = tot_stp = tot_sfn = 0
    for etype in seen:
        bt = by_type.get(etype)
        if not bt:
            continue
        o = bt["opus_count"]
        h = bt["haiku_count"]
        htp = bt["haiku_tp"]
        hfn = len(bt["haiku_fn"])
        sc = bt["sonnet_count"]
        stp = bt["sonnet_tp"]
        sfn = len(bt["sonnet_fn"])
        if o == 0 and h == 0 and sc == 0:
            continue
        tot_o += o
        tot_h += h
        tot_htp += htp
        tot_hfn += hfn
        tot_s += sc
        tot_stp += stp
        tot_sfn += sfn
        rows.append(
            f"{etype:<17} | {o:<4} | {h:<5} | {htp:<4} | {hfn:<4} | "
            f"{sc:<6} | {stp:<4} | {sfn}"
        )
    rows.append(
        "------------------+------+-------+------+------+--------+"
        "------+-----"
    )
    rows.append(
        f"{'TOTAL':<17} | {tot_o:<4} | {tot_h:<5} | {tot_htp:<4} | "
        f"{tot_hfn:<4} | {tot_s:<6} | {tot_stp:<4} | {tot_sfn}"
    )
    h = three_way_artifact["haiku_summary"]
    s = three_way_artifact["sonnet_summary"]
    rows.append("")
    rows.append(
        f"Haiku  recall vs Opus:    "
        f"{h['haiku_recall_vs_opus'] * 100:.1f}%"
        f"  |  Sonnet recall vs Opus:  "
        f"{s['haiku_recall_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"Haiku  precision vs Opus: "
        f"{h['haiku_precision_vs_opus'] * 100:.1f}%"
        f"  |  Sonnet precision:       "
        f"{s['haiku_precision_vs_opus'] * 100:.1f}%"
    )
    rows.append(
        f"Haiku  F1 vs Opus:        "
        f"{h['haiku_f1_vs_opus'] * 100:.1f}%"
        f"  |  Sonnet F1 vs Opus:      "
        f"{s['haiku_f1_vs_opus'] * 100:.1f}%"
    )
    return "\n".join(rows)


def _resolve_transcript(data_lake: Path, source_id: str) -> Optional[str]:
    """Resolve the transcript text for ``source_id`` via the source_record.

    Returns the transcript text on success, or ``None`` when the
    record / file is missing. Used for Phase 1 re-verification (the
    grounding gate is re-run against the CURRENT transcript so the
    comparison surfaces tampering or transcript mutation).

    The function deliberately does NOT halt on a missing transcript:
    re-verification is a defensive cross-check, not the primary gate
    (the primary gate ran at promotion time). When the transcript is
    not on disk this returns ``None`` and the caller sets
    ``tainted: false`` with a ``re_verification: skipped`` note.
    """
    meeting_dir = _meeting_dir(data_lake, source_id)
    sr_path = meeting_dir / "source_record.json"
    if not sr_path.is_file():
        return None
    try:
        record = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    payload = record.get("payload") or record
    transcript_path = payload.get("transcript_path") or payload.get(
        "extracted_text_path"
    )
    candidates: List[Path] = []
    if isinstance(transcript_path, str) and transcript_path:
        p = Path(transcript_path)
        if not p.is_absolute():
            # Resolve relative to data_lake root.
            candidates.append(data_lake / p)
        candidates.append(p)
    # Fallback: the conventional layout shipped with the data-lake.
    candidates.append(meeting_dir / "transcript.txt")
    candidates.append(
        data_lake / "store" / "raw" / "meetings" / source_id / "transcript.txt"
    )
    for cand in candidates:
        if cand.is_file():
            try:
                return cand.read_text(encoding="utf-8")
            except OSError:
                continue
    return None


def _artifact_schema_version(artifact: Dict[str, Any]) -> Optional[str]:
    """Return the schema_version stamped on an artifact envelope.

    Looks first at the envelope-level ``schema_version`` (the canonical
    location), then at ``payload.schema_version`` as a fallback for
    artifacts that stamp the version inside the payload. ``None`` when
    neither is a string — caller treats that as "version unknown" and
    halts unless ``--allow-mixed-schema`` is set.
    """
    env_v = artifact.get("schema_version")
    if isinstance(env_v, str) and env_v.strip():
        return env_v.strip()
    payload = artifact.get("payload")
    if isinstance(payload, dict):
        pv = payload.get("schema_version")
        if isinstance(pv, str) and pv.strip():
            return pv.strip()
    return None


def _baseline_at_version_exists(
    data_lake: Path, source_id: str, target_version: str
) -> bool:
    """True iff an opus baseline at ``target_version`` is on disk.

    Phase 2 schema-version coherence (Step 2.7). A Haiku artifact at
    1.4.0 must be diffed against an opus baseline ALSO at 1.4.0 (or
    later). The miner, when it detects a schema bump on ``main``, is
    expected to re-run the opus baseline at the new version BEFORE
    evaluating any candidate. This helper lets the comparison engine
    verify that re-baseline happened.
    """
    rows = load_opus_baseline(data_lake, source_id)
    for row in rows:
        v = row.get("schema_version")
        if isinstance(v, str) and v.strip() == target_version:
            return True
    return False


def _extraction_config_from_artifact(
    artifact: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Read the Phase 2 ``extraction_config`` block off an artifact.

    Returns the dict when present, ``None`` otherwise. The miner's
    PR-opener stamps ``expected_post_merge_prompt_hash`` separately
    and the comparison engine reads it via
    :func:`_load_expected_post_merge_hash`.
    """
    payload = artifact.get("payload") or {}
    prov = payload.get("provenance") or {}
    ec = prov.get("extraction_config")
    if isinstance(ec, dict):
        return ec
    return None


# Phase 5 — single source of truth for the comparison engine's default
# when an artifact omits `extraction_config.prompt_variant` (pre-Phase-5
# artifacts). Audit report `phase5_three_way_audit_report.md` enumerates
# every read site that consumes this default.
_DEFAULT_PROMPT_VARIANT: str = "production_haiku"


def _prompt_variant_of(artifact: Dict[str, Any]) -> str:
    """Read ``extraction_config.prompt_variant`` off an artifact.

    Returns the stamped value when present; otherwise the
    Phase-5 default ``production_haiku``. Pre-Phase-5 artifacts omit
    the field entirely — they predate the (prompt, model) discriminator
    and the comparison engine treats them as the production Haiku
    variant.
    """
    ec = _extraction_config_from_artifact(artifact)
    if isinstance(ec, dict):
        pv = ec.get("prompt_variant")
        if isinstance(pv, str) and pv.strip():
            return pv
    return _DEFAULT_PROMPT_VARIANT


def _load_expected_post_merge_hash(
    data_lake: Path, source_id: str
) -> Optional[str]:
    """Most-recent miner-run ``expected_post_merge_prompt_hash`` for source.

    The miner writes a ``correction_miner_run__*.json`` artifact
    alongside the eval_history rows; that file records the candidate's
    ``expected_post_merge_prompt_hash``. The comparison engine reads
    the most recent one and asserts the production artifact's
    ``prompt_content_hash`` matches. The function returns ``None``
    when no such artifact exists (the typical fresh-checkout case);
    callers treat that as "no drift gate active".
    """
    meeting_dir = (
        data_lake / "store" / "processed" / "meetings" / source_id
    )
    if not meeting_dir.is_dir():
        return None
    candidates: List[Tuple[float, Optional[str]]] = []
    for path in meeting_dir.glob("correction_miner_run__*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ts = path.stat().st_mtime
        h = data.get("expected_post_merge_prompt_hash") or (
            (data.get("payload") or {}).get("expected_post_merge_prompt_hash")
        )
        candidates.append((ts, h if isinstance(h, str) and h else None))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    for _, h in candidates:
        if h:
            return h
    return None


def _chunking_strategy_version_of(
    artifact: Dict[str, Any],
) -> str:
    """Phase 2.B: read ``chunking_strategy_version`` off an artifact.

    Looks at ``payload.provenance.chunking_strategy_version`` first
    (the canonical location in the meeting_minutes schema), then at a
    bare ``chunking_strategy_version`` on the row (for baseline rows
    that flatten the provenance into the top-level dict). Returns
    :data:`_DEFAULT_CHUNKING_STRATEGY_VERSION` (``speaker_turn_v1``)
    when neither is a non-empty string — pre-Phase-2.B artifacts and
    Phase-2.B default-off (CHUNK_OVERLAP_TURNS=0) artifacts both omit
    the field or stamp ``speaker_turn_v1`` explicitly, so the default
    keeps cross-version comparisons green when neither side opted into
    overlap.
    """
    payload = artifact.get("payload") or {}
    if isinstance(payload, dict):
        prov = payload.get("provenance") or {}
        if isinstance(prov, dict):
            v = prov.get("chunking_strategy_version")
            if isinstance(v, str) and v.strip():
                return v.strip()
    # Baseline rows (opus_reference_minutes.jsonl) flatten the
    # provenance fields into the row dict itself; tolerate that shape
    # so a baseline row produced under overlap is detected.
    flat = artifact.get("chunking_strategy_version")
    if isinstance(flat, str) and flat.strip():
        return flat.strip()
    return _DEFAULT_CHUNKING_STRATEGY_VERSION


def _baseline_chunking_strategy_version(
    baseline_rows: List[Dict[str, Any]],
) -> str:
    """Phase 2.B: dominant chunking_strategy_version across baseline rows.

    Mirrors :func:`_baseline_schema_version`: takes the first row's
    value and treats every later row as conforming. The opus baseline
    is produced in one run so all rows should agree on the strategy.
    Returns the default ``speaker_turn_v1`` when no row carries the
    field — keeps pre-Phase-2.B baselines comparable against
    default-off Phase-2.B artifacts.
    """
    for row in baseline_rows:
        v = _chunking_strategy_version_of(row)
        if v:
            return v
    return _DEFAULT_CHUNKING_STRATEGY_VERSION


def _baseline_schema_version(
    baseline_rows: List[Dict[str, Any]],
) -> Optional[str]:
    """Return the dominant schema_version across the opus baseline rows.

    All rows in a single baseline file are produced in one run so they
    should share a version. We return the FIRST row's version; if rows
    disagree among themselves the baseline is itself broken and we let
    the haiku-vs-baseline check raise on the first mismatch.
    """
    for row in baseline_rows:
        v = row.get("schema_version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _re_verify_haiku_grounding(
    *,
    haiku_artifact: Dict[str, Any],
    haiku_schema_version: Optional[str],
    transcript: Optional[str],
) -> Dict[str, Any]:
    """Re-run :func:`verify_grounding` on the haiku artifact.

    Returns a dict (always JSON-serializable) describing the
    re-verification outcome:

      - status: "ok" | "tainted" | "skipped"
      - reason: short token (e.g. "grounding_pre_1_4",
        "transcript_unavailable", "re_verification_passed",
        "re_verification_rejected_N_items")
      - grounding_rate: float ∈ [0.0, 1.0]
      - rejected_count: int
      - accepted_count: int
      - block_reason_code: optional reason if the gate blocked

    The gate is re-run only for artifacts at the binding schema_version
    (1.4.0+). Pre-1.4 artifacts have no grounding fields to verify so
    we return ``status: "skipped"`` with reason ``grounding_pre_1_4``.
    """
    if (
        haiku_schema_version is None
        or haiku_schema_version < _GROUNDING_BINDING_SCHEMA_VERSION
    ):
        return {
            "status": "skipped",
            "reason": "grounding_pre_1_4",
            "grounding_rate": 1.0,
            "rejected_count": 0,
            "accepted_count": 0,
        }
    if transcript is None or transcript == "":
        # We have a 1.4.0 artifact but no transcript on disk to verify
        # against. This is a defensive cross-check, not the primary
        # gate, so we surface the skip rather than halting the whole
        # comparison.
        return {
            "status": "skipped",
            "reason": "transcript_unavailable",
            "grounding_rate": 1.0,
            "rejected_count": 0,
            "accepted_count": 0,
        }
    # Import lazily so the comparison script keeps loading on a
    # checkout where the grounding module has not yet been installed.
    from spectrum_systems_core.promotion.gate import verify_grounding

    payload = haiku_artifact.get("payload") or {}
    report = verify_grounding(payload, transcript)
    if report.rejected_items:
        return {
            "status": "tainted",
            "reason": (
                f"re_verification_rejected_{len(report.rejected_items)}_items"
            ),
            "grounding_rate": report.grounding_rate,
            "rejected_count": len(report.rejected_items),
            "accepted_count": len(report.accepted_items),
            "block_reason_code": report.block_reason_code,
            "rejected_reason_codes": sorted(
                {r.reason_code for r in report.rejected_items}
            ),
        }
    return {
        "status": "ok",
        "reason": "re_verification_passed",
        "grounding_rate": report.grounding_rate,
        "rejected_count": 0,
        "accepted_count": len(report.accepted_items),
    }


def run_comparison(
    *,
    data_lake: Path,
    source_id: str,
    dry_run: bool,
    print_inputs: bool = False,
    print_scores: bool = False,
    include_sonnet: bool = False,
    allow_mixed_schema: bool = False,
    use_cascade_output: bool = False,
    chunking_strategy_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Orchestrate one comparison. Returns a summary dict; raises on halt.

    ``print_inputs`` / ``print_scores`` are observe-only debug readouts
    written to STDERR. STDOUT stays pure JSON so the workflow's
    summary/threshold steps still parse it; neither flag changes the
    comparison or what is written. ``dry_run`` runs the full comparison
    but writes no artifact and no eval_history row.

    ``include_sonnet`` is the ONLY behaviour switch. When False
    (default) this is byte-for-byte the legacy two-way path — same
    STDOUT shape, same on-disk ``haiku_vs_opus_<ts>.json``, same
    eval_history row, a Sonnet artifact in the data-lake is completely
    ignored. When True a second candidate (Sonnet) is REQUIRED: if it
    is missing or all-empty the run halts fail-closed
    (``missing_candidate_artifact`` / ``empty_candidate_artifact``) — it
    never silently degrades to a two-way result mislabelled three-way.
    """
    types = extraction_types()
    baseline_rows = load_opus_baseline(data_lake, source_id)
    # Resolve the chunking strategy the haiku selector must filter by.
    # Auto-detect from the Opus baseline when no explicit override is
    # given so the default behaviour is strategy-aware — the operator
    # never needs to specify anything for the common case. An override
    # (``--chunking-strategy <version>``) lets the operator pin
    # selection to a specific strategy; the cross-check halt at
    # ``_CHUNKING_STRATEGY_MISMATCH_REASON`` below still fires if the
    # override and the baseline disagree, so the flag controls
    # selection, not gate bypass.
    baseline_strategy = _baseline_chunking_strategy_version(baseline_rows)
    if chunking_strategy_override is not None:
        target_strategy = chunking_strategy_override
    else:
        target_strategy = baseline_strategy
    if use_cascade_output:
        # Phase 6 — comparison runs against the cascade-filtered Haiku
        # artifact instead of the raw one. Fail-closed: no cascade
        # artifact exists → halt cascade_artifact_not_found rather than
        # silently fall through. The grounding re-verification gate
        # below is skipped for cascade artifacts (their items mirror
        # the source by reference; tampering is caught when the source
        # itself is re-verified). Strategy filtering does NOT apply to
        # cascade artifacts because the synthetic envelope does not
        # carry chunking_strategy_version (cascade preserves items by
        # reference from the source artifact).
        haiku_artifact, haiku_path = find_cascade_filtered_artifact(
            data_lake, source_id
        )
    else:
        haiku_artifact, haiku_path = find_haiku_artifact(
            data_lake,
            source_id,
            target_chunking_strategy_version=target_strategy,
        )

    # Phase 2.B: chunking-strategy cross-check. A Haiku artifact
    # produced under CHUNK_OVERLAP_TURNS=N (stamped
    # `speaker_turn_v1_overlap{N}`) cannot be honestly diffed against
    # an Opus baseline produced under the default strategy (stamped
    # `speaker_turn_v1`). The two F1 numbers are not comparable
    # because the inputs to the models were structurally different.
    # Fail closed; the operator's options are to (a) re-run the
    # reference under the same overlap setting (matched baseline) or
    # (b) use --allow-strategy-mismatch (future CLI extension; not in
    # this PR — the halt is the binding gate, the override is a
    # follow-up). Treats absent / null on either side as
    # ``speaker_turn_v1`` so pre-Phase-2.B artifacts compared against
    # default-off Phase-2.B artifacts do NOT halt.
    haiku_strategy = _chunking_strategy_version_of(haiku_artifact)
    baseline_strategy = _baseline_chunking_strategy_version(baseline_rows)
    if haiku_strategy != baseline_strategy:
        raise ComparisonError(
            _CHUNKING_STRATEGY_MISMATCH_REASON,
            (
                f"Haiku chunking_strategy_version={haiku_strategy!r} differs "
                f"from baseline chunking_strategy_version={baseline_strategy!r}. "
                "Re-run the reference baseline under the same "
                "CHUNK_OVERLAP_TURNS setting to produce a matched "
                "comparison; cross-strategy F1 numbers are not "
                "comparable. (Pre-Phase-2.B artifacts default to "
                "'speaker_turn_v1' and are not affected.)"
            ),
        )

    # Phase 1 (Step 1.7): schema_version cross-check. A Haiku artifact
    # at a different schema_version than the Opus baseline rows means
    # the comparison is mixing items produced under different gates —
    # e.g. 1.4.0 Haiku with grounding fields against a 1.0.0 baseline
    # that has none. Fail closed unless --allow-mixed-schema is set.
    # The flag is CLI-only: it is never read from env / config.
    #
    # SCOPING: the check ONLY fires when the Haiku artifact is at the
    # grounding-binding version (1.4.0+). Pre-1.4 mixed-schema
    # comparisons (e.g. legacy 1.1.0 Haiku vs 1.0.0 baseline) are
    # legitimate — neither side carries grounding, so the mismatch is
    # cosmetic and the legacy comparison behaviour must be preserved
    # so the existing integration-contract suite stays passing. The
    # halt's purpose is to catch the SPECIFIC foot-gun where a 1.4.0
    # producer is silently diffed against an ungated baseline; pre-1.4
    # mismatches are out of scope for the halt.
    haiku_schema_version = _artifact_schema_version(haiku_artifact)
    baseline_schema_version = _baseline_schema_version(baseline_rows)
    if (
        baseline_schema_version is not None
        and haiku_schema_version is not None
        and haiku_schema_version != baseline_schema_version
        and haiku_schema_version >= _GROUNDING_BINDING_SCHEMA_VERSION
        and not allow_mixed_schema
    ):
        # Phase 2 (Step 2.7) refinement: when the Haiku artifact is at
        # the binding version but a baseline at the matching version
        # ALSO exists on disk, the comparison can proceed — the miner
        # is expected to re-run the opus baseline at the bumped
        # version before evaluating any candidate. The mismatch the
        # halt was designed to catch is "1.4.0 producer silently diffed
        # against an ungated baseline because no matching baseline was
        # ever created". With a matching baseline present we use it.
        if not _baseline_at_version_exists(
            data_lake, source_id, haiku_schema_version
        ):
            raise ComparisonError(
                _MIXED_SCHEMA_REASON,
                (
                    f"Haiku schema_version={haiku_schema_version!r} differs "
                    f"from baseline schema_version={baseline_schema_version!r}, "
                    "and no opus baseline at the matching version is on disk. "
                    "Re-run the opus baseline at the new schema BEFORE "
                    "the next miner evaluation, or pass --allow-mixed-schema "
                    "as a last-resort operator override (CLI-only)."
                ),
            )

    # Phase 2 (Step 2.6) prompt-drift gate. Fires when:
    #   1. The Haiku artifact carries a Phase 2 `extraction_config`
    #      (legacy artifacts skip silently — they pre-date the gate).
    #   2. A recent miner run has an `expected_post_merge_prompt_hash`
    #      recorded for this source.
    #   3. The two hashes disagree.
    # When all three hold the comparison halts with `prompt_drift_post_merge`
    # so the operator catches a post-merge prompt edit instead of having
    # the next miner run silently disagree with production. The gate is
    # off when the miner has never run for this source (fresh checkout).
    #
    # Phase 6 — cascade output carries the SOURCE artifact's
    # extraction_config (with prompt_variant overridden to
    # `production_haiku_with_cascade_filter`) so the prompt-drift gate
    # would fire on a legitimate cascade run. Skip the gate when
    # --use-cascade-output is set; the gate still protects the raw
    # comparison path that runs on every default invocation.
    haiku_extraction_config = (
        None if use_cascade_output
        else _extraction_config_from_artifact(haiku_artifact)
    )
    if haiku_extraction_config is not None:
        production_hash = haiku_extraction_config.get("prompt_content_hash")
        expected_hash = _load_expected_post_merge_hash(data_lake, source_id)
        if (
            isinstance(production_hash, str)
            and isinstance(expected_hash, str)
            and production_hash != expected_hash
        ):
            raise ComparisonError(
                _PROMPT_DRIFT_REASON,
                (
                    f"Production prompt_content_hash={production_hash!r} != "
                    f"miner expected_post_merge_hash={expected_hash!r}. "
                    "A post-merge prompt edit drifted from what the miner "
                    "measured. Re-run the miner against the live prompt "
                    "before relying on this comparison."
                ),
            )

    sonnet_artifact: Optional[Dict[str, Any]] = None
    sonnet_path: Optional[Path] = None
    if include_sonnet:
        # Fail-closed BEFORE any artifact is built: a three-way run that
        # cannot find a populated Sonnet artifact must halt, never emit
        # a two-way result wearing a three-way label. find_haiku is
        # unaffected — a missing Sonnet does not also mask a Haiku halt
        # (Haiku was already resolved above, so the two failures stay
        # independent and each surfaces its own clear reason).
        sonnet_artifact, sonnet_path = find_candidate_artifact(
            data_lake, source_id, "sonnet"
        )
    gt_pairs = load_gt_pairs(data_lake, source_id)
    if gt_pairs is None:
        print(
            "no GT pairs — skipping GT metrics", file=sys.stderr
        )

    haiku_payload = haiku_artifact.get("payload") or {}
    sonnet_payload = (
        (sonnet_artifact or {}).get("payload") or {}
    )

    if print_inputs:
        opus_path = _opus_baseline_path(data_lake, source_id)
        opus_item_count = len(baseline_rows)
        haiku_item_count = sum(
            len(v)
            for v in (haiku_payload.get(t) for t in types)
            if isinstance(v, list)
        )
        print("=== print_inputs ===", file=sys.stderr)
        print(f"opus artifact path:  {opus_path}", file=sys.stderr)
        print(f"haiku artifact path: {haiku_path}", file=sys.stderr)
        print(f"opus item count:     {opus_item_count}", file=sys.stderr)
        print(
            f"haiku item count:    {haiku_item_count}", file=sys.stderr
        )
        if include_sonnet:
            sonnet_item_count = sum(
                len(v)
                for v in (sonnet_payload.get(t) for t in types)
                if isinstance(v, list)
            )
            print(
                f"sonnet artifact path: {sonnet_path}",
                file=sys.stderr,
            )
            print(
                f"sonnet item count:   {sonnet_item_count}",
                file=sys.stderr,
            )
        print("=== /print_inputs ===", file=sys.stderr)

    metrics = compute_comparison(
        baseline_rows=baseline_rows,
        haiku_payload=haiku_payload,
        gt_pairs=gt_pairs,
        types=types,
    )
    compared_at = _now_utc_iso()

    # Phase 1 (Step 1.7): re-verify grounding on the haiku artifact
    # against the CURRENT transcript. If any promoted item now fails
    # the gate, the artifact is ``tainted`` — log a warning and set the
    # tainted flag in the summary so the caller can re-run the
    # pipeline. The check is observe-only; it does NOT mutate metrics.
    #
    # Phase 6 — when --use-cascade-output is set the artifact in hand
    # is a SYNTHETIC envelope wrapped around the cascade-filtered
    # items. The cascade preserves items by reference (it cannot
    # invent or mutate them), so a tampering event on the source
    # artifact is what we need to detect — re-verifying the cascade
    # would re-do the same byte-match check that ran on the source.
    # Skip the gate here and run a separate cascade-side check via
    # the items_in_artifact_count sanity assertion below.
    transcript_text = _resolve_transcript(data_lake, source_id)
    if use_cascade_output:
        haiku_reverification = {
            "status": "skipped_cascade",
            "reason": "cascade_artifact",
            "rejected_count": 0,
            "grounding_rate": 1.0,
        }
    else:
        haiku_reverification = _re_verify_haiku_grounding(
            haiku_artifact=haiku_artifact,
            haiku_schema_version=haiku_schema_version,
            transcript=transcript_text,
        )
    if haiku_reverification["status"] == "tainted":
        print(
            f"WARNING: haiku artifact at {haiku_path} failed grounding "
            f"re-verification: {haiku_reverification['reason']!r} "
            f"({haiku_reverification['rejected_count']} items rejected). "
            "The promoted artifact may have been produced against a "
            "different transcript or tampered with. Marking comparison "
            "as tainted.",
            file=sys.stderr,
        )

    if include_sonnet:
        sonnet_metrics = compute_comparison(
            baseline_rows=baseline_rows,
            haiku_payload=sonnet_payload,
            gt_pairs=gt_pairs,
            types=types,
        )
        artifact = build_three_way_comparison_artifact(
            source_id=source_id,
            haiku_artifact=haiku_artifact,
            sonnet_artifact=sonnet_artifact or {},
            baseline_rows=baseline_rows,
            haiku_metrics=metrics,
            sonnet_metrics=sonnet_metrics,
            compared_at=compared_at,
        )
        # Validate our OWN output before writing it (fail-closed: never
        # write a malformed comparison_result). The schema's three_way
        # branch is enforced here.
        validate_artifact(artifact, COMPARISON_ARTIFACT_TYPE)

        if print_scores:
            print("=== print_scores ===", file=sys.stderr)
            print(
                json.dumps(artifact, indent=2, sort_keys=True),
                file=sys.stderr,
            )
            print("=== /print_scores ===", file=sys.stderr)

        table = render_three_way_table(artifact, types)
        print(table, file=sys.stderr)

        out_path = _three_way_out_path(
            data_lake, source_id, compared_at
        )
        hs = metrics["summary"]
        ss = sonnet_metrics["summary"]
        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(
                    artifact, sort_keys=True, separators=(",", ":")
                )
                + "\n",
                encoding="utf-8",
            )
            _append_eval_history(
                data_lake,
                source_id,
                {
                    "eval_type": "three_way_comparison",
                    "haiku_f1_vs_opus": hs["haiku_f1_vs_opus"],
                    "haiku_recall_vs_opus": hs["haiku_recall_vs_opus"],
                    "haiku_precision_vs_opus": hs[
                        "haiku_precision_vs_opus"
                    ],
                    "sonnet_f1_vs_opus": ss["haiku_f1_vs_opus"],
                    "sonnet_recall_vs_opus": ss["haiku_recall_vs_opus"],
                    "sonnet_precision_vs_opus": ss[
                        "haiku_precision_vs_opus"
                    ],
                    "timestamp": compared_at,
                    "comparison_artifact_path": str(out_path),
                },
            )
        else:
            print("DRY RUN — artifact not written", file=sys.stderr)

        return {
            "status": "success",
            "source_id": source_id,
            "dry_run": dry_run,
            "comparison_mode": "three_way",
            "haiku_artifact_path": str(haiku_path),
            "sonnet_artifact_path": str(sonnet_path),
            "comparison_artifact_path": str(out_path),
            "gt_pairs_present": artifact["gt_pairs_present"],
            # ``summary`` keeps the Haiku metrics so the workflow's
            # existing ``d["summary"]["haiku_f1_vs_opus"]`` parse and
            # any two-way consumer keep working unchanged; the Sonnet
            # metrics are additive under ``sonnet_summary``.
            "summary": hs,
            "sonnet_summary": ss,
            # Phase 5 — additive prompt_variant labels so the CLI
            # readout (and the print_three_way_delta helper) labels
            # each candidate with its (prompt, model) tag.
            "haiku_prompt_variant": artifact["haiku_prompt_variant"],
            "sonnet_prompt_variant": artifact["sonnet_prompt_variant"],
            "table": table,
            # Phase 1 additive fields. Pre-1.4 callers ignore them.
            "haiku_schema_version": haiku_schema_version,
            "baseline_schema_version": baseline_schema_version,
            "allow_mixed_schema": allow_mixed_schema,
            "tainted": haiku_reverification["status"] == "tainted",
            "grounding_rate": haiku_reverification["grounding_rate"],
            "re_verification": haiku_reverification,
        }

    artifact = build_comparison_artifact(
        source_id=source_id,
        haiku_artifact=haiku_artifact,
        baseline_rows=baseline_rows,
        metrics=metrics,
        compared_at=compared_at,
    )
    # Validate our OWN output before writing it (fail-closed: never
    # write a malformed comparison_result).
    validate_artifact(artifact, COMPARISON_ARTIFACT_TYPE)

    if print_scores:
        print("=== print_scores ===", file=sys.stderr)
        print(
            json.dumps(artifact, indent=2, sort_keys=True),
            file=sys.stderr,
        )
        print("=== /print_scores ===", file=sys.stderr)

    table = render_summary_table(metrics, types)
    # Human-readable table to STDERR so STDOUT stays pure JSON (the
    # workflow parses STDOUT for haiku_f1_vs_opus + the table).
    print(table, file=sys.stderr)

    out_path = _comparison_out_path(data_lake, source_id, compared_at)
    s = metrics["summary"]
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(artifact, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        _append_eval_history(
            data_lake,
            source_id,
            {
                "eval_type": "haiku_vs_opus_comparison",
                "haiku_recall_vs_opus": s["haiku_recall_vs_opus"],
                "haiku_precision_vs_opus": s["haiku_precision_vs_opus"],
                "haiku_f1_vs_opus": s["haiku_f1_vs_opus"],
                "gt_recall_haiku": s["gt_recall_haiku"],
                "gt_recall_opus": s["gt_recall_opus"],
                "timestamp": compared_at,
                "comparison_artifact_path": str(out_path),
            },
        )
    else:
        print("DRY RUN — artifact not written", file=sys.stderr)

    return {
        "status": "success",
        "source_id": source_id,
        "dry_run": dry_run,
        "haiku_artifact_path": str(haiku_path),
        "comparison_artifact_path": str(out_path),
        "gt_pairs_present": metrics["gt_pairs_present"],
        "summary": s,
        "table": table,
        # Phase 1 additive fields. Pre-1.4 callers ignore them.
        "haiku_schema_version": haiku_schema_version,
        "baseline_schema_version": baseline_schema_version,
        "allow_mixed_schema": allow_mixed_schema,
        "tainted": haiku_reverification["status"] == "tainted",
        "grounding_rate": haiku_reverification["grounding_rate"],
        "re_verification": haiku_reverification,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the diff; write no artifact and no eval_history.",
    )
    parser.add_argument(
        "--print-inputs",
        action="store_true",
        help=(
            "Observe-only: print opus/haiku artifact paths and item "
            "counts to STDERR before comparison runs."
        ),
    )
    parser.add_argument(
        "--print-scores",
        action="store_true",
        help=(
            "Observe-only: print the full comparison_result payload "
            "to STDERR after comparison runs."
        ),
    )
    parser.add_argument(
        "--include-sonnet",
        action="store_true",
        help=(
            "Also compare the Sonnet candidate artifact (a "
            "meeting_minutes_llm artifact whose provenance.model_id "
            "contains 'sonnet') against the SAME Opus baseline and "
            "emit a three-way comparison_result. Fail-closed: if no "
            "populated Sonnet artifact exists the run halts rather "
            "than degrading to a mislabelled two-way result."
        ),
    )
    parser.add_argument(
        "--allow-mixed-schema",
        action="store_true",
        help=(
            "Phase 1: allow comparison when the Haiku artifact's "
            "schema_version differs from the Opus baseline's "
            "schema_version. CLI-ONLY: this flag is NEVER read from "
            "environment variables or config files (deliberate — a "
            "mixed-schema comparison is a foot-gun that must be "
            "consciously opted into per-invocation). The flag's "
            "presence is logged in the returned summary for audit."
        ),
    )
    parser.add_argument(
        "--use-cascade-output",
        action="store_true",
        default=False,
        help=(
            "Phase 6: compare the cascade-filtered Haiku artifact "
            "(`meeting_minutes_filtered__*.json`) against Opus instead "
            "of the raw `meeting_minutes__*.json`. Fail-closed with "
            "`cascade_artifact_not_found` if no cascade artifact exists "
            "for the source. Default OFF — the comparison engine's "
            "default behaviour is byte-identical to pre-Phase-6."
        ),
    )
    parser.add_argument(
        "--chunking-strategy",
        default=None,
        help=(
            "Phase 2.B follow-up: override the chunking_strategy_version "
            "used to filter haiku artifact candidates. When omitted "
            "(the recommended default) the script auto-detects the "
            "strategy from the Opus baseline so the selector prefers "
            "the haiku artifact that MATCHES the baseline. When "
            "provided, candidates are filtered to those at the given "
            "version (e.g. `speaker_turn_v1_overlap2`). The flag "
            "controls SELECTION only; the cross-check halt "
            "`chunking_strategy_mismatch` still fires if the resulting "
            "haiku artifact's strategy differs from the baseline's, so "
            "this flag is NOT a gate bypass."
        ),
    )
    args = parser.parse_args(argv)
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    chunking_strategy_override: Optional[str] = (
        args.chunking_strategy
        if isinstance(args.chunking_strategy, str)
        and args.chunking_strategy.strip()
        else None
    )

    try:
        result = run_comparison(
            data_lake=data_lake,
            source_id=args.source_id,
            dry_run=args.dry_run,
            print_inputs=args.print_inputs,
            print_scores=args.print_scores,
            include_sonnet=args.include_sonnet,
            allow_mixed_schema=args.allow_mixed_schema,
            use_cascade_output=args.use_cascade_output,
            chunking_strategy_override=chunking_strategy_override,
        )
    except ComparisonError as exc:
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
