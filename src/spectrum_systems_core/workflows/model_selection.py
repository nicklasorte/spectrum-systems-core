"""Phase 5 — single source of truth for the ``--model`` CLI flag.

Maps the four CLI tokens (``haiku`` / ``sonnet`` / ``sonnet-unconstrained``
/ ``opus``) to the (model_id, prompt_path, prompt_variant) tuple the
production CLI hands to ``governed_pipeline_run``.

The mapping lives here — NOT in ``cli.py`` — so a test can verify
exhaustiveness against the schema's ``prompt_variant`` enum and against
``cost_constants.json`` without dragging the whole CLI import graph in.

Model strings: the values below are the Phase 5 spec's nomenclature
(``claude-haiku-4-7`` / ``claude-sonnet-4-6`` / ``claude-opus-4-7``).
The on-disk model registry (``ai/registry/model_registry.json``) uses
its own concrete strings — including dated point releases such as
``claude-haiku-4-5-20251001`` — and a runtime call still goes through
that registry by default. The strings here are the OVERRIDE values the
CLI sets when an operator passes ``--model``; the rollback contract
flags them for operator verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from ..pipeline.governed_run import (
    PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET,
    PROMPT_VARIANT_OPUS_BASELINE,
    PROMPT_VARIANT_OPUS_PROMPT_WITH_SONNET,
    PROMPT_VARIANT_PRODUCTION_HAIKU,
)


# CLI tokens accepted by ``--model``. Order matters only for argparse
# help rendering and the four red-team coverage tests.
MODEL_TOKEN_HAIKU: str = "haiku"
MODEL_TOKEN_SONNET: str = "sonnet"
MODEL_TOKEN_SONNET_UNCONSTRAINED: str = "sonnet-unconstrained"
MODEL_TOKEN_OPUS: str = "opus"

ALL_MODEL_TOKENS: tuple[str, ...] = (
    MODEL_TOKEN_HAIKU,
    MODEL_TOKEN_SONNET,
    MODEL_TOKEN_SONNET_UNCONSTRAINED,
    MODEL_TOKEN_OPUS,
)

# Phase 5 model string map. Single source of truth — every CLI dispatch
# and every test references this dict. Two CLI tokens map to the SAME
# model string (apples-to-apples vs unconstrained-capability), discriminated
# by the prompt path / prompt_variant pair below.
MODEL_STRINGS: Dict[str, str] = {
    MODEL_TOKEN_HAIKU: "claude-haiku-4-7",
    MODEL_TOKEN_SONNET: "claude-sonnet-4-6",
    MODEL_TOKEN_SONNET_UNCONSTRAINED: "claude-sonnet-4-6",
    MODEL_TOKEN_OPUS: "claude-opus-4-7",
}


_PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
HAIKU_PROMPT_PATH: Path = _PROMPTS_DIR / "meeting_minutes_llm.md"
OPUS_PROMPT_PATH: Path = _PROMPTS_DIR / "meeting_minutes_opus.md"


class ModelSelectionError(ValueError):
    """Raised when the ``--model`` token cannot be resolved fail-closed.

    Carries ``reason_code`` so a caller pattern-matches a stable token
    rather than parsing a message string.
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True)
class ResolvedModelSelection:
    """One CLI ``--model`` token's resolution."""

    model_token: str
    model_id: str
    prompt_path: Path
    prompt_variant: str


def resolve_model_selection(model_token: str) -> ResolvedModelSelection:
    """Resolve ``--model <token>`` into (model_id, prompt_path, prompt_variant).

    Fail-closed branches:

    * Unknown token → ``unknown_model_token`` (argparse should catch
      this first; the redundant guard exists so a programmer-side bug
      that bypasses argparse fails loudly rather than silently picking
      a default).
    * ``sonnet-unconstrained`` requires the Phase 4a Opus prompt on
      disk. If the file is missing the resolver raises
      ``opus_prompt_not_found_for_sonnet_unconstrained`` — the run
      halts BEFORE any artifact / API call.
    """
    if model_token not in MODEL_STRINGS:
        raise ModelSelectionError(
            "unknown_model_token",
            f"--model must be one of {list(ALL_MODEL_TOKENS)}, got {model_token!r}",
        )

    if model_token == MODEL_TOKEN_HAIKU:
        return ResolvedModelSelection(
            model_token=MODEL_TOKEN_HAIKU,
            model_id=MODEL_STRINGS[MODEL_TOKEN_HAIKU],
            prompt_path=HAIKU_PROMPT_PATH,
            prompt_variant=PROMPT_VARIANT_PRODUCTION_HAIKU,
        )
    if model_token == MODEL_TOKEN_SONNET:
        # Apples-to-apples: same prompt as production Haiku, swapped
        # model. The F1 delta is purely a model-capability signal.
        return ResolvedModelSelection(
            model_token=MODEL_TOKEN_SONNET,
            model_id=MODEL_STRINGS[MODEL_TOKEN_SONNET],
            prompt_path=HAIKU_PROMPT_PATH,
            prompt_variant=PROMPT_VARIANT_HAIKU_PROMPT_WITH_SONNET,
        )
    if model_token == MODEL_TOKEN_SONNET_UNCONSTRAINED:
        if not OPUS_PROMPT_PATH.exists():
            raise ModelSelectionError(
                "opus_prompt_not_found_for_sonnet_unconstrained",
                "--model sonnet-unconstrained requires the Opus prompt at "
                f"{OPUS_PROMPT_PATH}. Phase 4a creates this prompt; ensure "
                "the Phase 4a PR has merged before running this variant.",
            )
        return ResolvedModelSelection(
            model_token=MODEL_TOKEN_SONNET_UNCONSTRAINED,
            model_id=MODEL_STRINGS[MODEL_TOKEN_SONNET_UNCONSTRAINED],
            prompt_path=OPUS_PROMPT_PATH,
            prompt_variant=PROMPT_VARIANT_OPUS_PROMPT_WITH_SONNET,
        )
    if model_token == MODEL_TOKEN_OPUS:
        if not OPUS_PROMPT_PATH.exists():
            raise ModelSelectionError(
                "opus_prompt_not_found",
                "--model opus requires the Opus prompt at "
                f"{OPUS_PROMPT_PATH}. Phase 4a creates this prompt.",
            )
        return ResolvedModelSelection(
            model_token=MODEL_TOKEN_OPUS,
            model_id=MODEL_STRINGS[MODEL_TOKEN_OPUS],
            prompt_path=OPUS_PROMPT_PATH,
            prompt_variant=PROMPT_VARIANT_OPUS_BASELINE,
        )

    # Unreachable — kept for safety; the lookup above is exhaustive.
    raise ModelSelectionError(
        "unknown_model_token",
        f"unhandled model_token={model_token!r}",
    )


def read_prompt(path: Path) -> str:
    """Read and return the prompt content. HALT on read failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelSelectionError(
            "prompt_read_failed",
            f"cannot read prompt at {path}: {exc}",
        ) from exc
    if not text.strip():
        raise ModelSelectionError(
            "prompt_empty",
            f"prompt at {path} is empty",
        )
    return text


__all__ = [
    "ALL_MODEL_TOKENS",
    "HAIKU_PROMPT_PATH",
    "MODEL_STRINGS",
    "MODEL_TOKEN_HAIKU",
    "MODEL_TOKEN_OPUS",
    "MODEL_TOKEN_SONNET",
    "MODEL_TOKEN_SONNET_UNCONSTRAINED",
    "ModelSelectionError",
    "OPUS_PROMPT_PATH",
    "ResolvedModelSelection",
    "read_prompt",
    "resolve_model_selection",
]
