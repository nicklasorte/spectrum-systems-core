"""BundleAssembler: assemble a deterministic context_bundle for a synthesis run.

Reads only promoted/evidenced artifacts (FINDING-F-002). Estimates tokens
conservatively at 1 token ≈ 4 chars and stops adding items once the
configured token budget is hit (FINDING-F-001). The resulting bundle_hash
is a sha256 over sorted artifact_ids + recipe_id + audience and so two
runs with identical inputs produce identical hashes (CHECK-RT2-004).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import jsonschema

from ..ingestion.source_loader import SOURCE_FAMILIES
from ._paths import synthesis_run_dir, synthesis_schema_path
from .retrieval_registry import RetrievalRegistry


_COMPONENT_NAME = "bundle_assembler"
_COMPONENT_VERSION = "1.0.0"

VALID_AUDIENCES = ("technical", "policy", "executive", "public")
VALID_PURPOSES = ("report", "keynote", "both")
PROMOTED_STATUSES = {"promoted", "evidenced"}

TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4
MAX_BUNDLE_TOKENS = 6000
EXCERPT_LIMIT = 400


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _truncate(text: str, limit: int = EXCERPT_LIMIT) -> str:
    text = (text or "").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _estimate_tokens(text: str) -> int:
    if not text:
        return 1
    return max(1, len(text) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def _execution_fingerprint(*parts: str) -> str:
    seed = "|".join(parts) + f"|{_COMPONENT_NAME}:{_COMPONENT_VERSION}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _read_promoted_dir(dir_path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not dir_path.is_dir():
        return out
    for child in sorted(dir_path.glob("*.json")):
        try:
            out.append(json.loads(child.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _iter_processed_dirs(repo_root: Path) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    base = repo_root / "processed"
    if not base.is_dir():
        return out
    for family in SOURCE_FAMILIES:
        family_dir = base / family
        if not family_dir.is_dir():
            continue
        for source_dir in sorted(family_dir.iterdir()):
            if source_dir.is_dir():
                out.append((source_dir.name, source_dir))
    return out


def _materiality_rank(claim: Dict[str, Any]) -> int:
    rank = {"high": 0, "medium": 1, "low": 2}
    return rank.get(str(claim.get("materiality") or "low"), 99)


def _tier_rank(story: Dict[str, Any]) -> int:
    rank = {"tier_1": 0, "tier_2": 1, "tier_3": 2}
    return rank.get(str(story.get("tier_guess") or "tier_3"), 99)


def _confidence_rank(prediction: Dict[str, Any]) -> int:
    rank = {"high": 0, "medium": 1, "low": 2}
    return rank.get(str(prediction.get("confidence") or "low"), 99)


class BundleAssembler:
    """Assemble a context_bundle artifact from promoted/evidenced sources."""

    def assemble(
        self,
        run_id: str,
        recipe_id: str,
        audience: str,
        purpose: str,
        repo_root: str,
    ) -> Dict[str, Any]:
        if audience not in VALID_AUDIENCES:
            return {
                "status": "failure",
                "bundle": {},
                "reason": f"invalid_audience: {audience}",
            }
        if purpose not in VALID_PURPOSES:
            return {
                "status": "failure",
                "bundle": {},
                "reason": f"invalid_purpose: {purpose}",
            }
        try:
            recipe = RetrievalRegistry().get_recipe(recipe_id)
        except KeyError as exc:
            return {
                "status": "failure",
                "bundle": {},
                "reason": f"unknown_recipe: {exc}",
            }

        repo_root_path = Path(repo_root).resolve()
        token_budget = int(recipe.get("max_total_tokens", MAX_BUNDLE_TOKENS))
        # Hard ceiling: never trust a recipe to set budget above MAX_BUNDLE_TOKENS.
        token_budget = min(token_budget, MAX_BUNDLE_TOKENS)

        items: List[Dict[str, Any]] = []
        running_total = 0
        input_artifact_ids: List[str] = []

        for source_spec in recipe["sources"]:
            source_type = source_spec["source_type"]
            max_items = int(source_spec.get("max_items", 1))
            promoted_only = bool(source_spec.get("promoted_only", True))
            candidates = self._collect_candidates(
                source_type, repo_root_path, promoted_only
            )
            for candidate in candidates[:max_items]:
                excerpt = candidate["excerpt"]
                tokens = _estimate_tokens(excerpt)
                if running_total + tokens > token_budget:
                    # Stop adding items rather than blow the budget.
                    continue
                item = {
                    "item_id": str(uuid.uuid4()),
                    "artifact_id": candidate["artifact_id"],
                    "artifact_type": source_type,
                    "source_id": candidate["source_id"],
                    "content_excerpt": excerpt,
                    "token_estimate": tokens,
                    "promoted_status": candidate["status"],
                    "inclusion_reason": candidate["reason"],
                }
                # Promotion enforcement (CHECK-RT1-001 / CHECK-RT2-001).
                if (
                    promoted_only
                    and item["promoted_status"] not in PROMOTED_STATUSES
                ):
                    continue
                items.append(item)
                running_total += tokens
                input_artifact_ids.append(candidate["artifact_id"])

        if not items:
            return {
                "status": "failure",
                "bundle": {},
                "reason": "no_eligible_artifacts",
            }

        bundle_hash = self._bundle_hash(
            [it["artifact_id"] for it in items], recipe_id, audience
        )
        bundle = {
            "bundle_id": str(uuid.uuid4()),
            "run_id": run_id,
            "recipe_id": recipe_id,
            "recipe_version": recipe["recipe_version"],
            "audience": audience,
            "purpose": purpose,
            "items": items,
            "total_token_estimate": running_total,
            "token_budget": token_budget,
            "bundle_hash": bundle_hash,
            "assembled_at": _now_iso(),
            "provenance": {
                "produced_by": {
                    "component": _COMPONENT_NAME,
                    "version": _COMPONENT_VERSION,
                },
                "input_artifact_ids": input_artifact_ids,
                "execution_fingerprint_hash": _execution_fingerprint(
                    run_id, recipe_id, audience, bundle_hash
                ),
            },
        }

        try:
            schema = json.loads(
                synthesis_schema_path("context_bundle").read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(bundle)
            item_schema = json.loads(
                synthesis_schema_path("context_bundle_item")
                .read_text(encoding="utf-8")
            )
            item_validator = jsonschema.Draft202012Validator(item_schema)
            for it in items:
                item_validator.validate(it)
        except jsonschema.ValidationError as exc:
            return {
                "status": "failure",
                "bundle": {},
                "reason": f"schema_violation: {exc.message}",
            }

        if running_total > token_budget:
            return {
                "status": "blocked",
                "bundle": bundle,
                "reason": "token_budget_exceeded",
            }

        run_dir = synthesis_run_dir(repo_root_path, run_id, create=True)
        (run_dir / "context_bundle.json").write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"status": "success", "bundle": bundle, "reason": ""}

    def _bundle_hash(
        self, artifact_ids: List[str], recipe_id: str, audience: str
    ) -> str:
        seed = "|".join(sorted(artifact_ids)) + f"|{recipe_id}|{audience}"
        return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def _collect_candidates(
        self,
        source_type: str,
        repo_root: Path,
        promoted_only: bool,
    ) -> List[Dict[str, Any]]:
        if source_type == "technical_claim":
            return self._collect_claims(repo_root, promoted_only)
        if source_type == "story_candidate":
            return self._collect_stories(repo_root, promoted_only)
        if source_type == "theme_record":
            return self._collect_themes(repo_root, promoted_only)
        if source_type == "objection_prediction":
            return self._collect_objection_predictions(repo_root)
        return []

    def _collect_claims(
        self, repo_root: Path, promoted_only: bool
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for source_id, source_dir in _iter_processed_dirs(repo_root):
            claims_path = source_dir / "paper" / "claims.jsonl"
            for claim in _read_jsonl(claims_path):
                status = str(claim.get("status") or "")
                if promoted_only and status != "evidenced":
                    continue
                if str(claim.get("materiality") or "") != "high":
                    continue
                excerpt = _truncate(
                    f"{claim.get('claim_type', '?')}: "
                    f"{claim.get('claim_text', '')}"
                )
                out.append(
                    {
                        "artifact_id": claim["claim_id"],
                        "source_id": source_id,
                        "excerpt": excerpt,
                        "status": "evidenced",
                        "_rank": _materiality_rank(claim),
                        "reason": "high_materiality_evidenced_claim",
                    }
                )
        out.sort(key=lambda c: (c["_rank"], c["artifact_id"]))
        for c in out:
            c.pop("_rank", None)
        return out

    def _collect_stories(
        self, repo_root: Path, promoted_only: bool
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for source_id, source_dir in _iter_processed_dirs(repo_root):
            promoted_dir = source_dir / "stories" / "promoted"
            for story in _read_promoted_dir(promoted_dir):
                status = str(story.get("status") or "")
                if promoted_only and status != "promoted":
                    continue
                if str(story.get("tier_guess") or "") != "tier_1":
                    continue
                excerpt = _truncate(story.get("story_summary", ""))
                out.append(
                    {
                        "artifact_id": story["story_id"],
                        "source_id": source_id,
                        "excerpt": excerpt,
                        "status": "promoted",
                        "_rank": _tier_rank(story),
                        "reason": "tier_1_promoted_story",
                    }
                )
        out.sort(key=lambda c: (c["_rank"], c["artifact_id"]))
        for c in out:
            c.pop("_rank", None)
        return out

    def _collect_themes(
        self, repo_root: Path, promoted_only: bool
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for source_id, source_dir in _iter_processed_dirs(repo_root):
            promoted_dir = source_dir / "knowledge" / "promoted"
            for artifact in _read_promoted_dir(promoted_dir):
                if str(artifact.get("status") or "") != "promoted":
                    if promoted_only:
                        continue
                if "theme_id" not in artifact:
                    continue
                excerpt = _truncate(
                    f"{artifact.get('theme_name', '?')}: "
                    f"{artifact.get('description', '')}"
                )
                out.append(
                    {
                        "artifact_id": artifact["theme_id"],
                        "source_id": source_id,
                        "excerpt": excerpt,
                        "status": "promoted",
                        "_rank": 0,
                        "reason": "promoted_theme",
                    }
                )
        out.sort(key=lambda c: (c["_rank"], c["artifact_id"]))
        for c in out:
            c.pop("_rank", None)
        return out

    def _collect_objection_predictions(
        self, repo_root: Path
    ) -> List[Dict[str, Any]]:
        # Objection predictions are advisory only and carry status
        # "candidate" / "reviewed" / "dismissed" — never "promoted" or
        # "evidenced". Per FINDING-F-002 the constitution forbids candidates
        # in context bundles and EVAL-CTX-002 would block any bundle that
        # included one. The default_report_v1 recipe lists this source for
        # forward-compatibility, but the assembler returns no items today.
        return []
