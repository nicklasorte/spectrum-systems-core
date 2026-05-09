"""Shared helpers for Phase H AI tests.

Every test seeds a temp repo with a copy of ai/registry/prompts.json,
the ai/ directory tree, the contracts/ tree, and at least 3 promoted
artifacts so that BundleEval clears MIN_BUNDLE_ITEMS. All tests mock
the Anthropic API — zero live calls (FINDING-H-004).
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tests.synthesis._fixtures import (
    write_evidenced_claim,
    write_promoted_story,
    write_promoted_theme,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "ai"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_phase_h_repo(temp_root: Path) -> Path:
    """Copy ai/registry, contracts schemas, and seed required dirs."""
    temp_root = Path(temp_root).resolve()
    temp_root.mkdir(parents=True, exist_ok=True)

    # Copy contracts/ wholesale (schemas + evals).
    src_contracts = REPO_ROOT / "contracts"
    dst_contracts = temp_root / "contracts"
    if dst_contracts.exists():
        shutil.rmtree(dst_contracts)
    shutil.copytree(src_contracts, dst_contracts)

    # Copy ai/registry/prompts.json.
    src_registry = REPO_ROOT / "ai" / "registry" / "prompts.json"
    dst_registry = temp_root / "ai" / "registry" / "prompts.json"
    dst_registry.parent.mkdir(parents=True, exist_ok=True)
    dst_registry.write_text(src_registry.read_text(encoding="utf-8"), encoding="utf-8")

    # Reset the PromptRegistry class cache so each test gets a fresh load.
    from spectrum_systems_core.ai.prompt_registry import PromptRegistry

    PromptRegistry.reset_cache()
    return temp_root


def seed_promoted_artifacts(
    temp_root: Path,
    story_id: Optional[str] = None,
    extra_story_ids: Optional[List[str]] = None,
    extra_claim_ids: Optional[List[str]] = None,
    extra_theme_ids: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """Create promoted/evidenced artifacts so the bundle has >=3 items."""
    temp_root = Path(temp_root).resolve()
    story_ids: List[str] = []
    claim_ids: List[str] = []
    theme_ids: List[str] = []

    if story_id is None:
        story_id = str(uuid.uuid4())
    sid = write_promoted_story(temp_root, story_id=story_id)
    story_ids.append(sid)

    for extra in extra_story_ids or []:
        story_ids.append(write_promoted_story(temp_root, story_id=extra))
    for extra in extra_claim_ids or []:
        claim_ids.append(write_evidenced_claim(temp_root, claim_id=extra))
    for extra in extra_theme_ids or []:
        theme_ids.append(write_promoted_theme(temp_root, theme_id=extra))

    # Always provide at least 3 total items: pad with a claim and a theme.
    if not claim_ids:
        claim_ids.append(write_evidenced_claim(temp_root))
    if not theme_ids:
        theme_ids.append(write_promoted_theme(temp_root))

    return {
        "story_ids": story_ids,
        "claim_ids": claim_ids,
        "theme_ids": theme_ids,
    }


class FakeDataLakeChecker:
    """Stand-in for synthesis.DataLakeChecker that uses a hardcoded set."""

    def __init__(self, known_ids: Optional[Dict[str, bool]] = None):
        self._known = dict(known_ids or {})

    def add(self, artifact_id: str, exists: bool = True) -> None:
        self._known[artifact_id] = exists

    def add_many(self, ids: List[str]) -> None:
        for aid in ids:
            self._known[aid] = True

    def exists(self, artifact_id: str) -> bool:
        return bool(self._known.get(artifact_id, False))


def load_fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


class CountingAPICaller:
    """Mock api_caller — returns canonical_response and counts calls."""

    def __init__(
        self,
        response_text: str,
        input_tokens: int = 100,
        output_tokens: int = 50,
    ):
        self.response_text = response_text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: List[Tuple[Dict[str, Any], str]] = []

    def __call__(
        self, task_def: Dict[str, Any], prompt: str
    ) -> Tuple[str, int, int]:
        self.calls.append((task_def, prompt))
        return self.response_text, self.input_tokens, self.output_tokens

    @property
    def call_count(self) -> int:
        return len(self.calls)
