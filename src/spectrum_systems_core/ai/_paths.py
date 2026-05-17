"""Path helpers for Phase H AI artifacts.

ai/registry/prompts.json   — task-type -> prompt template registry
ai/queries/<query_id>.json — ai_query_record artifacts
ai/outputs/<output_id>.json — ai_output artifacts
ai/costs/<query_id>.json   — memory_query_cost_record artifacts
ai/costs/monthly.json      — cumulative monthly cost tracker
ai/failures/<failure_id>.json — ai_grounding_failure artifacts
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_AI_SCHEMAS = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "schemas"
    / "ai"
)


def ai_schema_path(name: str) -> Path:
    return _AI_SCHEMAS / f"{name}.schema.json"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads(ai_schema_path(name).read_text(encoding="utf-8"))


def ai_root(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / "ai"


def ai_registry_path(repo_root: str | Path) -> Path:
    return ai_root(repo_root) / "registry" / "prompts.json"


def ai_queries_dir(repo_root: str | Path, create: bool = False) -> Path:
    path = ai_root(repo_root) / "queries"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ai_outputs_dir(repo_root: str | Path, create: bool = False) -> Path:
    path = ai_root(repo_root) / "outputs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ai_costs_dir(repo_root: str | Path, create: bool = False) -> Path:
    path = ai_root(repo_root) / "costs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ai_failures_dir(repo_root: str | Path, create: bool = False) -> Path:
    path = ai_root(repo_root) / "failures"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ai_monthly_costs_path(repo_root: str | Path) -> Path:
    return ai_costs_dir(repo_root) / "monthly.json"
