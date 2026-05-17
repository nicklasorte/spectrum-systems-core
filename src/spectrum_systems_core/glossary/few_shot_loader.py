"""Phase V.3: few-shot examples loader (target-distribution).

Loads ``store/artifacts/evals/few_shot/decision_examples_v1.json`` and
returns only the examples whose ``verified`` flag is true. Unverified
examples never appear in the prompt regardless of any global flag.

Behavioural contract:
- Missing artifact + FEW_SHOT_REQUIRED=false (default): return
  ``FewShotLoadResult(examples=[], finding=info)``; extraction proceeds.
- Missing artifact + FEW_SHOT_REQUIRED=true: ``finding`` is severity
  ``halt`` so the orchestrator can fail-closed.
- Present artifact with zero verified examples: ``finding`` is severity
  ``info`` with code ``few_shot_no_verified_examples``.
- Present artifact with one or more verified examples: ``finding`` is
  None.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FEW_SHOT_ARTIFACT_FILENAME: str = "decision_examples_v1.json"
# Phase V artifact_type. Distinct from legacy ``few_shot_examples``
# (contracts/schemas/eval/) used by ``evals.m4.few_shot.FewShotLoader``
# -- the two are independent.
FEW_SHOT_ARTIFACT_TYPE: str = "decision_few_shot_examples"
FEW_SHOT_SCHEMA_VERSION: str = "1.0.0"

_FEW_SHOT_REQUIRED_ENV: str = "FEW_SHOT_REQUIRED"

_LOG = logging.getLogger(__name__)


def _few_shot_required() -> bool:
    raw = os.environ.get(_FEW_SHOT_REQUIRED_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FewShotLoadResult:
    """The result of attempting to load few-shot examples.

    ``examples``: list of verified example dicts. Always a list (never
    None) so downstream prompt construction is unconditional.
    ``finding_code``: one of ``few_shot_artifact_missing`` or
    ``few_shot_no_verified_examples`` if a finding should be emitted;
    None when the load was successful with at least one verified
    example.
    ``severity``: severity for the finding when one is present;
    "halt" only when FEW_SHOT_REQUIRED=true and the artifact is
    missing.
    ``remediation``: operator-facing remediation string (non-empty
    when a finding is present).
    """

    examples: list[dict[str, Any]]
    finding_code: str | None = None
    severity: str | None = None
    remediation: str = ""
    artifact_present: bool = False


def _resolve_path(
    sdl_root: Path | str | None,
    *,
    explicit_path: Path | str | None = None,
) -> Path:
    if explicit_path is not None:
        return Path(explicit_path)
    env = os.environ.get("FEW_SHOT_ARTIFACT_PATH", "").strip()
    if env:
        return Path(env)
    root = Path(sdl_root) if sdl_root is not None else Path(".")
    return root / "evals" / "few_shot" / FEW_SHOT_ARTIFACT_FILENAME


def load_few_shot_examples(
    sdl_root: Path | str | None,
    *,
    explicit_path: Path | str | None = None,
) -> FewShotLoadResult:
    """Load the few-shot artifact and filter to verified examples.

    The function never raises on a missing or malformed file -- it
    produces a structured result the caller can route to a finding.
    """
    path = _resolve_path(sdl_root, explicit_path=explicit_path)
    if not path.is_file():
        if _few_shot_required():
            return FewShotLoadResult(
                examples=[],
                finding_code="few_shot_artifact_missing",
                severity="halt",
                remediation=(
                    "Create the few-shot artifact at "
                    f"{path} or unset FEW_SHOT_REQUIRED."
                ),
                artifact_present=False,
            )
        return FewShotLoadResult(
            examples=[],
            finding_code="few_shot_artifact_missing",
            severity="info",
            remediation=(
                "Place verified examples at "
                f"{path}; extraction continues with zero examples."
            ),
            artifact_present=False,
        )

    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("few_shot_load_failed: %s", exc)
        return FewShotLoadResult(
            examples=[],
            finding_code="few_shot_artifact_missing",
            severity="info",
            remediation=f"Re-author {path} as valid JSON; current parse failed.",
            artifact_present=False,
        )

    examples_raw = artifact.get("examples") if isinstance(artifact, dict) else None
    if not isinstance(examples_raw, list):
        return FewShotLoadResult(
            examples=[],
            finding_code="few_shot_no_verified_examples",
            severity="info",
            remediation=(
                "Add at least one example with verified: true to "
                f"{path}."
            ),
            artifact_present=True,
        )

    verified = [
        e for e in examples_raw
        if isinstance(e, dict) and e.get("verified") is True
    ]
    if not verified:
        return FewShotLoadResult(
            examples=[],
            finding_code="few_shot_no_verified_examples",
            severity="info",
            remediation=(
                "Set verified: true on examples in "
                f"{path} after manual review."
            ),
            artifact_present=True,
        )
    return FewShotLoadResult(
        examples=verified, artifact_present=True
    )


def build_few_shot_block(examples: list[dict[str, Any]]) -> str:
    """Render the FEW-SHOT EXAMPLES prompt block.

    Empty list -> empty string (no block).
    """
    if not examples:
        return ""
    lines: list[str] = [
        "FEW-SHOT EXAMPLES",
        "=" * 17,
        "The following are correct decision extractions from prior "
        "NTIA/FCC meetings. Use these as a guide for the schema and "
        "level of detail expected.",
        "",
    ]
    for idx, ex in enumerate(examples, start=1):
        src = ex.get("input_text", "")
        out = ex.get("expected_output", {})
        try:
            out_rendered = json.dumps(out, sort_keys=True)
        except TypeError:
            out_rendered = "{}"
        lines.append(f"Example {idx}:")
        lines.append(f"  Source: {src}")
        lines.append(f"  Extraction: {out_rendered}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "FEW_SHOT_ARTIFACT_FILENAME",
    "FEW_SHOT_ARTIFACT_TYPE",
    "FEW_SHOT_SCHEMA_VERSION",
    "FewShotLoadResult",
    "build_few_shot_block",
    "load_few_shot_examples",
]
