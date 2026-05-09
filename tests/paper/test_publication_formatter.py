"""Phase J: PublicationFormatter tests."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest

from spectrum_systems_core.paper.publication_formatter import (
    PublicationFormatter,
)


_FAMILY = "working_papers"
_METADATA_TITLE = "A Working Paper on Spectrum Coordination"
_METADATA_AUTHORS = ["Alice Researcher", "Bob Engineer"]
_METADATA_ABSTRACT = (
    "This paper examines the governance properties required for "
    "deterministic spectrum coordination across federated systems "
    "with heterogeneous trust assumptions."
)


def _formatted_schema() -> Dict[str, Any]:
    return json.loads(
        Path("contracts/schemas/paper/formatted_paper_artifact.schema.json")
        .read_text(encoding="utf-8")
    )


def _write_revised_draft(
    repo_root: Path,
    *,
    source_id: str,
    revised_sections: Dict[str, str],
    applied_instruction_ids: List[str] | None = None,
    schema_version: str = "1.0.0",
    omit_source_id: bool = False,
) -> Path:
    paper_dir = repo_root / "processed" / _FAMILY / source_id / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at": "2026-05-09T00:00:00+00:00",
        "revised_sections": revised_sections,
        "applied_instruction_ids": applied_instruction_ids or [],
    }
    if not omit_source_id:
        payload["source_id"] = source_id
    target = paper_dir / "revised_draft.json"
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _write_paper_metadata(
    repo_root: Path,
    *,
    source_id: str,
    title: str = _METADATA_TITLE,
    authors: List[str] | None = None,
    abstract: str = _METADATA_ABSTRACT,
) -> Path:
    paper_dir = repo_root / "processed" / _FAMILY / source_id / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    target = paper_dir / "paper_metadata.json"
    target.write_text(
        json.dumps(
            {
                "title": title,
                "authors": authors or list(_METADATA_AUTHORS),
                "abstract": abstract,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _section_with_citations(
    source_artifact_ids: List[str],
    body_template: str,
) -> str:
    markers = "".join(f"[source: {sid}]" for sid in source_artifact_ids)
    return body_template + " " + markers


def _setup_basic_paper(
    repo_root: Path,
    *,
    source_id: str | None = None,
    sources: List[str] | None = None,
) -> tuple[str, List[str]]:
    if source_id is None:
        source_id = str(uuid.uuid4())
    if sources is None:
        sources = [str(uuid.uuid4()), str(uuid.uuid4())]
    sec_1_id = "section-introduction"
    sec_2_id = "section-methodology"
    sections = {
        sec_1_id: (
            "Introduction body discussing the problem space "
            "with sufficient depth to satisfy the eval. "
            f"[source: {sources[0]}]"
        ),
        sec_2_id: (
            "Methodology body laying out the deterministic procedure "
            "for the evaluation harness with concrete steps. "
            f"[source: {sources[1]}] [source: {sources[0]}]"
        ),
    }
    _write_revised_draft(
        repo_root,
        source_id=source_id,
        revised_sections=sections,
        applied_instruction_ids=[str(uuid.uuid4())],
    )
    _write_paper_metadata(repo_root, source_id=source_id)
    return source_id, sources


def test_revised_draft_to_formatted_artifact(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    artifact = result["artifact"]
    assert artifact["source_revised_draft_id"] == source_id
    assert artifact["title"] == _METADATA_TITLE
    assert artifact["authors"] == list(_METADATA_AUTHORS)
    assert artifact["abstract"] == _METADATA_ABSTRACT
    assert len(artifact["sections"]) == 2
    assert artifact["provenance"]["produced_by"] == "PublicationFormatter"
    assert artifact["provenance"]["formatter_version"] == "1.0.0"
    written = Path(result["artifact_path"])
    assert written.is_file()


def test_citation_marker_to_numbered_reference(tmp_path: Path) -> None:
    source_id, sources = _setup_basic_paper(tmp_path)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    artifact = result["artifact"]
    bodies = " ".join(s["body"] for s in artifact["sections"])
    for src_id in sources:
        assert f"[source: {src_id}]" not in bodies
    assert "[ref-1]" in bodies and "[ref-2]" in bodies
    assert all(
        re.match(r"^cite-\d+$", c["citation_id"])
        for c in artifact["citations"]
    )


def test_citation_ordering_by_first_occurrence(tmp_path: Path) -> None:
    source_id = str(uuid.uuid4())
    src_a = str(uuid.uuid4())
    src_b = str(uuid.uuid4())
    sections = {
        "section-a-introduction": (
            "Body of section one mentioning a primary source. "
            f"[source: {src_a}]"
        ),
        "section-b-methodology": (
            "Body of section two mentioning a different source first, "
            "then the original source again. "
            f"[source: {src_b}] [source: {src_a}]"
        ),
    }
    _write_revised_draft(
        tmp_path, source_id=source_id, revised_sections=sections
    )
    _write_paper_metadata(tmp_path, source_id=source_id)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    references = result["artifact"]["references"]
    refs_by_source = {r["source_artifact_id"]: r["ref_id"] for r in references}
    assert refs_by_source[src_a] == "ref-1"
    assert refs_by_source[src_b] == "ref-2"


def test_unique_references_only(tmp_path: Path) -> None:
    source_id = str(uuid.uuid4())
    src_a = str(uuid.uuid4())
    sections = {
        "section-only": (
            "A section that cites the same source three times to "
            "test deduplication of the references list. "
            f"[source: {src_a}] middle [source: {src_a}] end "
            f"[source: {src_a}]"
        ),
    }
    _write_revised_draft(
        tmp_path, source_id=source_id, revised_sections=sections
    )
    _write_paper_metadata(tmp_path, source_id=source_id)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    artifact = result["artifact"]
    assert len(artifact["citations"]) == 3
    assert len(artifact["references"]) == 1
    assert artifact["references"][0]["source_artifact_id"] == src_a


def test_missing_revised_draft_returns_failure_not_exception(
    tmp_path: Path,
) -> None:
    bogus_id = str(uuid.uuid4())
    result = PublicationFormatter().format(
        revised_draft_id=bogus_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "failure"
    assert result["artifact"] is None
    assert result["reason"].startswith("revised_draft_not_found:")
    assert bogus_id in result["reason"]


def test_unreadable_paper_metadata_returns_blocked(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    metadata_path = (
        tmp_path / "processed" / _FAMILY / source_id / "paper" / "paper_metadata.json"
    )
    metadata_path.write_text("{not valid json", encoding="utf-8")
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "blocked"
    assert result["reason"].startswith("paper_metadata_unreadable:")
    assert source_id in result["reason"]


def test_source_id_mismatch_returns_blocked(tmp_path: Path) -> None:
    requested_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    paper_dir = tmp_path / "processed" / _FAMILY / requested_id / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "revised_draft.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "source_id": other_id,
                "generated_at": "2026-05-09T00:00:00+00:00",
                "revised_sections": {"section-x": "Body content for the section."},
                "applied_instruction_ids": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _write_paper_metadata(tmp_path, source_id=requested_id)
    result = PublicationFormatter().format(
        revised_draft_id=requested_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "blocked"
    assert result["reason"].startswith(f"source_id_mismatch:{requested_id}:")
    assert other_id in result["reason"]


def test_invalid_input_schema_returns_blocked(tmp_path: Path) -> None:
    source_id = str(uuid.uuid4())
    _write_revised_draft(
        tmp_path,
        source_id=source_id,
        revised_sections={"section-x": "Body."},
        omit_source_id=True,
    )
    _write_paper_metadata(tmp_path, source_id=source_id)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "blocked"
    assert result["reason"].startswith("input_schema_invalid:")
    assert source_id in result["reason"]


def test_output_schema_validates(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    schema = _formatted_schema()
    jsonschema.Draft202012Validator(schema).validate(result["artifact"])


def test_status_ready_for_certification(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    assert (
        result["artifact"]["publication_metadata"]["status"]
        == "ready_for_certification"
    )


def test_deterministic_content_hash_across_two_runs(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    formatter = PublicationFormatter()
    first = formatter.format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    second = formatter.format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert first["status"] == "success" and second["status"] == "success"
    assert first["artifact"]["content_hash"] == second["artifact"]["content_hash"]
    assert first["artifact"]["paper_id"] != second["artifact"]["paper_id"]


def test_content_hash_excludes_timestamps_and_paper_id(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    formatter = PublicationFormatter()
    a = formatter.format(revised_draft_id=source_id, repo_root=str(tmp_path))
    b = formatter.format(revised_draft_id=source_id, repo_root=str(tmp_path))
    assert a["status"] == "success" and b["status"] == "success"
    assert a["artifact"]["paper_id"] != b["artifact"]["paper_id"]
    assert a["artifact"]["content_hash"] == b["artifact"]["content_hash"]


def test_empty_tables_and_figures_not_invented(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    result = PublicationFormatter().format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result
    artifact = result["artifact"]
    assert artifact["tables"] == []
    assert artifact["figures"] == []


def test_view_only_banner_is_first_line(tmp_path: Path) -> None:
    source_id, _sources = _setup_basic_paper(tmp_path)
    formatter = PublicationFormatter()
    result = formatter.format(
        revised_draft_id=source_id, repo_root=str(tmp_path)
    )
    assert result["status"] == "success", result

    from spectrum_systems_core.ingestion.obsidian_projection import (
        ObsidianProjection,
    )

    vault_root = tmp_path / "vault"
    projection_path = ObsidianProjection().write_formatted_paper_projection(
        result["artifact"], vault_root
    )
    first_line = Path(projection_path).read_text(encoding="utf-8").splitlines()[0]
    assert first_line == (
        "<!-- VIEW ONLY — generated by PublicationFormatter — "
        "do not edit -->"
    )


def test_no_uuid_literals_in_publication_formatter_source() -> None:
    source = Path(
        "src/spectrum_systems_core/paper/publication_formatter.py"
    ).read_text(encoding="utf-8")
    uuid_literal = re.compile(
        r"['\"][0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}['\"]"
    )
    matches = uuid_literal.findall(source)
    assert matches == [], f"uuid literals found: {matches}"
