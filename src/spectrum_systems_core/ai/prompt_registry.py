"""PromptRegistry: the only source of AI prompts in the system (FINDING-H-001).

ai/registry/prompts.json defines a versioned task_type catalog. Each entry
declares model, temperature, token limits, recipe id, output schema, and
the prompt template. Templates must contain both {context} and {question}
placeholders. Temperature must be 0 (determinism). Unknown task_types
fail immediately — no fallback, no default. No code in the codebase may
construct a prompt string outside of this registry lookup.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._paths import ai_registry_path

_REQUIRED_FIELDS = (
    "version",
    "model",
    "temperature",
    "max_tokens",
    "max_bundle_tokens",
    "recipe_id",
    "output_schema",
    "prompt_template",
)


class PromptRegistry:
    """Read-only registry of versioned AI task types."""

    _registry: dict[str, Any] | None = None
    _registry_root: str | None = None

    def load(self, repo_root: str) -> None:
        """Load and validate ai/registry/prompts.json. Cached at class level."""
        repo_root_str = str(Path(repo_root).resolve())
        if (
            PromptRegistry._registry is not None
            and PromptRegistry._registry_root == repo_root_str
        ):
            return
        path = ai_registry_path(repo_root)
        if not path.is_file():
            raise FileNotFoundError(
                f"prompt_registry_missing: {path}"
            )
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"prompt_registry_unreadable: {exc}"
            ) from exc
        task_types = doc.get("task_types") or {}
        if not isinstance(task_types, dict) or not task_types:
            raise ValueError("prompt_registry_empty: no task_types defined")

        for name, task in task_types.items():
            for field in _REQUIRED_FIELDS:
                if field not in task:
                    raise ValueError(
                        f"prompt_registry_invalid: task '{name}' "
                        f"missing field '{field}'"
                    )
            template = task["prompt_template"]
            if "{context}" not in template:
                raise ValueError(
                    f"prompt_registry_invalid: task '{name}' template "
                    f"missing required {{context}} placeholder"
                )
            if "{question}" not in template:
                raise ValueError(
                    f"prompt_registry_invalid: task '{name}' template "
                    f"missing required {{question}} placeholder"
                )
            if task["temperature"] != 0:
                raise ValueError(
                    f"prompt_registry_invalid: task '{name}' temperature "
                    f"must be 0 (got {task['temperature']!r})"
                )

        PromptRegistry._registry = doc
        PromptRegistry._registry_root = repo_root_str

    def _ensure_loaded(self, repo_root: str | None) -> None:
        if PromptRegistry._registry is not None:
            return
        if not repo_root:
            raise ValueError(
                "prompt_registry_not_loaded: call load(repo_root) first"
            )
        self.load(repo_root)

    def get(self, task_type: str, repo_root: str | None = None) -> dict[str, Any]:
        """Return the task definition. Raise ValueError on unknown task_type."""
        self._ensure_loaded(repo_root)
        registry = PromptRegistry._registry or {}
        task_types = registry.get("task_types") or {}
        if task_type not in task_types:
            raise ValueError(f"unregistered_task_type: {task_type}")
        return dict(task_types[task_type])

    def list_task_types(self, repo_root: str | None = None) -> list[str]:
        self._ensure_loaded(repo_root)
        registry = PromptRegistry._registry or {}
        return sorted((registry.get("task_types") or {}).keys())

    def render_prompt(
        self,
        task_type: str,
        question: str,
        context: str,
        repo_root: str | None = None,
    ) -> str:
        """Render the prompt template — substitutes {question} and {context} only."""
        task = self.get(task_type, repo_root=repo_root)
        template = task["prompt_template"]
        return template.format_map({"question": question, "context": context})

    @classmethod
    def reset_cache(cls) -> None:
        """Test hook: drop the cached registry."""
        cls._registry = None
        cls._registry_root = None
