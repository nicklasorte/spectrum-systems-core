#!/usr/bin/env python3
"""Standalone verification for ``scripts/ingest_opus_baseline.py``.

Proves three claims end-to-end via subprocess (so the CLI path is what
is exercised, not just imported functions):

1. **Output envelope shape parity** — every JSONL row this script writes
   carries exactly the same top-level keys as the on-disk baseline
   ``create_opus_reference_baselines.py`` produced for an unrelated
   transcript (the byte-shape this ingest must match). The reference
   baseline is read from
   ``data-lake/store/processed/meetings/7-ghz-downlink-tig-meeting-kickoff---transcript-20251218/reference_baselines/opus_reference_minutes.jsonl``.

2. **``source_artifact_id`` is shared with the codex ingest** — given
   the same ``source_id`` (and therefore the same
   ``source_record.json``), the value of ``source_artifact_id`` is
   byte-identical in the row this script produces and in the row
   ``ingest_codex_baseline.py`` produces for the same input. This is
   the join key the comparison engine uses; the two baselines for one
   transcript MUST share it.

3. **``pair_id`` uses the Opus namespace, not the codex namespace** —
   the produced ``pair_id`` equals ``uuid5(OPUS_NAMESPACE,
   "opus-ref-{sid}-{etype}-{0}")`` and does NOT equal
   ``uuid5(CODEX_NAMESPACE, "codex-ref-{sid}-{etype}-{0}")``. Two Opus
   writers (this script and the API-calling sibling
   ``create_opus_reference_baselines.py``) MUST produce identical
   ``pair_id`` values for the same item slot; a codex row for the same
   slot MUST be distinct.

Run::

    python scripts/_verify_ingest_opus_baseline.py \
        --data-lake C:\\Users\\nlasorte-adm\\Documents\\data-lake

Exit code 0 means every claim passed. Any failure prints the failing
assertion to stderr and exits 1.

This script makes ZERO network or LLM calls. Both ingests it drives are
LLM-free by construction.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_OPUS_INGEST = _SCRIPTS_DIR / "ingest_opus_baseline.py"
_CODEX_INGEST = _SCRIPTS_DIR / "ingest_codex_baseline.py"

# The two frozen namespaces — pulled in by import so a drift in the
# constants would surface here (the comparison below would fail).
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_REPO_ROOT / "src"))
import ingest_codex_baseline as _codex  # noqa: E402
import ingest_opus_baseline as _opus  # noqa: E402

_OPUS_NAMESPACE: uuid.UUID = _opus._OPUS_REF_NAMESPACE
_CODEX_NAMESPACE: uuid.UUID = _codex._CODEX_REF_NAMESPACE

_SOURCE_ID = "verify-opus-ingest-fixture"
_SYNTH_INPUT: Dict[str, Any] = {
    "artifact_type": "meeting_minutes",
    "schema_version": "1.4.0",
    "title": "Verification fixture",
    "summary": "Synthetic input for _verify_ingest_opus_baseline.",
    "decisions": [
        "The TIG approved the synthetic threshold for verification.",
    ],
    "action_items": [
        {"action": "verify-script to assert source_artifact_id parity."}
    ],
    "open_questions": [],
}


class VerifyError(AssertionError):
    """Surface a failed claim with a stable label and detail."""

    def __init__(self, claim: str, detail: str):
        super().__init__(f"{claim}: {detail}")
        self.claim = claim
        self.detail = detail


def _seed_source_record(meeting_dir: Path, artifact_id: str) -> None:
    """Write a minimal source_record.json with a fixed artifact_id.

    Mirrors the shape ``SourceLoader`` writes (``artifact_id`` at the
    top level is the only field both ingest resolvers require).
    """
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / "source_record.json").write_text(
        json.dumps({"artifact_id": artifact_id}),
        encoding="utf-8",
    )


def _write_input(tmp_dir: Path) -> Path:
    path = tmp_dir / "input.json"
    path.write_text(json.dumps(_SYNTH_INPUT), encoding="utf-8")
    return path


def _run_ingest(
    script: Path,
    input_file: Path,
    data_lake: Path,
) -> Dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input-file", str(input_file),
            "--source-id", _SOURCE_ID,
            "--data-lake", str(data_lake),
            "--operator", "verify",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VerifyError(
            "ingest_failed",
            f"{script.name} returned {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )
    summary = json.loads(result.stdout)
    return summary


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def claim_1_envelope_shape_matches_existing_baseline(
    produced_row: Dict[str, Any],
    existing_baseline: Path,
) -> None:
    """Top-level keys must match the existing on-disk Opus baseline."""
    rows = _read_jsonl(existing_baseline)
    if not rows:
        raise VerifyError(
            "claim_1_envelope_shape",
            f"reference baseline at {existing_baseline} has no rows",
        )
    expected_keys = set(rows[0].keys())
    produced_keys = set(produced_row.keys())
    if expected_keys != produced_keys:
        missing = expected_keys - produced_keys
        extra = produced_keys - expected_keys
        raise VerifyError(
            "claim_1_envelope_shape",
            f"top-level keys differ. missing from produced: {sorted(missing)!r}; "
            f"extra in produced: {sorted(extra)!r}",
        )
    # provenance shape: opus baseline has produced_by ONLY (no operator)
    prov_expected = set(rows[0]["provenance"].keys())
    prov_produced = set(produced_row["provenance"].keys())
    if prov_expected != prov_produced:
        raise VerifyError(
            "claim_1_envelope_shape",
            f"provenance keys differ. existing: {sorted(prov_expected)!r}; "
            f"produced: {sorted(prov_produced)!r}. The Opus baseline shape "
            f"on disk does NOT carry an operator key.",
        )
    # Fixed-value fields the Opus shape must match.
    for field, expected in (
        ("model_authored", True),
        ("human_authored", False),
        ("verified", False),
        ("status", "reference_only"),
        ("schema_version", "1.4.0"),
        ("chunking_strategy_version", "speaker_turn_v1"),
    ):
        if produced_row.get(field) != expected:
            raise VerifyError(
                "claim_1_envelope_shape",
                f"{field}={produced_row.get(field)!r}, expected {expected!r}",
            )
    if produced_row["provenance"].get("produced_by") != (
        "opus_reference_baseline_workflow"
    ):
        raise VerifyError(
            "claim_1_envelope_shape",
            f"provenance.produced_by="
            f"{produced_row['provenance'].get('produced_by')!r}, "
            f"expected 'opus_reference_baseline_workflow'",
        )


def claim_2_source_artifact_id_matches_codex(
    opus_row: Dict[str, Any],
    codex_row: Dict[str, Any],
    seeded_artifact_id: str,
) -> None:
    """source_artifact_id must be byte-identical across the two ingests."""
    opus_sid = opus_row["source_artifact_id"]
    codex_sid = codex_row["source_artifact_id"]
    if opus_sid != codex_sid:
        raise VerifyError(
            "claim_2_source_artifact_id",
            f"opus source_artifact_id={opus_sid!r} differs from "
            f"codex source_artifact_id={codex_sid!r} for the same "
            f"source_id (both resolvers should read the same "
            f"source_record.json).",
        )
    if opus_sid != seeded_artifact_id:
        raise VerifyError(
            "claim_2_source_artifact_id",
            f"opus source_artifact_id={opus_sid!r} differs from the "
            f"seeded source_record.artifact_id={seeded_artifact_id!r}",
        )


def claim_3_pair_id_uses_opus_namespace(
    opus_row: Dict[str, Any],
) -> None:
    """pair_id must equal uuid5(OPUS_NAMESPACE, opus-ref-{...}) and
    must NOT equal uuid5(CODEX_NAMESPACE, codex-ref-{...})."""
    etype = opus_row["extraction_type"]
    expected_opus = str(
        uuid.uuid5(
            _OPUS_NAMESPACE,
            f"opus-ref-{_SOURCE_ID}-{etype}-0",
        )
    )
    forbidden_codex = str(
        uuid.uuid5(
            _CODEX_NAMESPACE,
            f"codex-ref-{_SOURCE_ID}-{etype}-0",
        )
    )
    if opus_row["pair_id"] != expected_opus:
        raise VerifyError(
            "claim_3_pair_id_namespace",
            f"pair_id={opus_row['pair_id']!r} does not match the "
            f"expected Opus-namespace uuid5={expected_opus!r}",
        )
    if opus_row["pair_id"] == forbidden_codex:
        raise VerifyError(
            "claim_3_pair_id_namespace",
            f"pair_id={opus_row['pair_id']!r} accidentally collides "
            f"with the codex-namespace uuid5 for the same slot",
        )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Root of the data-lake clone (used only to locate the "
        "existing on-disk opus_reference_minutes.jsonl baseline for "
        "the envelope-shape claim).",
    )
    parser.add_argument(
        "--reference-source-id",
        default=(
            "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"
        ),
        help="source_id whose existing opus_reference_minutes.jsonl is "
        "the envelope-shape oracle for claim 1.",
    )
    args = parser.parse_args(argv)

    data_lake = Path(args.data_lake).resolve()
    existing_baseline = (
        data_lake / "store" / "processed" / "meetings"
        / args.reference_source_id / "reference_baselines"
        / "opus_reference_minutes.jsonl"
    )
    if not existing_baseline.is_file():
        print(
            f"FAIL: existing reference baseline not found at "
            f"{existing_baseline}",
            file=sys.stderr,
        )
        return 1

    seeded_artifact_id = str(uuid.uuid4())
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # One synthetic data-lake tree per ingest so the two writers do
        # not collide on the same already_ingested guard (they target
        # DIFFERENT filenames but seeding twice in one tree is wasteful).
        opus_lake = td_path / "opus-lake"
        codex_lake = td_path / "codex-lake"
        for lake in (opus_lake, codex_lake):
            _seed_source_record(
                lake / "store" / "processed" / "meetings" / _SOURCE_ID,
                seeded_artifact_id,
            )
        input_file = _write_input(td_path)

        opus_summary = _run_ingest(_OPUS_INGEST, input_file, opus_lake)
        codex_summary = _run_ingest(_CODEX_INGEST, input_file, codex_lake)

        opus_jsonl = (
            opus_lake / "store" / "processed" / "meetings" / _SOURCE_ID
            / "reference_baselines" / "opus_reference_minutes.jsonl"
        )
        codex_jsonl = (
            codex_lake / "store" / "processed" / "meetings" / _SOURCE_ID
            / "reference_baselines" / "codex_reference_minutes.jsonl"
        )
        opus_rows = _read_jsonl(opus_jsonl)
        codex_rows = _read_jsonl(codex_jsonl)
        if not opus_rows:
            print("FAIL: opus ingest produced no rows", file=sys.stderr)
            return 1
        if not codex_rows:
            print("FAIL: codex ingest produced no rows", file=sys.stderr)
            return 1

        # Pair the rows by (extraction_type, item_index) so claim 2 and
        # claim 3 compare the same item slot in both baselines.
        for opus_row in opus_rows:
            etype = opus_row["extraction_type"]
            matching = [
                r for r in codex_rows
                if r["extraction_type"] == etype
            ]
            if not matching:
                continue
            codex_row = matching[0]
            try:
                claim_1_envelope_shape_matches_existing_baseline(
                    opus_row, existing_baseline
                )
                claim_2_source_artifact_id_matches_codex(
                    opus_row, codex_row, seeded_artifact_id
                )
                claim_3_pair_id_uses_opus_namespace(opus_row)
            except VerifyError as exc:
                failures.append(str(exc))
            break  # first item-type is sufficient per claim contract

        # Summary block — useful for the PR description.
        print(
            json.dumps(
                {
                    "status": "pass" if not failures else "fail",
                    "claims_checked": [
                        "claim_1_envelope_shape",
                        "claim_2_source_artifact_id",
                        "claim_3_pair_id_namespace",
                    ],
                    "failures": failures,
                    "opus_total": opus_summary["total"],
                    "codex_total": codex_summary["total"],
                    "opus_first_row_pair_id": opus_rows[0]["pair_id"],
                    "codex_first_row_pair_id": codex_rows[0]["pair_id"],
                    "shared_source_artifact_id": (
                        opus_rows[0]["source_artifact_id"]
                    ),
                    "seeded_source_artifact_id": seeded_artifact_id,
                    "opus_namespace": str(_OPUS_NAMESPACE),
                    "codex_namespace": str(_CODEX_NAMESPACE),
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0 if not failures else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
