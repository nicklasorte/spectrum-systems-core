"""Phase AC.2 — corpus-wide extraction comparison runner.

Runs the Phase AB three-point ``compare-extraction`` instrument over
EVERY ``.txt`` transcript under a directory, then aggregates the
per-entity F1 (decisions / actions / questions) across the corpus into
a single ``corpus_comparison`` instrument artifact plus a Markdown
projection.

This phase adds NO new evals. Per-entity F1 is the Phase AC.1
``evals.extraction_gap.compute_per_entity_metrics`` drill-down lifted
across meetings; the aggregate is the unweighted mean of the
successful meetings only.

Fail-closed order (mirrors ``comparison_runner`` so the two behave
identically):

  1. pre-flight ANTHROPIC_API_KEY (non-empty) — unless stub mode.
     Missing → exit 1, ``missing_credentials``, NO artifact written.
  2. ``--transcripts`` must be a directory containing at least one
     ``.txt`` → otherwise ``empty_transcripts_dir`` /
     ``transcripts_dir_not_found``, exit 1, NO artifact written.
  3. run compare-extraction per transcript; a per-transcript failure
     is RECORDED and the corpus run CONTINUES.
  4. always write the corpus_comparison artifact (+ markdown) once at
     least one transcript was attempted.
  5. corpus_status: ``complete`` (all ok) / ``degraded`` (≥1 extractor
     failure or empty transcript) / ``rejected`` (<50% of meetings
     succeeded for either extractor). Exit 1 only on ``rejected`` or a
     pre-flight failure.

A corpus_comparison is a run-level measurement record (like
``extraction_comparison``): written even on partial failure so a
blocked corpus still explains itself, never promoted, never indexed,
Git-tracked: NO (it lives under ``processed/``).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..data_lake.paths import processed_corpus_dir, processed_meeting_dir
from ..data_lake.serialize import artifact_to_dict, canonical_json
from ..evals.extraction_gap import (
    CATEGORIES,
    compute_per_entity_metrics,
    parse_opus_output,
)
from . import llm_haiku, llm_opus
from .comparison_runner import (
    COMPARISON_TYPE,
    STUB_ENV_FLAG,
    TELEMETRY_TYPE,
    UNCONSTRAINED_TYPE,
    _new_instrument_artifact,
    _preflight_credentials,
    _stub_opus_extract,
    run_compare_extraction,
    slugify,
)

CORPUS_TYPE = "corpus_comparison"

# The independent gold set sits next to the transcript in the Phase
# AB.4 ``comparison_gold`` layout. When absent, per-entity F1 is NOT
# fabricated as 0.0 — it is recorded as ``null`` with a ``no_gold_set``
# finding and the meeting is excluded from the aggregate mean
# (red-team Pass 1: a fake 0.0 reads like a real measurement).
GOLD_FILENAME = "independent_gold.json"

# Aggregate F1 is the unweighted mean of successful meetings; this is
# the minimum fraction of meetings that must succeed for EACH extractor
# before the corpus is trustworthy at all.
MIN_SUCCESS_RATIO = 0.5


def _discover_transcripts(
    transcripts_dir: Path,
) -> tuple[list[Path], list[str]]:
    """Return ``(txt_paths_sorted, findings)``.

    Only ``*.txt`` files are processed. Any other file (e.g. a
    ``.docx``) is SKIPPED and a ``skipped_non_txt:<name>`` finding is
    emitted so a reader is never left wondering why a transcript was
    ignored (red-team Pass 1 item 2a). Recursion is intentional: the
    ``comparison_gold`` fixture nests ``<meeting>/transcript.txt`` one
    level deep, while the real corpus is a flat directory of ``.txt``
    files — both are covered. Sorted by POSIX path for determinism.
    """
    findings: list[str] = []
    txts: list[Path] = []
    for p in sorted(transcripts_dir.rglob("*"), key=lambda q: q.as_posix()):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".txt":
            txts.append(p)
        elif p.name == GOLD_FILENAME:
            # The sibling gold file is an input, not a skipped
            # transcript — do not emit a noisy finding for it.
            continue
        else:
            findings.append(f"skipped_non_txt:{p.name}")
    return txts, findings


def _read_back_instruments(
    lake_root: Path, meeting_id: str
) -> tuple[dict | None, str | None, dict | None, str]:
    """Read the instrument artifacts ``run_compare_extraction`` just
    wrote. Returns ``(comparison_payload, comparison_artifact_id,
    telemetry_payload, opus_raw_text)``. ``comparison_artifact_id`` is
    the comparison ENVELOPE id (the cross-artifact reference the corpus
    record points at), distinct from ``payload.transcript_artifact_id``
    (the transcript hash). ``opus_raw_text`` is "" when Opus failed (no
    unconstrained artifact)."""
    mdir = processed_meeting_dir(lake_root, meeting_id)
    comp = mdir / f"{COMPARISON_TYPE}__{meeting_id}.json"
    tele = mdir / f"{TELEMETRY_TYPE}__{meeting_id}.json"
    unc = mdir / f"{UNCONSTRAINED_TYPE}__{meeting_id}.json"

    comparison_payload = None
    comparison_artifact_id = None
    telemetry_payload = None
    opus_raw = ""
    if comp.is_file():
        comp_doc = json.loads(comp.read_text(encoding="utf-8"))
        comparison_payload = comp_doc.get("payload")
        comparison_artifact_id = comp_doc.get("artifact_id")
    if tele.is_file():
        telemetry_payload = json.loads(tele.read_text(encoding="utf-8")).get(
            "payload"
        )
    if unc.is_file():
        opus_raw = (
            json.loads(unc.read_text(encoding="utf-8"))
            .get("payload", {})
            .get("raw_output", "")
        )
    return (
        comparison_payload,
        comparison_artifact_id,
        telemetry_payload,
        opus_raw,
    )


def _load_gold(txt_path: Path) -> dict | None:
    """The independent gold set, if a sibling ``independent_gold.json``
    exists and is a JSON object. A malformed gold file is treated as
    absent (no per-entity F1) rather than crashing the whole corpus."""
    gp = txt_path.parent / GOLD_FILENAME
    if not gp.is_file():
        return None
    try:
        gold = json.loads(gp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return gold if isinstance(gold, dict) else None


def _retry_hint(lake_root: Path, txt_path: Path) -> str:
    """Appended to every ``failed:`` status so a new engineer can
    re-run exactly one meeting without reverse-engineering the corpus
    (red-team Pass 1 item 3)."""
    return (
        f"|retry: spectrum-core compare-extraction --lake {lake_root} "
        f"--transcript-file {txt_path}"
    )


def _f1_or_none(metric_block: dict | None, category: str) -> float | None:
    if not metric_block:
        return None
    cat = metric_block.get(category)
    if not isinstance(cat, dict):
        return None
    return cat.get("f1")


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def run_compare_corpus(
    *,
    lake_root: Path | str,
    transcripts_dir: Path | str,
    env: Mapping[str, str] | None = None,
    haiku_extract: Callable[[str], Any] | None = None,
    opus_extract: Callable[[str], Any] | None = None,
    stream=None,
) -> int:
    """Run the three-point comparison over every ``.txt`` transcript
    under ``transcripts_dir`` and write one ``corpus_comparison``
    instrument artifact + Markdown projection.

    Returns 0 when the corpus is ``complete`` or ``degraded`` (the
    instrument was produced and is usable); 1 on a pre-flight failure
    (no artifact) or a ``rejected`` corpus (a corpus-level failure).
    Injected extractors are the test seam; production passes none and
    the real adapters run after the fail-closed pre-flight gate.
    """
    out = stream if stream is not None else sys.stdout
    env = env if env is not None else os.environ
    lake_root = Path(lake_root)
    transcripts_dir = Path(transcripts_dir)

    stub = str(env.get(STUB_ENV_FLAG, "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }

    # 1. pre-flight credentials (skipped only in stub mode), ahead of
    #    any directory walk so a credential-less invocation fails closed
    #    before touching disk.
    if not _preflight_credentials(env, out, stub=stub):
        return 1

    # 2. resolve the transcripts directory.
    if not transcripts_dir.is_dir():
        print(
            f"ERROR: transcripts_dir_not_found:{transcripts_dir}", file=out
        )
        return 1
    txts, discovery_findings = _discover_transcripts(transcripts_dir)
    if not txts:
        print(
            f"ERROR: empty_transcripts_dir:{transcripts_dir} "
            "(no .txt transcript files found)",
            file=out,
        )
        for f in discovery_findings:
            print(f"  finding: {f}", file=out)
        return 1

    if stub:
        haiku_extract = haiku_extract or llm_haiku.stub_extract
        opus_extract = opus_extract or _stub_opus_extract
    else:
        haiku_extract = haiku_extract or llm_haiku.real_extract
        opus_extract = opus_extract or llm_opus.real_extract

    per_meeting: dict[str, dict] = {}
    meeting_ids: list[str] = []
    # Accumulators for the aggregate. F1 lists hold ONLY successful,
    # gold-backed meetings — failed extractors and gold-less meetings
    # are excluded from the mean (red-team Pass 2 item 3).
    f1_acc: dict[str, dict[str, list[float]]] = {
        cat: {"haiku": [], "opus": []} for cat in CATEGORIES
    }
    cost_acc = {"haiku": 0.0, "opus": 0.0}
    latency_acc = {"haiku": 0, "opus": 0}

    for txt in txts:
        meeting_id = slugify(txt.stem)
        # Two transcripts whose stems slugify identically would clobber
        # each other on disk and in per_meeting. Record the collision
        # explicitly instead of silently overwriting (a silent pass
        # path — red-team Pass 1).
        if meeting_id in per_meeting:
            per_meeting[meeting_id].setdefault("findings", []).append(
                f"slug_collision:{txt.as_posix()}"
            )
            continue
        meeting_ids.append(meeting_id)
        entry: dict[str, Any] = {
            "transcript_file": txt.as_posix(),
            "comparison_artifact_id": None,
            "extractor_status": {},
            "per_entity_f1": None,
            "per_entity_metrics": None,
            "gold_present": False,
            "findings": [],
        }

        raw = txt.read_bytes()
        if not raw.strip():
            # Empty transcript: pre-flight failure for this meeting. Do
            # NOT call the runner (it would just error); record both
            # extractors failed and degrade the corpus.
            hint = _retry_hint(lake_root, txt)
            entry["extractor_status"] = {
                "haiku": f"failed:empty_transcript{hint}",
                "opus": f"failed:empty_transcript{hint}",
            }
            entry["findings"].append("empty_transcript")
            per_meeting[meeting_id] = entry
            print(f"  {meeting_id}: failed:empty_transcript", file=out)
            continue

        # 3. run the three-point comparison for this transcript. A
        #    per-transcript failure is recorded; the corpus continues.
        sink = io.StringIO()
        run_compare_extraction(
            lake_root=lake_root,
            transcript_file=txt,
            env=env,
            haiku_extract=haiku_extract,
            opus_extract=opus_extract,
            stream=sink,
        )
        (
            comparison_payload,
            comparison_artifact_id,
            telemetry_payload,
            opus_raw,
        ) = _read_back_instruments(lake_root, meeting_id)
        if comparison_payload is None:
            entry["extractor_status"] = {
                "haiku": "failed:no_comparison_artifact"
                + _retry_hint(lake_root, txt),
                "opus": "failed:no_comparison_artifact"
                + _retry_hint(lake_root, txt),
            }
            entry["findings"].append("no_comparison_artifact")
            per_meeting[meeting_id] = entry
            print(f"  {meeting_id}: failed:no_comparison_artifact", file=out)
            continue

        raw_status = comparison_payload.get("extractor_status", {})
        hint = _retry_hint(lake_root, txt)
        status = {}
        for name in ("haiku", "opus"):
            s = raw_status.get(name, "failed:unknown")
            status[name] = s if s == "ok" else f"{s}{hint}"
        entry["extractor_status"] = status
        entry["comparison_artifact_id"] = comparison_artifact_id

        # cost / latency: summed from telemetry; a failed extractor
        # contributed 0 (its telemetry block carries 0.0 / 0).
        if telemetry_payload:
            for name in ("haiku", "opus"):
                blk = telemetry_payload.get(name, {}) or {}
                cost_acc[name] += float(blk.get("cost_usd", 0.0) or 0.0)
                latency_acc[name] += int(blk.get("latency_ms", 0) or 0)

        gold = _load_gold(txt)
        if gold is None:
            entry["findings"].append("no_gold_set")
            print(
                f"  {meeting_id}: haiku={status['haiku'].split('|')[0]} "
                f"opus={status['opus'].split('|')[0]} (no gold)",
                file=out,
            )
            per_meeting[meeting_id] = entry
            continue

        entry["gold_present"] = True
        haiku_out = comparison_payload.get("haiku_output") or {}
        opus_parsed, _ = parse_opus_output(opus_raw)
        haiku_pe = compute_per_entity_metrics(haiku_out, gold)
        opus_pe = compute_per_entity_metrics(opus_parsed, gold)
        entry["per_entity_metrics"] = {"haiku": haiku_pe, "opus": opus_pe}
        entry["per_entity_f1"] = {
            cat: {
                "haiku": haiku_pe[cat]["f1"],
                "opus": opus_pe[cat]["f1"],
            }
            for cat in CATEGORIES
        }

        # Only a SUCCESSFUL extractor on a gold-backed meeting feeds the
        # aggregate mean.
        for cat in CATEGORIES:
            if status["haiku"] == "ok":
                f1_acc[cat]["haiku"].append(haiku_pe[cat]["f1"])
            if status["opus"] == "ok":
                f1_acc[cat]["opus"].append(opus_pe[cat]["f1"])

        per_meeting[meeting_id] = entry
        print(
            f"  {meeting_id}: haiku={status['haiku'].split('|')[0]} "
            f"opus={status['opus'].split('|')[0]} (gold)",
            file=out,
        )

    total_meetings = len(meeting_ids)
    haiku_ok = sum(
        1
        for m in meeting_ids
        if per_meeting[m]["extractor_status"].get("haiku") == "ok"
    )
    opus_ok = sum(
        1
        for m in meeting_ids
        if per_meeting[m]["extractor_status"].get("opus") == "ok"
    )
    meetings_failed = sum(
        1
        for m in meeting_ids
        if per_meeting[m]["extractor_status"].get("haiku") != "ok"
        or per_meeting[m]["extractor_status"].get("opus") != "ok"
    )
    meetings_processed = total_meetings - meetings_failed

    haiku_ratio = haiku_ok / total_meetings if total_meetings else 0.0
    opus_ratio = opus_ok / total_meetings if total_meetings else 0.0
    if (
        total_meetings == 0
        or haiku_ratio < MIN_SUCCESS_RATIO
        or opus_ratio < MIN_SUCCESS_RATIO
    ):
        corpus_status = "rejected"
    elif meetings_failed > 0:
        corpus_status = "degraded"
    else:
        corpus_status = "complete"

    aggregate = {
        "per_entity_f1": {
            cat: {
                "haiku": _mean(f1_acc[cat]["haiku"]),
                "opus": _mean(f1_acc[cat]["opus"]),
            }
            for cat in CATEGORIES
        },
        # How many meetings actually fed each mean. A reader seeing
        # "Haiku F1: 0.7" must be able to tell it is the mean of n
        # gold-backed successful meetings, not of all meetings
        # (red-team Pass 1 / Pass 2 item 3).
        "per_entity_f1_n_averaged": {
            cat: {
                "haiku": len(f1_acc[cat]["haiku"]),
                "opus": len(f1_acc[cat]["opus"]),
            }
            for cat in CATEGORIES
        },
        "total_cost_usd": {
            "haiku": round(cost_acc["haiku"], 6),
            "opus": round(cost_acc["opus"], 6),
        },
        "total_latency_ms": {
            "haiku": latency_acc["haiku"],
            "opus": latency_acc["opus"],
        },
        "meetings_processed": meetings_processed,
        "meetings_failed": meetings_failed,
    }

    # Deterministic corpus id: a hash of the sorted meeting ids + the
    # transcripts dir. Two runs over the same corpus reuse the same id
    # (overwrite, never accumulate — the extraction_comparison
    # slug==meeting_id precedent).
    corpus_seed = (
        "|".join(sorted(meeting_ids)) + "||" + transcripts_dir.as_posix()
    )
    corpus_id = (
        "corpus-" + hashlib.sha256(corpus_seed.encode("utf-8")).hexdigest()[:16]
    )

    payload = {
        "schema_version": "1.0.0",
        "corpus_id": corpus_id,
        "transcripts_dir": transcripts_dir.as_posix(),
        "meeting_ids": sorted(meeting_ids),
        "discovery_findings": discovery_findings,
        "per_meeting": per_meeting,
        "aggregate": aggregate,
        "corpus_status": corpus_status,
    }
    trace_id = f"corpus-{hashlib.sha256(corpus_seed.encode()).hexdigest()[:16]}"
    artifact = _new_instrument_artifact(CORPUS_TYPE, payload, trace_id)
    artifact.status = "promoted" if corpus_status == "complete" else "rejected"

    target_dir = processed_corpus_dir(lake_root, corpus_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{CORPUS_TYPE}__{corpus_id}.json"
    target_path.write_text(
        canonical_json(artifact_to_dict(artifact)), encoding="utf-8"
    )

    from ..data_lake.markdown_views import write_corpus_comparison_markdown

    md_path = write_corpus_comparison_markdown(
        lake_root, corpus_id=corpus_id, corpus_payload=artifact.payload
    )

    print(f"corpus_id: {corpus_id}", file=out)
    print(f"corpus status: {corpus_status}", file=out)
    print(
        f"meetings: {meetings_processed} ok / {total_meetings} total "
        f"(failed: {meetings_failed})",
        file=out,
    )
    for f in discovery_findings:
        print(f"  finding: {f}", file=out)
    print(f"wrote: {target_path}", file=out)
    print(f"wrote: {md_path}", file=out)

    return 0 if corpus_status in ("complete", "degraded") else 1


__all__ = [
    "CORPUS_TYPE",
    "GOLD_FILENAME",
    "MIN_SUCCESS_RATIO",
    "run_compare_corpus",
]
