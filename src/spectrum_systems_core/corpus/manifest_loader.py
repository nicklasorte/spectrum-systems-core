"""Phase 4 — corpus manifest loader.

Loads ``data/corpus/manifest.json``, validates against the JSON Schema,
recomputes and verifies the ``manifest_hash``, and enforces the
custom rules JSON Schema cannot express:

* ``source_id`` uniqueness across the sources array.
* ``expected_path`` uniqueness across the sources array, with one
  documented exception: a duplicate is allowed when the new entry's
  ``supersedes`` points to an existing source_id.
* ``supersedes`` references a valid source_id in the same manifest.

The loader is fail-closed: any violation raises
:class:`CorpusManifestError` with a precise reason code so a CI gate
can grep for it. The reason codes are part of the public contract;
adding or renaming one is a contract change.

``manifest_hash`` is computed as the sha256 hex digest of the
canonicalized ``sources`` array, where canonicalization sorts:

* the top-level sources array by ``source_id`` ascending,
* every nested object's keys.

This means two manifests with the same source set in different file
order (or with idle whitespace differences) produce the same hash —
exactly the property we want a content hash to have.

The writer side (``rewrite_manifest_with_observed``) is invoked from
the ingest CLI to update ``observed`` fields and refresh the hash.
The writer NEVER touches ``declared`` fields; an operator edits those
by hand and re-runs the loader, which catches any new validation
failure before any ingest step runs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema

from ..schemas import schema_path


# Reason codes the loader emits. They are part of the contract: tests
# assert on these strings, and the status CLI surfaces them through to
# the operator.
HASH_MISMATCH: str = "corpus_manifest_hash_mismatch"
SCHEMA_VIOLATION: str = "corpus_manifest_schema_violation"
SOURCE_ID_DUPLICATE: str = "corpus_manifest_source_id_duplicate"
PATH_DUPLICATE_NO_SUPERSEDES: str = (
    "corpus_manifest_expected_path_duplicate_no_supersedes"
)
SUPERSEDES_UNKNOWN: str = "corpus_manifest_supersedes_unknown_source"
SUPERSEDES_SELF: str = "corpus_manifest_supersedes_self"
MANIFEST_NOT_FOUND: str = "corpus_manifest_not_found"
MANIFEST_UNREADABLE: str = "corpus_manifest_unreadable"

_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST_PATH: Path = _REPO_ROOT / "data" / "corpus" / "manifest.json"

# The placeholder string the manifest ships with before its hash has
# been computed. The loader rejects this literal so an un-hashed
# manifest cannot be used as an authoritative input.
PLACEHOLDER_HASH: str = "PLACEHOLDER_HASH_REGENERATED_BY_LOADER"


class CorpusManifestError(ValueError):
    """Raised on any manifest validation failure.

    Carries the contract reason code in :attr:`reason_code` so callers
    (the CLI, the status report builder) can branch on it without
    string-matching the message.
    """

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class LoadedManifest:
    """Result of :func:`load_manifest`.

    Carries the validated payload and the resolved on-disk path so a
    caller writing back observed updates does not need to remember
    which path it loaded.
    """

    path: Path
    payload: Dict[str, Any]
    manifest_hash: str


def _canonical_sources_for_hash(sources: List[Dict[str, Any]]) -> str:
    """Serialize the sources array deterministically for hashing.

    Sort sources by ``source_id``, then field-sort every nested
    object. Use ``json.dumps`` with ``sort_keys=True`` so the inner
    sort is built in.
    """
    sorted_sources = sorted(
        sources, key=lambda s: str(s.get("source_id", ""))
    )
    return json.dumps(sorted_sources, sort_keys=True, separators=(",", ":"))


def compute_manifest_hash(sources: List[Dict[str, Any]]) -> str:
    """Compute the sha256 hex of the canonicalized sources array."""
    canonical = _canonical_sources_for_hash(sources)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_schema() -> Dict[str, Any]:
    path = schema_path("corpus_manifest")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(data: Any) -> None:
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    try:
        validator.validate(data)
    except jsonschema.ValidationError as exc:
        raise CorpusManifestError(
            SCHEMA_VIOLATION,
            f"corpus manifest schema violation: {exc.message} at "
            f"path={list(exc.absolute_path)}",
        ) from exc


def _validate_custom_rules(payload: Dict[str, Any]) -> None:
    """The three rules JSON Schema cannot express natively."""
    sources = payload["sources"]

    seen_ids: Dict[str, int] = {}
    for idx, entry in enumerate(sources):
        sid = entry["source_id"]
        if sid in seen_ids:
            raise CorpusManifestError(
                SOURCE_ID_DUPLICATE,
                f"source_id {sid!r} appears at indices {seen_ids[sid]} "
                f"and {idx}",
            )
        seen_ids[sid] = idx

    # supersedes must reference a known source_id (or be null), and must
    # not point at the entry itself.
    for entry in sources:
        sup = entry["declared"]["supersedes"]
        if sup is None:
            continue
        if sup == entry["source_id"]:
            raise CorpusManifestError(
                SUPERSEDES_SELF,
                f"source_id {entry['source_id']!r} declares "
                f"supersedes={sup!r} (self-reference forbidden)",
            )
        if sup not in seen_ids:
            raise CorpusManifestError(
                SUPERSEDES_UNKNOWN,
                f"source_id {entry['source_id']!r} declares "
                f"supersedes={sup!r}, but no such source_id is in the "
                f"manifest",
            )

    # expected_path uniqueness, with the supersedes exception. A path
    # may be shared by two entries iff one of them supersedes the
    # other. We compute the equivalence class via the supersedes
    # chain and only complain about duplicates that are NOT in a
    # supersession relationship.
    by_path: Dict[str, List[str]] = {}
    by_id: Dict[str, Dict[str, Any]] = {e["source_id"]: e for e in sources}
    for entry in sources:
        path = entry["declared"]["expected_path"]
        by_path.setdefault(path, []).append(entry["source_id"])
    for path, ids in by_path.items():
        if len(ids) <= 1:
            continue
        # Build the supersession graph for these ids: an edge from
        # newer -> older. Any path collision where at least one entry
        # supersedes another in the same group is allowed.
        ok = False
        id_set = set(ids)
        for sid in ids:
            sup = by_id[sid]["declared"]["supersedes"]
            if sup is not None and sup in id_set:
                ok = True
                break
        if not ok:
            raise CorpusManifestError(
                PATH_DUPLICATE_NO_SUPERSEDES,
                f"expected_path {path!r} is shared by source_ids "
                f"{sorted(ids)} without a supersedes relationship",
            )


def _verify_hash(payload: Dict[str, Any]) -> None:
    declared = payload["manifest_hash"]
    if declared == PLACEHOLDER_HASH:
        raise CorpusManifestError(
            HASH_MISMATCH,
            f"manifest_hash is still the placeholder "
            f"{PLACEHOLDER_HASH!r}; regenerate via "
            f"manifest_loader.rewrite_manifest_with_observed() before "
            f"the loader will accept it",
        )
    expected = compute_manifest_hash(payload["sources"])
    if declared != expected:
        raise CorpusManifestError(
            HASH_MISMATCH,
            f"manifest_hash mismatch: file declares {declared!r} but "
            f"loader computed {expected!r}",
        )


def load_manifest(
    path: Path | str | None = None,
    *,
    skip_hash_check: bool = False,
) -> LoadedManifest:
    """Read, schema-validate, hash-verify, and custom-validate the manifest.

    ``skip_hash_check`` is the ONE escape hatch — it exists so the
    bootstrap helper (used at first-write time, before any hash
    exists) can validate the schema + custom rules without circular
    pain. Production callers (the CLI) always leave it False.
    """
    p = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    if not p.is_file():
        raise CorpusManifestError(
            MANIFEST_NOT_FOUND,
            f"corpus manifest not found at {p}",
        )
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusManifestError(
            MANIFEST_UNREADABLE,
            f"could not read corpus manifest at {p}: {exc}",
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorpusManifestError(
            MANIFEST_UNREADABLE,
            f"corpus manifest at {p} is not valid JSON: {exc}",
        ) from exc

    _validate_schema(payload)
    _validate_custom_rules(payload)
    if not skip_hash_check:
        _verify_hash(payload)

    return LoadedManifest(
        path=p,
        payload=payload,
        manifest_hash=payload["manifest_hash"],
    )


def find_source(
    manifest: LoadedManifest, source_id: str
) -> Optional[Dict[str, Any]]:
    """Return the source entry for ``source_id`` or ``None``."""
    for entry in manifest.payload["sources"]:
        if entry["source_id"] == source_id:
            return entry
    return None


def rewrite_manifest_with_observed(
    *,
    path: Path | str | None = None,
    observed_updates: Dict[str, Dict[str, Any]],
) -> LoadedManifest:
    """Atomically rewrite the manifest with new ``observed`` fields.

    ``observed_updates`` maps source_id -> a dict of the four
    observed fields. The writer overwrites the existing observed
    block wholesale (manual edits to ``observed`` are not respected,
    by design — the schema description states this), recomputes the
    hash, and writes the file atomically (tmp + rename).

    The function reloads-and-validates the existing manifest first so
    a malformed manifest is never overwritten by a partial update.
    """
    loaded = load_manifest(path)
    payload = json.loads(json.dumps(loaded.payload))  # deep copy

    for entry in payload["sources"]:
        sid = entry["source_id"]
        if sid in observed_updates:
            entry["observed"] = dict(observed_updates[sid])

    new_hash = compute_manifest_hash(payload["sources"])
    payload["manifest_hash"] = new_hash

    # Re-validate before writing so a bug in the updater cannot leave
    # an invalid manifest on disk.
    _validate_schema(payload)
    _validate_custom_rules(payload)

    tmp_path = loaded.path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(loaded.path)
    return LoadedManifest(
        path=loaded.path,
        payload=payload,
        manifest_hash=new_hash,
    )


def bootstrap_hash(path: Path | str | None = None) -> str:
    """Read the manifest, compute its hash, write the hash back.

    Used by the operator (or a one-shot fixup) when a manifest's
    sources block has been edited by hand and the hash must be
    refreshed. Returns the new hash. Validates the manifest's schema
    and custom rules BEFORE rewriting so a malformed manifest never
    gets a fresh hash that masks the underlying problem.
    """
    p = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    text = p.read_text(encoding="utf-8")
    payload = json.loads(text)
    _validate_schema(payload)
    _validate_custom_rules(payload)
    new_hash = compute_manifest_hash(payload["sources"])
    payload["manifest_hash"] = new_hash
    tmp_path = p.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(p)
    return new_hash


__all__ = [
    "CorpusManifestError",
    "DEFAULT_MANIFEST_PATH",
    "HASH_MISMATCH",
    "LoadedManifest",
    "MANIFEST_NOT_FOUND",
    "MANIFEST_UNREADABLE",
    "PATH_DUPLICATE_NO_SUPERSEDES",
    "PLACEHOLDER_HASH",
    "SCHEMA_VIOLATION",
    "SOURCE_ID_DUPLICATE",
    "SUPERSEDES_SELF",
    "SUPERSEDES_UNKNOWN",
    "bootstrap_hash",
    "compute_manifest_hash",
    "find_source",
    "load_manifest",
    "rewrite_manifest_with_observed",
]
