"""FewShotLoader: load and version-gate the hand-authored few-shot seed.

Phase M.4. The few-shot seed is a small JSON artifact (5-10 KB) shipped
with the repo (or under SDL_ROOT) that gives the extraction prompts a
fixed set of in-distribution examples. Stanford HELM finding: three
in-distribution examples is enough to lift consistency by ~5 points.

The loader is intentionally fail-soft:

* File missing -> return (None, "missing"). Extraction continues
  without few-shot injection; a single warning is logged.
* JSON unreadable / schema invalid -> return (None, reason). Same as
  missing -- extraction continues without injection.
* prompt_schema_version mismatch -> return (None, "version_mismatch").
  This is the case where the few-shot examples were authored against a
  different extraction schema than the one currently in use; using
  them risks producing structurally-invalid model output. Warning
  ``few_shot_version_mismatch`` is logged with both versions.
* Match -> return (examples_artifact, "ok"). Caller injects the
  examples into the prompt.

This is a deliberate departure from "fail-closed by default": few-shot
prompts are an optimization, not a correctness gate. Production
extraction must keep running if the seed is missing or out-of-date.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import jsonschema

from ...ingestion._paths import contracts_root

logger = logging.getLogger(__name__)

SCHEMA_FILE = "few_shot_examples.schema.json"
SEED_FILENAME = "extraction_few_shot_v1.json"


def _load_schema() -> Optional[Dict[str, Any]]:
    schema_path = contracts_root() / "schemas" / "eval" / SCHEMA_FILE
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_seed_path(
    data_lake_path: Optional[str], explicit_path: Optional[str]
) -> Path:
    if explicit_path:
        return Path(explicit_path)
    env = os.environ.get("SPECTRUM_FEW_SHOT_PATH", "").strip()
    if env:
        return Path(env)
    # Prefer the data lake location if provided. The seed itself can
    # live in either ``$SDL_ROOT/few_shot/<filename>`` (operator-managed)
    # or in the repo at ``contracts/eval/seeds/<filename>`` (the
    # default hand-authored seed). Data lake wins.
    if data_lake_path:
        candidate = (
            Path(data_lake_path) / "store" / "artifacts" / "few_shot" / SEED_FILENAME
        )
        if candidate.is_file():
            return candidate
    # RT1 finding: the shipped seed lives at contracts/eval/seeds/, not
    # at <repo>/eval/seeds/. Earlier resolution used .parent which
    # pointed outside the contracts tree.
    return contracts_root() / "eval" / "seeds" / SEED_FILENAME


class FewShotLoader:
    """Load + validate + version-gate the few-shot seed artifact."""

    def load(
        self,
        prompt_schema_version: str,
        *,
        data_lake_path: Optional[str] = None,
        seed_path: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Return ``(artifact, status)``.

        Status values: ``ok``, ``missing``, ``unreadable``,
        ``schema_invalid``, ``version_mismatch``.
        """
        path = _resolve_seed_path(data_lake_path, seed_path)
        if not path.is_file():
            logger.warning("few_shot_missing path=%s", path)
            return None, "missing"

        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("few_shot_unreadable path=%s err=%s", path, exc)
            return None, "unreadable"

        schema = _load_schema()
        if schema is not None:
            try:
                jsonschema.Draft202012Validator(schema).validate(artifact)
            except jsonschema.ValidationError as exc:
                logger.warning(
                    "few_shot_schema_invalid path=%s err=%s",
                    path,
                    exc.message,
                )
                return None, "schema_invalid"

        artifact_version = artifact.get("prompt_schema_version")
        if artifact_version != prompt_schema_version:
            logger.warning(
                "few_shot_version_mismatch "
                "artifact_version=%s current_version=%s path=%s",
                artifact_version,
                prompt_schema_version,
                path,
            )
            return None, "version_mismatch"

        return artifact, "ok"


def load_few_shot_examples(
    prompt_schema_version: str,
    *,
    data_lake_path: Optional[str] = None,
    seed_path: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Convenience function. Equivalent to ``FewShotLoader().load(...)``."""
    return FewShotLoader().load(
        prompt_schema_version,
        data_lake_path=data_lake_path,
        seed_path=seed_path,
    )


def format_examples_for_prompt(
    artifact: Dict[str, Any], example_type: Optional[str] = None
) -> str:
    """Render a few_shot_examples artifact as a prompt-ready string.

    Callers (extraction prompts when LLM extraction lands) inject the
    returned block BEFORE the output schema section so the model sees
    examples first, schema second.

    ``example_type`` filters the examples to one type (decision /
    action_item / claim). ``None`` returns every example.
    """
    if not isinstance(artifact, dict):
        return ""
    examples = artifact.get("examples") or []
    if not isinstance(examples, list):
        return ""

    lines = ["Here are examples of valid extraction:"]
    for i, ex in enumerate(examples, 1):
        if not isinstance(ex, dict):
            continue
        if example_type and ex.get("example_type") != example_type:
            continue
        lines.append(f"--- example {i} ({ex.get('example_type', '')}) ---")
        lines.append(f"INPUT CHUNK:\n{ex.get('input_chunk_text', '')}")
        speaker = ex.get("speaker", "")
        if speaker:
            lines.append(f"SPEAKER: {speaker}")
        expected = ex.get("expected_output", {})
        import json as _json
        lines.append(
            "EXPECTED OUTPUT:\n"
            + _json.dumps(expected, indent=2, sort_keys=True)
        )
        rationale = ex.get("rationale", "")
        if rationale:
            lines.append(f"WHY: {rationale}")
    lines.append("--- end examples ---")
    return "\n".join(lines)
