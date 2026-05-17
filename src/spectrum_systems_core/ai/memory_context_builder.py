"""MemoryContextBuilder: assemble the governed context bundle for an AI query.

Reuses Phase F's RetrievalRegistry, BundleAssembler, and BundleEval. The
recipe is selected from the prompt registry's task definition. EVAL-CTX-002
(promoted_only_enforced) blocks any candidate artifact before any AI call
is ever made (FINDING-H-002).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ..synthesis import BundleAssembler, BundleEval, RetrievalRegistry
from .prompt_registry import PromptRegistry

_AI_BUNDLE_AUDIENCE = "technical"
_AI_BUNDLE_PURPOSE = "report"
_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4


def _build_context_text(items, char_budget: int) -> str:
    parts = []
    running = 0
    for item in items:
        artifact_type = item.get("artifact_type", "?")
        artifact_id = item.get("artifact_id", "")
        excerpt = item.get("content_excerpt", "")
        chunk = (
            f"{artifact_type} [source: {artifact_id}]:\n{excerpt}\n"
        )
        if running + len(chunk) > char_budget and parts:
            break
        parts.append(chunk)
        running += len(chunk)
    return "\n---\n".join(parts)


class MemoryContextBuilder:
    """Assemble a governed context bundle for one AI query."""

    def build(
        self,
        task_type: str,
        question: str,
        repo_root: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            task_def = PromptRegistry().get(task_type, repo_root=repo_root)
        except ValueError as exc:
            return {
                "status": "failure",
                "bundle": None,
                "context_text": "",
                "reason": f"unregistered_task_type: {exc}",
            }

        recipe_id = task_def["recipe_id"]
        try:
            RetrievalRegistry().get_recipe(recipe_id)
        except KeyError as exc:
            return {
                "status": "failure",
                "bundle": None,
                "context_text": "",
                "reason": f"unknown_recipe: {exc}",
            }

        ephemeral_run_id = run_id or str(uuid.uuid4())
        bundle_result = BundleAssembler().assemble(
            ephemeral_run_id,
            recipe_id,
            _AI_BUNDLE_AUDIENCE,
            _AI_BUNDLE_PURPOSE,
            str(Path(repo_root).resolve()),
        )
        if bundle_result["status"] != "success":
            return {
                "status": "failure",
                "bundle": None,
                "context_text": "",
                "reason": (
                    f"bundle_assembly_{bundle_result['status']}: "
                    f"{bundle_result.get('reason', '')}"
                ),
            }
        bundle = bundle_result["bundle"]

        bundle_eval = BundleEval().run(bundle)
        if bundle_eval["decision"] != "allow":
            return {
                "status": "blocked",
                "bundle": bundle,
                "context_text": "",
                "reason": (
                    "bundle_eval_blocked: "
                    + ", ".join(bundle_eval.get("reason_codes", []))
                ),
            }

        char_budget = (
            int(task_def.get("max_bundle_tokens", 4000))
            * _TOKEN_ESTIMATE_CHARS_PER_TOKEN
        )
        context_text = _build_context_text(bundle.get("items", []), char_budget)

        return {
            "status": "success",
            "bundle": bundle,
            "context_text": context_text,
            "reason": "",
        }
