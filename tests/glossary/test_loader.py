"""Phase 2P glossary loader tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from spectrum_systems_core.glossary.loader import (
    ARTIFACT_TYPE,
    GLOSSARY_SCHEMA_VERSION,
    Glossary,
    GlossaryEntry,
    GlossaryError,
    build_chunk_context,
    compute_allowed_sources_hash,
    compute_glossary_hash,
    format_terminology_block,
    load_glossary,
    validate_entry,
)


ALLOWED = ["NTIA Manual", "47 CFR", "ITU-R", "3GPP", "NIST", "IEEE", "ANSI", "NTIA TR"]


def _entry(**overrides) -> dict:
    base = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "term": "FOO",
        "aliases": [],
        "definition": "an example term.",
        "authoritative_source": "47 CFR 2.1",
        "category": "spectrum_service_class",
        "created_at": "2026-05-20T00:00:00Z",
        "is_acronym": True,
        "disambiguation_required": False,
        "co_occurring_terms": [],
        "priority_weight": 1.0,
    }
    base.update(overrides)
    return base


def _write_glossary_dir(
    tmp: Path,
    entries: list[dict],
    *,
    glossary_hash: str | None = None,
    allowed_hash: str | None = None,
    allowed: list[str] | None = None,
) -> Path:
    glossary_dir = tmp / "glossary"
    glossary_dir.mkdir()
    glossary_path = glossary_dir / "ntia_dod_spectrum_v1.jsonl"
    lines = [
        json.dumps(e, sort_keys=True, separators=(",", ":")) for e in entries
    ]
    glossary_path.write_text("\n".join(lines), encoding="utf-8")
    if allowed is None:
        allowed = list(ALLOWED)
    actual_allowed_hash = compute_allowed_sources_hash(allowed)
    allowed_path = glossary_dir / "allowed_sources.json"
    allowed_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "allowed_sources": allowed,
                "sha256_hash": allowed_hash or actual_allowed_hash,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "1.0.0",
        "version": "1.0.0",
        "glossary_file": "ntia_dod_spectrum_v1.jsonl",
        "sha256_hash": glossary_hash or compute_glossary_hash(entries),
        "allowed_sources_hash": allowed_hash or actual_allowed_hash,
        "entry_count": len(entries),
    }
    (glossary_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return glossary_dir


# ---------------- pure hash helpers ----------------


def test_allowed_sources_hash_is_canonical() -> None:
    h = compute_allowed_sources_hash(ALLOWED)
    expected = hashlib.sha256(
        json.dumps(ALLOWED, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert h == expected


def test_glossary_hash_is_canonical_and_order_sensitive() -> None:
    a = _entry(term="ALPHA")
    b = _entry(term="BETA")
    forward = compute_glossary_hash([a, b])
    backward = compute_glossary_hash([b, a])
    # Order is part of the byte layout, so reordering changes the hash.
    assert forward != backward


# ---------------- validate_entry ----------------


@pytest.mark.parametrize("modal", ["shall", "should", "may", "will", "would"])
def test_validate_rejects_modal_verb_as_term(modal: str) -> None:
    entry = _entry(term=modal)
    errors = validate_entry(entry, ALLOWED)
    assert any("modal_verb_as_term" in e for e in errors)


@pytest.mark.parametrize("modal", ["Shall", "SHOULD", "MAY"])
def test_validate_rejects_modal_verb_case_insensitive(modal: str) -> None:
    entry = _entry(term=modal)
    errors = validate_entry(entry, ALLOWED)
    assert any("modal_verb_as_term" in e for e in errors)


def test_validate_rejects_modal_verb_as_alias() -> None:
    entry = _entry(aliases=["shall"])
    errors = validate_entry(entry, ALLOWED)
    assert any("modal_verb_as_alias" in e for e in errors)


def test_validate_rejects_non_whitelist_source() -> None:
    entry = _entry(authoritative_source="random rule book")
    errors = validate_entry(entry, ALLOWED)
    assert any("authoritative_source_not_allowed" in e for e in errors)


def test_validate_accepts_whitelist_prefix_with_suffix() -> None:
    entry = _entry(authoritative_source="47 CFR 96")
    errors = validate_entry(entry, ALLOWED)
    assert not any("authoritative_source_not_allowed" in e for e in errors)


def test_validate_rejects_source_that_only_contains_prefix_substring() -> None:
    entry = _entry(authoritative_source="47 CFRevil")
    errors = validate_entry(entry, ALLOWED)
    assert any("authoritative_source_not_allowed" in e for e in errors)


def test_validate_priority_weight_range() -> None:
    assert any(
        "priority_weight_out_of_range" in e
        for e in validate_entry(_entry(priority_weight=0.05), ALLOWED)
    )
    assert any(
        "priority_weight_out_of_range" in e
        for e in validate_entry(_entry(priority_weight=10.1), ALLOWED)
    )


def test_validate_disambiguation_requires_co_occurring_terms() -> None:
    entry = _entry(disambiguation_required=True, co_occurring_terms=[])
    errors = validate_entry(entry, ALLOWED)
    assert any(
        "disambiguation_required_without_co_occurring_terms" in e
        for e in errors
    )


# ---------------- load_glossary ----------------


def test_load_glossary_happy_path(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(tmp_path, [_entry(term="CBRS")])
    glossary = load_glossary(
        glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
        manifest_path=glossary_dir / "MANIFEST.json",
    )
    assert glossary.version == "1.0.0"
    assert len(glossary.entries) == 1
    assert glossary.entries[0].term == "CBRS"


def test_load_glossary_missing_manifest(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(tmp_path, [_entry(term="CBRS")])
    (glossary_dir / "MANIFEST.json").unlink()
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_manifest_unreadable"


def test_load_glossary_malformed_manifest(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(tmp_path, [_entry(term="CBRS")])
    (glossary_dir / "MANIFEST.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_manifest_unreadable"


def test_load_glossary_hash_mismatch_on_glossary_file(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(tmp_path, [_entry(term="CBRS")])
    # Tamper one byte in the JSONL after the manifest was written.
    jsonl_path = glossary_dir / "ntia_dod_spectrum_v1.jsonl"
    tampered = jsonl_path.read_text(encoding="utf-8").replace(
        "an example term.", "a different example term."
    )
    jsonl_path.write_text(tampered, encoding="utf-8")
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=jsonl_path,
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_manifest_hash_mismatch"


def test_load_glossary_hash_mismatch_on_allowed_sources(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(tmp_path, [_entry(term="CBRS")])
    allowed_path = glossary_dir / "allowed_sources.json"
    doc = json.loads(allowed_path.read_text(encoding="utf-8"))
    doc["allowed_sources"].append("HACKED")  # tamper without updating hash
    allowed_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_allowed_sources_hash_mismatch"


def test_load_glossary_rejects_modal_verb_term(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(
        tmp_path, [_entry(term="shall", is_acronym=False)]
    )
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_entry_invalid"
    assert "modal_verb_as_term" in exc_info.value.detail


def test_load_glossary_rejects_duplicate_term(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(
        tmp_path, [_entry(term="DUP"), _entry(term="DUP")]
    )
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_duplicate_term"


def test_load_glossary_rejects_disambiguation_without_terms(tmp_path: Path) -> None:
    glossary_dir = _write_glossary_dir(
        tmp_path,
        [_entry(term="amb", disambiguation_required=True, co_occurring_terms=[])],
    )
    with pytest.raises(GlossaryError) as exc_info:
        load_glossary(
            glossary_path=glossary_dir / "ntia_dod_spectrum_v1.jsonl",
            manifest_path=glossary_dir / "MANIFEST.json",
        )
    assert exc_info.value.reason == "glossary_entry_invalid"
    assert "disambiguation_required_without_co_occurring_terms" in exc_info.value.detail


# ---------------- match ----------------


def _glossary_from(entries: list[GlossaryEntry], version_hash: str = "h") -> Glossary:
    return Glossary(entries=tuple(entries), version="1.0.0", version_hash=version_hash)


def _ge(
    term: str,
    *,
    aliases: tuple[str, ...] = (),
    is_acronym: bool = True,
    disambiguation_required: bool = False,
    co_occurring_terms: tuple[str, ...] = (),
    priority_weight: float = 1.0,
) -> GlossaryEntry:
    return GlossaryEntry(
        term=term,
        aliases=aliases,
        definition=f"def of {term}",
        authoritative_source="47 CFR 2.1",
        category="test",
        is_acronym=is_acronym,
        disambiguation_required=disambiguation_required,
        co_occurring_terms=co_occurring_terms,
        priority_weight=priority_weight,
    )


def test_match_alphanumeric_lookarounds() -> None:
    g = _glossary_from([_ge("CBRS")])
    for chunk in ["CBRS-PAL", "CBRS_certified", "(CBRS).", "see CBRS-2 today"]:
        matched, _ = g.match(chunk)
        assert any(e.term == "CBRS" for e in matched), chunk


def test_match_does_not_match_within_word() -> None:
    g = _glossary_from([_ge("MSS")])
    for chunk in ["missions", "commission", "dismissal", "MSSx"]:
        matched, _ = g.match(chunk)
        assert not matched, chunk


def test_match_acronym_is_case_sensitive() -> None:
    g = _glossary_from([_ge("MSS", is_acronym=True)])
    upper, _ = g.match("MSS in the band")
    lower, _ = g.match("mss in the band")
    assert any(e.term == "MSS" for e in upper)
    assert not lower


def test_match_non_acronym_is_case_insensitive() -> None:
    g = _glossary_from([_ge("allocation", is_acronym=False)])
    matched, _ = g.match("the Allocation rule")
    assert any(e.term == "allocation" for e in matched)


def test_disambiguation_required_blocks_match_without_co_term() -> None:
    g = _glossary_from(
        [
            _ge(
                "allocation",
                is_acronym=False,
                disambiguation_required=True,
                co_occurring_terms=("band", "frequency", "spectrum"),
            )
        ]
    )
    assert g.match("resource allocation strategy")[0] == []
    assert g.match("allocation")[0] == []
    matched, _ = g.match("spectrum allocation in this band")
    assert any(e.term == "allocation" for e in matched)


def test_match_alias_is_matched() -> None:
    g = _glossary_from(
        [
            _ge(
                "CBRS",
                aliases=("Citizens Broadband Radio Service",),
                is_acronym=False,
            )
        ]
    )
    matched, _ = g.match("the Citizens Broadband Radio Service rules")
    assert any(e.term == "CBRS" for e in matched)


def test_match_top_k_by_priority_weight() -> None:
    entries = [
        _ge(f"TERM{i:02d}", priority_weight=1.0 + i / 100) for i in range(10)
    ]
    g = _glossary_from(entries)
    chunk = " ".join(e.term for e in entries)
    matched, truncated = g.match(chunk, max_terms=3)
    assert len(matched) == 3
    # Top 3 priorities: TERM09 (1.09), TERM08 (1.08), TERM07 (1.07)
    assert [e.term for e in matched] == ["TERM09", "TERM08", "TERM07"]
    assert truncated == 7


def test_match_tiebreak_alphabetical() -> None:
    entries = [
        _ge("ZETA", priority_weight=1.0),
        _ge("ALPHA", priority_weight=1.0),
        _ge("BETA", priority_weight=1.0),
    ]
    g = _glossary_from(entries)
    matched, _ = g.match("see ALPHA BETA ZETA", max_terms=2)
    assert [e.term for e in matched] == ["ALPHA", "BETA"]


def test_match_handles_regex_metachars_in_term() -> None:
    # I/N contains '/' which is not a regex metacharacter, but a more
    # adversarial term such as 'C/I' or 'C++' must be ``re.escape``-d
    # before compilation. The matcher uses re.escape, so this works.
    g = _glossary_from([_ge("I/N", is_acronym=True)])
    matched, _ = g.match("the I/N is -6 dB")
    assert any(e.term == "I/N" for e in matched)


def test_match_empty_chunk() -> None:
    g = _glossary_from([_ge("FSS")])
    assert g.match("") == ([], 0)
    assert g.match("   ") == ([], 0)


def test_match_max_terms_zero() -> None:
    g = _glossary_from([_ge("FSS"), _ge("MSS")])
    matched, truncated = g.match("FSS and MSS appear", max_terms=0)
    assert matched == []
    assert truncated >= 0


# ---------------- terminology block ----------------


def test_terminology_block_includes_term_and_source() -> None:
    g = _glossary_from([_ge("CBRS")], version_hash="abc123")
    matched, _ = g.match("CBRS spectrum")
    block = format_terminology_block(matched, 0, version_hash="abc123")
    assert "CBRS:" in block
    assert "47 CFR" in block
    assert "abc123" in block


def test_terminology_block_renders_truncation_count() -> None:
    block = format_terminology_block(
        [_ge("CBRS")], truncated=5, version_hash="vv"
    )
    assert "5 additional terms truncated" in block


def test_terminology_block_empty_for_no_match() -> None:
    assert format_terminology_block([], 0) == ""


def test_build_chunk_context_no_glossary_returns_existing() -> None:
    out = build_chunk_context("CBRS", glossary=None, existing_block="prior")
    assert out == "prior"


def test_build_chunk_context_appends_block_when_matched() -> None:
    g = _glossary_from([_ge("CBRS")], version_hash="vhash")
    out = build_chunk_context("see CBRS rules", glossary=g, existing_block="prior")
    assert out.startswith("prior")
    assert "Terminology relevant to this section:" in out
    assert "CBRS:" in out


def test_build_chunk_context_no_block_when_no_match() -> None:
    g = _glossary_from([_ge("XYZ")], version_hash="vhash")
    out = build_chunk_context("totally unrelated", glossary=g, existing_block="prior")
    assert out == "prior"


# ---------------- Red Team Pass 2 mutation tests ----------------


@pytest.mark.parametrize(
    "chunk,expect_match",
    [
        ("MSS", True),
        ("MSS-2", True),
        ("MSS.", True),
        ("(MSS)", True),
        ("see MSS today", True),
        ("MSS_certified", True),
        ("missions", False),
        ("commissions", False),
        ("dismissal", False),
        ("MSSx", False),
        ("xMSS", False),
        ("1MSS", False),
        ("MSS9", False),
    ],
)
def test_regex_mutation_for_acronym(chunk: str, expect_match: bool) -> None:
    g = _glossary_from([_ge("MSS", is_acronym=True)])
    matched, _ = g.match(chunk)
    if expect_match:
        assert any(e.term == "MSS" for e in matched), chunk
    else:
        assert not matched, chunk


@pytest.mark.parametrize(
    "chunk,expect_match",
    [
        ("spectrum allocation in this band", True),
        ("resource allocation strategy", False),
        ("allocation", False),
        ("frequency allocation rules", True),
        ("the band allocation table", True),
    ],
)
def test_disambiguation_mutations(chunk: str, expect_match: bool) -> None:
    g = _glossary_from(
        [
            _ge(
                "allocation",
                is_acronym=False,
                disambiguation_required=True,
                co_occurring_terms=("band", "frequency", "spectrum"),
            )
        ]
    )
    matched, _ = g.match(chunk)
    if expect_match:
        assert any(e.term == "allocation" for e in matched), chunk
    else:
        assert not matched, chunk


def test_truncation_block_announces_correct_count() -> None:
    """End-to-end: 10 matches with distinct priorities -> top 3 + 7 truncated."""
    entries = [
        _ge(f"TERM{i:02d}", priority_weight=1.0 + i / 100) for i in range(10)
    ]
    g = _glossary_from(entries, version_hash="abcdef0123456789")
    chunk = " ".join(e.term for e in entries)
    matched, truncated = g.match(chunk, max_terms=3)
    block = format_terminology_block(matched, truncated, version_hash=g.version_hash)
    assert "7 additional terms truncated by priority" in block
    assert truncated == 7


def test_version_hash_in_block_distinguishes_versions() -> None:
    """A different version_hash produces different chunk-context bytes."""
    entries = [_ge("CBRS")]
    g1 = _glossary_from(entries, version_hash="aaaa1111bbbb2222")
    g2 = _glossary_from(entries, version_hash="cccc3333dddd4444")
    out1 = build_chunk_context("see CBRS", g1)
    out2 = build_chunk_context("see CBRS", g2)
    assert out1 != out2
