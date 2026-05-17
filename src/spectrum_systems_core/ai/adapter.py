"""AIAdapter: the only call site for AI memory queries (Phase H).

Single loop:
    question -> retrieve from governed memory -> assemble context bundle
    -> AI generation -> grounding eval -> advisory output

All AI outputs are advisory only. ai_advisory=True and
requires_human_review=True are immutable on every output (FINDING-H-005).
The Anthropic call is wrapped behind an injectable `api_caller` so the
test suite never makes a live API call (FINDING-H-004).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

from ..synthesis import DataLakeChecker
from ._paths import (
    ai_costs_dir,
    ai_failures_dir,
    ai_monthly_costs_path,
    ai_outputs_dir,
    ai_queries_dir,
    load_schema,
)
from .grounding_eval import (
    UUID_PATTERN,
    AIGroundingEval,
)
from .memory_context_builder import MemoryContextBuilder
from .prompt_registry import PromptRegistry

_COMPONENT_NAME = "ai_adapter"
_COMPONENT_VERSION = "1.0.0"
_INPUT_USD_PER_MTOK = 3.0
_OUTPUT_USD_PER_MTOK = 15.0
_CITATION_RE = re.compile(r"\[source:\s*([^\]]+?)\s*\]", re.IGNORECASE)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * _INPUT_USD_PER_MTOK
        + output_tokens * _OUTPUT_USD_PER_MTOK
    ) / 1_000_000


def _extract_citations(raw_response: dict[str, Any], response_text: str) -> list[str]:
    """Collect citations from both inline `[source: ...]` markers and
    structured fields (citations / supporting_citations / counter_citations
    / basis_citation / story_id)."""
    seen: list[str] = []

    def _add(val: Any) -> None:
        if isinstance(val, str):
            stripped = val.strip()
            # Tolerate values delivered as "[source: <id>]".
            inner = _CITATION_RE.search(stripped)
            if inner:
                cid = inner.group(1).strip()
            else:
                cid = stripped
            if cid and cid not in seen:
                seen.append(cid)

    for match in _CITATION_RE.findall(response_text or ""):
        cid = match.strip()
        if cid and cid not in seen:
            seen.append(cid)

    if isinstance(raw_response, dict):
        for key in ("citations", "supporting_citations", "counter_citations"):
            for val in raw_response.get(key) or []:
                _add(val)
        # objection_check structured fields
        for obj in raw_response.get("likely_objections") or []:
            if isinstance(obj, dict):
                _add(obj.get("basis_citation"))
        # story_fit structured fields
        for story in raw_response.get("relevant_stories") or []:
            if isinstance(story, dict):
                _add(story.get("story_id"))
                _add(story.get("citation"))
    return seen


def _validate_or_none(record: dict[str, Any], schema_name: str) -> str | None:
    try:
        schema = load_schema(schema_name)
        jsonschema.Draft202012Validator(schema).validate(record)
    except jsonschema.ValidationError as exc:
        return f"schema_violation: {exc.message}"
    except (FileNotFoundError, OSError) as exc:
        return f"schema_unreadable: {exc}"
    return None


class AIAdapter:
    """Govern one AI memory query end to end."""

    def __init__(
        self,
        api_caller: Callable[[dict[str, Any], str], tuple[str, int, int]] | None = None,
        data_lake_checker: DataLakeChecker | None = None,
    ):
        # api_caller signature: (task_def, prompt) -> (response_text, input_tokens, output_tokens)
        self._api_caller = api_caller
        self._data_lake_checker = data_lake_checker

    def query(
        self,
        task_type: str,
        question: str,
        repo_root: str,
        vault_root: str | None = None,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        query_id = str(uuid.uuid4())
        started_at = _now_iso()

        # 1. REGISTRY CHECK (FINDING-H-001) — fail before any API call.
        try:
            task_def = PromptRegistry().get(task_type, repo_root=str(repo_root_path))
        except (ValueError, FileNotFoundError) as exc:
            return self._fail_unregistered(
                query_id, task_type, str(exc), str(repo_root_path)
            )

        # 2. BUILD CONTEXT.
        context_result = MemoryContextBuilder().build(
            task_type, question, str(repo_root_path)
        )
        if context_result["status"] != "success":
            return self._fail_context(
                query_id,
                task_type,
                task_def,
                question,
                started_at,
                context_result,
                str(repo_root_path),
            )

        bundle = context_result["bundle"]
        context_text = context_result["context_text"]

        # 3. RENDER PROMPT (only call site outside the registry/adapter).
        prompt = PromptRegistry().render_prompt(
            task_type, question, context_text, repo_root=str(repo_root_path)
        )

        # 4. WRITE TENTATIVE QUERY RECORD.
        query_record = {
            "query_id": query_id,
            "task_type": task_type,
            "task_version": task_def["version"],
            "question": question,
            "bundle_id": bundle["bundle_id"],
            "bundle_hash": bundle["bundle_hash"],
            "model": task_def["model"],
            "temperature": 0,
            "started_at": started_at,
            "completed_at": None,
            "status": "success",
            "failure_reason": "",
        }
        self._write_query_record(query_record, str(repo_root_path))

        # 5. CALL THE MODEL.
        try:
            response_text, input_tokens, output_tokens = self._call_api(task_def, prompt)
        except Exception as exc:  # noqa: BLE001
            query_record["status"] = "failure"
            query_record["completed_at"] = _now_iso()
            query_record["failure_reason"] = f"api_error: {exc}"
            self._write_query_record(query_record, str(repo_root_path))
            return {
                "status": "failure",
                "output": None,
                "failure": None,
                "reason": f"api_error: {exc}",
            }

        # 6. RECORD COST (FINDING-H-006).
        cost_record = self._record_cost(
            query_id=query_id,
            task_type=task_type,
            model=task_def["model"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            repo_root=str(repo_root_path),
        )

        raw_response_hash = _hash_text(response_text)

        # 7. PARSE RESPONSE.
        try:
            raw_response = json.loads(response_text)
            if not isinstance(raw_response, dict):
                raise json.JSONDecodeError("not_object", response_text, 0)
        except json.JSONDecodeError as exc:
            return self._block(
                query_id=query_id,
                task_type=task_type,
                question=question,
                query_record=query_record,
                failure_type="schema_violation",
                failure_detail=f"json_parse_error: {exc}",
                raw_response_hash=raw_response_hash,
                repo_root=str(repo_root_path),
            )

        # 8. EXTRACT CITATIONS.
        citations = _extract_citations(raw_response, response_text)

        # FINDING-H-003: vault paths or any non-uuid citation -> block.
        bad = [c for c in citations if not UUID_PATTERN.match(c)]
        if bad:
            return self._block(
                query_id=query_id,
                task_type=task_type,
                question=question,
                query_record=query_record,
                failure_type="non_uuid_citation",
                failure_detail="non_uuid_citations: " + ", ".join(bad),
                raw_response_hash=raw_response_hash,
                repo_root=str(repo_root_path),
            )

        # 9. VERIFY CITATIONS via DataLake.exists() (FINDING-H-003 / H-004).
        checker = self._data_lake_checker or DataLakeChecker(str(repo_root_path))
        verified: list[str] = []
        unverified: list[str] = []
        for cid in citations:
            if checker.exists(cid):
                verified.append(cid)
            else:
                unverified.append(cid)

        confidence = raw_response.get("confidence")
        if confidence not in ("high", "medium", "low"):
            confidence = None

        # 10. ASSEMBLE OUTPUT.
        output_id = str(uuid.uuid4())
        ai_output = {
            "output_id": output_id,
            "query_id": query_id,
            "task_type": task_type,
            "raw_response": raw_response,
            "citations": citations,
            "verified_citations": verified,
            "unverified_citations": unverified,
            "grounded": bool(citations) and not unverified,
            "ai_advisory": True,  # immutable
            "requires_human_review": True,  # immutable
            "confidence": confidence,
            "created_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "bundle_id": bundle["bundle_id"],
                "bundle_hash": bundle["bundle_hash"],
                "model": task_def["model"],
                "temperature": 0,
            },
        }

        # 11. SCHEMA VALIDATE OUTPUT.
        violation = _validate_or_none(ai_output, "ai_output")
        if violation:
            return self._block(
                query_id=query_id,
                task_type=task_type,
                question=question,
                query_record=query_record,
                failure_type="schema_violation",
                failure_detail=violation,
                raw_response_hash=raw_response_hash,
                repo_root=str(repo_root_path),
            )

        # 12. RUN AI GROUNDING EVAL.
        eval_result = AIGroundingEval().run(
            ai_output, query_id, str(repo_root_path)
        )
        if eval_result["decision"] == "block":
            failure_type = (
                eval_result["failure_types"][0]
                if eval_result.get("failure_types")
                else "schema_violation"
            )
            return self._block(
                query_id=query_id,
                task_type=task_type,
                question=question,
                query_record=query_record,
                failure_type=failure_type,
                failure_detail=", ".join(eval_result.get("reason_codes", [])),
                raw_response_hash=raw_response_hash,
                repo_root=str(repo_root_path),
            )

        # 13. WRITE OUTPUT + FINALIZE QUERY RECORD.
        self._write_output(ai_output, str(repo_root_path))
        query_record["status"] = "success"
        query_record["completed_at"] = _now_iso()
        self._write_query_record(query_record, str(repo_root_path))

        # 14. OBSIDIAN PROJECTION (view only).
        if vault_root:
            try:
                from ..ingestion.obsidian_projection import ObsidianProjection

                ObsidianProjection().write_ai_query_projection(
                    ai_output=ai_output,
                    question=question,
                    task_type=task_type,
                    vault_root=vault_root,
                )
            except (FileNotFoundError, OSError, AttributeError):
                pass

        return {
            "status": "success",
            "output": ai_output,
            "failure": None,
            "reason": "",
            "cost_record": cost_record,
            "eval_result": eval_result,
        }

    # -- helpers --

    def _call_api(
        self, task_def: dict[str, Any], prompt: str
    ) -> tuple[str, int, int]:
        if self._api_caller is not None:
            return self._api_caller(task_def, prompt)
        import anthropic  # imported lazily so tests run without it

        client = anthropic.Anthropic()
        message = client.messages.create(
            model=task_def["model"],
            max_tokens=int(task_def["max_tokens"]),
            temperature=0,
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

    def _write_query_record(self, record: dict[str, Any], repo_root: str) -> None:
        # Only enforce schema for terminal states; in-flight records have
        # completed_at=None (allowed by the schema).
        violation = _validate_or_none(record, "ai_query_record")
        if violation and record.get("status") == "success" and record.get("completed_at"):
            raise ValueError(violation)
        target = ai_queries_dir(repo_root, create=True) / f"{record['query_id']}.json"
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_output(self, ai_output: dict[str, Any], repo_root: str) -> None:
        target = ai_outputs_dir(repo_root, create=True) / f"{ai_output['output_id']}.json"
        target.write_text(
            json.dumps(ai_output, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _record_cost(
        self,
        *,
        query_id: str,
        task_type: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        repo_root: str,
    ) -> dict[str, Any]:
        cost_usd = _estimate_cost_usd(input_tokens, output_tokens)
        record = {
            "cost_id": str(uuid.uuid4()),
            "query_id": query_id,
            "task_type": task_type,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "estimated_cost_usd": float(cost_usd),
            "recorded_at": _now_iso(),
        }
        target = ai_costs_dir(repo_root, create=True) / f"{query_id}.json"
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._update_monthly_cost(cost_usd, repo_root)
        return record

    def _update_monthly_cost(self, cost_usd: float, repo_root: str) -> None:
        path = ai_monthly_costs_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        current_month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        doc = {"month": current_month, "total_cost_usd": 0.0, "query_count": 0}
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                doc = {"month": current_month, "total_cost_usd": 0.0, "query_count": 0}
        if doc.get("month") != current_month:
            doc = {"month": current_month, "total_cost_usd": 0.0, "query_count": 0}
        doc["total_cost_usd"] = float(doc.get("total_cost_usd", 0.0)) + float(cost_usd)
        doc["query_count"] = int(doc.get("query_count", 0)) + 1
        path.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_failure(
        self,
        *,
        query_id: str,
        task_type: str,
        failure_type: str,
        failure_detail: str,
        raw_response_hash: str,
        repo_root: str,
    ) -> dict[str, Any]:
        failure = {
            "failure_id": str(uuid.uuid4()),
            "query_id": query_id,
            "task_type": task_type,
            "failure_type": failure_type,
            "failure_detail": failure_detail,
            "raw_response_hash": raw_response_hash,
            "created_at": _now_iso(),
        }
        target = ai_failures_dir(repo_root, create=True) / f"{failure['failure_id']}.json"
        target.write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return failure

    def _fail_unregistered(
        self,
        query_id: str,
        task_type: str,
        detail: str,
        repo_root: str,
    ) -> dict[str, Any]:
        failure = self._write_failure(
            query_id=query_id,
            task_type=task_type or "unknown",
            failure_type="unregistered_task_type",
            failure_detail=detail,
            raw_response_hash=_hash_text(""),
            repo_root=repo_root,
        )
        return {
            "status": "failure",
            "output": None,
            "failure": failure,
            "reason": f"unregistered_task_type: {detail}",
        }

    def _fail_context(
        self,
        query_id: str,
        task_type: str,
        task_def: dict[str, Any],
        question: str,
        started_at: str,
        context_result: dict[str, Any],
        repo_root: str,
    ) -> dict[str, Any]:
        # Persist a query_record for traceability even when the bundle fails.
        record = {
            "query_id": query_id,
            "task_type": task_type,
            "task_version": task_def.get("version", "0.0.0"),
            "question": question,
            "bundle_id": (context_result.get("bundle") or {}).get(
                "bundle_id", str(uuid.UUID(int=0))
            ),
            "bundle_hash": (context_result.get("bundle") or {}).get(
                "bundle_hash",
                "sha256:" + ("0" * 64),
            ),
            "model": task_def.get("model", "unknown"),
            "temperature": 0,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "status": "blocked"
            if context_result["status"] == "blocked"
            else "failure",
            "failure_reason": context_result.get("reason", ""),
        }
        try:
            self._write_query_record(record, repo_root)
        except (ValueError, OSError):
            pass

        failure = self._write_failure(
            query_id=query_id,
            task_type=task_type,
            failure_type="schema_violation",
            failure_detail=context_result.get("reason", ""),
            raw_response_hash=_hash_text(""),
            repo_root=repo_root,
        )
        return {
            "status": context_result["status"],
            "output": None,
            "failure": failure,
            "reason": context_result.get("reason", ""),
        }

    def _block(
        self,
        *,
        query_id: str,
        task_type: str,
        question: str,
        query_record: dict[str, Any],
        failure_type: str,
        failure_detail: str,
        raw_response_hash: str,
        repo_root: str,
    ) -> dict[str, Any]:
        query_record["status"] = "blocked"
        query_record["completed_at"] = _now_iso()
        query_record["failure_reason"] = (
            f"{failure_type}: {failure_detail}"
        )[:500]
        try:
            self._write_query_record(query_record, repo_root)
        except (ValueError, OSError):
            pass
        failure = self._write_failure(
            query_id=query_id,
            task_type=task_type,
            failure_type=failure_type,
            failure_detail=failure_detail,
            raw_response_hash=raw_response_hash,
            repo_root=repo_root,
        )
        return {
            "status": "blocked",
            "output": None,
            "failure": failure,
            "reason": f"{failure_type}: {failure_detail}",
        }
