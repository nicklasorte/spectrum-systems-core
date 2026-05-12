"""ClassificationCache: skip Haiku calls for chunks we have already classified.

Phase Perf. The cache is a pure speed optimization -- it never blocks the
classifier and never raises. A miss returns ``None`` and the caller goes
through the normal API path.

Cache key: SHA-256 of the chunk's full text.

Choosing text-hash (vs ``chunk_id``) means:
- A re-run on the SAME transcript with regenerated chunk_ids still hits.
- A modification to a chunk's content invalidates that one entry without
  affecting any other chunk in the transcript.

The full text is hashed -- speaker turns can be many KB and frequently
share a long preamble (e.g. an introductory disclaimer). Hashing only a
prefix would collide on different turns that share that preamble and
serve the wrong classification on re-runs. We use a 16-character hex
prefix of the SHA-256 (64 bits of entropy) for storage compactness; per
cache file (one per source_id, ~200 chunks) the birthday-collision
probability is ~1e-15.

The hash is independent of chunk_id, so two chunks with identical text
deliberately collapse to the same cache entry -- this is correct: we
want them to receive the same classification.

TTL: 30 days. Stale entries are dropped on ``load()``. The cache file is
plain JSON under ``<sdl_root>/cache/classifications/<source_id>_cache.json``.
One file per source_id keeps cache bookkeeping local to the matrix job
that wrote it.

The cache must NEVER raise: every public method swallows IO and JSON
errors, logs a warning, and returns a safe default. The classifier path
that uses the cache always treats a None result as "miss, call the API".
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

_LOG = logging.getLogger(__name__)

_DEFAULT_TTL_DAYS = 30
_HASH_LENGTH = 16


class ClassificationCache:
    """Best-effort persistent cache of chunk_id text -> classification."""

    CACHE_TTL_DAYS: int = _DEFAULT_TTL_DAYS
    HASH_LENGTH: int = _HASH_LENGTH

    def __init__(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir) / "cache" / "classifications"
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _LOG.warning("classification_cache_mkdir_failed: %s", exc)
        self._memory: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------
    @classmethod
    def _chunk_hash(cls, chunk: Any) -> Optional[str]:
        """Return a stable hex hash for a chunk, or None if unhashable."""
        if not isinstance(chunk, dict):
            return None
        text = chunk.get("text")
        if not isinstance(text, str) or not text:
            return None
        return hashlib.sha256(
            text.encode("utf-8", errors="replace")
        ).hexdigest()[: cls.HASH_LENGTH]

    # ------------------------------------------------------------------
    # In-memory operations
    # ------------------------------------------------------------------
    def get(self, chunk: Any) -> Optional[str]:
        """Return the cached classification for ``chunk`` or None on miss.

        Never raises.
        """
        try:
            key = self._chunk_hash(chunk)
            if key is None:
                return None
            entry = self._memory.get(key)
            if not entry:
                return None
            classification = entry.get("classification")
            return classification if isinstance(classification, str) else None
        except Exception as exc:  # pragma: no cover -- pure defensive
            _LOG.warning("classification_cache_get_failed: %s", exc)
            return None

    def set(self, chunk: Any, classification: str) -> None:
        """Cache the classification result for ``chunk``. Never raises."""
        try:
            key = self._chunk_hash(chunk)
            if key is None or not isinstance(classification, str):
                return
            self._memory[key] = {
                "classification": classification,
                "cached_at": _now_iso(),
            }
        except Exception as exc:  # pragma: no cover
            _LOG.warning("classification_cache_set_failed: %s", exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _path_for(self, source_id: str) -> Path:
        # source_id is already slugified by the orchestrator; we still
        # restrict the filename to a safe alphabet to defend against any
        # caller that bypasses the slugify step.
        safe = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in source_id
        ) or "_"
        return self._cache_dir / f"{safe}_cache.json"

    def load(self, source_id: str) -> None:
        """Load the cache file for ``source_id`` into memory.

        Drops entries whose ``cached_at`` is older than ``CACHE_TTL_DAYS``.
        Never raises -- a corrupt file logs a warning and resets the
        in-memory cache to empty.
        """
        path = self._path_for(source_id)
        if not path.exists():
            self._memory = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _LOG.warning("classification_cache_load_failed: %s: %s", path, exc)
            self._memory = {}
            return
        if not isinstance(data, dict):
            self._memory = {}
            return

        cutoff = _now() - datetime.timedelta(days=self.CACHE_TTL_DAYS)
        kept: Dict[str, Dict[str, Any]] = {}
        for key, entry in data.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            classification = entry.get("classification")
            cached_at = entry.get("cached_at")
            if not isinstance(classification, str) or not isinstance(cached_at, str):
                continue
            try:
                ts = datetime.datetime.fromisoformat(cached_at)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            if ts < cutoff:
                continue
            kept[key] = {"classification": classification, "cached_at": cached_at}
        self._memory = kept
        _LOG.info(
            "classification_cache_loaded: source_id=%s entries=%d",
            source_id, len(kept),
        )

    def save(self, source_id: str) -> None:
        """Persist the in-memory cache to disk. Never raises."""
        path = self._path_for(source_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._memory, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            _LOG.info(
                "classification_cache_saved: source_id=%s entries=%d",
                source_id, len(self._memory),
            )
        except OSError as exc:
            _LOG.warning("classification_cache_save_failed: %s: %s", path, exc)

    def __len__(self) -> int:
        return len(self._memory)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%S+00:00")
