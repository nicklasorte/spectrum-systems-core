#!/usr/bin/env python3
"""Cross-meeting synthesis over the promoted ``meeting_minutes`` corpus.

This is a reusable, triggerable workflow step. It reads EVERY promoted
``meeting_minutes`` artifact in the data-lake (the STRUCTURED product
artifacts — never a raw transcript), runs a SINGLE Opus pass over the
whole corpus, and writes one ``cross_meeting_synthesis`` instrument
artifact: decision threads, an open-action registry, claim drift, the
unresolved-question registry, and a narrative arc of the TIG process.

It is distinct from the per-transcript Opus reference baseline
(``create_opus_reference_baselines.py``): that reads one raw transcript
per source; this reads the promoted, evaluated, governed
``meeting_minutes`` artifacts and synthesizes ACROSS all meetings.

Why an instrument artifact (not a promoted product, not ground truth):
the synthesis is a single model pass over the corpus. It is NEVER
promoted, NEVER read back into the governed loop, and NEVER enters
``indexes/meetings/artifact_index.jsonl``. It is the cross-meeting
analogue of ``comparison_result`` / ``corpus_comparison``.

Fail-closed contract (every gate halts the run; nothing partial is
written):

* ``--data-lake`` is not a directory -> ``data_lake_not_a_directory``.
* ``--model`` missing/empty -> ``missing_model`` (the workflow resolves
  it from ``ai/registry/model_registry.json``; the script NEVER
  hardcodes a model string).
* Fewer than ``max(--min-meetings, 2)`` promoted ``meeting_minutes``
  artifacts -> ``insufficient_corpus`` (cross-meeting synthesis is
  meaningless on a single meeting; the floor of 2 is a hard constraint
  regardless of ``--min-meetings``).
* A ``meeting_minutes`` file present but failing the meeting_minutes
  schema -> ``invalid_minutes_artifact`` (the flat
  ``{"artifact_type": "meeting_minutes", **payload}`` form is validated
  via ``_artifact_validator.validate_artifact`` BEFORE any field is
  read off the payload — CLAUDE.md read-path co-requirement).
* The model transport fails -> ``llm_transport_error`` (no fallback to
  a weaker model, no partial file).
* The model returns non-JSON / a non-object -> ``malformed_synthesis_response``.
* The model cites a ``source_id`` not in the corpus ->
  ``malformed_synthesis_response`` (attribution must tie back to a
  promoted artifact actually read; this is what makes "reads promoted
  artifacts only" verifiable end-to-end).
* The narrative is empty or < 100 chars -> ``narrative_too_short``.
* After writing, the artifact is shadowed by a ``.gitignore`` rule in
  the data-lake clone -> ``gitignore_blocks_artifact``.

Determinism that matters here: the model pass itself is not byte-stable
(it is an Opus synthesis, like the reference baseline — that is why a
synthesis is written once per run with a timestamped filename, never
overwritten). But every field the SCRIPT controls is deterministic and
fail-closed:

* ``source_ids`` / ``corpus_span`` / ``provenance.input_artifact_ids``
  are computed from the artifacts on disk, never from the model.
* Every ``*_id`` is re-stamped with a frozen-namespace UUID5 so id
  quality never depends on the model.
* Every ``*_date`` is OVERRIDDEN with the date the script derives from
  the cited ``source_id`` slug, so the model can never invent a date
  and a cited meeting that is not in the corpus is a hard halt.
* ``open_actions[].status`` is RECOMPUTED from ``closed_meeting``
  corpus membership: a closure recorded in any meeting in the corpus
  marks the action ``closed``; a ``closed_meeting`` not present in the
  corpus is ``unclear``; no ``closed_meeting`` is ``open``. A later
  meeting's closure can NEVER be mislabelled "open" because the whole
  corpus is read in one pass.
* ``decision_threads[].open`` is RECOMPUTED: a thread is open iff any
  of its decisions is ``active`` or ``deferred``.

Context-window guard: when the corpus carries more than
``_CONTEXT_ITEM_LIMIT`` (500) content items, the per-meeting context is
SUMMARIZED — per-type counts plus a deterministic capped sample — so a
large corpus can never blow the Opus context window into a truncated,
invalid response.

Test seam: ``run_synthesis`` accepts an injected ``client`` callable
``(*, system: str, user: str) -> str`` (the SAME seam
``workflows/llm_client.py`` defines); the subprocess integration test
drives it via the ``CROSS_MEETING_SYNTHESIS_STUB_RESPONSE`` env var so
the suite needs no API key and no network.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# scripts/ on sys.path so the artifact validator import works whether
# this file is run as a script or imported as a module by tests.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from spectrum_systems_core.workflows.llm_client import (  # noqa: E402
    AnthropicJSONClient,
    LLMClientError,
)

ARTIFACT_TYPE = "cross_meeting_synthesis"
SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "cross_meeting_synthesis_workflow"
MINUTES_TYPE = "meeting_minutes"
# Preferred producer when a meeting has more than one promoted
# meeting_minutes artifact (the LLM read is richer than the regex one).
_PREFERRED_PROVENANCE = "meeting_minutes_llm"

# Offline / test transport seam. The SAME explicit env-var pattern
# ``create_opus_reference_baselines.py`` uses
# (``OPUS_REFERENCE_BASELINE_STUB_RESPONSE``): set ONLY in tests, so it
# can never silently shadow a production run, and it is a transport
# stub, NOT a model-string override (``--model`` is still required and
# is what gets stamped into the artifact).
_STUB_ENV = "CROSS_MEETING_SYNTHESIS_STUB_RESPONSE"

# Frozen namespace. Freezing it is the id-determinism contract:
# re-running over the same model output reproduces the same ids.
# Changing this constant re-keys every future synthesis.
_SYNTHESIS_NAMESPACE = uuid.UUID("8c2e1b4a-7d6f-5a3c-9b0e-2f1d4c6a8e07")

# Above this many corpus content items, the per-meeting context is
# summarized (per-type counts + a capped sample) instead of listing
# every item, so a large corpus cannot truncate the Opus response into
# invalid JSON. The whole corpus is still READ and counted; only the
# prompt rendering is condensed.
_CONTEXT_ITEM_LIMIT = 500
_SAMPLE_PER_TYPE = 12

# A long synthesis over 13+ meetings can need several thousand output
# tokens; set an explicit generous bound rather than inheriting the
# shared 4000 default that would truncate the response into invalid
# JSON. Mirrors the opus reference baseline rationale.
_SYNTHESIS_MAX_TOKENS = 16384

_SYSTEM_PROMPT = (
    "You are analyzing a corpus of federal spectrum policy meeting "
    "artifacts. Identify decision threads, track action item closure, "
    "detect claim drift, and surface unresolved questions. Return only "
    "valid JSON matching the synthesis schema."
)

# How the user message instructs the model to shape its JSON. The
# script re-stamps every id and overrides every date, so the model is
# told ids/dates are optional — only the analytical content and the
# verbatim source_id attribution matter.
_RESPONSE_CONTRACT = """
Return ONLY a single JSON object (no markdown, no prose) with exactly
these keys:

{
  "decision_threads": [
    {
      "topic": "<short topic>",
      "summary": "<2-3 sentence thread summary>",
      "decisions": [
        {
          "source_id": "<verbatim source_id from the corpus>",
          "text": "<decision text>",
          "regulatory_verb": "<verb or null>",
          "status": "active|superseded|deferred|resolved"
        }
      ]
    }
  ],
  "open_actions": [
    {
      "text": "<action text>",
      "owner": "<owner or empty string>",
      "assigned_meeting": "<verbatim source_id where it was assigned>",
      "closed_meeting": "<verbatim source_id where it was closed, or null>"
    }
  ],
  "claim_drift": [
    {
      "topic": "<short topic>",
      "drift_detected": true,
      "drift_summary": "<how the claim drifted, or null>",
      "instances": [
        {
          "source_id": "<verbatim source_id from the corpus>",
          "text": "<claim text>",
          "speaker": "<speaker or empty string>"
        }
      ]
    }
  ],
  "unresolved_questions": [
    {
      "text": "<question text>",
      "raised_meeting": "<verbatim source_id where it was raised>",
      "resolution": "<resolution text or null>",
      "resolved": false
    }
  ],
  "narrative_summary": "<2-3 paragraph narrative of the TIG process arc: what was decided, what remains open, what the trajectory is>"
}

Every source_id you cite MUST be one of the verbatim source_id values
listed in the corpus below. Do not invent ids or dates; the harness
assigns them. The narrative_summary must be at least one full
paragraph.
""".strip()

# Primary text field per meeting_minutes content type. ``None`` means a
# plain-string item (the three legacy arrays may also be objects). This
# MIRRORS scripts/compare_opus_haiku._PRIMARY_TEXT_FIELD so the corpus
# is read into text the SAME way every other reader does.
_DECISION_OBJ_FIELD = "text"
_ACTION_OBJ_FIELD = "action"
_QUESTION_OBJ_FIELD = "question_text"
_CLAIM_OBJ_FIELD = "claim_text"

_DATE_TOKEN_RES = (
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
)


class SynthesisError(RuntimeError):
    """A fail-closed halt. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _date_from_source_id(source_id: str) -> Optional[str]:
    """ISO date parsed from a source_id slug, or None. Never inferred.

    Slugs in this corpus end with a date token (e.g.
    ``...-transcript-20251218``). We read it deterministically off the
    promoted artifact's own slug — we NEVER open the raw transcript or
    raw metadata to get a date (the synthesis reads promoted artifacts
    only). The LAST date token in the slug wins so a leading numeric
    fragment cannot shadow the real meeting date.
    """
    found: Optional[str] = None
    for pat in _DATE_TOKEN_RES:
        for m in pat.finditer(source_id):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                found = datetime.date(y, mo, d).isoformat()
            except ValueError:
                continue
    return found


def _strip_fence(text: str) -> str:
    """Strip a markdown code fence the model may have wrapped JSON in.

    Runs before ``json.loads`` so a fenced-but-valid response is not
    treated as malformed. Mirrors
    ``create_opus_reference_baselines._strip_fence``.
    """
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body[3:]
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    return body.strip()


def _item_text(item: Any, obj_field: str) -> str:
    """Comparable text for one content item (string- or object-form).

    The meeting_minutes schema allows a legacy string OR a structured
    object for decisions/action_items/open_questions; claims are always
    objects. Returns '' for an unreadable item (it is then skipped, not
    a halt — a single bad item must not abort a corpus-wide synthesis).
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        val = item.get(obj_field)
        if isinstance(val, str) and val.strip():
            return val.strip()
        # Tolerant fallback: first non-empty string value.
        for v in item.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _decision_verb(item: Any) -> Optional[str]:
    if isinstance(item, dict):
        v = item.get("verb")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _claim_speaker(item: Any) -> str:
    if isinstance(item, dict):
        s = item.get("speaker")
        if isinstance(s, str) and s.strip():
            return s.strip()
    return ""


def _meeting_dirs(data_lake: Path) -> List[Path]:
    meetings = data_lake / "store" / "processed" / "meetings"
    if not meetings.is_dir():
        return []
    return sorted(
        (p for p in meetings.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )


def _select_minutes_artifact(
    meeting_dir: Path,
) -> Optional[Tuple[Dict[str, Any], Path]]:
    """The one promoted meeting_minutes artifact for this meeting.

    A meeting may carry more than one ``meeting_minutes__*.json`` (regex
    + LLM). We pick deterministically: the FIRST whose
    ``payload.provenance.produced_by == "meeting_minutes_llm"``, else
    the lexicographically first. The flat
    ``{"artifact_type": "meeting_minutes", **payload}`` form is
    validated against the meeting_minutes schema BEFORE any field is
    read (CLAUDE.md read-path co-requirement); a drifted/garbage
    artifact halts ``invalid_minutes_artifact`` rather than silently
    feeding garbage into the synthesis. Returns ``None`` when the
    meeting has no meeting_minutes artifact at all (it is simply not
    part of the corpus — not a halt).
    """
    candidates = sorted(meeting_dir.glob(f"{MINUTES_TYPE}__*.json"))
    if not candidates:
        return None
    chosen: Optional[Tuple[Dict[str, Any], Path]] = None
    fallback: Optional[Tuple[Dict[str, Any], Path]] = None
    for path in candidates:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SynthesisError(
                "invalid_minutes_artifact",
                f"meeting_minutes artifact at {path} unreadable/!json: "
                f"{exc}",
            ) from exc
        if not isinstance(envelope, dict):
            raise SynthesisError(
                "invalid_minutes_artifact",
                f"meeting_minutes artifact at {path} is "
                f"{type(envelope).__name__}, expected a JSON object",
            )
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise SynthesisError(
                "invalid_minutes_artifact",
                f"meeting_minutes artifact at {path} has no payload "
                f"object",
            )
        flat = {"artifact_type": MINUTES_TYPE, **payload}
        try:
            validate_artifact(flat, MINUTES_TYPE, str(path))
        except ArtifactValidationError as exc:
            raise SynthesisError(
                "invalid_minutes_artifact",
                f"meeting_minutes artifact at {path} failed schema: "
                f"{exc}",
            ) from exc
        prov = payload.get("provenance")
        produced_by = (
            prov.get("produced_by") if isinstance(prov, dict) else None
        )
        if produced_by == _PREFERRED_PROVENANCE and chosen is None:
            chosen = (envelope, path)
        if fallback is None:
            fallback = (envelope, path)
    return chosen or fallback


def load_corpus(
    data_lake: Path, required_min: int
) -> List[Dict[str, Any]]:
    """Every promoted meeting_minutes artifact, oldest-slug-first.

    Each corpus entry is ``{source_id, meeting_date, artifact_id,
    payload, decisions[], action_items[], open_questions[], claims[]}``
    where the four lists are the readable text strings. Halts
    ``insufficient_corpus`` when fewer than ``required_min`` meetings
    carry a promoted meeting_minutes artifact.
    """
    corpus: List[Dict[str, Any]] = []
    for mdir in _meeting_dirs(data_lake):
        selected = _select_minutes_artifact(mdir)
        if selected is None:
            continue
        envelope, _path = selected
        payload = envelope["payload"]
        source_id = mdir.name
        meeting_date = _date_from_source_id(source_id)

        def _texts(key: str, obj_field: str) -> List[str]:
            raw = payload.get(key)
            if not isinstance(raw, list):
                return []
            out: List[str] = []
            for it in raw:
                t = _item_text(it, obj_field)
                if t:
                    out.append(t)
            return out

        decisions_raw = (
            payload.get("decisions")
            if isinstance(payload.get("decisions"), list)
            else []
        )
        decisions = [
            {
                "text": _item_text(it, _DECISION_OBJ_FIELD),
                "verb": _decision_verb(it),
            }
            for it in decisions_raw
            if _item_text(it, _DECISION_OBJ_FIELD)
        ]
        claims_raw = (
            payload.get("claims")
            if isinstance(payload.get("claims"), list)
            else []
        )
        claims = [
            {
                "text": _item_text(it, _CLAIM_OBJ_FIELD),
                "speaker": _claim_speaker(it),
            }
            for it in claims_raw
            if _item_text(it, _CLAIM_OBJ_FIELD)
        ]

        corpus.append(
            {
                "source_id": source_id,
                "meeting_date": meeting_date,
                "artifact_id": envelope.get("artifact_id", ""),
                "title": payload.get("title", ""),
                "decisions": decisions,
                "action_items": _texts("action_items", _ACTION_OBJ_FIELD),
                "open_questions": _texts(
                    "open_questions", _QUESTION_OBJ_FIELD
                ),
                "claims": claims,
            }
        )

    if len(corpus) < required_min:
        raise SynthesisError(
            "insufficient_corpus",
            f"found {len(corpus)} promoted meeting_minutes artifact(s); "
            f"cross-meeting synthesis requires at least {required_min} "
            f"(a single meeting cannot be synthesized across)",
        )
    return corpus


def _total_items(corpus: List[Dict[str, Any]]) -> int:
    return sum(
        len(m["decisions"])
        + len(m["action_items"])
        + len(m["open_questions"])
        + len(m["claims"])
        for m in corpus
    )


def build_corpus_context(
    corpus: List[Dict[str, Any]],
) -> Tuple[str, int, bool]:
    """Render the corpus into the Opus user message.

    Returns ``(context_text, total_items, summarized)``. When the
    corpus carries more than ``_CONTEXT_ITEM_LIMIT`` items the
    per-meeting block is summarized: per-type counts plus the first
    ``_SAMPLE_PER_TYPE`` items per type (deterministic — original list
    order) with an explicit "... N more" marker. Otherwise every item
    is listed. The whole corpus is read either way; only the rendering
    is condensed so a large corpus never truncates the response.
    """
    total = _total_items(corpus)
    summarized = total > _CONTEXT_ITEM_LIMIT

    def _render_list(label: str, items: List[str]) -> List[str]:
        lines = [f"  {label} ({len(items)}):"]
        shown = items if not summarized else items[:_SAMPLE_PER_TYPE]
        for t in shown:
            lines.append(f"    - {t}")
        if summarized and len(items) > len(shown):
            lines.append(f"    ... {len(items) - len(shown)} more")
        return lines

    blocks: List[str] = []
    for m in corpus:
        blocks.append(
            f"### MEETING source_id={m['source_id']} "
            f"date={m['meeting_date'] or 'unknown'} "
            f"title={m['title'] or '(untitled)'}"
        )
        blocks.extend(
            _render_list(
                "decisions", [d["text"] for d in m["decisions"]]
            )
        )
        blocks.extend(_render_list("action_items", m["action_items"]))
        blocks.extend(
            _render_list("open_questions", m["open_questions"])
        )
        blocks.extend(
            _render_list("claims", [c["text"] for c in m["claims"]])
        )
        blocks.append("")

    header = (
        f"CORPUS: {len(corpus)} promoted meeting_minutes artifacts, "
        f"{total} total content items"
        + (
            " (per-meeting lists summarized to fit the context window; "
            "counts are exact)"
            if summarized
            else ""
        )
        + ".\n"
        "Synthesize ACROSS these meetings.\n"
    )
    context = (
        header
        + "\n".join(blocks)
        + "\n\n"
        + _RESPONSE_CONTRACT
    )
    return context, total, summarized


def parse_synthesis_response(raw: str) -> Dict[str, Any]:
    """Model text -> JSON object, or HALT ``malformed_synthesis_response``.

    No coercion, no partial accept: a non-JSON / non-object response is
    a fail-closed halt.
    """
    body = _strip_fence(raw)
    if not body:
        raise SynthesisError(
            "malformed_synthesis_response", "model returned empty text"
        )
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SynthesisError(
            "malformed_synthesis_response",
            f"model response is not valid JSON: {exc}",
        ) from exc
    if not isinstance(doc, dict):
        raise SynthesisError(
            "malformed_synthesis_response",
            f"model response JSON is {type(doc).__name__}, not an "
            f"object",
        )
    return doc


def _stamp(*parts: str) -> str:
    return str(uuid.uuid5(_SYNTHESIS_NAMESPACE, "|".join(parts)))


def _require_list(doc: Dict[str, Any], key: str) -> List[Any]:
    val = doc.get(key)
    if not isinstance(val, list):
        raise SynthesisError(
            "malformed_synthesis_response",
            f"{key!r} is {type(val).__name__}, expected a list",
        )
    return val


def _require_str(obj: Dict[str, Any], key: str, where: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise SynthesisError(
            "malformed_synthesis_response",
            f"{where}: missing/empty string field {key!r}",
        )
    return val.strip()


def _checked_source_id(
    obj: Dict[str, Any], key: str, where: str, corpus_ids: set
) -> str:
    sid = _require_str(obj, key, where)
    if sid not in corpus_ids:
        raise SynthesisError(
            "malformed_synthesis_response",
            f"{where}: {key}={sid!r} is not a source_id in the corpus "
            f"(the synthesis must attribute to a promoted artifact it "
            f"actually read)",
        )
    return sid


def assemble_artifact(
    *,
    model_doc: Dict[str, Any],
    corpus: List[Dict[str, Any]],
    model: str,
    synthesized_at: str,
    total_items: int,
) -> Dict[str, Any]:
    """Build the validated cross_meeting_synthesis artifact.

    Every id is re-stamped, every date is overridden from the cited
    source_id, ``open_actions[].status`` and ``decision_threads[].open``
    are recomputed deterministically, and the narrative length floor is
    enforced. The assembled artifact is validated against the schema
    before it is returned (the caller never writes an unvalidated
    artifact).
    """
    corpus_ids = {m["source_id"] for m in corpus}
    date_by_id = {m["source_id"]: m["meeting_date"] for m in corpus}

    # --- decision_threads -------------------------------------------------
    threads_out: List[Dict[str, Any]] = []
    for ti, thread in enumerate(_require_list(model_doc, "decision_threads")):
        if not isinstance(thread, dict):
            raise SynthesisError(
                "malformed_synthesis_response",
                f"decision_threads[{ti}] is not an object",
            )
        topic = _require_str(thread, "topic", f"decision_threads[{ti}]")
        decisions_in = thread.get("decisions")
        if not isinstance(decisions_in, list):
            raise SynthesisError(
                "malformed_synthesis_response",
                f"decision_threads[{ti}].decisions is not a list",
            )
        decisions_out: List[Dict[str, Any]] = []
        for di, dec in enumerate(decisions_in):
            where = f"decision_threads[{ti}].decisions[{di}]"
            if not isinstance(dec, dict):
                raise SynthesisError(
                    "malformed_synthesis_response", f"{where} not an object"
                )
            sid = _checked_source_id(dec, "source_id", where, corpus_ids)
            status = dec.get("status")
            if status not in (
                "active",
                "superseded",
                "deferred",
                "resolved",
            ):
                raise SynthesisError(
                    "malformed_synthesis_response",
                    f"{where}: status={status!r} not one of "
                    f"active/superseded/deferred/resolved",
                )
            verb = dec.get("regulatory_verb")
            decisions_out.append(
                {
                    "source_id": sid,
                    "meeting_date": date_by_id[sid],
                    "text": _require_str(dec, "text", where),
                    "regulatory_verb": (
                        verb if isinstance(verb, str) and verb else None
                    ),
                    "status": status,
                }
            )
        # Recomputed, never trusted: a thread is open iff any decision
        # is still active or deferred.
        is_open = any(
            d["status"] in ("active", "deferred") for d in decisions_out
        )
        summary = thread.get("summary")
        threads_out.append(
            {
                "thread_id": _stamp("thread", str(ti), topic),
                "topic": topic,
                "decisions": decisions_out,
                "open": is_open,
                "summary": summary if isinstance(summary, str) else "",
            }
        )

    # --- open_actions -----------------------------------------------------
    actions_out: List[Dict[str, Any]] = []
    for ai, act in enumerate(_require_list(model_doc, "open_actions")):
        where = f"open_actions[{ai}]"
        if not isinstance(act, dict):
            raise SynthesisError(
                "malformed_synthesis_response", f"{where} is not an object"
            )
        text = _require_str(act, "text", where)
        assigned = _checked_source_id(
            act, "assigned_meeting", where, corpus_ids
        )
        closed_raw = act.get("closed_meeting")
        closed_meeting: Optional[str] = (
            closed_raw.strip()
            if isinstance(closed_raw, str) and closed_raw.strip()
            else None
        )
        # status is RECOMPUTED from corpus membership — never trusted
        # from the model. The whole corpus is read in one pass, so a
        # closure recorded in any meeting is visible here; a closure
        # that points at a meeting NOT in the corpus is "unclear", and
        # no closure is "open". This is the red-team fix for "a closed
        # item marked open because the closure was in a later meeting".
        if closed_meeting is None:
            status = "open"
            closed_date = None
        elif closed_meeting in corpus_ids:
            status = "closed"
            closed_date = date_by_id[closed_meeting]
        else:
            status = "unclear"
            closed_date = None
        owner = act.get("owner")
        actions_out.append(
            {
                "action_id": _stamp("action", str(ai), text),
                "text": text,
                "owner": owner if isinstance(owner, str) else "",
                "assigned_meeting": assigned,
                "assigned_date": date_by_id[assigned],
                "closed_meeting": closed_meeting,
                "closed_date": closed_date,
                "status": status,
            }
        )

    # --- claim_drift ------------------------------------------------------
    drift_out: List[Dict[str, Any]] = []
    for ci, claim in enumerate(_require_list(model_doc, "claim_drift")):
        where = f"claim_drift[{ci}]"
        if not isinstance(claim, dict):
            raise SynthesisError(
                "malformed_synthesis_response", f"{where} is not an object"
            )
        topic = _require_str(claim, "topic", where)
        instances_in = claim.get("instances")
        if not isinstance(instances_in, list):
            raise SynthesisError(
                "malformed_synthesis_response",
                f"{where}.instances is not a list",
            )
        instances_out: List[Dict[str, Any]] = []
        for ii, inst in enumerate(instances_in):
            iw = f"{where}.instances[{ii}]"
            if not isinstance(inst, dict):
                raise SynthesisError(
                    "malformed_synthesis_response", f"{iw} not an object"
                )
            sid = _checked_source_id(inst, "source_id", iw, corpus_ids)
            spk = inst.get("speaker")
            instances_out.append(
                {
                    "source_id": sid,
                    "meeting_date": date_by_id[sid],
                    "text": _require_str(inst, "text", iw),
                    "speaker": spk if isinstance(spk, str) else "",
                }
            )
        drift_detected = bool(claim.get("drift_detected", False))
        ds = claim.get("drift_summary")
        drift_out.append(
            {
                "claim_id": _stamp("claim", str(ci), topic),
                "topic": topic,
                "instances": instances_out,
                "drift_detected": drift_detected,
                "drift_summary": ds if isinstance(ds, str) and ds else None,
            }
        )

    # --- unresolved_questions --------------------------------------------
    questions_out: List[Dict[str, Any]] = []
    for qi, q in enumerate(
        _require_list(model_doc, "unresolved_questions")
    ):
        where = f"unresolved_questions[{qi}]"
        if not isinstance(q, dict):
            raise SynthesisError(
                "malformed_synthesis_response", f"{where} is not an object"
            )
        text = _require_str(q, "text", where)
        raised = _checked_source_id(
            q, "raised_meeting", where, corpus_ids
        )
        res = q.get("resolution")
        questions_out.append(
            {
                "question_id": _stamp("question", str(qi), text),
                "text": text,
                "raised_meeting": raised,
                "raised_date": date_by_id[raised],
                "resolution": res if isinstance(res, str) and res else None,
                "resolved": bool(q.get("resolved", False)),
            }
        )

    # --- narrative --------------------------------------------------------
    narrative = model_doc.get("narrative_summary")
    if not isinstance(narrative, str) or len(narrative.strip()) < 100:
        raise SynthesisError(
            "narrative_too_short",
            f"narrative_summary must be a non-empty string of at least "
            f"100 chars; got "
            f"{len(narrative.strip()) if isinstance(narrative, str) else 0}",
        )

    # --- deterministic envelope fields (script-owned, never the model) ---
    source_ids = sorted(corpus_ids)
    dated = sorted(
        d for d in (m["meeting_date"] for m in corpus) if d is not None
    )
    corpus_span = {
        "earliest_meeting": dated[0] if dated else None,
        "latest_meeting": dated[-1] if dated else None,
        "total_meetings": len(corpus),
        "total_items_read": total_items,
    }
    input_artifact_ids = sorted(
        a for a in (m["artifact_id"] for m in corpus) if a
    )

    artifact = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "source_ids": source_ids,
        "model_id": model,
        "synthesized_at": synthesized_at,
        "corpus_span": corpus_span,
        "decision_threads": threads_out,
        "open_actions": actions_out,
        "claim_drift": drift_out,
        "unresolved_questions": questions_out,
        "narrative_summary": narrative.strip(),
        "provenance": {
            "produced_by": PRODUCED_BY,
            "input_artifact_ids": input_artifact_ids,
        },
    }

    # Validate our OWN output before returning it (fail-closed: the
    # caller never writes a malformed cross_meeting_synthesis).
    try:
        validate_artifact(artifact, ARTIFACT_TYPE)
    except ArtifactValidationError as exc:
        raise SynthesisError(
            "malformed_synthesis_response",
            f"assembled synthesis failed its own schema: {exc}",
        ) from exc
    return artifact


def _out_path(data_lake: Path, synthesized_at: str) -> Path:
    safe_ts = (
        synthesized_at.replace(":", "").replace("+", "").replace("-", "")
    )
    return (
        data_lake
        / "store"
        / "artifacts"
        / "synthesis"
        / f"cross_meeting_synthesis_{safe_ts}.json"
    )


def _is_git_worktree(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _assert_not_gitignored(data_lake: Path, abs_path: Path) -> None:
    """Refuse to leave behind a committed-never artifact.

    Mirrors ``create_opus_reference_baselines._assert_not_gitignored``
    and ``scripts/_gitignore_audit.py``: ``git check-ignore -v`` returns
    rc=0 for ANY matched pattern including ``!`` un-ignore patterns, so
    a matched ``!``-pattern means the path is NOT ignored.
    """
    if not _is_git_worktree(data_lake):
        return
    try:
        rel = abs_path.relative_to(data_lake)
    except ValueError:
        rel = abs_path
    result = subprocess.run(
        [
            "git", "-C", str(data_lake), "check-ignore", "-v",
            "--no-index", str(rel),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return  # not ignored
    if result.returncode == 0:
        line = result.stdout.strip()
        try:
            head, _ = line.split("\t", 1)
            pattern = head.rsplit(":", 1)[1]
        except (IndexError, ValueError):
            pattern = ""
        if pattern.startswith("!"):
            return  # matched an un-ignore negation -> not ignored
        raise SynthesisError(
            "gitignore_blocks_artifact",
            f"{rel} is ignored by data-lake .gitignore rule: {line} — "
            f"add '!**/artifacts/**/' (re-include the directory chain) "
            f"and '!**/artifacts/synthesis/*.json' before committing",
        )
    raise SynthesisError(
        "gitignore_blocks_artifact",
        f"git check-ignore returned rc={result.returncode} for {rel}: "
        f"{result.stderr.strip()}",
    )


def run_synthesis(
    *,
    data_lake: Path,
    dry_run: bool,
    model: str,
    min_meetings: int,
    client: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """Orchestrate one synthesis. Returns a summary dict; raises on halt."""
    if not model or not model.strip():
        raise SynthesisError(
            "missing_model",
            "--model is required and must be non-empty (the workflow "
            "resolves it from ai/registry/model_registry.json)",
        )
    model = model.strip()

    # Hard floor of 2 regardless of --min-meetings: a cross-meeting
    # synthesis over a single meeting is meaningless (constraint).
    required_min = max(int(min_meetings), 2)

    corpus = load_corpus(data_lake, required_min)
    context, total_items, summarized = build_corpus_context(corpus)

    if client is not None:
        active_client: Callable[..., str] = client
    else:
        stub = os.environ.get(_STUB_ENV)
        if stub is not None:
            def _stub_client(*, system: str, user: str) -> str:  # noqa: ARG001
                return stub
            active_client = _stub_client
        else:
            active_client = AnthropicJSONClient(
                model=model, max_tokens=_SYNTHESIS_MAX_TOKENS
            )

    try:
        raw = active_client(system=_SYSTEM_PROMPT, user=context)
    except LLMClientError as exc:
        raise SynthesisError(
            "llm_transport_error",
            f"model transport failed: {exc} — no fallback model, no "
            f"partial file written",
        ) from exc

    model_doc = parse_synthesis_response(raw)
    synthesized_at = _now_utc_iso()
    artifact = assemble_artifact(
        model_doc=model_doc,
        corpus=corpus,
        model=model,
        synthesized_at=synthesized_at,
        total_items=total_items,
    )

    out_path = _out_path(data_lake, synthesized_at)
    summary = {
        "status": "success",
        "dry_run": dry_run,
        "model": model,
        "summarized_context": summarized,
        "output_path": str(out_path),
        "meetings_synthesized": len(corpus),
        "decision_threads": len(artifact["decision_threads"]),
        "decision_threads_open": sum(
            1 for t in artifact["decision_threads"] if t["open"]
        ),
        "open_actions": sum(
            1 for a in artifact["open_actions"] if a["status"] == "open"
        ),
        "claim_drift_detected": sum(
            1 for c in artifact["claim_drift"] if c["drift_detected"]
        ),
        "unresolved_questions": sum(
            1 for q in artifact["unresolved_questions"] if not q["resolved"]
        ),
        "total_items_read": total_items,
    }

    if dry_run:
        return summary

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _assert_not_gitignored(data_lake, out_path)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the synthesis but write nothing.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model string, resolved by the workflow from "
        "ai/registry/model_registry.json. No default — the script "
        "never hardcodes a model.",
    )
    parser.add_argument(
        "--min-meetings",
        type=int,
        default=2,
        help="Minimum promoted meeting_minutes artifacts required "
        "(default 2; the effective floor is always at least 2).",
    )
    args = parser.parse_args(argv)

    # Mobile workflow_dispatch inputs often arrive with a trailing
    # space pasted from a phone keyboard; strip every string arg.
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
        print(
            f"FAIL: --data-lake is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 2

    try:
        result = run_synthesis(
            data_lake=data_lake,
            dry_run=args.dry_run,
            model=args.model,
            min_meetings=args.min_meetings,
        )
    except SynthesisError as exc:
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
        print(f"FAIL: {exc.reason} — {exc.detail}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        f"Meetings synthesized: {result['meetings_synthesized']}\n"
        f"Decision threads: {result['decision_threads']} "
        f"({result['decision_threads_open']} open)\n"
        f"Open actions: {result['open_actions']}\n"
        f"Claim drift detected: {result['claim_drift_detected']}\n"
        f"Unresolved questions: {result['unresolved_questions']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
