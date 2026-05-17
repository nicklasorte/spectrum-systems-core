"""AliasNormalizer: deterministic agency name -> agency_slug.

FINDING-E-002: same agency under multiple names. Exact-match alias lookup
runs before any profile read or write. No LLM. No fuzzy matching.

The seed dict is augmented at runtime from agency/<slug>/profile.json
files (their canonical agency_name + aliases array).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

KNOWN_AGENCY_ALIASES: dict[str, list[str]] = {
    "fcc": ["federal communications commission", "f.c.c."],
    "ntia": ["national telecommunications and information administration"],
    "dod": [
        "department of defense",
        "dept. of defense",
        "u.s. department of defense",
    ],
    "faa": ["federal aviation administration"],
    "nasa": ["national aeronautics and space administration"],
}

_SLUG_INVALID = re.compile(r"[^a-z0-9_-]+")


def _normalize_input(name: str) -> str:
    return (name or "").strip().lower()


def _slugify(name: str) -> str:
    """Generate a filesystem-safe agency_slug from a raw name.

    Lowercase, spaces -> '_', strip non-alphanumeric except _ and -.
    Truncated to 64 chars (schema pattern limit).
    """
    lowered = (name or "").strip().lower()
    lowered = lowered.replace(" ", "_")
    cleaned = _SLUG_INVALID.sub("", lowered).strip("-_") or "unknown"
    return cleaned[:64]


def _scan_profiles(repo_root: Path) -> list[dict[str, object]]:
    """Read every agency/<slug>/profile.json on disk."""
    base = repo_root / "agency"
    if not base.is_dir():
        return []
    out: list[dict[str, object]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        profile_path = child / "profile.json"
        if not profile_path.is_file():
            continue
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


class AliasNormalizer:
    """Maps an agency name string to a canonical agency_slug."""

    def normalize(self, name: str, repo_root: str) -> str:
        """Return the canonical agency_slug for a raw name. Never raises."""
        repo_root_path = Path(repo_root).resolve()
        lowered = _normalize_input(name)
        if not lowered:
            return "unknown"

        # 1. Exact match against known slug keys.
        if lowered in KNOWN_AGENCY_ALIASES:
            return lowered

        # 2. Exact match against any seed alias value.
        for slug, aliases in KNOWN_AGENCY_ALIASES.items():
            for alias in aliases:
                if lowered == alias.lower().strip():
                    return slug

        # 3. Exact match against on-disk profile names + aliases.
        for profile in _scan_profiles(repo_root_path):
            slug = profile.get("agency_slug")
            if not isinstance(slug, str):
                continue
            canonical = str(profile.get("agency_name", "")).strip().lower()
            aliases = profile.get("aliases") or []
            if lowered == canonical:
                return slug
            for alias in aliases:
                if isinstance(alias, str) and lowered == alias.strip().lower():
                    return slug

        # 4. Generate slug from name.
        return _slugify(name)

    def would_duplicate(
        self, agency_name: str, agency_slug: str, repo_root: str
    ) -> bool:
        """Return True if a profile already covers this name or slug.

        Considers: agency_slug match, agency_name match, alias match
        across all profile.json files on disk. (FINDING-E-002)
        """
        repo_root_path = Path(repo_root).resolve()
        lowered_name = _normalize_input(agency_name)
        lowered_slug = (agency_slug or "").strip().lower()
        for profile in _scan_profiles(repo_root_path):
            existing_slug = str(profile.get("agency_slug") or "").strip().lower()
            existing_name = str(profile.get("agency_name") or "").strip().lower()
            existing_aliases = [
                str(a).strip().lower()
                for a in (profile.get("aliases") or [])
                if isinstance(a, str)
            ]
            if existing_slug and existing_slug == lowered_slug:
                return True
            if existing_name and existing_name == lowered_name:
                return True
            if lowered_name and lowered_name in existing_aliases:
                return True
        return False
