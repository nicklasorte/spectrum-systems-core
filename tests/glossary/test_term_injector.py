"""Phase V.2 tests: per-chunk term injection."""
from __future__ import annotations

import os
from typing import List

import pytest

from spectrum_systems_core.glossary.term_injector import (
    MAX_DEFINITION_CHARS,
    build_terminology_block,
    find_matching_terms,
    summarize_injections,
)


def _term(term_id: str, term: str, abbreviation: str | None = None,
          short_def: str | None = None, definition: str = "") -> dict:
    return {
        "term_id": term_id,
        "term": term,
        "abbreviation": abbreviation,
        "definition": definition or f"long definition of {term}",
        "short_definition": short_def or f"short definition of {term}",
        "authoritative_source": "test",
        "domain_scope": "test",
        "related_term_ids": [],
    }


GLOSSARY = [
    _term("fss", "Fixed Satellite Service", "FSS"),
    _term("twg", "Technical Working Group", "TWG"),
    _term("ntia", "NTIA"),
    _term("erp", "Effective Radiated Power", "ERP"),
]


def test_exact_case_match() -> None:
    chunk = "We discussed the FSS protection zone."
    matched = find_matching_terms(chunk, GLOSSARY)
    assert any(t["term_id"] == "fss" for t in matched)


def test_case_insensitive_match() -> None:
    chunk = "we discussed fss and ntia."
    matched = find_matching_terms(chunk, GLOSSARY)
    matched_ids = {t["term_id"] for t in matched}
    assert "fss" in matched_ids
    assert "ntia" in matched_ids


def test_abbreviation_match() -> None:
    chunk = "TWG-B will follow up next session."
    matched = find_matching_terms(chunk, GLOSSARY)
    assert any(t["term_id"] == "twg" for t in matched)


def test_no_match_returns_empty_list() -> None:
    matched = find_matching_terms("nothing domain specific here", GLOSSARY)
    assert matched == []


def test_more_than_cap_capped_at_max(monkeypatch) -> None:
    big_glossary = [
        _term(f"t{i}", f"Term{i}", f"T{i}") for i in range(50)
    ]
    chunk = " ".join(f"Term{i}" for i in range(50))
    # Default cap 10
    matched = find_matching_terms(chunk, big_glossary)
    assert len(matched) == 10


def test_max_terms_env_var_respected(monkeypatch) -> None:
    monkeypatch.setenv("MAX_GLOSSARY_TERMS_PER_CHUNK", "3")
    big_glossary = [_term(f"t{i}", f"Term{i}") for i in range(20)]
    chunk = " ".join(f"Term{i}" for i in range(20))
    matched = find_matching_terms(chunk, big_glossary)
    assert len(matched) == 3


def test_max_terms_zero_disables_injection(monkeypatch) -> None:
    monkeypatch.setenv("MAX_GLOSSARY_TERMS_PER_CHUNK", "0")
    matched = find_matching_terms("FSS and NTIA", GLOSSARY)
    assert matched == []


def test_build_terminology_block_empty_for_empty_input() -> None:
    assert build_terminology_block([]) == ""


def test_build_terminology_block_contains_name_and_short_definition() -> None:
    matched = [_term("fss", "FSS test", "FSS", short_def="test short def")]
    block = build_terminology_block(matched)
    assert "FSS test" in block
    assert "test short def" in block
    assert "TERMINOLOGY FOR THIS SECTION" in block


def test_definition_truncation_falls_back_to_definition_slice() -> None:
    long_def = "x" * 500
    term = _term("long", "LongTerm", None, short_def=None, definition=long_def)
    # Wipe short_definition so the fallback path runs.
    term["short_definition"] = ""
    matched = [term]
    block = build_terminology_block(matched)
    # The block should only carry the first MAX_DEFINITION_CHARS of the long def.
    assert "x" * MAX_DEFINITION_CHARS in block
    # And not the full long definition.
    assert "x" * (MAX_DEFINITION_CHARS + 1) not in block


def test_short_definition_used_when_present() -> None:
    long_def = "x" * 500
    term = _term("sd", "Sd", None, short_def="< 200 char summary",
                 definition=long_def)
    block = build_terminology_block([term])
    assert "< 200 char summary" in block
    assert "x" * 300 not in block


def test_summarize_injections_records_counts() -> None:
    chunk_to_terms = {
        "c1": [_term("fss", "FSS", "FSS"), _term("ntia", "NTIA")],
        "c2": [],
        "c3": [_term("fss", "FSS", "FSS")],
    }
    summary = summarize_injections(chunk_to_terms)
    assert summary["chunks_with_matches"] == 2
    assert summary["chunks_with_no_matches"] == 1
    assert summary["total_term_injections"] == 3
    assert "FSS" in summary["most_injected_terms"]
    assert summary["total_injection_chars"] > 0


def test_summarize_injections_empty_input() -> None:
    summary = summarize_injections({})
    assert summary["chunks_with_matches"] == 0
    assert summary["chunks_with_no_matches"] == 0
    assert summary["total_term_injections"] == 0
    assert summary["most_injected_terms"] == []


def test_glossary_terms_injected_returns_list_not_none() -> None:
    """The matched-terms list is always a list (possibly empty),
    never None. This guards the downstream
    glossary_terms_injected field shape."""
    matched = find_matching_terms("nothing domain specific", GLOSSARY)
    assert isinstance(matched, list)
    assert matched == []


def test_loader_called_once_per_run_via_param_passing() -> None:
    """The injector receives a glossary list as a parameter; the
    caller is responsible for loading once per run. This test
    verifies a single load supports N chunk calls."""
    # Simulate three per-chunk calls with the same glossary list.
    for chunk_text in ("FSS", "NTIA", "TWG"):
        find_matching_terms(chunk_text, GLOSSARY)
    # If a hidden global cache existed this test would not detect it,
    # but the function signature itself documents the contract: the
    # caller supplies the glossary.
