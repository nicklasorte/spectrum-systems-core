#!/usr/bin/env python3
"""Validate data-lake artifact integrity.

Walks ``<data_lake>/store/artifacts/`` and runs a small fixed set of
field-level checks against the artifact types that gate downstream
workflows. The script is intentionally lightweight: pure stdlib, no
package install, no schema validation library — just the predicates
that mirror what ``validate-and-baseline.yml`` will later assert at
much greater cost.

Failure classes this script is designed to catch BEFORE
validate-and-baseline runs:

  * Malformed JSON in any artifact under ``store/artifacts/``.
  * ``decision_few_shot_examples.examples[*].verified`` set to a string
    (``"true"``) or ``None`` instead of the boolean the Phase W wiring
    signal predicate requires.
  * ``decision_few_shot_examples.audit_log`` entries whose ``action``
    is outside the schema enum (``selected``, ``verified``,
    ``unverified``, ``force-verified``).
  * Missing glossary aggregate at
    ``store/artifacts/glossary/spectrum_glossary_v1.json``, OR an empty
    ``terms`` list — either of which makes the term injector load zero
    terms silently.
  * ``orchestration_result.glossary_injection_summary`` missing or set
    to a non-dict when the glossary aggregate has terms.

Exit codes:

  0 — every check passed.
  1 — at least one check failed. The summary printed to stdout names
      which checks failed and points at the offending file(s).

Fail-closed: missing the few-shot file or the glossary aggregate is
treated as FAIL, not SKIP. A workflow that runs without those
artifacts has nothing to validate against, which is the bug class
this script exists to detect.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple

# Canonical relative paths inside ``store/artifacts/``. These mirror
# the artifact manifest entries — change here AND in the manifest if
# the on-disk layout ever moves.
FEW_SHOT_RELPATH = "evals/few_shot/decision_examples_v1.json"
GLOSSARY_AGGREGATE_RELPATH = "glossary/spectrum_glossary_v1.json"
ORCHESTRATION_DIR_RELPATH = "orchestration"

# Schema-defined enum for the audit_log.action field. Mirrors
# ``src/spectrum_systems_core/schemas/decision_few_shot_examples.schema.json``.
ALLOWED_AUDIT_ACTIONS = frozenset(
    {"selected", "verified", "unverified", "force-verified"}
)


@dataclasses.dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    failures: List[str] = dataclasses.field(default_factory=list)


def _load_json(path: pathlib.Path) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(doc, None)`` on success, ``(None, error)`` on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read: {exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"json parse error: {exc}"


def check_json_validity(artifacts_root: pathlib.Path) -> CheckResult:
    """Every .json file under store/artifacts/ must parse."""
    failures: List[str] = []
    total = 0
    for path in sorted(artifacts_root.rglob("*.json")):
        total += 1
        _, err = _load_json(path)
        if err is not None:
            failures.append(f"{path.relative_to(artifacts_root)}: {err}")
    if failures:
        return CheckResult(
            name="json_validity",
            passed=False,
            detail=f"{total - len(failures)}/{total} files parse cleanly",
            failures=failures,
        )
    return CheckResult(
        name="json_validity",
        passed=True,
        detail=f"{total}/{total} files parse cleanly",
    )


def _validate_example(example: Any, idx: int) -> List[str]:
    """Return per-example failure messages (empty list when OK)."""
    out: List[str] = []
    if not isinstance(example, dict):
        return [f"examples[{idx}] is not an object (got {type(example).__name__})"]
    example_id = example.get("example_id")
    if not isinstance(example_id, str) or not example_id:
        out.append(
            f"examples[{idx}].example_id missing or not a non-empty string"
        )
    verified = example.get("verified")
    # Strict bool check: a string "true" / "false", int 1, or None all fail.
    # This mirrors the wiring-signal predicate `ex.get("verified") is True`.
    if not isinstance(verified, bool):
        out.append(
            f"examples[{idx}].verified must be a bool, got "
            f"{type(verified).__name__} ({verified!r})"
        )
    return out


def _validate_audit_entry(entry: Any, idx: int) -> List[str]:
    out: List[str] = []
    if not isinstance(entry, dict):
        return [f"audit_log[{idx}] is not an object (got {type(entry).__name__})"]
    action = entry.get("action")
    if action not in ALLOWED_AUDIT_ACTIONS:
        out.append(
            f"audit_log[{idx}].action={action!r} is not in "
            f"{sorted(ALLOWED_AUDIT_ACTIONS)}"
        )
    example_id = entry.get("example_id")
    if not isinstance(example_id, str) or not example_id:
        out.append(
            f"audit_log[{idx}].example_id missing or not a non-empty string"
        )
    return out


def check_decision_few_shot_examples(
    artifacts_root: pathlib.Path,
) -> CheckResult:
    """Field-level checks on decision_examples_v1.json.

    Fails when:
      * the file is missing (fail-closed; this is THE artifact the
        wiring signal predicate reads),
      * the file is not a dict,
      * any required top-level field is missing or the wrong type,
      * any per-example ``verified`` is not bool (string ``"true"``,
        ``None``, or ``1`` all fail),
      * any audit_log entry's ``action`` is outside the schema enum.
    """
    path = artifacts_root / FEW_SHOT_RELPATH
    if not path.is_file():
        return CheckResult(
            name="decision_few_shot_examples",
            passed=False,
            detail=f"missing required artifact at {FEW_SHOT_RELPATH}",
            failures=[f"file does not exist: {path}"],
        )
    doc, err = _load_json(path)
    if err is not None:
        return CheckResult(
            name="decision_few_shot_examples",
            passed=False,
            detail="failed to parse",
            failures=[f"{FEW_SHOT_RELPATH}: {err}"],
        )
    if not isinstance(doc, dict):
        return CheckResult(
            name="decision_few_shot_examples",
            passed=False,
            detail="not a JSON object",
            failures=[f"{FEW_SHOT_RELPATH}: top-level is {type(doc).__name__}"],
        )

    failures: List[str] = []
    artifact_type = doc.get("artifact_type")
    if artifact_type != "decision_few_shot_examples":
        failures.append(
            f"artifact_type={artifact_type!r}, expected "
            "'decision_few_shot_examples'"
        )
    schema_version = doc.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        failures.append(
            f"schema_version must be a non-empty string, got {schema_version!r}"
        )

    # Top-level ``verified`` is OPTIONAL per schema, but if present must
    # be a bool. The per-example ``verified`` field is what the wiring
    # signal predicate reads, so we check that strictly below.
    if "verified" in doc and not isinstance(doc["verified"], bool):
        failures.append(
            f"top-level verified must be bool, got "
            f"{type(doc['verified']).__name__} ({doc['verified']!r})"
        )

    examples = doc.get("examples")
    if not isinstance(examples, list):
        failures.append(
            f"examples must be a list, got {type(examples).__name__}"
        )
        examples = []

    audit_log = doc.get("audit_log")
    # audit_log is technically optional but required by the task
    # description; treat missing-or-not-list as a failure to keep
    # parity with downstream readers.
    if audit_log is None:
        failures.append("audit_log is missing (required)")
        audit_log = []
    elif not isinstance(audit_log, list):
        failures.append(
            f"audit_log must be a list, got {type(audit_log).__name__}"
        )
        audit_log = []

    for idx, ex in enumerate(examples):
        failures.extend(_validate_example(ex, idx))
    for idx, entry in enumerate(audit_log):
        failures.extend(_validate_audit_entry(entry, idx))

    if failures:
        return CheckResult(
            name="decision_few_shot_examples",
            passed=False,
            detail=f"{len(failures)} field-level issue(s)",
            failures=failures,
        )
    return CheckResult(
        name="decision_few_shot_examples",
        passed=True,
        detail=(
            f"{len(examples)} example(s), {len(audit_log)} audit entries — "
            "all fields well-typed"
        ),
    )


def _load_glossary_aggregate(
    artifacts_root: pathlib.Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = artifacts_root / GLOSSARY_AGGREGATE_RELPATH
    if not path.is_file():
        return None, f"missing required artifact at {GLOSSARY_AGGREGATE_RELPATH}"
    doc, err = _load_json(path)
    if err is not None:
        return None, f"{GLOSSARY_AGGREGATE_RELPATH}: {err}"
    if not isinstance(doc, dict):
        return None, (
            f"{GLOSSARY_AGGREGATE_RELPATH}: top-level is "
            f"{type(doc).__name__}, expected object"
        )
    return doc, None


def check_spectrum_glossary(artifacts_root: pathlib.Path) -> CheckResult:
    """Glossary aggregate present, well-typed, and non-empty."""
    doc, err = _load_glossary_aggregate(artifacts_root)
    if err is not None or doc is None:
        return CheckResult(
            name="spectrum_glossary",
            passed=False,
            detail="aggregate missing or malformed",
            failures=[err or "unknown error"],
        )
    failures: List[str] = []
    if doc.get("artifact_type") != "spectrum_glossary":
        failures.append(
            f"artifact_type={doc.get('artifact_type')!r}, expected "
            "'spectrum_glossary'"
        )
    terms = doc.get("terms")
    if not isinstance(terms, list):
        failures.append(f"terms must be a list, got {type(terms).__name__}")
        terms = []
    elif not terms:
        # The wiring signal `glossary_terms_injected_present` cannot
        # possibly pass when the aggregate has zero terms, so this is
        # a hard FAIL not a warning.
        failures.append("terms list is empty")

    if failures:
        return CheckResult(
            name="spectrum_glossary",
            passed=False,
            detail=f"{len(failures)} issue(s)",
            failures=failures,
        )
    return CheckResult(
        name="spectrum_glossary",
        passed=True,
        detail=f"aggregate present with {len(terms)} term(s)",
    )


def check_orchestration_results(artifacts_root: pathlib.Path) -> CheckResult:
    """When the glossary has terms, every orchestration_result must carry
    a dict ``glossary_injection_summary``.

    The schema marks the field optional, but on a healthy data-lake
    where the glossary has been seeded, every orchestration write
    SHOULD emit the rollup. A missing or non-dict field here is the
    same bug class that flipped ``glossary_terms_injected_present`` to
    MISSING in previous runs.
    """
    glossary_doc, glossary_err = _load_glossary_aggregate(artifacts_root)
    glossary_has_terms = (
        glossary_err is None
        and isinstance(glossary_doc, dict)
        and isinstance(glossary_doc.get("terms"), list)
        and len(glossary_doc.get("terms") or []) > 0
    )

    orch_dir = artifacts_root / ORCHESTRATION_DIR_RELPATH
    if not orch_dir.is_dir():
        return CheckResult(
            name="orchestration_result",
            passed=True,
            detail="no orchestration/ directory — nothing to check",
        )

    failures: List[str] = []
    checked = 0
    # ``glob("*.json")`` (not rglob) deliberately excludes the
    # raw_responses/ debug subtree — those are not orchestration_result
    # artifacts and have a different schema.
    for path in sorted(orch_dir.glob("*.json")):
        doc, err = _load_json(path)
        if err is not None:
            # JSON validity is reported by check_json_validity; do not
            # double-report here.
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("artifact_type") != "orchestration_result":
            continue
        checked += 1
        if glossary_has_terms:
            summary = doc.get("glossary_injection_summary")
            if not isinstance(summary, dict):
                failures.append(
                    f"{path.relative_to(artifacts_root)}: "
                    f"glossary_injection_summary must be a dict, got "
                    f"{type(summary).__name__}"
                )

    if failures:
        return CheckResult(
            name="orchestration_result",
            passed=False,
            detail=(
                f"{len(failures)}/{checked} orchestration_result artifact(s) "
                "missing glossary_injection_summary"
            ),
            failures=failures,
        )
    detail = (
        f"{checked} orchestration_result artifact(s) — glossary check "
        f"{'applied' if glossary_has_terms else 'skipped (no glossary terms)'}"
    )
    return CheckResult(
        name="orchestration_result", passed=True, detail=detail
    )


def check_wiring_few_shot_present_with_verified(
    artifacts_root: pathlib.Path,
) -> CheckResult:
    """Mirror of validate-and-baseline's wiring-signal predicate.

    Predicate (from ``.github/workflows/validate-and-baseline.yml``)::

        any(isinstance(ex, dict) and ex.get("verified") is True
            for ex in examples)

    The identity check ``is True`` means truthy strings ("true"),
    ``1``, or ``None`` are NOT accepted. This script catches the
    same regression class at validation time so the operator does not
    learn about it from a failed baseline run.
    """
    path = artifacts_root / FEW_SHOT_RELPATH
    if not path.is_file():
        return CheckResult(
            name="wiring_signal:few_shot_present_with_verified",
            passed=False,
            detail="few-shot artifact missing",
            failures=[f"file does not exist: {path}"],
        )
    doc, err = _load_json(path)
    if err is not None or not isinstance(doc, dict):
        return CheckResult(
            name="wiring_signal:few_shot_present_with_verified",
            passed=False,
            detail="few-shot artifact unreadable",
            failures=[err or "top-level is not an object"],
        )
    examples = doc.get("examples") or []
    matched = sum(
        1
        for ex in examples
        if isinstance(ex, dict) and ex.get("verified") is True
    )
    if matched == 0:
        return CheckResult(
            name="wiring_signal:few_shot_present_with_verified",
            passed=False,
            detail="zero examples with verified is True",
            failures=[
                "no example satisfies `ex.get('verified') is True` — "
                "this will flip the Phase W wiring signal to MISSING"
            ],
        )
    return CheckResult(
        name="wiring_signal:few_shot_present_with_verified",
        passed=True,
        detail=f"{matched} example(s) with verified is True",
    )


def check_wiring_glossary_aggregate_nonempty(
    artifacts_root: pathlib.Path,
) -> CheckResult:
    """The glossary aggregate must exist AND have a non-empty terms list.

    Without both, ``glossary_terms_injected_present`` can never go
    green: the term injector loads zero terms and every chunk's
    ``glossary_injection_summary.total_term_injections`` is 0.
    """
    doc, err = _load_glossary_aggregate(artifacts_root)
    if err is not None or doc is None:
        return CheckResult(
            name="wiring_signal:glossary_aggregate_nonempty",
            passed=False,
            detail="aggregate missing or malformed",
            failures=[err or "unknown error"],
        )
    terms = doc.get("terms") or []
    if not isinstance(terms, list) or len(terms) == 0:
        return CheckResult(
            name="wiring_signal:glossary_aggregate_nonempty",
            passed=False,
            detail="aggregate present but terms is empty / not a list",
            failures=[
                f"terms is {type(terms).__name__} of length "
                f"{len(terms) if hasattr(terms, '__len__') else 'n/a'}"
            ],
        )
    return CheckResult(
        name="wiring_signal:glossary_aggregate_nonempty",
        passed=True,
        detail=f"{len(terms)} term(s) in aggregate",
    )


def run_checks(data_lake: pathlib.Path) -> List[CheckResult]:
    artifacts_root = data_lake / "store" / "artifacts"
    if not artifacts_root.is_dir():
        return [
            CheckResult(
                name="data_lake_root",
                passed=False,
                detail=f"store/artifacts not found under {data_lake}",
                failures=[f"directory does not exist: {artifacts_root}"],
            )
        ]
    return [
        check_json_validity(artifacts_root),
        check_decision_few_shot_examples(artifacts_root),
        check_spectrum_glossary(artifacts_root),
        check_orchestration_results(artifacts_root),
        check_wiring_few_shot_present_with_verified(artifacts_root),
        check_wiring_glossary_aggregate_nonempty(artifacts_root),
    ]


def render_report(
    data_lake: pathlib.Path, results: List[CheckResult]
) -> str:
    lines: List[str] = []
    lines.append("== Data-lake validation report ==")
    lines.append(f"data_lake: {data_lake}")
    lines.append("")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"[{status}] {r.name}: {r.detail}")
        for f in r.failures:
            lines.append(f"  - {f}")
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    lines.append("")
    lines.append(f"SUMMARY: {passed} PASS, {failed} FAIL")
    lines.append("FAIL" if failed > 0 else "PASS")
    return "\n".join(lines) + "\n"


def _append_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-lake",
        required=True,
        help="Path to the local data-lake clone (the directory that "
        "contains ``store/artifacts/``).",
    )
    args = parser.parse_args(argv)

    data_lake = pathlib.Path(args.data_lake).resolve()
    results = run_checks(data_lake)
    report = render_report(data_lake, results)
    sys.stdout.write(report)
    _append_step_summary("```\n" + report + "```\n")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
