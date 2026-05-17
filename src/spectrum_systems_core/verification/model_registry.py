"""Thin model registry used by Phase V's PostHocVerifier.

Reads SDL_ROOT/config/model_registry.json if present. The file is
expected to be of the form::

    {
      "task_types": {
        "generation": { "model": "claude-sonnet-4-6", "version": "1.0.0" },
        "extraction": { "model": "claude-haiku-4-5-20251001", "version": "1.0.0" }
      }
    }

If the file is missing, ``get(task_type)`` returns the documented default
for that task_type. This is intentional: the file is the *registry* (so
operators can pin), but a missing file is a soft failure -- the verifier
still runs (with its default model) so the build is not gated on the
seed step. ``PHASE_V_ENABLED`` callers always have a model to call.
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

_LOG = logging.getLogger(__name__)

# Sonnet-class for generation tasks (Phase V verifier);
# Haiku-class for extraction tasks (existing extractor stack).
_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "generation": {"model": "claude-sonnet-4-6", "version": "default"},
    "extraction": {"model": "claude-haiku-4-5-20251001", "version": "default"},
}


class ModelRegistryError(LookupError):
    """Raised when an unknown task_type is requested."""


class ModelRegistry:
    """Read SDL_ROOT/config/model_registry.json with sensible defaults."""

    def __init__(self, sdl_root: str | pathlib.Path | None = None):
        self.sdl_root = pathlib.Path(sdl_root) if sdl_root else None
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if self.sdl_root is None:
            self._cache = {}
            return self._cache
        path = self.sdl_root / "config" / "model_registry.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._cache = data
                return self._cache
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning(
                "model_registry_unreadable: %s -> falling back to defaults. %s",
                path, exc,
            )
        self._cache = {}
        return self._cache

    def get(self, task_type: str) -> dict[str, str]:
        """Return ``{"model": ..., "version": ...}`` for ``task_type``."""
        if not isinstance(task_type, str) or not task_type:
            raise ModelRegistryError(f"invalid task_type: {task_type!r}")
        data = self._load()
        task_types = (data.get("task_types") or {}) if isinstance(data, dict) else {}
        entry = task_types.get(task_type)
        if isinstance(entry, dict) and "model" in entry:
            return {
                "model": str(entry["model"]),
                "version": str(entry.get("version", "registry")),
            }
        default = _DEFAULT_MODELS.get(task_type)
        if default is not None:
            return dict(default)
        raise ModelRegistryError(
            f"no model configured for task_type={task_type!r}"
        )
