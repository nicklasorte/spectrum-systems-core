"""PublicationFormatter: deterministic Phase D -> Phase K formatter.

Reads paper/revised_draft.json (Phase D output) and emits a
formatted_paper_artifact under paper/formatted/<paper_id>.json. The
output is the GOV-10-ready handoff: a status of "ready_for_certification"
indicates the artifact is shaped for Phase K certification, not that it
is certified.

Zero LLM calls. Deterministic. Never raises.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ._paths import find_paper_dir, paper_schema_path

_FORMATTER_VERSION = "1.0.0"
_FORMATTED_SCHEMA_VERSION = "1.0.0"
_PRODUCED_BY = "PublicationFormatter"
_DEFAULT_PUBLICATION_VERSION = "1"
_DOI_PLACEHOLDER = "doi-pending"
_PAPER_METADATA_FILENAME = "paper_metadata.json"
_REVISED_DRAFT_FILENAME = "revised_draft.json"
_FORMATTED_DIRNAME = "formatted"

_HASHED_FIELDS = (
    "abstract",
    "citations",
    "figures",
    "references",
    "sections",
    "tables",
    "title",
)

_CITATION_MARKER_PATTERN = re.compile(
    r"\[source:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]"
)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _content_hash(formatted: Dict[str, Any]) -> str:
    subset = {k: formatted[k] for k in _HASHED_FIELDS}
    digest = hashlib.sha256(_canonical_json(subset).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _load_schema(name: str) -> Dict[str, Any]:
    return json.loads(paper_schema_path(name).read_text(encoding="utf-8"))


def _validation_detail(error: jsonschema.ValidationError) -> str:
    location = "/".join(str(p) for p in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


_READ_MISSING = "missing"
_READ_UNREADABLE = "unreadable"


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (payload, error_tag). error_tag is None on success or missing.

    "missing" signals no file at the path; "unreadable" signals the file
    exists but cannot be parsed. Callers distinguish the two so absent
    optional inputs don't get treated as corrupt ones.
    """
    if not path.is_file():
        return None, _READ_MISSING
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError):
        return None, _READ_UNREADABLE


class PublicationFormatter:
    """Transform a revised_draft into a formatted_paper_artifact."""

    def format(
        self,
        revised_draft_id: str,
        repo_root: str,
        vault_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        paper_dir, _family = find_paper_dir(repo_root_path, revised_draft_id)
        if paper_dir is None or not (
            paper_dir / _REVISED_DRAFT_FILENAME
        ).is_file():
            return {
                "status": "failure",
                "artifact": None,
                "reason": f"revised_draft_not_found:{revised_draft_id}",
            }

        revised_draft, draft_err = _read_json(
            paper_dir / _REVISED_DRAFT_FILENAME
        )
        if draft_err == _READ_UNREADABLE or revised_draft is None:
            return {
                "status": "blocked",
                "artifact": None,
                "reason": (
                    f"input_schema_invalid:{revised_draft_id}:"
                    f"revised_draft_unreadable"
                ),
            }

        input_schema = _load_schema("revised_draft")
        validator = jsonschema.Draft202012Validator(input_schema)
        errors = sorted(validator.iter_errors(revised_draft), key=lambda e: e.path)
        if errors:
            return {
                "status": "blocked",
                "artifact": None,
                "reason": (
                    "input_schema_invalid:"
                    f"{revised_draft_id}:{_validation_detail(errors[0])}"
                ),
            }

        if revised_draft.get("source_id") != revised_draft_id:
            return {
                "status": "blocked",
                "artifact": None,
                "reason": (
                    f"source_id_mismatch:{revised_draft_id}:"
                    f"{revised_draft.get('source_id')}"
                ),
            }

        metadata, metadata_err = _read_json(
            paper_dir / _PAPER_METADATA_FILENAME
        )
        if metadata_err == _READ_UNREADABLE:
            return {
                "status": "blocked",
                "artifact": None,
                "reason": (
                    f"paper_metadata_unreadable:{revised_draft_id}"
                ),
            }
        formatted = self._build_formatted_artifact(
            revised_draft=revised_draft, metadata=metadata or {}
        )

        output_schema = _load_schema("formatted_paper_artifact")
        out_validator = jsonschema.Draft202012Validator(output_schema)
        out_errors = sorted(
            out_validator.iter_errors(formatted), key=lambda e: e.path
        )
        if out_errors:
            return {
                "status": "blocked",
                "artifact": None,
                "reason": (
                    "output_schema_invalid:"
                    f"{revised_draft_id}:{_validation_detail(out_errors[0])}"
                ),
            }

        formatted_dir = paper_dir / _FORMATTED_DIRNAME
        formatted_dir.mkdir(parents=True, exist_ok=True)
        target_path = formatted_dir / f"{formatted['paper_id']}.json"
        target_path.write_text(
            json.dumps(formatted, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        return {
            "status": "success",
            "artifact": formatted,
            "reason": "",
            "artifact_path": str(target_path),
        }

    def _build_formatted_artifact(
        self,
        *,
        revised_draft: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        paper_id = str(uuid.uuid4())
        formatted_at = _now_iso()
        revised_sections = revised_draft.get("revised_sections") or {}
        section_keys = sorted(revised_sections.keys())

        # Two-pass citation transform on sections in deterministic order.
        # Pass 1: assign ref_id by first-occurrence of source_artifact_id.
        ref_assignments: Dict[str, str] = {}
        for section_key in section_keys:
            body = revised_sections.get(section_key) or ""
            for match in _CITATION_MARKER_PATTERN.finditer(body):
                source_id = match.group(1).lower()
                if source_id not in ref_assignments:
                    ref_assignments[source_id] = (
                        f"ref-{len(ref_assignments) + 1}"
                    )

        # Pass 2: rewrite bodies and build citations list.
        sections: List[Dict[str, Any]] = []
        citations: List[Dict[str, Any]] = []
        citation_counter = 0
        for order_index, section_key in enumerate(section_keys):
            body = revised_sections.get(section_key) or ""

            def _replace(match: "re.Match[str]") -> str:
                nonlocal citation_counter
                source_id = match.group(1).lower()
                ref_id = ref_assignments[source_id]
                citation_counter += 1
                citations.append(
                    {
                        "citation_id": f"cite-{citation_counter}",
                        "source_artifact_id": source_id,
                        "inline_marker": match.group(0),
                        "formatted_text": f"[{ref_id}]",
                    }
                )
                return f"[{ref_id}]"

            rewritten = _CITATION_MARKER_PATTERN.sub(_replace, body)
            sections.append(
                {
                    "section_id": section_key,
                    "heading": section_key,
                    "body": rewritten,
                    "order": order_index,
                }
            )

        references = [
            {
                "ref_id": ref_assignments[src_id],
                "source_artifact_id": src_id,
                "formatted_text": f"Source artifact {src_id}",
            }
            for src_id in sorted(
                ref_assignments, key=lambda s: int(ref_assignments[s].split("-")[1])
            )
        ]

        title = str(metadata.get("title") or "")
        authors_raw = metadata.get("authors") or []
        authors = [str(a) for a in authors_raw if isinstance(a, str)]
        abstract = str(metadata.get("abstract") or "")

        formatted: Dict[str, Any] = {
            "paper_id": paper_id,
            "source_revised_draft_id": revised_draft["source_id"],
            "formatted_at": formatted_at,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "sections": sections,
            "citations": citations,
            "tables": [],
            "figures": [],
            "references": references,
            "publication_metadata": {
                "version": _DEFAULT_PUBLICATION_VERSION,
                "doi_placeholder": _DOI_PLACEHOLDER,
                "publication_date": formatted_at,
                "status": "ready_for_certification",
            },
            "schema_version": _FORMATTED_SCHEMA_VERSION,
            "content_hash": "",
            "provenance": {
                "produced_by": _PRODUCED_BY,
                "input_artifact_ids": [revised_draft["source_id"]],
                "formatter_version": _FORMATTER_VERSION,
            },
        }
        formatted["content_hash"] = _content_hash(formatted)
        return formatted

    def locate_formatted_path(
        self, revised_draft_id: str, repo_root: str, paper_id: str
    ) -> Optional[Path]:
        paper_dir, _family = find_paper_dir(
            Path(repo_root).resolve(), revised_draft_id
        )
        if paper_dir is None:
            return None
        return paper_dir / _FORMATTED_DIRNAME / f"{paper_id}.json"
