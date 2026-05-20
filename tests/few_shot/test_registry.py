"""Phase 3P registry-shape tests.

Asserts the registry-level invariants the prompt depends on:

- The implicit_decision example is the LAST positional entry.
- Every entry's ``gold_extraction`` carries all 22 required arrays.
- ``speaker_names_stripped`` is True on every entry.
- The manifest hash matches the JSONL file's canonical bytes.
- Synthetic entries are flagged by both the manifest and the loaded
  registry's ``has_synthetic_entries``.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from spectrum_systems_core.few_shot import (
    FEW_SHOT_EXAMPLES_PATH,
    FEW_SHOT_MANIFEST_PATH,
    FewShotError,
    compute_examples_hash,
    load_few_shot_registry,
)
from spectrum_systems_core.few_shot.loader import REQUIRED_GOLD_KEYS


def _load_jsonl_dicts() -> list[dict]:
    raw = FEW_SHOT_EXAMPLES_PATH.read_text(encoding="utf-8")
    out: list[dict] = []
    for line in raw.splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def test_implicit_decision_is_last_positional_entry() -> None:
    entries = _load_jsonl_dicts()
    # Positional access — the test must fail if you swap entries 2/3.
    assert entries[2]["example_type"] == "implicit_decision", (
        "Recency-bias property: the LAST entry must be implicit_decision. "
        f"got example_type={entries[2]['example_type']!r}"
    )


def test_every_entry_has_all_22_gold_extraction_keys() -> None:
    entries = _load_jsonl_dicts()
    assert len(entries) == 3
    for idx, e in enumerate(entries, start=1):
        gold = e["gold_extraction"]
        missing = [k for k in REQUIRED_GOLD_KEYS if k not in gold]
        extra = [k for k in gold if k not in REQUIRED_GOLD_KEYS]
        assert not missing, f"entry {idx} missing arrays: {missing}"
        assert not extra, f"entry {idx} has unexpected arrays: {extra}"
        assert len(gold) == 22, f"entry {idx} must have exactly 22 arrays"


def test_speaker_names_stripped_is_true_everywhere() -> None:
    for e in _load_jsonl_dicts():
        assert e["speaker_names_stripped"] is True, (
            f"entry {e['id']}: speaker_names_stripped must be true"
        )


def test_manifest_sha256_matches_actual_file() -> None:
    manifest = json.loads(FEW_SHOT_MANIFEST_PATH.read_text(encoding="utf-8"))
    declared = manifest["sha256_hash"]
    actual = compute_examples_hash(_load_jsonl_dicts())
    assert declared == actual, (
        f"manifest claims {declared}, file hashes to {actual}"
    )


def test_manifest_entry_count_matches() -> None:
    manifest = json.loads(FEW_SHOT_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["entry_count"] == 3


def test_manifest_has_synthetic_matches_file() -> None:
    manifest = json.loads(FEW_SHOT_MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = _load_jsonl_dicts()
    actual_synthetic = any(bool(e.get("synthetic", False)) for e in entries)
    assert manifest["has_synthetic_entries"] is actual_synthetic, (
        f"manifest claims has_synthetic_entries={manifest['has_synthetic_entries']}, "
        f"file has synthetic={actual_synthetic}"
    )


def test_registry_loads_successfully() -> None:
    reg = load_few_shot_registry()
    assert len(reg.entries) == 3
    assert reg.entries[-1].example_type == "implicit_decision"
    assert all(e.speaker_names_stripped for e in reg.entries)
    assert reg.version_hash == compute_examples_hash(_load_jsonl_dicts())


def test_registry_includes_pretty_synthetic_flag() -> None:
    """The registry's has_synthetic_entries mirrors the file content,
    not just the manifest claim."""
    reg = load_few_shot_registry()
    entries = _load_jsonl_dicts()
    expected = any(bool(e.get("synthetic", False)) for e in entries)
    assert reg.has_synthetic_entries is expected


# ---------------------------------------------------------------------
# Fail-closed rejection tests — each gate paired with a rejection case.
# ---------------------------------------------------------------------


def _copy_registry_to(tmp: Path) -> tuple[Path, Path]:
    examples = tmp / "examples_v1.jsonl"
    manifest = tmp / "MANIFEST.json"
    shutil.copy(FEW_SHOT_EXAMPLES_PATH, examples)
    shutil.copy(FEW_SHOT_MANIFEST_PATH, manifest)
    return examples, manifest


def test_manifest_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    examples, manifest = _copy_registry_to(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["sha256_hash"] = "0" * 64
    manifest.write_text(json.dumps(doc, indent=2))
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(examples_path=examples, manifest_path=manifest)
    assert exc_info.value.reason == "few_shot_manifest_hash_mismatch"


def test_speaker_names_stripped_false_fails_validation(tmp_path: Path) -> None:
    examples, manifest = _copy_registry_to(tmp_path)
    rows = [json.loads(line) for line in examples.read_text().splitlines() if line.strip()]
    rows[0]["speaker_names_stripped"] = False
    examples.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    # Recompute manifest hash to bypass the hash gate so we surface the
    # schema-level rejection rather than the hash mismatch.
    actual = compute_examples_hash(rows)
    doc = json.loads(manifest.read_text())
    doc["sha256_hash"] = actual
    manifest.write_text(json.dumps(doc, indent=2))
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(examples_path=examples, manifest_path=manifest)
    assert exc_info.value.reason == "few_shot_entry_invalid"


def test_example_type_invalid_fails_validation(tmp_path: Path) -> None:
    examples, manifest = _copy_registry_to(tmp_path)
    rows = [json.loads(line) for line in examples.read_text().splitlines() if line.strip()]
    rows[0]["example_type"] = "unknown_kind"
    examples.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    actual = compute_examples_hash(rows)
    doc = json.loads(manifest.read_text())
    doc["sha256_hash"] = actual
    manifest.write_text(json.dumps(doc, indent=2))
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(examples_path=examples, manifest_path=manifest)
    assert exc_info.value.reason == "few_shot_entry_invalid"


def test_missing_gold_array_fails_validation(tmp_path: Path) -> None:
    examples, manifest = _copy_registry_to(tmp_path)
    rows = [json.loads(line) for line in examples.read_text().splitlines() if line.strip()]
    # Drop one of the 22 required arrays.
    rows[0]["gold_extraction"].pop("decisions")
    examples.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    actual = compute_examples_hash(rows)
    doc = json.loads(manifest.read_text())
    doc["sha256_hash"] = actual
    manifest.write_text(json.dumps(doc, indent=2))
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(examples_path=examples, manifest_path=manifest)
    assert exc_info.value.reason == "few_shot_entry_invalid"


def test_implicit_decision_not_last_fails_ordering(tmp_path: Path) -> None:
    """Swap the LAST entry's example_type away from implicit_decision."""
    examples, manifest = _copy_registry_to(tmp_path)
    rows = [json.loads(line) for line in examples.read_text().splitlines() if line.strip()]
    # Swap entries 1 and 2 (0-indexed) so implicit_decision moves from
    # position 2 to position 1.
    rows[1], rows[2] = rows[2], rows[1]
    # Re-id so the schema's pattern still validates.
    rows[1]["id"] = "fewshot-002"
    rows[2]["id"] = "fewshot-003"
    examples.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    actual = compute_examples_hash(rows)
    doc = json.loads(manifest.read_text())
    doc["sha256_hash"] = actual
    manifest.write_text(json.dumps(doc, indent=2))
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(examples_path=examples, manifest_path=manifest)
    assert exc_info.value.reason == "few_shot_ordering_invalid"


def test_id_pattern_invalid_fails_validation(tmp_path: Path) -> None:
    examples, manifest = _copy_registry_to(tmp_path)
    rows = [json.loads(line) for line in examples.read_text().splitlines() if line.strip()]
    rows[0]["id"] = "wrong-id"
    examples.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows)
        + "\n"
    )
    actual = compute_examples_hash(rows)
    doc = json.loads(manifest.read_text())
    doc["sha256_hash"] = actual
    manifest.write_text(json.dumps(doc, indent=2))
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(examples_path=examples, manifest_path=manifest)
    assert exc_info.value.reason == "few_shot_entry_invalid"


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    examples = tmp_path / "examples_v1.jsonl"
    shutil.copy(FEW_SHOT_EXAMPLES_PATH, examples)
    with pytest.raises(FewShotError) as exc_info:
        load_few_shot_registry(
            examples_path=examples,
            manifest_path=tmp_path / "MISSING.json",
        )
    assert exc_info.value.reason == "few_shot_manifest_unreadable"
