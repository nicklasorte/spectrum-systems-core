"""Phase 4a — tests for scripts/verify_opus_baseline_consistency.py.

The verifier is a CLI script under ``scripts/`` so it is invoked via
``subprocess.run`` against a real on-disk fixture.

Coverage:

* P1 #6 tolerance: in-range item count exits 0; out-of-range exits 1.
* P1 #7 legacy artifact: a legacy JSONL without prompt_content_hash
  is reported as a WARNING but the verifier still exits 0.
* P2 #1 paired rejection: a count below the hard range exits 1; a
  count above the hard range exits 1.
* P3 hash-comparison output includes both the artifact and canonical
  hashes (truncated) when both are available.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

SCRIPT_PATH: Path = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "verify_opus_baseline_consistency.py"
)


def _content_arrays() -> List[str]:
    from spectrum_systems_core.corpus.baseline_opus import _CONTENT_ARRAYS

    return list(_CONTENT_ARRAYS)


def _write_phase_4a_artifact(
    lake: Path,
    *,
    source_id: str,
    per_type_count: int = 5,
    target_total: int | None = None,
    include_prompt_hash: bool = True,
) -> Path:
    """Write a fixture artifact.

    Pass ``target_total`` to land at an exact item count regardless of
    array shape; the helper distributes items across the 23 arrays
    round-robin. Default ``per_type_count=5`` (23 × 5 = 115 items) sits
    inside the hard range but just above the reference range.
    """
    meeting_dir = lake / "processed" / "meetings" / source_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    arrays = _content_arrays()
    if target_total is not None:
        payload: Dict = {k: [] for k in arrays}
        for i in range(target_total):
            payload[arrays[i % len(arrays)]].append({"text": f"item-{i}"})
    else:
        payload = {
            k: [{"text": f"{k}-{i}"} for i in range(per_type_count)]
            for k in arrays
        }
    payload["grounding"] = []
    payload["provenance"] = {
        "produced_by": "opus_baseline_cli",
        "model_id": "claude-opus-4-7",
    }
    if include_prompt_hash:
        payload["provenance"]["prompt_content_hash"] = "a" * 64
    art = {
        "artifact_id": "abc",
        "artifact_type": "meeting_minutes_opus",
        "schema_version": "1.0.0",
        "status": "promoted",
        "created_at": "1970-01-01T00:00:00+00:00",
        "trace_id": "t",
        "input_refs": [],
        "content_hash": "sha256:" + "0" * 64,
        "payload": payload,
    }
    out = meeting_dir / "meeting_minutes_opus__20260101T000000Z.json"
    out.write_text(
        json.dumps(art, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def _write_legacy_jsonl(lake: Path, *, source_id: str, item_count: int) -> Path:
    """Write a Phase-3 style opus_reference_minutes.jsonl.

    The legacy format is one row per item with an ``extraction_type``
    and no prompt_content_hash anywhere — the WARNING path of the
    verifier.
    """
    meeting_dir = lake / "store" / "processed" / "meetings" / source_id / "reference_baselines"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    arrays = _content_arrays()
    for i in range(item_count):
        rows.append(
            json.dumps(
                {
                    "extraction_type": arrays[i % len(arrays)],
                    "ground_truth_text": f"item-{i}",
                    "pair_id": str(i),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    out = meeting_dir / "opus_reference_minutes.jsonl"
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return out


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_in_range_phase_4a_artifact_exits_0(tmp_path: Path) -> None:
    """106 items (the legacy Dec 18 count) is squarely in the
    reference range [100, 112] and inside the hard range [90, 125]."""
    _write_phase_4a_artifact(
        tmp_path, source_id="src-a", target_total=106
    )
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "FAIL" not in p.stdout
    assert "OK" in p.stdout


def test_in_hard_range_but_outside_reference_warns(tmp_path: Path) -> None:
    """115 items is inside the hard range but above the reference
    range — exits 0 with a WARN."""
    _write_phase_4a_artifact(
        tmp_path, source_id="src-warn", target_total=120
    )
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 0
    assert "WARN" in p.stdout
    assert "FAIL" not in p.stdout


def test_below_hard_range_exits_1(tmp_path: Path) -> None:
    # 23 × 2 = 46 items: well below the [90, 125] hard range.
    _write_phase_4a_artifact(tmp_path, source_id="src-low", per_type_count=2)
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 1
    assert "FAIL" in p.stdout


def test_above_hard_range_exits_1(tmp_path: Path) -> None:
    # 23 × 6 = 138 items: above the upper bound of 125.
    _write_phase_4a_artifact(tmp_path, source_id="src-high", per_type_count=6)
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 1
    assert "FAIL" in p.stdout


def test_legacy_jsonl_without_prompt_hash_still_exits_0_in_range(
    tmp_path: Path,
) -> None:
    """A 106-item legacy JSONL exits 0 with a WARNING about the missing hash."""
    _write_legacy_jsonl(tmp_path, source_id="src-legacy", item_count=106)
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 0
    assert "WARN" in p.stdout
    assert "pre-Phase-2 legacy" in p.stdout


def test_no_baseline_present_exits_1(tmp_path: Path) -> None:
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 1
    assert "no Opus baseline" in p.stderr


def test_source_id_scope(tmp_path: Path) -> None:
    """Restricting to one source_id ignores others."""
    _write_phase_4a_artifact(tmp_path, source_id="src-ok", target_total=106)
    _write_phase_4a_artifact(tmp_path, source_id="src-bad", target_total=46)
    p_ok = _run("--lake", str(tmp_path), "--source-id", "src-ok")
    p_bad = _run("--lake", str(tmp_path), "--source-id", "src-bad")
    assert p_ok.returncode == 0
    assert p_bad.returncode == 1


def test_json_emission(tmp_path: Path) -> None:
    _write_phase_4a_artifact(tmp_path, source_id="src-a", target_total=106)
    p = _run("--lake", str(tmp_path), "--json")
    assert p.returncode == 0
    parsed = json.loads(p.stdout)
    assert isinstance(parsed, list)
    assert parsed[0]["classification"] == "OK"
    assert "per_type" in parsed[0]


def test_phase_4a_artifact_hash_compared_against_canonical(
    tmp_path: Path,
) -> None:
    """When the artifact carries a prompt_content_hash, the verifier
    prints both the artifact and the canonical hash for the operator
    to compare. A differing hash is INFO, not a failure."""
    _write_phase_4a_artifact(
        tmp_path,
        source_id="src-a",
        target_total=106,
        include_prompt_hash=True,
    )
    p = _run("--lake", str(tmp_path))
    assert p.returncode == 0
    # Both 16-char prefixes should appear in the output.
    assert "artifact=" in p.stdout
    assert "canonical=" in p.stdout
