#!/usr/bin/env python3
"""Create NON-CIRCULAR ground-truth pairs from the human-authored minutes.

The single-transcript validation baseline is self-referential today:
``scripts/generate_gt_pairs.py`` synthesizes ``ground_truth_pair``
artifacts from the pipeline's own ``meeting_extraction`` output, so a
coverage of 1.000 / precision of 1.000 only proves the pipeline agrees
with itself. This script breaks that circularity.

It reads the human-authored meeting-minutes ``.docx`` from the
data-lake and asks Claude Sonnet 4 (``claude-sonnet-4-6`` — see the
``EXTRACTION_MODEL`` note below) to extract the
decisions, action items, and claims that a human recorded as having
actually happened in the meeting. The minutes document is the
authoritative ground truth. The pipeline's extraction output is NEVER
read as input here — that is the entire point.

Each emitted pair is a full, schema-valid ``ground_truth_pair``
envelope (one JSON object per JSONL line) carrying the trust markers
that distinguish it from a self-referential pair::

    human_authored: true
    verified:       true
    verified_by:    "human_minutes_20251218"
    provenance.produced_by: "HumanMinutesGTPairs"

Non-circularity invariant (self-review checkpoint): the ONLY files this
script reads are (1) the human minutes ``.docx`` and (2)
``source_record.json`` for the transcript's stable artifact id.
``source_record.json`` is the ingestion-time identity record (it holds
only ``artifact_id`` / ``source_id`` / ``created_at`` — no extracted
content), so reading it introduces no pipeline output into the ground
truth. This script does NOT read any ``meeting_extraction``,
``orchestration_result``, or other pipeline artifact, and does NOT call
the pipeline runner.

Usage::

    python scripts/create_human_gt_pairs.py \\
        --data-lake data-lake/ \\
        --source-id 7-ghz-downlink-tig-meeting-kickoff---transcript-20251218 \\
        --minutes-file "store/raw/minutes/7 GHz Downlink TIG Kickoff Meeting Minutes 20251218 FINAL.docx" \\
        --dry-run

Offline / test seam: when ``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE`` is
set in the environment, its value is used verbatim as the model
response instead of calling Anthropic. This lets the integration
contract test exercise the full write path with no API key, mirroring
the env-var seams used elsewhere in the codebase
(``RAW_RESPONSE_LOG_ENABLED``, ``EXTRACTION_MODE``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)

# The task mandates Sonnet for this one-shot ground-truth seeding step
# (explicitly NOT the Haiku extraction default) so the ground truth is
# as strong as possible — it is the yardstick the pipeline is measured
# against, so it must not share the pipeline's weaker extraction model.
# The task text named the dated 2025-05-14 Sonnet 4 alias; that exact
# string is forbidden by the binding CI gate
# ``tests/ci/test_no_deprecated_model_strings.py`` (deprecation deadline
# 2026-06-15) which maps it to ``claude-sonnet-4-6``. We use the
# non-deprecated id: it is the same Sonnet 4 generation, fully
# preserving the task's intent while keeping the governed gate intact
# (grandfathering a new deprecated string would defeat that gate's
# stated purpose of blocking NEW occurrences).
EXTRACTION_MODEL = "claude-sonnet-4-6"

# Stable namespace for uuid5 so a re-run that extracts the same text
# produces the SAME pair_id and rewrites the same JSONL line instead of
# duplicating it. Arbitrary but frozen once shipped.
_GT_PAIR_NAMESPACE = uuid.UUID("0d1f6e2a-7b4c-4d1a-9e3f-2c5a8b7d6e10")

# Deterministic timestamp so re-runs over identical extracted text yield
# byte-identical envelope timestamps (matches generate_gt_pairs.py and
# data_lake/pipeline.py's epoch sentinel). The LLM content itself is not
# guaranteed identical across runs, but the envelope plumbing is.
_DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00+00:00"

# The authoritative human source. Used for verified_by / confirmed_by /
# minutes_artifact_id (the schema requires a non-empty minutes_artifact_id
# but there is no minutes_record artifact in this flow — the .docx itself
# is the source of truth, identified by this stable label).
_HUMAN_SOURCE_LABEL = "human_minutes_20251218"

_VALID_EXTRACTION_TYPES = ("decision", "action_item", "claim")

# YYYYMMDD trailing-date pattern in the transcript slug.
_SLUG_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")

_STUB_ENV = "CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE"

_MAX_TOKENS = 8000


def _meeting_date_from_slug(source_id: str) -> str:
    """Best-effort YYYY-MM-DD from the slug; safe sentinel on failure."""
    import datetime

    for m in _SLUG_DATE_RE.finditer(source_id):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        try:
            return datetime.date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            continue
    return "1970-01-01"


def _meeting_name_from_slug(source_id: str) -> str:
    name = " ".join(source_id.replace("-", " ").split())
    return name or source_id


def _resolve_source_artifact_id(
    data_lake: Path, source_id: str
) -> Optional[str]:
    """Resolve the slug to the transcript's stable artifact id.

    Reads ONLY ``store/processed/meetings/<source_id>/source_record.json``
    — the ingestion-time identity record. This file contains no extracted
    content (just artifact_id / source_id / created_at), so reading it
    keeps the ground truth non-circular.
    """
    sr_path = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "source_record.json"
    )
    if not sr_path.is_file():
        return None
    try:
        data = json.loads(sr_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    aid = data.get("artifact_id") if isinstance(data, dict) else None
    return aid if isinstance(aid, str) and aid else None


def _read_minutes_text(minutes_path: Path) -> str:
    """Extract plain text from the minutes .docx.

    Reuses the battle-tested ``DocxExtractor`` (paragraphs + tables in
    document order) but writes to a throwaway tempfile rather than next
    to the source, so nothing is ever written under the data-lake's
    ``raw/`` tree (the data-lake contract forbids core writing to raw/).
    """
    from spectrum_systems_core.ingestion.docx_extractor import DocxExtractor

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "minutes.txt"
        result = DocxExtractor().extract(str(minutes_path), output_path=str(out))
        if result.get("status") != "success":
            raise RuntimeError(
                f"docx extraction failed: {result.get('reason')!r} "
                f"for {minutes_path}"
            )
        return out.read_text(encoding="utf-8")


def build_extraction_prompt(minutes_text: str) -> str:
    """Prompt instructing the model to extract ONLY from the minutes.

    The prompt is deliberately explicit that the minutes text is the
    sole source: the model must not infer, summarize loosely, or import
    outside knowledge. ``ground_truth_text`` must be verbatim or
    near-verbatim so the pair is a faithful record of what a human wrote
    actually happened.
    """
    return (
        "You are extracting GROUND TRUTH from an authoritative, "
        "human-authored meeting-minutes document.\n\n"
        "The text between the markers below is the COMPLETE and ONLY "
        "source. Extract every distinct item that the minutes record as "
        "having actually occurred in the meeting, in three categories:\n"
        "  - decision: a decision the body made (approved, rejected, "
        "deferred, adopted, etc.)\n"
        "  - action_item: a task/follow-up assigned to a person or "
        "group (owners, due dates).\n"
        "  - claim: a substantive technical, procedural, or regulatory "
        "assertion stated in the minutes.\n\n"
        "Rules (binding):\n"
        "  1. Use ONLY the text below. Do not infer, do not add outside "
        "knowledge, do not merge unrelated items.\n"
        "  2. ground_truth_text MUST be verbatim or near-verbatim from "
        "the minutes (you may trim leading bullets/numbering and join a "
        "wrapped sentence, nothing more).\n"
        "  3. If the minutes do not clearly record an item, do not "
        "invent one. Fewer faithful items beats more speculative ones.\n"
        "  4. Output STRICT JSON only, no prose, no markdown fences.\n\n"
        'Output schema: {"pairs": [{"ground_truth_text": "<verbatim '
        'text>", "extraction_type": "decision|action_item|claim"}]}\n\n'
        "----- BEGIN HUMAN MINUTES -----\n"
        f"{minutes_text}\n"
        "----- END HUMAN MINUTES -----\n"
    )


def _strip_markdown_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def parse_extraction_response(raw: str) -> List[Dict[str, str]]:
    """Parse the model response into a list of {text, type} dicts.

    Tolerant of a leading/trailing markdown fence and of the model
    wrapping the array in a ``pairs`` object or returning a bare array.
    Skips malformed items rather than raising so one bad row does not
    sink the whole extraction.
    """
    body = _strip_markdown_fence(raw)
    if not body:
        return []
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        # Last resort: grab the first {...} or [...] block.
        m = re.search(r"(\{.*\}|\[.*\])", body, re.DOTALL)
        if not m:
            return []
        try:
            doc = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

    if isinstance(doc, dict):
        items = doc.get("pairs")
    elif isinstance(doc, list):
        items = doc
    else:
        items = None
    if not isinstance(items, list):
        return []

    out: List[Dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = it.get("ground_truth_text")
        etype = it.get("extraction_type")
        if not isinstance(text, str) or not text.strip():
            continue
        if etype not in _VALID_EXTRACTION_TYPES:
            continue
        out.append(
            {"ground_truth_text": text.strip(), "extraction_type": etype}
        )
    return out


def build_pair(
    *,
    source_id: str,
    source_artifact_id: str,
    ground_truth_text: str,
    extraction_type: str,
    meeting_date: Optional[str] = None,
    meeting_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one full schema-valid ``ground_truth_pair`` envelope.

    This is the REAL writer the integration fixture factory calls so a
    field-name drift here breaks the contract test, not production.
    ``target_type`` mirrors ``extraction_type`` so existing rubric
    tooling that filters on ``target_type`` keeps working.
    """
    pair_id = str(
        uuid.uuid5(
            _GT_PAIR_NAMESPACE,
            f"{source_id}|{extraction_type}|{ground_truth_text}",
        )
    )
    return {
        "pair_id": pair_id,
        "source_artifact_id": source_artifact_id,
        "minutes_artifact_id": _HUMAN_SOURCE_LABEL,
        "meeting_date": meeting_date or _meeting_date_from_slug(source_id),
        "meeting_name": meeting_name or _meeting_name_from_slug(source_id),
        "match_confidence": "high",
        "status": "confirmed",
        "created_at": _DETERMINISTIC_CREATED_AT,
        "confirmed_at": _DETERMINISTIC_CREATED_AT,
        "confirmed_by": _HUMAN_SOURCE_LABEL,
        "schema_version": "1.0.0",
        "provenance": {"produced_by": "HumanMinutesGTPairs"},
        "source_id": source_id,
        "ground_truth_text": ground_truth_text,
        "target_type": extraction_type,
        "extraction_type": extraction_type,
        "human_authored": True,
        "verified": True,
        "verified_by": _HUMAN_SOURCE_LABEL,
    }


def _build_anthropic_extractor(model: str) -> Callable[[str], str]:
    """Return ``prompt -> raw response text``.

    Honors the ``CREATE_HUMAN_GT_PAIRS_STUB_RESPONSE`` offline/test
    seam first. Otherwise lazily constructs the Anthropic client (so
    offline runs/tests never need the SDK) and requires
    ``ANTHROPIC_API_KEY``.
    """
    stub = os.environ.get(_STUB_ENV)
    if stub is not None:
        def _stub_call(_prompt: str) -> str:
            return stub
        return _stub_call

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is unset and no "
            f"{_STUB_ENV} provided — cannot extract GT pairs from the "
            "minutes. Set the key (the create-human-gt-pairs workflow "
            "passes secrets.ANTHROPIC_API_KEY)."
        )

    import anthropic

    client = anthropic.Anthropic()

    def _call(prompt: str) -> str:
        message = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        parts: List[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    return _call


def extract_gt_pairs(
    *,
    minutes_text: str,
    source_id: str,
    source_artifact_id: str,
    extractor: Callable[[str], str],
) -> List[Dict[str, Any]]:
    """Extract, build, and schema-validate the pairs. Pure given inputs."""
    prompt = build_extraction_prompt(minutes_text)
    raw = extractor(prompt)
    items = parse_extraction_response(raw)

    pairs: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for it in items:
        pair = build_pair(
            source_id=source_id,
            source_artifact_id=source_artifact_id,
            ground_truth_text=it["ground_truth_text"],
            extraction_type=it["extraction_type"],
        )
        if pair["pair_id"] in seen_ids:
            continue  # exact-duplicate item from the model
        try:
            validate_artifact(
                pair,
                "ground_truth_pair",
                None,
                require_artifact_type_field=False,
            )
        except ArtifactValidationError as exc:
            print(
                f"skip: pair from {it['extraction_type']} "
                f"text={it['ground_truth_text'][:60]!r} failed schema: {exc}",
                file=sys.stderr,
            )
            continue
        seen_ids.add(pair["pair_id"])
        pairs.append(pair)

    # Deterministic on-disk order.
    pairs.sort(key=lambda p: p["pair_id"])
    return pairs


def _output_path(data_lake: Path, source_id: str) -> Path:
    return (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "ground_truth"
        / "human_minutes_gt_pairs.jsonl"
    )


def _write_jsonl(path: Path, pairs: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(p, sort_keys=True, separators=(",", ":")) for p in pairs
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_minutes_file(data_lake: Path, minutes_file: str) -> Path:
    p = Path(minutes_file)
    if p.is_absolute() and p.is_file():
        return p
    candidate = data_lake / minutes_file
    if candidate.is_file():
        return candidate
    if p.is_file():
        return p
    raise FileNotFoundError(
        f"minutes file not found. Tried absolute {p} and "
        f"data-lake-relative {candidate}"
    )


def create_pairs(
    *,
    data_lake: Path,
    source_id: str,
    minutes_file: str,
    dry_run: bool,
    extractor: Optional[Callable[[str], str]] = None,
    model: str = EXTRACTION_MODEL,
) -> Dict[str, Any]:
    """Orchestrate the full create flow. Returns a summary dict."""
    source_artifact_id = _resolve_source_artifact_id(data_lake, source_id)
    if not source_artifact_id:
        return {
            "status": "failure",
            "reason": "missing_source_record",
            "detail": (
                f"no source_record.json with artifact_id under "
                f"store/processed/meetings/{source_id}/"
            ),
            "pairs_written": 0,
            "by_type": {},
            "output_path": "",
            "dry_run": dry_run,
        }

    minutes_path = _resolve_minutes_file(data_lake, minutes_file)
    minutes_text = _read_minutes_text(minutes_path)
    if not minutes_text.strip():
        return {
            "status": "failure",
            "reason": "empty_minutes",
            "detail": str(minutes_path),
            "pairs_written": 0,
            "by_type": {},
            "output_path": "",
            "dry_run": dry_run,
        }

    if extractor is None:
        extractor = _build_anthropic_extractor(model)

    pairs = extract_gt_pairs(
        minutes_text=minutes_text,
        source_id=source_id,
        source_artifact_id=source_artifact_id,
        extractor=extractor,
    )

    by_type: Dict[str, int] = {}
    for p in pairs:
        by_type[p["extraction_type"]] = by_type.get(p["extraction_type"], 0) + 1

    out_path = _output_path(data_lake, source_id)

    if not pairs:
        return {
            "status": "failure",
            "reason": "no_pairs_extracted",
            "detail": (
                "the model returned no schema-valid pairs from the "
                "minutes text"
            ),
            "pairs_written": 0,
            "by_type": {},
            "output_path": "",
            "dry_run": dry_run,
        }

    if dry_run:
        return {
            "status": "success",
            "reason": "dry_run",
            "pairs_written": 0,
            "pairs_extracted": len(pairs),
            "by_type": by_type,
            "output_path": str(out_path),
            "dry_run": True,
            "pairs": pairs,
        }

    _write_jsonl(out_path, pairs)
    return {
        "status": "success",
        "reason": "",
        "pairs_written": len(pairs),
        "by_type": by_type,
        "output_path": str(out_path),
        "dry_run": False,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--minutes-file", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and print pairs but do NOT write the JSONL file.",
    )
    parser.add_argument("--model", default=EXTRACTION_MODEL)
    args = parser.parse_args(argv)

    # Mobile workflow_dispatch inputs frequently arrive with a trailing
    # space pasted from a phone keyboard; strip every string arg.
    for _attr in vars(args):
        _val = getattr(args, _attr)
        if isinstance(_val, str):
            setattr(args, _attr, _val.strip())

    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            f"error: --data-lake is not a directory: {data_lake}",
            file=sys.stderr,
        )
        return 1

    try:
        result = create_pairs(
            data_lake=data_lake,
            source_id=args.source_id,
            minutes_file=args.minutes_file,
            dry_run=args.dry_run,
            model=args.model,
        )
    except (RuntimeError, FileNotFoundError) as exc:
        print(json.dumps({
            "status": "failure",
            "reason": "exception",
            "detail": str(exc),
        }, indent=2, sort_keys=True))
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "success":
        print(
            f"FAIL: {result.get('reason')} — {result.get('detail', '')}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
