"""GroundTruthLinker: pair transcript source_records with minutes_records.

Phase L.2. Reads transcript ``source_record`` artifacts (source_family
``meetings``) from ``store/processed/meetings/<source_id>/source_record.json``
and from flat ``$SDL_ROOT/<artifact_id>.json`` files. Reads minutes
artifacts from ``$SDL_ROOT/minutes/<minutes_id>.json``.

Matching:
  1. Exact ``meeting_date`` match in a 1-vs-1 cardinality → ``ground_truth_pair``
     with ``match_confidence='high'`` and ``status='confirmed'``.
  2. Within ±1 calendar day in a 1-vs-1 cardinality (after exact pass
     leaves both sides unmatched) → ``ground_truth_pair`` with
     ``match_confidence='medium'`` and ``status='pending_review'``.
  3. Anything else → recorded in ``linking_report`` as unmatched. Never
     auto-paired.

Hard guards:
  * ``meeting_date`` of ``None`` on either side never matches anything,
    including another ``None``.
  * Any date with more than one record on EITHER side routes ALL records
    on that date to unmatched (``duplicate_date_collision``). The linker
    refuses to pick.
  * Within the fuzzy ±1-day pass, any record with more than one candidate
    is routed to unmatched (``ambiguous_fuzzy_match``); never paired
    against an arbitrary peer.

linking_report is ALWAYS written, including when zero pairs are produced.
Never raises; always returns a dict.
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ._paths import contracts_root

PAIR_SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "GroundTruthLinker"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _resolve_sdl_root(data_lake_path: str) -> Optional[Path]:
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        p = Path(env)
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        return p
    if not data_lake_path:
        return None
    base = Path(data_lake_path)
    if not base.exists():
        return None
    return base / "store" / "artifacts"


class GroundTruthLinker:
    """Match transcript source_records to minutes_records by meeting_date."""

    def link(self, data_lake_path: str) -> Dict[str, Any]:
        try:
            return self._link(data_lake_path)
        except Exception as exc:  # defensive: never raise
            return _failure_result(f"unexpected_error:{exc}")

    # -- internals ---------------------------------------------------------

    def _link(self, data_lake_path: str) -> Dict[str, Any]:
        sdl_root = _resolve_sdl_root(data_lake_path)
        if sdl_root is None:
            return _failure_result(
                "sdl_root_unresolved:set SDL_ROOT or pass a valid data_lake_path"
            )

        transcripts = _load_transcripts(data_lake_path, sdl_root)
        minutes = _load_minutes(sdl_root)

        unmatched_t: List[Dict[str, Any]] = []
        unmatched_m: List[Dict[str, Any]] = []

        # 1. Anything with no meeting_date on either side is unmatched up-front.
        t_with_date: List[Dict[str, Any]] = []
        for t in transcripts:
            if t["meeting_date"] is None:
                unmatched_t.append(_unmatched_t(t, "no_meeting_date"))
            else:
                t_with_date.append(t)
        m_with_date: List[Dict[str, Any]] = []
        for m in minutes:
            if m["meeting_date"] is None:
                unmatched_m.append(_unmatched_m(m, "no_meeting_date"))
            else:
                m_with_date.append(m)

        # 2. Bucket by date for the exact-match pass.
        t_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        m_by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in t_with_date:
            t_by_date[t["meeting_date"]].append(t)
        for m in m_with_date:
            m_by_date[m["meeting_date"]].append(m)

        paired_t_ids: set = set()
        paired_m_ids: set = set()
        pairs_high: List[Dict[str, Any]] = []

        all_dates_with_either = set(t_by_date.keys()) | set(m_by_date.keys())
        leftover_t: List[Dict[str, Any]] = []
        leftover_m: List[Dict[str, Any]] = []
        for d in sorted(all_dates_with_either):
            ts = t_by_date.get(d, [])
            ms = m_by_date.get(d, [])
            # Collision on either side: refuse to pair, route ALL involved
            # to unmatched with the explicit reason. Never silently choose.
            if len(ts) > 1 or len(ms) > 1:
                for t in ts:
                    unmatched_t.append(
                        _unmatched_t(t, "duplicate_date_collision")
                    )
                    paired_t_ids.add(t["source_artifact_id"])
                for m in ms:
                    unmatched_m.append(
                        _unmatched_m(m, "duplicate_date_collision")
                    )
                    paired_m_ids.add(m["minutes_artifact_id"])
                continue
            if len(ts) == 1 and len(ms) == 1:
                pair = _build_pair(ts[0], ms[0], confidence="high")
                pairs_high.append(pair)
                paired_t_ids.add(ts[0]["source_artifact_id"])
                paired_m_ids.add(ms[0]["minutes_artifact_id"])
            else:
                if ts:
                    leftover_t.extend(ts)
                if ms:
                    leftover_m.extend(ms)

        # 3. Fuzzy ±1-day pass on the leftovers. Each record may match
        #    multiple peers within ±1 day; if so, route to unmatched
        #    (``ambiguous_fuzzy_match``) — never pair arbitrarily.
        pairs_medium: List[Dict[str, Any]] = []
        # Sort leftovers deterministically for stable test outcomes.
        leftover_t_sorted = sorted(
            leftover_t, key=lambda x: (x["meeting_date"], x["source_artifact_id"])
        )
        leftover_m_sorted = sorted(
            leftover_m, key=lambda x: (x["meeting_date"], x["minutes_artifact_id"])
        )

        # Build candidate sets in both directions.
        candidates_for_t: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        candidates_for_m: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in leftover_t_sorted:
            td = datetime.date.fromisoformat(t["meeting_date"])
            for m in leftover_m_sorted:
                md = datetime.date.fromisoformat(m["meeting_date"])
                if abs((td - md).days) <= 1 and td != md:
                    candidates_for_t[t["source_artifact_id"]].append(m)
                    candidates_for_m[m["minutes_artifact_id"]].append(t)

        ambiguous_t_ids: set = set()
        ambiguous_m_ids: set = set()
        for t in leftover_t_sorted:
            cands = candidates_for_t.get(t["source_artifact_id"], [])
            if len(cands) > 1:
                ambiguous_t_ids.add(t["source_artifact_id"])
        for m in leftover_m_sorted:
            cands = candidates_for_m.get(m["minutes_artifact_id"], [])
            if len(cands) > 1:
                ambiguous_m_ids.add(m["minutes_artifact_id"])

        for t in leftover_t_sorted:
            if t["source_artifact_id"] in paired_t_ids:
                continue
            cands = candidates_for_t.get(t["source_artifact_id"], [])
            if not cands:
                unmatched_t.append(_unmatched_t(t, "no_candidate"))
                paired_t_ids.add(t["source_artifact_id"])
                continue
            if t["source_artifact_id"] in ambiguous_t_ids:
                unmatched_t.append(_unmatched_t(t, "ambiguous_fuzzy_match"))
                paired_t_ids.add(t["source_artifact_id"])
                # Propagate ambiguity to every candidate minutes record that
                # lists ONLY this transcript. If a candidate M has its own
                # ambiguity (multiple t-candidates), the m-loop's
                # ambiguous_m_ids branch handles it. Otherwise M's only
                # candidate just got blocked — the cause is fuzzy ambiguity,
                # not absence of candidates, so record the reason that
                # matches the cause.
                for cand_m in cands:
                    m_aid = cand_m["minutes_artifact_id"]
                    if m_aid in paired_m_ids:
                        continue
                    if m_aid in ambiguous_m_ids:
                        continue
                    unmatched_m.append(
                        _unmatched_m(cand_m, "ambiguous_fuzzy_match")
                    )
                    paired_m_ids.add(m_aid)
                continue
            # Exactly one candidate from t's side; verify the candidate
            # also has exactly one candidate (this t) before pairing.
            m = cands[0]
            if m["minutes_artifact_id"] in paired_m_ids:
                # The minutes record was already routed elsewhere
                # (collision/ambiguity).
                unmatched_t.append(_unmatched_t(t, "no_candidate"))
                paired_t_ids.add(t["source_artifact_id"])
                continue
            if m["minutes_artifact_id"] in ambiguous_m_ids:
                unmatched_t.append(_unmatched_t(t, "ambiguous_fuzzy_match"))
                paired_t_ids.add(t["source_artifact_id"])
                # The minutes side is handled in the minutes loop below.
                continue
            pair = _build_pair(t, m, confidence="medium")
            pairs_medium.append(pair)
            paired_t_ids.add(t["source_artifact_id"])
            paired_m_ids.add(m["minutes_artifact_id"])

        for m in leftover_m_sorted:
            if m["minutes_artifact_id"] in paired_m_ids:
                continue
            cands = candidates_for_m.get(m["minutes_artifact_id"], [])
            if not cands:
                unmatched_m.append(_unmatched_m(m, "no_candidate"))
                paired_m_ids.add(m["minutes_artifact_id"])
                continue
            if m["minutes_artifact_id"] in ambiguous_m_ids:
                unmatched_m.append(_unmatched_m(m, "ambiguous_fuzzy_match"))
                paired_m_ids.add(m["minutes_artifact_id"])
                continue
            # Counterpart was paired or routed; nothing left for this m.
            unmatched_m.append(_unmatched_m(m, "no_candidate"))
            paired_m_ids.add(m["minutes_artifact_id"])

        # 4. Validate every pair, then write everything atomically-ish.
        try:
            pair_schema = _load_schema("ground_truth_pair")
            report_schema = _load_schema("linking_report")
        except (FileNotFoundError, OSError) as exc:
            return _failure_result(f"schema_unreadable:{exc}")

        pair_validator = jsonschema.Draft202012Validator(pair_schema)
        for pair in (*pairs_high, *pairs_medium):
            try:
                pair_validator.validate(pair)
            except jsonschema.ValidationError as exc:
                return _failure_result(
                    f"pair_schema_violation:{exc.message}"
                )

        # 5. Build the linking_report.
        run_id = str(uuid.uuid4())
        report = {
            "run_id": run_id,
            "created_at": _now_iso(),
            "data_lake_path": str(data_lake_path) if data_lake_path else "",
            "total_transcripts": len(transcripts),
            "total_minutes": len(minutes),
            "high_confidence_pairs": len(pairs_high),
            "medium_confidence_pairs": len(pairs_medium),
            "unmatched_transcripts": _stable_sorted_unmatched_t(unmatched_t),
            "unmatched_minutes": _stable_sorted_unmatched_m(unmatched_m),
            "schema_version": REPORT_SCHEMA_VERSION,
        }
        try:
            jsonschema.Draft202012Validator(report_schema).validate(report)
        except jsonschema.ValidationError as exc:
            return _failure_result(f"report_schema_violation:{exc.message}")

        # 6. Write pairs and report. All-or-nothing: any write error
        #    short-circuits to a failure result without leaving partial
        #    pair writes silently un-reported.
        try:
            pairs_dir = sdl_root / "ground_truth"
            reports_dir = pairs_dir / "reports"
            pairs_dir.mkdir(parents=True, exist_ok=True)
            reports_dir.mkdir(parents=True, exist_ok=True)
            for pair in (*pairs_high, *pairs_medium):
                target = pairs_dir / f"{pair['pair_id']}.json"
                target.write_text(
                    json.dumps(pair, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            report_path = reports_dir / f"{run_id}_linking_report.json"
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return _failure_result(f"write_error:{exc}")

        partial = bool(unmatched_t or unmatched_m)
        status = "partial" if partial else "success"
        if not pairs_high and not pairs_medium and partial:
            status = "partial"

        return {
            "status": status,
            "pairs_produced": len(pairs_high) + len(pairs_medium),
            "pairs_pending_review": len(pairs_medium),
            "unmatched_transcripts": report["unmatched_transcripts"],
            "unmatched_minutes": report["unmatched_minutes"],
            "linking_report_path": str(report_path),
            "reason": "",
        }


# -- record loaders ----------------------------------------------------------


def _load_transcripts(
    data_lake_path: str, sdl_root: Path
) -> List[Dict[str, Any]]:
    """Collect transcripts (source_family == 'meetings') from disk.

    Reads BOTH ``store/processed/meetings/<sid>/source_record.json`` and
    flat ``$SDL_ROOT/<artifact_id>.json``. De-duplicates by
    ``payload.source_id``; the ``processed/`` location wins (it is the
    canonical written form by SourceLoader and is more likely to carry
    the up-to-date metadata).
    """
    by_source_id: Dict[str, Dict[str, Any]] = {}

    # processed/ dir.
    if data_lake_path:
        processed_root = Path(data_lake_path) / "store" / "processed" / "meetings"
        if processed_root.is_dir():
            for sid_dir in sorted(processed_root.iterdir()):
                if not sid_dir.is_dir():
                    continue
                rec_path = sid_dir / "source_record.json"
                rec = _read_source_record(rec_path)
                if rec is None:
                    continue
                source_id = _payload_get(rec, "source_id")
                if not source_id:
                    continue
                by_source_id[source_id] = rec

    # SDL_ROOT flat files (meeting source_records only). Skip subdirs
    # (where minutes/ground_truth live).
    if sdl_root.is_dir():
        for path in sorted(sdl_root.glob("*.json")):
            if not path.is_file():
                continue
            rec = _read_source_record(path)
            if rec is None:
                continue
            if _payload_get(rec, "source_family") != "meetings":
                continue
            source_id = _payload_get(rec, "source_id")
            if not source_id:
                continue
            by_source_id.setdefault(source_id, rec)

    out: List[Dict[str, Any]] = []
    for source_id, rec in sorted(by_source_id.items()):
        out.append(_normalize_transcript(rec, source_id))
    return out


def _read_source_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("artifact_kind") != "source_record":
        return None
    return rec


def _payload_get(rec: Dict[str, Any], key: str) -> Optional[str]:
    payload = rec.get("payload") if isinstance(rec, dict) else None
    if not isinstance(payload, dict):
        return None
    val = payload.get(key)
    if isinstance(val, str) and val:
        return val
    return None


def _normalize_transcript(rec: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    payload = rec.get("payload", {}) or {}
    metadata = payload.get("metadata", {}) or {}
    raw_date = metadata.get("date") if isinstance(metadata, dict) else None
    meeting_date = _normalize_date(raw_date if isinstance(raw_date, str) else None)
    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    artifact_id = rec.get("artifact_id", "")
    return {
        "source_id": source_id,
        "source_artifact_id": artifact_id if isinstance(artifact_id, str) else "",
        "meeting_date": meeting_date,
        "meeting_name": title,
    }


def _load_minutes(sdl_root: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    minutes_dir = sdl_root / "minutes"
    if not minutes_dir.is_dir():
        return out
    seen: set = set()
    for path in sorted(minutes_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("provenance", {}).get("produced_by") != "MinutesProcessor":
            continue
        minutes_id = rec.get("minutes_id", "")
        if not isinstance(minutes_id, str) or not minutes_id:
            continue
        if minutes_id in seen:
            continue
        seen.add(minutes_id)
        meeting_date = _normalize_date(rec.get("meeting_date"))
        meeting_name = rec.get("meeting_name") or None
        out.append(
            {
                "minutes_id": minutes_id,
                "minutes_artifact_id": minutes_id,
                "meeting_date": meeting_date,
                "meeting_name": meeting_name if isinstance(meeting_name, str) else None,
            }
        )
    return out


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Accept YYYY-MM-DD directly.
    try:
        return datetime.date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    # Accept a few common fallback formats.
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# -- helpers -----------------------------------------------------------------


def _build_pair(
    transcript: Dict[str, Any],
    minutes: Dict[str, Any],
    *,
    confidence: str,
) -> Dict[str, Any]:
    # The pair takes its meeting_date from the transcript side (it's the
    # canonical eval anchor). meeting_name prefers the transcript's title;
    # falls back to the minutes-derived name.
    meeting_date = transcript["meeting_date"]
    meeting_name = (
        transcript.get("meeting_name") or minutes.get("meeting_name") or "untitled"
    )
    status = "confirmed" if confidence == "high" else "pending_review"
    return {
        "pair_id": str(uuid.uuid4()),
        "source_artifact_id": transcript["source_artifact_id"],
        "minutes_artifact_id": minutes["minutes_artifact_id"],
        "meeting_date": meeting_date,
        "meeting_name": meeting_name,
        "match_confidence": confidence,
        "status": status,
        "created_at": _now_iso(),
        "confirmed_at": None,
        "confirmed_by": None,
        "schema_version": PAIR_SCHEMA_VERSION,
        "provenance": {"produced_by": PRODUCED_BY},
    }


def _unmatched_t(t: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "source_id": t.get("source_id", "") or "",
        "source_artifact_id": t.get("source_artifact_id", "") or "",
        "meeting_date": t.get("meeting_date"),
        "meeting_name": t.get("meeting_name"),
        "reason": reason,
    }


def _unmatched_m(m: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "minutes_id": m.get("minutes_id", "") or "",
        "minutes_artifact_id": m.get("minutes_artifact_id", "") or "",
        "meeting_date": m.get("meeting_date"),
        "meeting_name": m.get("meeting_name"),
        "reason": reason,
    }


def _stable_sorted_unmatched_t(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (
            x.get("meeting_date") or "",
            x.get("source_id") or "",
            x.get("source_artifact_id") or "",
        ),
    )


def _stable_sorted_unmatched_m(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (
            x.get("meeting_date") or "",
            x.get("minutes_id") or "",
            x.get("minutes_artifact_id") or "",
        ),
    )


def _load_schema(name: str) -> Dict[str, Any]:
    schema_file = (
        contracts_root() / "schemas" / "ingestion" / f"{name}.schema.json"
    )
    return json.loads(schema_file.read_text(encoding="utf-8"))


def _failure_result(reason: str) -> Dict[str, Any]:
    return {
        "status": "failure",
        "pairs_produced": 0,
        "pairs_pending_review": 0,
        "unmatched_transcripts": [],
        "unmatched_minutes": [],
        "linking_report_path": "",
        "reason": reason,
    }
