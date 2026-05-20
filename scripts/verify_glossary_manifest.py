"""Phase 2P glossary manifest hash verifier.

Reads ``data/glossary/MANIFEST.json``, re-computes the canonical
sha256 of the glossary JSONL and ``allowed_sources.json``, and
compares each against the manifest's declared hashes. Exits 0 on
match, non-zero on mismatch (with a clear message naming which hash
failed).

Intended to run as a CI step and as part of the pre-PR verification
loop. Pure stdlib so it works in any checkout without ``pip
install``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_GLOSSARY_DIR = REPO_ROOT / "data" / "glossary"


def _import_loader_helpers():
    """Import the canonical hash helpers from the loader module.

    The loader uses the same canonicalization as this script; importing
    them keeps the two byte-for-byte aligned. We add the src/ tree to
    sys.path explicitly so this verifier runs without ``pip install``.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from spectrum_systems_core.glossary.loader import (  # noqa: E402
        compute_allowed_sources_hash,
        compute_glossary_hash,
    )
    return compute_glossary_hash, compute_allowed_sources_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Phase 2P glossary manifest hashes."
    )
    parser.add_argument(
        "--glossary-dir",
        default=str(DEFAULT_GLOSSARY_DIR),
        help="Directory containing MANIFEST.json, the JSONL, and allowed_sources.json.",
    )
    args = parser.parse_args(argv)

    glossary_dir = pathlib.Path(args.glossary_dir)
    manifest_path = glossary_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        print(f"FAIL glossary_manifest_unreadable: missing {manifest_path}")
        return 2
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL glossary_manifest_unreadable: {exc}")
        return 2

    glossary_file = manifest.get("glossary_file")
    declared_glossary_hash = manifest.get("sha256_hash")
    declared_allowed_hash = manifest.get("allowed_sources_hash")
    declared_count = manifest.get("entry_count")
    if (
        not isinstance(glossary_file, str)
        or not isinstance(declared_glossary_hash, str)
        or not isinstance(declared_allowed_hash, str)
    ):
        print(
            "FAIL glossary_manifest_unreadable: "
            "missing glossary_file/sha256_hash/allowed_sources_hash"
        )
        return 2

    glossary_path = glossary_dir / glossary_file
    allowed_path = glossary_dir / "allowed_sources.json"
    if not glossary_path.is_file():
        print(f"FAIL glossary_entries_unreadable: missing {glossary_path}")
        return 2
    if not allowed_path.is_file():
        print(f"FAIL glossary_allowed_sources_unreadable: missing {allowed_path}")
        return 2

    compute_glossary_hash, compute_allowed_sources_hash = _import_loader_helpers()

    raw = glossary_path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    try:
        entries = [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:
        print(f"FAIL glossary_entries_unreadable: {exc}")
        return 2

    actual_glossary_hash = compute_glossary_hash(entries)
    if actual_glossary_hash != declared_glossary_hash:
        print(
            "FAIL glossary_manifest_hash_mismatch: "
            f"manifest claims {declared_glossary_hash}, "
            f"file hashes to {actual_glossary_hash}"
        )
        return 1

    try:
        allowed_doc = json.loads(allowed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL glossary_allowed_sources_unreadable: {exc}")
        return 2
    allowed = allowed_doc.get("allowed_sources")
    if not isinstance(allowed, list):
        print("FAIL glossary_allowed_sources_unreadable: allowed_sources not a list")
        return 2
    actual_allowed_hash = compute_allowed_sources_hash(allowed)
    if actual_allowed_hash != declared_allowed_hash:
        print(
            "FAIL glossary_allowed_sources_hash_mismatch: "
            f"manifest claims {declared_allowed_hash}, "
            f"file hashes to {actual_allowed_hash}"
        )
        return 1

    if declared_count is not None and declared_count != len(entries):
        print(
            "FAIL glossary_entry_count_mismatch: "
            f"manifest claims {declared_count}, file has {len(entries)}"
        )
        return 1

    print(
        "OK glossary_manifest_verified: "
        f"{len(entries)} entries, "
        f"glossary_hash={actual_glossary_hash[:16]}…, "
        f"allowed_sources_hash={actual_allowed_hash[:16]}…"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
