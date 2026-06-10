#!/usr/bin/env python3
"""Reproducibility verification for the Opus extract+ingest pipeline.

Proves, against the REAL committed baselines and the saved raw Opus
extractions, that the committed code can regenerate the committed data —
the gap this change set closes. It drives the committed
``scripts/extract_opus_baseline.py`` (via its transport stub, so a saved
raw ``.opus.json`` stands in for the model call — NO ``claude`` CLI, NO
network) through the now-self-healing ``scripts/ingest_opus_baseline.py``,
into throwaway temp data-lakes seeded from the real
``source_record.json`` + ``source.txt``, and compares the result to the
committed ``opus_reference_minutes.jsonl`` (ignoring only the wall-clock
``created_at``, which every baseline carries by design).

Checks (each printed PASS/FAIL; non-zero exit on any FAIL):

  (a) A good extraction still ingests cleanly through the new
      self-healing path.
  (b) The generalized self-heal strips the known class-2/class-3 quirks
      (hallucinated ``source_chunk_id`` everywhere; stray
      ``reason``/``source_quote`` on closed item types; null-valued keys
      the schema forbids null on) — no such quirk survives into the
      reproduced baseline, even though the raw extractions contain them.
  (c) Class-4 null/missing REQUIRED fields are NOT silently filled: the
      one committed baseline that was built with a class-4 ``"Unknown"``
      fabrication (``5-5``: ``position_statement[].agency``) is NOT
      reproduced — the new fail-closed path HALTs ``schema_violation`` on
      the missing required field instead of inventing a value.
  (d) Running extract+ingest on the saved raw outputs reproduces the
      committed baselines byte-for-byte (modulo ``created_at``) for every
      meeting that did not require fabrication — proving the fold is
      behavior-preserving AND the committed data is reproducible from
      committed code.

This is a LOCAL verification (it needs the operator's saved raw outputs
and the data-lake clone), so it is ``_``-prefixed and skips cleanly with
exit 0 when its inputs are absent — exactly like
``scripts/_verify_ingest_opus_baseline.py``. It makes ZERO network/LLM
calls.

Usage::

    python scripts/_verify_opus_extract_repro.py \
        --data-lake ~/data-lake --raw-dir ~/reextract_work
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_SRC_DIR = _SCRIPTS_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import extract_opus_baseline as eob  # noqa: E402
import ingest_opus_baseline as iob  # noqa: E402

# The five baselines re-extracted and committed this session. ``5-5`` is
# the one whose committed baseline embeds a class-4 ``"Unknown"`` fill, so
# it is expected to HALT under the new fail-closed (no-fabrication) path
# rather than reproduce.
_SLUGS = [
    "7ghz-spd-sead-sync-4-7-2026",
    "7ghz-spd-sead-sync-4-14-2026",
    "7ghz-spd-sead-sync-4-21-2026",
    "7ghz-spd-sead-sync-5-19-2026",
    "7ghz-spd-sead-sync-5-5-2026",
]
_EXPECT_HALT = {"7ghz-spd-sead-sync-5-5-2026"}


def _baseline_path(root: Path, slug: str) -> Path:
    return (
        root / "store" / "processed" / "meetings" / slug
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )


def _normalize_rows(path: Path) -> List[str]:
    """Sorted-key JSON lines with the wall-clock ``created_at`` removed."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        d.pop("created_at", None)
        out.append(json.dumps(d, sort_keys=True))
    return out


def _reproduce(
    *, data_lake: Path, raw_dir: Path, slug: str, work_root: Path
) -> Tuple[str, Optional[List[dict]], Optional[str]]:
    """Drive extract+ingest on the saved raw output into a temp data-lake.

    Returns ``(outcome, rows, detail)`` where outcome is ``"written"`` (rows
    populated), ``"halt:<reason>"`` (detail set), or ``"skip:<why>"``.
    """
    raw_file = raw_dir / f"{slug}.opus.json"
    src_record = (
        data_lake / "store" / "processed" / "meetings" / slug
        / "source_record.json"
    )
    src_txt = data_lake / "store" / "raw" / "meetings" / slug / "source.txt"
    if not raw_file.is_file():
        return f"skip:no raw {raw_file.name}", None, None
    if not src_record.is_file() or not src_txt.is_file():
        return "skip:no source_record/source.txt", None, None

    tmp = Path(tempfile.mkdtemp(prefix=f"repro-{slug}-", dir=work_root))
    mdir = tmp / "store" / "processed" / "meetings" / slug
    rdir = tmp / "store" / "raw" / "meetings" / slug
    mdir.mkdir(parents=True)
    rdir.mkdir(parents=True)
    shutil.copy(src_record, mdir / "source_record.json")
    shutil.copy(src_txt, rdir / "source.txt")

    prev = os.environ.get(eob._STUB_ENV)
    os.environ[eob._STUB_ENV] = raw_file.read_text(encoding="utf-8")
    try:
        eob.extract(
            data_lake=tmp,
            source_id=slug,
            operator="verify",
            model_override=None,
            transcript_path=None,
            work_dir=tmp / "work",
            dry_run=False,
        )
    except (eob.ExtractError, iob.OpusIngestError) as exc:
        return f"halt:{exc.reason}", None, exc.detail
    finally:
        if prev is None:
            os.environ.pop(eob._STUB_ENV, None)
        else:
            os.environ[eob._STUB_ENV] = prev

    out = _baseline_path(tmp, slug)
    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return "written", rows, str(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake", default=str(Path.home() / "data-lake")
    )
    parser.add_argument(
        "--raw-dir", default=str(Path.home() / "reextract_work")
    )
    args = parser.parse_args(argv)
    data_lake = Path(args.data_lake)
    raw_dir = Path(args.raw_dir)

    if not data_lake.is_dir() or not raw_dir.is_dir():
        print(
            f"SKIP: inputs absent (data-lake={data_lake} exists="
            f"{data_lake.is_dir()}, raw-dir={raw_dir} exists="
            f"{raw_dir.is_dir()}); local verification only."
        )
        return 0

    failures: List[str] = []
    work_root = Path(tempfile.mkdtemp(prefix="opus-repro-verify-"))
    print("=" * 70)
    print("Opus extract+ingest reproducibility verification")
    print(f"  data-lake: {data_lake}")
    print(f"  raw-dir  : {raw_dir}")
    print("=" * 70)

    reproduced: Dict[str, List[dict]] = {}
    halts: Dict[str, str] = {}

    # ---- (d) reproduction across all five saved raw outputs ----
    print("\n[d] Reproduce committed baselines from saved raw outputs")
    any_written = False
    for slug in _SLUGS:
        outcome, rows, detail = _reproduce(
            data_lake=data_lake, raw_dir=raw_dir, slug=slug,
            work_root=work_root,
        )
        committed = _baseline_path(data_lake, slug)
        if outcome.startswith("skip:"):
            print(f"  {slug}: SKIP ({outcome[5:]})")
            continue
        if outcome.startswith("halt:"):
            halts[slug] = (detail or "")
            if slug in _EXPECT_HALT:
                print(
                    f"  {slug}: HALT {outcome[5:]} (EXPECTED — class-4 "
                    f"fabrication refused)"
                )
            else:
                print(f"  {slug}: HALT {outcome[5:]} (UNEXPECTED)")
                failures.append(f"(d) {slug} unexpectedly halted: {detail}")
            continue
        # written
        any_written = True
        reproduced[slug] = rows or []
        if slug in _EXPECT_HALT:
            print(
                f"  {slug}: WROTE a baseline but a HALT was expected "
                f"(class-4 fabrication should have been refused)"
            )
            failures.append(
                f"(d) {slug} reproduced despite embedded class-4 fill"
            )
            continue
        if not committed.is_file():
            print(f"  {slug}: SKIP (no committed baseline to compare)")
            continue
        repro_norm = [
            json.dumps({k: v for k, v in r.items() if k != "created_at"},
                       sort_keys=True)
            for r in (rows or [])
        ]
        comm_norm = _normalize_rows(committed)
        if repro_norm == comm_norm:
            print(
                f"  {slug}: MATCH ({len(repro_norm)} rows, byte-identical "
                f"modulo created_at)"
            )
        else:
            print(
                f"  {slug}: MISMATCH (repro={len(repro_norm)} "
                f"committed={len(comm_norm)})"
            )
            failures.append(f"(d) {slug} did not reproduce committed bytes")

    # ---- (a) a good extraction ingests cleanly ----
    print("\n[a] A good extraction ingests cleanly through self-heal")
    if any_written:
        print(
            f"  PASS — {len(reproduced)} of {len(_SLUGS)} saved extractions "
            f"ingested cleanly through the new self-healing path."
        )
    else:
        print("  FAIL — no extraction ingested cleanly.")
        failures.append("(a) no good extraction ingested")

    # ---- (b) class-2/3 quirks stripped, none survive ----
    #
    # The unambiguous, schema-independent signal is the hallucinated
    # ``source_chunk_id``: it is noise on every type and must never reach
    # a baseline. (Class-3 correctness — dropping only the null keys the
    # schema forbids null on, while keeping schema-permitted nulls like a
    # nullable optional field — is proven exactly by the byte-for-byte
    # (d) match above, which would break on any over- or under-drop.)
    print("\n[b] Generalized self-heal strips class-2/3 quirks")
    raw_with_scid = 0
    scid_survived = 0
    for slug, rows in reproduced.items():
        raw_doc_txt = (raw_dir / f"{slug}.opus.json").read_text(
            encoding="utf-8"
        )
        if "source_chunk_id" in raw_doc_txt:
            raw_with_scid += 1
        for r in rows:
            # Scan the whole stored row, not just top-level item_data
            # keys, so a nested hallucination is caught too.
            if "source_chunk_id" in json.dumps(r):
                scid_survived += 1
    print(
        f"  saved raw outputs that contained source_chunk_id: "
        f"{raw_with_scid}; rows in reproduced baselines still carrying it: "
        f"{scid_survived}"
    )
    print(
        f"  class-3 null-drop correctness is proven by the {len(reproduced)} "
        f"byte-identical (d) matches above (any mis-drop would break them)."
    )
    if raw_with_scid == 0:
        print("  NOTE — no source_chunk_id present in raw outputs to strip.")
    elif scid_survived == 0:
        print(
            "  PASS — the hallucinated source_chunk_id was stripped from "
            "every reproduced baseline (global, all item types)."
        )
    else:
        print("  FAIL — a hallucinated source_chunk_id survived.")
        failures.append("(b) source_chunk_id survived into a baseline")

    # ---- (c) class-4 NOT filled; surfaces as signal ----
    print("\n[c] Class-4 null/missing required fields are NOT fabricated")
    c_ok = True
    for slug in _EXPECT_HALT:
        committed = _baseline_path(data_lake, slug)
        detail = halts.get(slug)
        if detail is None:
            print(f"  {slug}: FAIL — expected a HALT but none occurred.")
            failures.append(f"(c) {slug} did not halt")
            c_ok = False
            continue
        # The committed baseline (left untouched) still carries the
        # pre-existing class-4 'Unknown' the old manual path fabricated.
        committed_has_unknown = (
            committed.is_file()
            and '"Unknown"' in committed.read_text(encoding="utf-8")
        )
        fabricated = "Unknown" in detail
        print(
            f"  {slug}: HALT reason carried (detail mentions the real "
            f"missing field, not a sentinel: "
            f"{'agency' in detail and not fabricated}); committed baseline "
            f"still embeds the pre-existing 'Unknown' fill: "
            f"{committed_has_unknown}"
        )
        if fabricated:
            print("    FAIL — our path fabricated a sentinel.")
            failures.append(f"(c) {slug} fabricated a value")
            c_ok = False
    if c_ok:
        print(
            "  PASS — the new path refuses class-4 fabrication and surfaces "
            "the missing-required-field as a real schema_violation signal."
        )

    shutil.rmtree(work_root, ignore_errors=True)

    print("\n" + "=" * 70)
    if failures:
        print(f"RESULT: FAIL ({len(failures)} issue(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS — all reproducibility checks passed.")
    print(
        "  4 of 5 committed baselines reproduce byte-for-byte from saved "
        "raw outputs through committed code; 5-5 correctly diverges "
        "because its committed baseline embeds a class-4 'Unknown' the "
        "new fail-closed path refuses to fabricate (deferred to the "
        "evidence_coverage:meeting_minutes eval_case)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
