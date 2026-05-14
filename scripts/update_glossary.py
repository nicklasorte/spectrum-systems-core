"""Phase P3-A T-3: operator-facing glossary version manager.

Creates a NEW versioned glossary artifact by bumping
``glossary_version`` on the existing
``spectrum_glossary_v<N>.json`` artifact. Writes the bumped version
to ``spectrum_glossary_v<N+1>.json`` so the prior version stays on
disk -- a regression triage can pin
``GLOSSARY_VERSION=<N>`` to extract against the prior glossary
without editing the live artifact.

Usage:

    python scripts/update_glossary.py --data-lake data-lake/
    python scripts/update_glossary.py --data-lake data-lake/ \
        --glossary-file my_new_glossary.json
    python scripts/update_glossary.py --data-lake data-lake/ \
        --bump-only

Modes:

  - default (no flags): read every ``glossary_term`` file under
    ``<data-lake>/store/glossary/`` (legacy per-term files written
    by ``scripts/seed_glossary.py``), rebuild the versioned
    artifact, bump the version, and write the new file.
  - ``--glossary-file <path>``: read the term list from a JSON
    file (an array of term dicts, or a full glossary artifact
    dict) and use those terms verbatim.
  - ``--bump-only``: copy the current latest glossary verbatim
    but with ``glossary_version`` incremented. Use to install a
    new version pointer without changing the term list (e.g.
    after a metadata-only audit).

The script is intentionally minimal -- it never deletes files and
never overwrites an existing ``spectrum_glossary_v<N>.json``. The
operator decides when to delete an old version.

Refuses to run while ``ANTHROPIC_API_KEY`` is set unless
``--force`` is passed; the glossary is a static asset, not
something the live extraction agent should self-modify mid-run.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_GLOSSARY_FILENAME_RE = re.compile(r"^spectrum_glossary_v(?P<n>\d+)\.json$")


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _compute_content_hash(glossary_version: str, terms: List[Dict[str, Any]]) -> str:
    """Mirror the canonical sort_keys=True / compact serialiser used by
    ``glossary_builder.compute_glossary_content_hash``. Reproduced here
    to keep the script standalone (no SDK import required)."""
    payload = {"glossary_version": glossary_version, "terms": terms}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _find_existing_versions(glossary_root: Path) -> List[Tuple[int, Path]]:
    """Return a sorted ``[(version, path)]`` list for every existing
    versioned glossary artifact in the root."""
    out: List[Tuple[int, Path]] = []
    if not glossary_root.is_dir():
        return out
    for path in glossary_root.iterdir():
        m = _GLOSSARY_FILENAME_RE.match(path.name)
        if m:
            out.append((int(m.group("n")), path))
    out.sort(key=lambda t: t[0])
    return out


def _load_term_list(path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, dict)]
    if isinstance(raw, dict):
        terms = raw.get("terms")
        if isinstance(terms, list):
            return [t for t in terms if isinstance(t, dict)]
    raise ValueError(
        f"--glossary-file {path} must be either a list of term dicts "
        "or a full glossary artifact dict with a 'terms' key"
    )


def _stable_uuid(term: str) -> str:
    h = hashlib.sha1(term.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=h[:16]))


def _build_term_from_legacy(legacy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a legacy ``glossary_term`` artifact into the versioned
    term shape required by ``spectrum_glossary.schema.json``. Returns
    None when the legacy record is malformed."""
    term = legacy.get("term")
    if not isinstance(term, str) or not term.strip():
        return None
    definition = legacy.get("definition") or ""
    if not isinstance(definition, str) or not definition.strip():
        return None
    short = definition[:200]
    return {
        "term_id": legacy.get("glossary_term_id") or _stable_uuid(term),
        "term": term.strip(),
        "abbreviation": legacy.get("abbreviation"),
        "definition": definition,
        "short_definition": short,
        "authoritative_source": legacy.get("authoritative_source") or "unknown",
        "domain_scope": legacy.get("domain_scope") or "spectrum",
        "related_term_ids": legacy.get("related_term_ids") or [],
    }


def _load_legacy_per_term_files(legacy_root: Path) -> List[Dict[str, Any]]:
    """Read every ``glossary_term`` JSON file under ``legacy_root``."""
    if not legacy_root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(legacy_root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"warn: could not read {path}: {exc}",
                file=sys.stderr,
            )
            continue
        if not isinstance(data, dict):
            continue
        # Skip files that look like a versioned glossary artifact;
        # we never want to recurse on the per-term loader.
        if data.get("artifact_type") == "spectrum_glossary":
            continue
        built = _build_term_from_legacy(data)
        if built is not None:
            out.append(built)
    return out


def _write_glossary_artifact(
    glossary_root: Path,
    new_version: int,
    terms: List[Dict[str, Any]],
) -> Path:
    glossary_version_str = str(new_version)
    artifact = {
        "artifact_type": "spectrum_glossary",
        "schema_version": "1.0.0",
        "glossary_version": glossary_version_str,
        "term_count": len(terms),
        "content_hash": _compute_content_hash(glossary_version_str, terms),
        "created_at": _now_iso(),
        "terms": terms,
    }
    target = glossary_root / f"spectrum_glossary_v{new_version}.json"
    if target.exists():
        raise FileExistsError(
            f"refusing to overwrite existing {target}; delete it first "
            "or choose a higher version number"
        )
    glossary_root.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def _resolve_glossary_root(data_lake: Path) -> Path:
    return data_lake / "store" / "artifacts" / "glossary"


def _resolve_legacy_term_dir(data_lake: Path) -> Path:
    """Per-term legacy files live under ``store/glossary/`` (NOT
    ``store/artifacts/glossary/``). The two paths are distinct; the
    versioned artifact and the legacy files are independent."""
    return data_lake / "store" / "glossary"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True, help="Data lake root")
    parser.add_argument(
        "--glossary-file",
        help="JSON file containing the term list (list or artifact)",
    )
    parser.add_argument(
        "--from-taxonomy",
        action="store_true",
        help="Build the term list from the seeded per-term legacy files",
    )
    parser.add_argument(
        "--bump-only",
        action="store_true",
        help="Bump version without changing the term list",
    )
    parser.add_argument(
        "--target-version",
        type=int,
        help="Explicit version number for the new artifact (default: current_max + 1)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow running while ANTHROPIC_API_KEY is set (default: refuse)",
    )
    args = parser.parse_args(argv)

    if os.environ.get("ANTHROPIC_API_KEY") and not args.force:
        print(
            "refusing to run while ANTHROPIC_API_KEY is set; pass --force "
            "if this is a controlled CI context.",
            file=sys.stderr,
        )
        return 2

    data_lake = Path(args.data_lake)
    glossary_root = _resolve_glossary_root(data_lake)
    existing = _find_existing_versions(glossary_root)
    current_max = existing[-1][0] if existing else 0
    new_version = args.target_version if args.target_version else current_max + 1
    if args.target_version is not None and args.target_version <= current_max:
        print(
            f"refusing: target-version {args.target_version} is not above "
            f"the current max {current_max}",
            file=sys.stderr,
        )
        return 2

    # Resolve term list per mode.
    if args.glossary_file:
        terms = _load_term_list(Path(args.glossary_file))
    elif args.bump_only:
        if not existing:
            print(
                "refusing: --bump-only requires an existing versioned glossary",
                file=sys.stderr,
            )
            return 2
        latest_path = existing[-1][1]
        existing_artifact = json.loads(latest_path.read_text(encoding="utf-8"))
        terms = list(existing_artifact.get("terms") or [])
    elif args.from_taxonomy:
        legacy_dir = _resolve_legacy_term_dir(data_lake)
        terms = _load_legacy_per_term_files(legacy_dir)
        if not terms:
            print(
                f"refusing: --from-taxonomy found zero terms under {legacy_dir}",
                file=sys.stderr,
            )
            return 2
    else:
        print(
            "must specify one of: --glossary-file <path>, --from-taxonomy, --bump-only",
            file=sys.stderr,
        )
        return 2

    written = _write_glossary_artifact(glossary_root, new_version, terms)
    print(f"wrote {written}")
    print(f"  glossary_version: {new_version}")
    print(f"  term_count: {len(terms)}")
    print(f"  prior version: {current_max if current_max else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
