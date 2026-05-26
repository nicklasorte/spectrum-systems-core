"""ReportGenerator: produce a multi-section grounded report draft via Sonnet.

Section-by-section. Temperature 0. Each Sonnet call writes a
synthesis_run_cost_record (FINDING-F-007). Bundle reference is shared
with KeynoteGenerator via bundle_id + bundle_hash (FINDING-F-005).
Citations must be inline as ``[source: <artifact_id>]`` (FINDING-F-004).
GroundingEval verifies them after the draft is written.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import synthesis_run_dir, synthesis_schema_path
from .cost_recorder import append_cost_record

_COMPONENT_NAME = "report_generator"
_COMPONENT_VERSION = "1.0.0"
GENERATION_MODEL = "claude-sonnet-4-20250514"
GENERATION_TEMPERATURE = 0
MAX_TOKENS_PER_SECTION = 1500

SECTION_TYPES = (
    "executive_summary",
    "background",
    "findings",
    "analysis",
    "recommendations",
    "conclusion",
)

SECTION_TITLE_BY_TYPE = {
    "executive_summary": "Executive Summary",
    "background": "Background",
    "findings": "Key Findings",
    "analysis": "Analysis",
    "recommendations": "Recommendations",
    "conclusion": "Conclusion",
}

CITATION_RE = re.compile(r"\[source:\s*([0-9a-f-]+)\s*\]", re.IGNORECASE)


REPORT_SECTION_PROMPT = """You are drafting one section of a technical report.

Audience: {audience}
Section type: {section_type}
Section title: {section_title}

Context (use ONLY this information — do not introduce facts not present here):
{context_excerpt}

CRITICAL RULES:
1. Every factual claim must be followed by an inline citation: [source: <artifact_id>]
2. Use ONLY the artifact_ids from the context above.
3. Do not invent artifact_ids.
4. Do not introduce facts not supported by the context.
5. Return ONLY the section text. No JSON wrapper. No preamble.

Write the {section_type} section now:"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _execution_fingerprint(*parts: str) -> str:
    seed = "|".join(parts) + f"|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _build_context_excerpt(bundle: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in bundle.get("items", []):
        parts.append(
            f"- artifact_id: {item['artifact_id']} | "
            f"type: {item['artifact_type']} | "
            f"source: {item['source_id']}\n"
            f"  excerpt: {item['content_excerpt']}"
        )
    return "\n\n".join(parts)


def _extract_citations(text: str) -> list[str]:
    seen: list[str] = []
    for match in CITATION_RE.findall(text or ""):
        cid = match.strip()
        if cid and cid not in seen:
            seen.append(cid)
    return seen


class ReportGenerator:
    """Generate a multi-section grounded report draft using Sonnet."""

    def __init__(
        self,
        api_caller: Callable[[str], tuple[str, int, int]] | None = None,
    ):
        # api_caller signature: (prompt) -> (text, input_tokens, output_tokens)
        self._api_caller = api_caller

    def generate(
        self,
        run_id: str,
        bundle: dict[str, Any],
        audience: str,
        repo_root: str,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()

        if self._api_caller is None:
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    self._api_caller = self._build_default_api_caller()
                except ImportError as exc:
                    return {
                        "status": "failure",
                        "draft_id": "",
                        "reason": f"anthropic_sdk_missing: {exc}",
                    }
            else:
                return {
                    "status": "failure",
                    "draft_id": "",
                    "reason": "api_key_missing",
                }

        context_excerpt = _build_context_excerpt(bundle)
        sections: list[dict[str, Any]] = []
        for section_type in SECTION_TYPES:
            section_title = SECTION_TITLE_BY_TYPE[section_type]
            prompt = REPORT_SECTION_PROMPT.format(
                audience=audience,
                section_type=section_type,
                section_title=section_title,
                context_excerpt=context_excerpt,
            )
            content = ""
            input_tokens = 0
            output_tokens = 0
            try:
                response = self._api_caller(prompt)
                if isinstance(response, tuple) and len(response) == 3:
                    content, input_tokens, output_tokens = response
                elif isinstance(response, str):
                    content = response
            except Exception as exc:  # noqa: BLE001
                content = ""
                input_tokens = 0
                output_tokens = 0
                # Fall through; we still record a cost row of zeros so the
                # human can see a section was attempted.

            try:
                append_cost_record(
                    run_id,
                    str(repo_root_path),
                    call_purpose=f"report_section_{section_type}",
                    input_tokens=int(input_tokens or 0),
                    output_tokens=int(output_tokens or 0),
                    model=GENERATION_MODEL,
                )
            except (OSError, jsonschema.ValidationError):
                pass

            citations = _extract_citations(content)
            sections.append(
                {
                    "section_id": str(uuid.uuid4()),
                    "section_title": section_title,
                    "section_type": section_type,
                    "content": content or "",
                    "inline_citations": citations,
                    "grounded": False,
                    "unverified_citations": [],
                }
            )

        draft = {
            "draft_id": str(uuid.uuid4()),
            "run_id": run_id,
            "bundle_id": bundle["bundle_id"],
            "bundle_hash": bundle["bundle_hash"],
            "audience": audience,
            "title": f"Report ({audience})",
            "sections": sections,
            "generation_model": GENERATION_MODEL,
            "generation_temperature": GENERATION_TEMPERATURE,
            "status": "draft",
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": [
                    item["artifact_id"] for item in bundle.get("items", [])
                ],
                "execution_fingerprint_hash": _execution_fingerprint(
                    run_id, bundle["bundle_hash"], audience
                ),
            },
        }

        try:
            schema = json.loads(
                synthesis_schema_path("report_draft").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(draft)
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "draft_id": "",
                "reason": f"schema_violation: {exc.message}",
            }

        run_dir = synthesis_run_dir(repo_root_path, run_id, create=True)
        (run_dir / "report_draft.json").write_text(
            json.dumps(draft, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # View-only Markdown projection.
        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            ObsidianProjection().write_report_projection(
                draft, str(repo_root_path)
            )
        except (FileNotFoundError, OSError, AttributeError):
            pass

        return {"status": "success", "draft_id": draft["draft_id"], "reason": ""}

    def _build_default_api_caller(self) -> Callable[[str], tuple[str, int, int]]:
        import anthropic

        client = anthropic.Anthropic()

        def _call(prompt: str) -> tuple[str, int, int]:
            # Stream to stay under the SDK's 10-minute non-streaming cap.
            with client.messages.stream(
                model=GENERATION_MODEL,
                max_tokens=MAX_TOKENS_PER_SECTION,
                temperature=GENERATION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
            parts: list[str] = []
            for block in message.content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            usage = getattr(message, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            return "\n".join(parts), input_tokens, output_tokens

        return _call
