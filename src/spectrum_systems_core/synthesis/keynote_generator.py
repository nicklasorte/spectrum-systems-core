"""KeynoteGenerator: produce a structured keynote scaffold via Sonnet.

Single Sonnet call, JSON output. Validates that every story_id and every
claim_id referenced is present in the bundle (FINDING-F-002 guarantees
the bundle is promoted-only). Shares bundle_id and bundle_hash with the
report draft so review-time inconsistency can be detected
(FINDING-F-005). Cost is appended to the same cost.jsonl as the report.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ._paths import synthesis_run_dir, synthesis_schema_path
from .cost_recorder import append_cost_record

_COMPONENT_NAME = "keynote_generator"
_COMPONENT_VERSION = "1.0.0"
GENERATION_MODEL = "claude-sonnet-4-20250514"
GENERATION_TEMPERATURE = 0
MAX_TOKENS = 2000


KEYNOTE_PROMPT = """You are creating a keynote speech scaffold.

Audience: {audience}
Available stories (promoted tier-1 only):
{stories_summary}

Available themes:
{themes_summary}

Key claims to support:
{claims_summary}

Create a structured keynote scaffold. Return ONLY valid JSON. No preamble.

{{
  "title": "keynote title (min 5 chars)",
  "opener": {{
    "story_id": "uuid of the opening story from the list above",
    "hook_text": "the opening hook (min 20 chars)",
    "why_this_story": "why this story opens the talk"
  }},
  "central_tension": "the core problem or question (min 20 chars)",
  "arc": [
    {{
      "beat_type": "opener|rising|climax|resolution|call_to_action",
      "content": "what happens in this beat (min 20 chars)",
      "story_id": "uuid or null",
      "claim_ids": ["list of artifact_ids cited in this beat"]
    }}
  ],
  "closing_call_to_action": "specific ask of the audience (min 10 chars)",
  "estimated_duration_minutes": 20
}}

RULES:
- opener.story_id MUST be a story_id from the list above. Do not invent ids.
- arc claim_ids MUST be artifact_ids from the claims_summary above.
- Include at least 3 arc beats (opener, rising/climax, call_to_action minimum).
- Do not introduce facts not present in the context above.
"""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _execution_fingerprint(*parts: str) -> str:
    seed = "|".join(parts) + f"|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _bundle_summaries(
    bundle: dict[str, Any],
) -> tuple[str, str, str, set, set]:
    stories: list[str] = []
    themes: list[str] = []
    claims: list[str] = []
    story_ids: set = set()
    claim_ids: set = set()
    for item in bundle.get("items", []):
        atype = item.get("artifact_type", "")
        artifact_id = item.get("artifact_id", "")
        excerpt = item.get("content_excerpt", "")
        if atype == "story_candidate":
            stories.append(f"- story_id: {artifact_id} | {excerpt}")
            story_ids.add(artifact_id)
        elif atype == "theme_record":
            themes.append(f"- theme_id: {artifact_id} | {excerpt}")
        elif atype == "technical_claim":
            claims.append(f"- claim_id: {artifact_id} | {excerpt}")
            claim_ids.add(artifact_id)
    return (
        "\n".join(stories) or "(none)",
        "\n".join(themes) or "(none)",
        "\n".join(claims) or "(none)",
        story_ids,
        claim_ids,
    )


class KeynoteGenerator:
    """Generate a single keynote_scaffold artifact using Sonnet."""

    def __init__(
        self,
        api_caller: Callable[[str], tuple[str, int, int]] | None = None,
    ):
        self._api_caller = api_caller

    def generate(
        self,
        run_id: str,
        bundle: dict[str, Any],
        audience: str,
        story_matrix: dict[str, Any],
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
                        "scaffold_id": "",
                        "reason": f"anthropic_sdk_missing: {exc}",
                    }
            else:
                return {
                    "status": "failure",
                    "scaffold_id": "",
                    "reason": "api_key_missing",
                }

        (
            stories_summary,
            themes_summary,
            claims_summary,
            bundle_story_ids,
            bundle_claim_ids,
        ) = _bundle_summaries(bundle)

        prompt = KEYNOTE_PROMPT.format(
            audience=audience,
            stories_summary=stories_summary,
            themes_summary=themes_summary,
            claims_summary=claims_summary,
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
            return {
                "status": "failure",
                "scaffold_id": "",
                "reason": f"api_error: {type(exc).__name__}: {exc}",
            }

        try:
            append_cost_record(
                run_id,
                str(repo_root_path),
                call_purpose="keynote_arc",
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                model=GENERATION_MODEL,
            )
        except (OSError, jsonschema.ValidationError):
            pass

        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            return {
                "status": "failure",
                "scaffold_id": "",
                "reason": f"json_parse_error: {exc}",
            }
        if not isinstance(parsed, dict):
            return {
                "status": "failure",
                "scaffold_id": "",
                "reason": "json_parse_error: not_object",
            }

        opener = parsed.get("opener") or {}
        opener_story_id = str(opener.get("story_id") or "")
        if opener_story_id and opener_story_id not in bundle_story_ids:
            return {
                "status": "blocked",
                "scaffold_id": "",
                "reason": f"fabricated_story_id_in_scaffold: {opener_story_id}",
            }

        arc = parsed.get("arc") or []
        if not isinstance(arc, list):
            arc = []
        for beat in arc:
            sid = beat.get("story_id")
            if sid and sid not in bundle_story_ids:
                return {
                    "status": "blocked",
                    "scaffold_id": "",
                    "reason": f"fabricated_story_id_in_scaffold: {sid}",
                }
            for cid in (beat.get("claim_ids") or []):
                if cid not in bundle_claim_ids:
                    return {
                        "status": "blocked",
                        "scaffold_id": "",
                        "reason": f"fabricated_claim_id_in_arc: {cid}",
                    }

        scaffold = {
            "scaffold_id": str(uuid.uuid4()),
            "run_id": run_id,
            "bundle_id": bundle["bundle_id"],
            "bundle_hash": bundle["bundle_hash"],
            "audience": audience,
            "title": str(parsed.get("title", "")) or f"Keynote ({audience})",
            "opener": {
                "story_id": opener_story_id,
                "hook_text": str(opener.get("hook_text", "")),
                "why_this_story": str(opener.get("why_this_story", "")),
            },
            "central_tension": str(parsed.get("central_tension", "")),
            "arc": [
                {
                    "beat_type": str(beat.get("beat_type", "")),
                    "content": str(beat.get("content", "")),
                    "story_id": (
                        str(beat["story_id"])
                        if isinstance(beat.get("story_id"), str)
                        and beat.get("story_id")
                        else None
                    ),
                    "claim_ids": list(beat.get("claim_ids") or []),
                }
                for beat in arc
            ],
            "closing_call_to_action": str(parsed.get("closing_call_to_action", "")),
            "estimated_duration_minutes": int(
                parsed.get("estimated_duration_minutes", 20) or 20
            ),
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
                synthesis_schema_path("keynote_scaffold")
                .read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(scaffold)
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "scaffold_id": "",
                "reason": f"schema_violation: {exc.message}",
            }

        run_dir = synthesis_run_dir(repo_root_path, run_id, create=True)
        (run_dir / "keynote_scaffold.json").write_text(
            json.dumps(scaffold, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            ObsidianProjection().write_keynote_projection(
                scaffold, str(repo_root_path)
            )
        except (FileNotFoundError, OSError, AttributeError):
            pass

        return {
            "status": "success",
            "scaffold_id": scaffold["scaffold_id"],
            "reason": "",
        }

    def _build_default_api_caller(self) -> Callable[[str], tuple[str, int, int]]:
        import anthropic

        client = anthropic.Anthropic()

        def _call(prompt: str) -> tuple[str, int, int]:
            message = client.messages.create(
                model=GENERATION_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=GENERATION_TEMPERATURE,
                messages=[{"role": "user", "content": prompt}],
            )
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
