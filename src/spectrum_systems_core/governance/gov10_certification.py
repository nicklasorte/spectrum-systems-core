"""Phase K (GOV-10) — Done Certification and Release.

GOV10CertificationStep runs 7 deterministic checks over the artifact chain
for one paper_id and emits a binary done_certification_record (PASSED or
FAILED — never partial). On PASSED, also writes a release_artifact.

Zero LLM calls. Fail-closed. Never raises (returns failure dict instead).

The 7 checks:
  CHECK-1: local_schema_correctness    - artifacts conform to their schemas
  CHECK-2: artifact_lineage_completeness - provenance walks back to source
  CHECK-3: replay_integrity            - re-run formatter, hashes match
  CHECK-4: contract_integrity          - schema files exist + version match
  CHECK-5: fail_closed_verification    - required eval results exist
  CHECK-6: eval_gate_coverage          - every chain artifact_type has evals
  CHECK-7: cost_governance             - total cost <= ceiling, records exist
"""
from __future__ import annotations

import datetime
import inspect
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import jsonschema

from ..ingestion.source_loader import SOURCE_FAMILIES
from ..paper.publication_formatter import PublicationFormatter
from .eval_coverage_scanner import EvalCoverageScanner

_LOG = logging.getLogger(__name__)


CERTIFICATION_COST_CEILING_USD: float = 5.00
_CERTIFIER_VERSION = "1.0.0"
_RECORD_SCHEMA_VERSION = "1.0.0"
_CERTIFICATION_LOGIC_VERSION = "1.0.0"
_PRODUCED_BY = "GOV10CertificationStep"

_CHECK_NAMES = (
    "local_schema_correctness",
    "artifact_lineage_completeness",
    "replay_integrity",
    "contract_integrity",
    "fail_closed_verification",
    "eval_gate_coverage",
    "cost_governance",
)


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _safe_read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable:{type(exc).__name__}"


def _certification_dir(repo_root: Path) -> Path:
    return repo_root / "governance" / "certifications"


def _release_dir(repo_root: Path) -> Path:
    return repo_root / "paper" / "released"


def _schema_path(repo_root: Path, schema_name: str) -> Path:
    return repo_root / "contracts" / "schemas" / f"{schema_name}.schema.json"


def _record_schema_path(repo_root: Path) -> Path:
    return _schema_path(repo_root, "certification/done_certification_record")


def _release_schema_path(repo_root: Path) -> Path:
    return _schema_path(repo_root, "certification/release_artifact")


def _load_json_schema(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    payload, err = _safe_read_json(path)
    if err is not None or payload is None:
        return None, err or "missing"
    try:
        jsonschema.Draft202012Validator.check_schema(payload)
    except jsonschema.SchemaError as exc:
        return None, f"invalid_schema:{exc.message[:80]}"
    return payload, None


class GOV10CertificationStep:
    """Run the 7 GOV-10 checks and emit a terminal certification record."""

    def certify(
        self,
        paper_id: str,
        run_id: str,
        repo_root: str,
        vault_root: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self._certify_impl(
                paper_id=paper_id,
                run_id=run_id,
                repo_root=repo_root,
                vault_root=vault_root,
            )
        except Exception as exc:  # never raise — always return a dict
            _LOG.warning(
                "GOV10CertificationStep unexpected failure: %s", exc, exc_info=True
            )
            return self._build_failure_envelope(
                paper_id=paper_id,
                repo_root=repo_root,
                reason=f"unexpected_error:{type(exc).__name__}",
            )

    def _certify_impl(
        self,
        *,
        paper_id: str,
        run_id: str,
        repo_root: str,
        vault_root: str | None,
    ) -> dict[str, Any]:
        repo_root_path = Path(repo_root).resolve()
        chain_artifact_ids: list[str] = []

        formatted_path, formatted_artifact = self._locate_formatted(
            repo_root_path, paper_id
        )
        if formatted_artifact is None:
            return self._emit(
                paper_id=paper_id,
                repo_root=repo_root_path,
                check_results=[
                    self._fail(
                        name,
                        f"formatted_paper_artifact_missing:{paper_id}"
                        if name == "local_schema_correctness"
                        else f"chain_unreachable:{paper_id}",
                    )
                    for name in _CHECK_NAMES
                ],
                cost_total=0.0,
                input_artifact_ids=[],
                vault_root=vault_root,
            )

        chain = self._build_chain(repo_root_path, formatted_path, formatted_artifact)
        chain_artifact_ids = [c["artifact_id"] for c in chain if c.get("artifact_id")]

        check_results: list[dict[str, str]] = []
        check_results.append(self._check_1_schemas(chain))
        check_results.append(self._check_2_lineage(repo_root_path, chain))
        check_results.append(
            self._check_3_replay(repo_root_path, formatted_artifact)
        )
        check_results.append(self._check_4_contracts(chain))
        check_results.append(
            self._check_5_required_eval_results(repo_root_path, run_id, chain)
        )
        check_results.append(self._check_6_eval_coverage(repo_root_path, chain))
        cost_check, cost_total = self._check_7_cost(repo_root_path, run_id)
        check_results.append(cost_check)

        return self._emit(
            paper_id=paper_id,
            repo_root=repo_root_path,
            check_results=check_results,
            cost_total=cost_total,
            input_artifact_ids=chain_artifact_ids,
            vault_root=vault_root,
            formatted_artifact=formatted_artifact,
        )

    # ------- chain construction ----------------------------------------

    def _locate_formatted(
        self, repo_root: Path, paper_id: str
    ) -> tuple[Path | None, dict[str, Any] | None]:
        for family in SOURCE_FAMILIES:
            family_dir = repo_root / "processed" / family
            if not family_dir.is_dir():
                continue
            for source_dir in sorted(family_dir.iterdir()):
                candidate = source_dir / "paper" / "formatted" / f"{paper_id}.json"
                if candidate.is_file():
                    payload, _err = _safe_read_json(candidate)
                    if isinstance(payload, dict):
                        return candidate, payload
        return None, None

    def _build_chain(
        self,
        repo_root: Path,
        formatted_path: Path,
        formatted_artifact: dict[str, Any],
    ) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = [
            {
                "artifact_type": "formatted_paper_artifact",
                "schema_name": "paper/formatted_paper_artifact",
                "schema_path": _schema_path(
                    repo_root, "paper/formatted_paper_artifact"
                ),
                "path": formatted_path,
                "payload": formatted_artifact,
                "artifact_id": formatted_artifact.get("paper_id"),
            }
        ]

        revised_path = formatted_path.parent.parent / "revised_draft.json"
        revised_payload, _err = _safe_read_json(revised_path)
        chain.append(
            {
                "artifact_type": "revised_draft",
                "schema_name": "paper/revised_draft",
                "schema_path": _schema_path(repo_root, "paper/revised_draft"),
                "path": revised_path,
                "payload": revised_payload,
                "artifact_id": (revised_payload or {}).get("source_id"),
            }
        )

        source_id: str | None = (revised_payload or {}).get("source_id")
        source_path: Path | None = None
        source_payload: dict[str, Any] | None = None
        if source_id:
            for family in SOURCE_FAMILIES:
                candidate = (
                    repo_root / "processed" / family / source_id / "source_record.json"
                )
                if candidate.is_file():
                    payload, _err2 = _safe_read_json(candidate)
                    if isinstance(payload, dict):
                        source_path = candidate
                        source_payload = payload
                        break
        chain.append(
            {
                "artifact_type": "source_record",
                "schema_name": "source_record",
                "schema_path": _schema_path(repo_root, "source_record"),
                "path": source_path,
                "payload": source_payload,
                "artifact_id": (source_payload or {}).get("artifact_id"),
            }
        )
        return chain

    # ------- the 7 checks ---------------------------------------------

    def _check_1_schemas(self, chain: list[dict[str, Any]]) -> dict[str, str]:
        for entry in chain:
            payload = entry.get("payload")
            if payload is None:
                return self._fail(
                    "local_schema_correctness",
                    f"schema_invalid:{entry['artifact_type']}:missing_artifact",
                )
            schema_path = entry["schema_path"]
            schema, err = _load_json_schema(schema_path)
            if schema is None:
                return self._fail(
                    "local_schema_correctness",
                    f"schema_invalid:{entry['schema_name']}:schema_unreadable:{err}",
                )
            validator = jsonschema.Draft202012Validator(schema)
            errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
            if errors:
                detail = "/".join(str(p) for p in errors[0].absolute_path) or "<root>"
                return self._fail(
                    "local_schema_correctness",
                    f"schema_invalid:{entry['artifact_type']}:"
                    f"{entry.get('artifact_id') or 'unknown'}:{detail}",
                )
        return self._pass("local_schema_correctness", "all_chain_artifacts_valid")

    def _check_2_lineage(
        self, repo_root: Path, chain: list[dict[str, Any]]
    ) -> dict[str, str]:
        formatted = chain[0]
        revised = chain[1]
        source = chain[2]

        prov = (formatted.get("payload") or {}).get("provenance") or {}
        inputs = prov.get("input_artifact_ids") or []
        if not inputs:
            return self._fail(
                "artifact_lineage_completeness",
                f"broken_lineage:{formatted.get('artifact_id')}:no_input_artifact_ids",
            )
        revised_id = revised.get("artifact_id")
        if revised_id not in inputs:
            return self._fail(
                "artifact_lineage_completeness",
                f"broken_lineage:{formatted.get('artifact_id')}:{inputs[0]}",
            )
        if revised.get("payload") is None or revised.get("path") is None:
            return self._fail(
                "artifact_lineage_completeness",
                f"broken_lineage:{formatted.get('artifact_id')}:{revised_id or 'missing'}",
            )

        # revised_draft's lineage anchor is its source_id field (Phase D
        # contract — no provenance.input_artifact_ids on revised_draft).
        # Verify the source artifact resolves on disk.
        source_payload = source.get("payload")
        if source_payload is None or source.get("path") is None:
            return self._fail(
                "artifact_lineage_completeness",
                f"broken_lineage:{revised_id}:{revised_id}",
            )
        # source_record itself must have provenance.input_artifact_ids (may be
        # empty, since it is a chain root).
        src_prov = source_payload.get("provenance") or {}
        if "input_artifact_ids" not in src_prov:
            return self._fail(
                "artifact_lineage_completeness",
                f"broken_lineage:{source.get('artifact_id')}:no_provenance",
            )
        return self._pass(
            "artifact_lineage_completeness", "chain_resolves_to_source_record"
        )

    def _check_3_replay(
        self, repo_root: Path, formatted_artifact: dict[str, Any]
    ) -> dict[str, str]:
        revised_id = formatted_artifact.get("source_revised_draft_id")
        stored_hash = formatted_artifact.get("content_hash")
        if not revised_id or not stored_hash:
            return self._fail(
                "replay_integrity",
                f"replay_hash_mismatch:{formatted_artifact.get('paper_id')}:"
                f"expected={stored_hash}:got=missing_inputs",
            )
        result = PublicationFormatter().format(
            revised_draft_id=revised_id, repo_root=str(repo_root)
        )
        if result.get("status") != "success" or not isinstance(
            result.get("artifact"), dict
        ):
            return self._fail(
                "replay_integrity",
                f"replay_hash_mismatch:{formatted_artifact.get('paper_id')}:"
                f"expected={stored_hash}:got=replay_failed",
            )
        recomputed = result["artifact"].get("content_hash")
        if recomputed != stored_hash:
            return self._fail(
                "replay_integrity",
                f"replay_hash_mismatch:{formatted_artifact.get('paper_id')}:"
                f"expected={stored_hash}:got={recomputed}",
            )
        return self._pass(
            "replay_integrity", f"content_hash_matches:{stored_hash[:23]}"
        )

    def _check_4_contracts(self, chain: list[dict[str, Any]]) -> dict[str, str]:
        for entry in chain:
            payload = entry.get("payload") or {}
            schema_path = entry["schema_path"]
            if not schema_path.is_file():
                return self._fail(
                    "contract_integrity",
                    f"contract_mismatch:{entry['schema_name']}:expected=present:found=missing",
                )
            schema, err = _load_json_schema(schema_path)
            if schema is None:
                return self._fail(
                    "contract_integrity",
                    f"contract_mismatch:{entry['schema_name']}:expected=valid_jsonschema:found={err}",
                )
            expected_version = self._expected_schema_version(schema)
            artifact_version = payload.get("schema_version")
            if expected_version is not None:
                if artifact_version is None or str(artifact_version) != str(
                    expected_version
                ):
                    return self._fail(
                        "contract_integrity",
                        f"contract_mismatch:{entry['schema_name']}:"
                        f"expected={expected_version}:found={artifact_version}",
                    )
        return self._pass("contract_integrity", "all_schemas_resolve")

    @staticmethod
    def _expected_schema_version(schema: dict[str, Any]) -> str | None:
        """Pull the schema_version pin from the schema document.

        Two sources, in order: a top-level `version` field (rare in this
        repo), or `properties.schema_version.const` (the pattern Phase J+
        schemas use). Returns None if neither is declared — comparison is
        skipped per the task spec's "if present" wording, but CHECK-1 will
        still validate the artifact against the full schema."""
        if "version" in schema:
            return str(schema["version"])
        props = schema.get("properties") or {}
        sv = props.get("schema_version")
        if isinstance(sv, dict) and "const" in sv:
            return str(sv["const"])
        return None

    def _check_5_required_eval_results(
        self,
        repo_root: Path,
        run_id: str,
        chain: list[dict[str, Any]],
    ) -> dict[str, str]:
        chain_types = {
            entry["artifact_type"] for entry in chain if entry.get("artifact_type")
        }
        eval_dir = repo_root / "contracts" / "evals"
        if not eval_dir.is_dir():
            return self._fail(
                "fail_closed_verification",
                "missing_eval_result:contracts_evals_dir_missing",
            )

        required_cases: list[dict[str, Any]] = []
        for path in sorted(eval_dir.glob("*.json")):
            payload, _err = _safe_read_json(path)
            if not isinstance(payload, list):
                continue
            for case in payload:
                if not isinstance(case, dict):
                    continue
                if not case.get("required"):
                    continue
                target = case.get("target_artifact_type")
                if target in chain_types:
                    required_cases.append(case)

        if not required_cases:
            # Treat unknown state as a bug — there must be at least one
            # required eval per chain artifact_type for fail-closed coverage.
            return self._fail(
                "fail_closed_verification",
                "missing_eval_result:no_required_cases_for_chain",
            )

        for case in required_cases:
            case_id = case.get("id") or case.get("name") or "<unknown>"
            if not self._eval_result_present(repo_root, run_id, case):
                return self._fail(
                    "fail_closed_verification", f"missing_eval_result:{case_id}"
                )
        return self._pass(
            "fail_closed_verification",
            f"all_required_eval_results_present:{len(required_cases)}",
        )

    def _eval_result_present(
        self,
        repo_root: Path,
        run_id: str,
        case: dict[str, Any],
    ) -> bool:
        """A required eval has a result iff its marker file exists at
        synthesis/<run_id>/evals/<case_id>.json AND the marker contains
        status="pass". A failed-but-present marker is NOT counted as
        result-present — that's a CHECK-5 failure surfaced as
        missing_eval_result so an upstream eval failure cannot silently
        slip through certification."""
        case_id = case.get("id") or case.get("name") or ""
        marker = (
            repo_root / "synthesis" / run_id / "evals" / f"{case_id}.json"
        )
        if not marker.is_file():
            return False
        payload, _err = _safe_read_json(marker)
        if not isinstance(payload, dict):
            return False
        return payload.get("status") == "pass"

    def _check_6_eval_coverage(
        self, repo_root: Path, chain: list[dict[str, Any]]
    ) -> dict[str, str]:
        chain_types = {
            entry["artifact_type"] for entry in chain if entry.get("artifact_type")
        }
        # Reuse Phase I scanner: scan() returns flagged_items including any
        # uncovered_artifact_type. Filter to chain types only.
        try:
            audit = EvalCoverageScanner().scan(repo_root)
        except Exception as exc:  # pragma: no cover — defensive
            return self._fail(
                "eval_gate_coverage",
                f"uncovered_artifact_type:scanner_failed:{type(exc).__name__}",
            )
        flagged = audit.get("flagged_items") or []
        for item in flagged:
            if item.get("item_type") != "uncovered_artifact_type":
                continue
            uncovered = item.get("item_id")
            if uncovered in chain_types:
                return self._fail(
                    "eval_gate_coverage", f"uncovered_artifact_type:{uncovered}"
                )
        return self._pass(
            "eval_gate_coverage", f"covered_types:{len(chain_types)}"
        )

    def _check_7_cost(
        self, repo_root: Path, run_id: str
    ) -> tuple[dict[str, str], float]:
        records: list[dict[str, Any]] = []

        run_cost_path = repo_root / "synthesis" / run_id / "cost.jsonl"
        if run_cost_path.is_file():
            try:
                with run_cost_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass

        # AI cost records linked via context_bundle.json's bundle_id.
        bundle_id = None
        bundle_payload, _bundle_err = _safe_read_json(
            repo_root / "synthesis" / run_id / "context_bundle.json"
        )
        if isinstance(bundle_payload, dict):
            bundle_id = bundle_payload.get("bundle_id")

        if bundle_id:
            queries_dir = repo_root / "ai" / "queries"
            costs_dir = repo_root / "ai" / "costs"
            if queries_dir.is_dir() and costs_dir.is_dir():
                for query_path in sorted(queries_dir.glob("*.json")):
                    query_record, _qerr = _safe_read_json(query_path)
                    if not isinstance(query_record, dict):
                        continue
                    if query_record.get("bundle_id") != bundle_id:
                        continue
                    cost_path = costs_dir / f"{query_record['query_id']}.json"
                    cost_record, _cerr = _safe_read_json(cost_path)
                    if isinstance(cost_record, dict):
                        records.append(cost_record)

        if not records:
            return (
                self._fail("cost_governance", "cost_records_missing"),
                0.0,
            )

        total = 0.0
        for record in records:
            total += float(record.get("estimated_cost_usd") or 0.0)

        if total > CERTIFICATION_COST_CEILING_USD:
            return (
                self._fail(
                    "cost_governance",
                    f"cost_overrun:total={total:.4f}:ceiling=5.00",
                ),
                total,
            )
        return (
            self._pass("cost_governance", f"total_cost_usd={total:.4f}"),
            total,
        )

    # ------- emission -------------------------------------------------

    def _emit(
        self,
        *,
        paper_id: str,
        repo_root: Path,
        check_results: list[dict[str, str]],
        cost_total: float,
        input_artifact_ids: list[str],
        vault_root: str | None,
        formatted_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        passed = sum(1 for c in check_results if c["status"] == "PASSED")
        failed = sum(1 for c in check_results if c["status"] == "FAILED")
        skipped = sum(1 for c in check_results if c["status"] == "SKIPPED")
        if passed + failed + skipped != 7:
            # Defensive — _CHECK_NAMES is length 7 and each emits exactly one.
            return self._build_failure_envelope(
                paper_id=paper_id,
                repo_root=str(repo_root),
                reason=f"check_count_invariant_violated:{passed}+{failed}+{skipped}",
            )

        status = "PASSED" if failed == 0 else "FAILED"
        failure_reasons = [c["detail"] for c in check_results if c["status"] == "FAILED"]
        certification_id = str(uuid.uuid4())

        record: dict[str, Any] = {
            "certification_id": certification_id,
            "paper_id": paper_id,
            "certified_at": _now_iso(),
            "certifier_version": _CERTIFIER_VERSION,
            "check_results": check_results,
            "status": status,
            "failure_reasons": failure_reasons,
            "total_checks": 7,
            "passed_checks": passed,
            "failed_checks": failed,
            "skipped_checks": skipped,
            "total_pipeline_cost_usd": float(cost_total),
            "schema_version": _RECORD_SCHEMA_VERSION,
            "provenance": {
                "produced_by": _PRODUCED_BY,
                "input_artifact_ids": [
                    aid for aid in input_artifact_ids if self._looks_uuid(aid)
                ],
                "certification_logic_version": _CERTIFICATION_LOGIC_VERSION,
            },
        }

        record_validation_err = self._validate_against_record_schema(
            record, repo_root
        )
        if record_validation_err is not None:
            return self._build_failure_envelope(
                paper_id=paper_id,
                repo_root=str(repo_root),
                reason=f"record_schema_invalid:{record_validation_err}",
            )

        # Build the release artifact in memory FIRST so a release-write failure
        # never leaves a PASSED record on disk without its release pointer.
        release_artifact: dict[str, Any] | None = None
        release_target: Path | None = None
        if status == "PASSED" and formatted_artifact is not None:
            built, build_err = self._build_release_artifact(
                repo_root=repo_root,
                paper_id=paper_id,
                certification_id=certification_id,
                formatted_artifact=formatted_artifact,
            )
            if build_err is not None or built is None:
                return self._build_failure_envelope(
                    paper_id=paper_id,
                    repo_root=str(repo_root),
                    reason=f"release_build_failed:{build_err}",
                )
            release_artifact, release_target = built

        write_err = self._write_record(record, repo_root)
        if write_err is not None:
            return self._build_failure_envelope(
                paper_id=paper_id,
                repo_root=str(repo_root),
                reason=f"record_write_failed:{write_err}",
            )

        if release_artifact is not None and release_target is not None:
            persist_err = self._persist_release(release_artifact, release_target)
            if persist_err is not None:
                # Roll back the just-written PASSED record so a failed release
                # write never leaves an orphan PASSED record on disk.
                self._delete_record_safe(certification_id, repo_root)
                return self._build_failure_envelope(
                    paper_id=paper_id,
                    repo_root=str(repo_root),
                    reason=f"release_write_failed:{persist_err}",
                )

        if vault_root:
            self._write_projection_safe(record, vault_root)

        return {
            "status": status,
            "certification_id": certification_id,
            "record": record,
            "release_artifact": release_artifact,
            "reason": "" if status == "PASSED" else ";".join(failure_reasons),
        }

    def _validate_against_record_schema(
        self, record: dict[str, Any], repo_root: Path
    ) -> str | None:
        schema, err = _load_json_schema(_record_schema_path(repo_root))
        if schema is None:
            return f"schema_load_failed:{err}"
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(record), key=lambda e: e.path)
        if errors:
            location = "/".join(str(p) for p in errors[0].absolute_path) or "<root>"
            return f"{location}: {errors[0].message[:120]}"
        return None

    def _write_record(self, record: dict[str, Any], repo_root: Path) -> str | None:
        try:
            target_dir = _certification_dir(repo_root)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{record['certification_id']}.json"
            target.write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return f"{type(exc).__name__}:{exc}"
        return None

    def _build_release_artifact(
        self,
        *,
        repo_root: Path,
        paper_id: str,
        certification_id: str,
        formatted_artifact: dict[str, Any],
    ) -> tuple[tuple[dict[str, Any], Path] | None, str | None]:
        target = _release_dir(repo_root) / f"{paper_id}.json"
        release_artifact = {
            "release_id": str(uuid.uuid4()),
            "paper_id": paper_id,
            "certification_id": certification_id,
            "released_at": _now_iso(),
            "formatted_paper_artifact_id": formatted_artifact["paper_id"],
            "release_path": str(target.relative_to(repo_root)).replace("\\", "/"),
            "schema_version": _RECORD_SCHEMA_VERSION,
            "provenance": {
                "produced_by": _PRODUCED_BY,
                "input_artifact_ids": [formatted_artifact["paper_id"]],
            },
        }
        schema, schema_err = _load_json_schema(_release_schema_path(repo_root))
        if schema is None:
            return None, f"schema_load_failed:{schema_err}"
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(release_artifact), key=lambda e: e.path)
        if errors:
            detail = "/".join(str(p) for p in errors[0].absolute_path) or "<root>"
            return None, f"{detail}: {errors[0].message[:120]}"
        return (release_artifact, target), None

    def _persist_release(
        self, release_artifact: dict[str, Any], target: Path
    ) -> str | None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(release_artifact, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return f"{type(exc).__name__}:{exc}"
        return None

    def _delete_record_safe(self, certification_id: str, repo_root: Path) -> None:
        path = _certification_dir(repo_root) / f"{certification_id}.json"
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass

    def _write_projection_safe(
        self, record: dict[str, Any], vault_root: str
    ) -> None:
        try:
            from ..ingestion.obsidian_projection import ObsidianProjection

            ObsidianProjection().write_certification_projection(record, vault_root)
        except Exception as exc:  # pragma: no cover — projection is advisory
            _LOG.warning("certification projection write failed: %s", exc)

    def _build_failure_envelope(
        self, *, paper_id: str, repo_root: str, reason: str
    ) -> dict[str, Any]:
        cert_id = str(uuid.uuid4())
        check_results = [self._fail(name, reason) for name in _CHECK_NAMES]
        record: dict[str, Any] = {
            "certification_id": cert_id,
            "paper_id": paper_id,
            "certified_at": _now_iso(),
            "certifier_version": _CERTIFIER_VERSION,
            "check_results": check_results,
            "status": "FAILED",
            "failure_reasons": [reason] * 7,
            "total_checks": 7,
            "passed_checks": 0,
            "failed_checks": 7,
            "skipped_checks": 0,
            "total_pipeline_cost_usd": 0.0,
            "schema_version": _RECORD_SCHEMA_VERSION,
            "provenance": {
                "produced_by": _PRODUCED_BY,
                "input_artifact_ids": [],
                "certification_logic_version": _CERTIFICATION_LOGIC_VERSION,
            },
        }
        try:
            target_dir = _certification_dir(Path(repo_root).resolve())
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{cert_id}.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return {
            "status": "FAILED",
            "certification_id": cert_id,
            "record": record,
            "release_artifact": None,
            "reason": reason,
        }

    @staticmethod
    def _looks_uuid(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            return False
        return True

    @staticmethod
    def _pass(name: str, detail: str) -> dict[str, str]:
        return {"check_name": name, "status": "PASSED", "detail": detail}

    @staticmethod
    def _fail(name: str, detail: str) -> dict[str, str]:
        return {"check_name": name, "status": "FAILED", "detail": detail}


def _verify_certify_signature_no_override() -> None:
    """Static guard — flag a programmer error if anyone adds an override
    parameter to certify(). Runs at import time."""
    sig = inspect.signature(GOV10CertificationStep.certify)
    forbidden = {"override", "bypass", "force", "human_override"}
    actual = set(sig.parameters.keys())
    intersection = forbidden & actual
    if intersection:
        raise RuntimeError(
            f"GOV10CertificationStep.certify must not have override/bypass "
            f"parameters; found {sorted(intersection)}"
        )


_verify_certify_signature_no_override()
