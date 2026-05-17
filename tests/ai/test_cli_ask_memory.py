"""CLI tests for ask-memory (RT3-005, RT4-003, RT5-003)."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

from spectrum_systems_core import cli as cli_module
from spectrum_systems_core.ai.adapter import AIAdapter
from spectrum_systems_core.ai.prompt_registry import PromptRegistry

from ._fixtures import (
    CountingAPICaller,
    FakeDataLakeChecker,
    load_fixture,
    seed_promoted_artifacts,
    setup_phase_h_repo,
)


@contextmanager
def _chdir(path):
    prev = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(prev)


def test_ask_memory_unknown_task_fails_before_api(tmp_path, capsys, monkeypatch):
    """RT4-003: unknown task -> exit 1, zero API calls, no output written."""
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path))
    store_root = tmp_path / "store"
    setup_phase_h_repo(store_root)
    PromptRegistry().load(str(store_root))

    with _chdir(store_root):
        rc = cli_module.main([
            "ask-memory",
            "--task", "totally_made_up",
            "--question", "What is X?",
        ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "unregistered_task_type" in (captured.err + captured.out)
    # No outputs or queries should be written for an unknown task.
    outputs = list((store_root / "ai" / "outputs").glob("*.json"))
    assert outputs == []


def test_ask_memory_advisory_banner_bookends(tmp_path, capsys, monkeypatch):
    """RT3-005 / RT5-003: advisory banner bookends the answer."""
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path))
    store_root = tmp_path / "store"
    setup_phase_h_repo(store_root)
    PromptRegistry().load(str(store_root))
    fixture = load_fixture("memory_query")
    primary_id = fixture["expected_citations"][0]
    seeds = seed_promoted_artifacts(store_root, story_id=primary_id)

    api = CountingAPICaller(fixture["canonical_response"])
    checker = FakeDataLakeChecker(fixture["mock_data_lake_exists"])
    checker.add_many(
        seeds["story_ids"] + seeds["claim_ids"] + seeds["theme_ids"]
    )

    real_init = AIAdapter.__init__

    def patched_init(self, api_caller=None, data_lake_checker=None):
        real_init(self, api_caller=api, data_lake_checker=checker)

    monkeypatch.setattr(AIAdapter, "__init__", patched_init)

    with _chdir(store_root):
        rc = cli_module.main([
            "ask-memory",
            "--task", fixture["task_type"],
            "--question", fixture["mock_question"],
        ])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    out = captured.out
    banner = "AI output is advisory only"
    assert out.count(banner) >= 2, "banner must appear at least twice (bookend)"
    # First banner must come before the answer.
    answer_pos = out.find("Question:")
    first_banner_pos = out.find(banner)
    assert 0 <= first_banner_pos < answer_pos


def test_ask_memory_writes_outputs_and_queries(tmp_path, capsys, monkeypatch):
    """RT4-004: ai/queries and ai/outputs populated for a successful query."""
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path))
    store_root = tmp_path / "store"
    setup_phase_h_repo(store_root)
    PromptRegistry().load(str(store_root))
    fixture = load_fixture("memory_query")
    primary_id = fixture["expected_citations"][0]
    seeds = seed_promoted_artifacts(store_root, story_id=primary_id)

    api = CountingAPICaller(fixture["canonical_response"])
    checker = FakeDataLakeChecker(fixture["mock_data_lake_exists"])
    checker.add_many(
        seeds["story_ids"] + seeds["claim_ids"] + seeds["theme_ids"]
    )
    real_init = AIAdapter.__init__
    monkeypatch.setattr(
        AIAdapter,
        "__init__",
        lambda self, api_caller=None, data_lake_checker=None: real_init(
            self, api_caller=api, data_lake_checker=checker
        ),
    )

    with _chdir(store_root):
        rc = cli_module.main([
            "ask-memory",
            "--task", fixture["task_type"],
            "--question", fixture["mock_question"],
        ])
    assert rc == 0
    queries = list((store_root / "ai" / "queries").glob("*.json"))
    outputs = list((store_root / "ai" / "outputs").glob("*.json"))
    assert len(queries) == 1
    assert len(outputs) == 1
    out_doc = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert out_doc["ai_advisory"] is True


def test_ask_memory_obsidian_projection_advisory(tmp_path, capsys, monkeypatch):
    """RT3-006: vault projection contains advisory warning in first 5 lines."""
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path))
    store_root = tmp_path / "store"
    setup_phase_h_repo(store_root)
    PromptRegistry().load(str(store_root))
    fixture = load_fixture("memory_query")
    primary_id = fixture["expected_citations"][0]
    seeds = seed_promoted_artifacts(store_root, story_id=primary_id)
    vault = tmp_path / "vault"
    vault.mkdir()

    api = CountingAPICaller(fixture["canonical_response"])
    checker = FakeDataLakeChecker(fixture["mock_data_lake_exists"])
    checker.add_many(
        seeds["story_ids"] + seeds["claim_ids"] + seeds["theme_ids"]
    )
    real_init = AIAdapter.__init__
    monkeypatch.setattr(
        AIAdapter,
        "__init__",
        lambda self, api_caller=None, data_lake_checker=None: real_init(
            self, api_caller=api, data_lake_checker=checker
        ),
    )

    with _chdir(store_root):
        rc = cli_module.main([
            "ask-memory",
            "--task", fixture["task_type"],
            "--question", fixture["mock_question"],
            "--vault", str(vault),
        ])
    assert rc == 0
    files = list((vault / "AI").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:15])
    assert "advisory" in head.lower()
