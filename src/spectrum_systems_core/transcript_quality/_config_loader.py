"""Phase 2R — load and validate the transcript-quality config.

The loader is the only place that reads ``data/transcript_quality_config.json``
from disk, validates it against ``transcript_quality_config.schema.json``,
and enforces the cross-field constraint that the JSON Schema cannot express
inline (``advisory_max_byte_length <= hard_max_byte_length``).

Keeping this out of :func:`spectrum_systems_core.transcript_quality.validate.validate`
preserves the validator's purity contract: the validator never touches the
file system.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "data" / "transcript_quality_config.json"
CONFIG_SCHEMA_PATH = Path(__file__).resolve().parent / "config.schema.json"


class TranscriptQualityConfigError(ValueError):
    """Raised when the config file is missing, malformed, or violates
    a cross-field constraint."""


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate a transcript-quality config file."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.is_file():
        raise TranscriptQualityConfigError(f"config not found: {p}")
    try:
        config = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranscriptQualityConfigError(
            f"config is not valid JSON: {exc}"
        ) from exc
    return validate_config(config)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate a config dict against the JSON Schema and the cross-field
    constraint. Returns the input unchanged on success."""
    schema = json.loads(CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema).validate(config)
    except jsonschema.ValidationError as exc:
        raise TranscriptQualityConfigError(
            f"config schema violation: {exc.message}"
        ) from exc
    adv = config.get("advisory_max_byte_length")
    hard = config.get("hard_max_byte_length")
    if (
        isinstance(adv, int)
        and isinstance(hard, int)
        and adv > hard
    ):
        raise TranscriptQualityConfigError(
            f"advisory_max_byte_length ({adv}) exceeds "
            f"hard_max_byte_length ({hard})"
        )
    return config
