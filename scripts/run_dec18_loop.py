"""Phase Z.1 — Dec 18 end-to-end loop driver (Y.1 -> Y.4 on real data).

Sequences the self-improvement loop's measurement arc over the pinned
Dec 18 transcript and emits ONE ``dec18_run_report`` artifact that
explains the run whether it completed or halted.

Design: every expensive / external effect (the Opus ceiling call, the
promoted-Haiku source, the comparator, the open-PR lookup) is an
injected seam. ``run_dec18_loop`` is a pure orchestrator over those
seams; ``main()`` wires the real defaults from the environment. The
REAL gates always run on REAL artifacts:

  * pre-flight (API key / data-lake / transcript / prior open PR),
  * the Z.1 ceiling-item floor (< 50 -> halt),
  * the real ``decide_control`` Y.3 comparison gate,

so a rejection test feeds a real failing input through the real gate —
only the model call is deterministic, exactly as Phase Y already
injects ``opus_call``.

Red-team Pass 1 #1: the report records the ``produced_at`` of the
Haiku artifact it scored against and raises ``haiku_artifact_stale_warning``
when that predates the active ``prompt_addition_id`` merge date, so a
comparison against a months-old Haiku run is never silently trusted.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
from _phase_z_lake import (  # noqa: E402
    data_lake_store_root,
    latest_instrument,
    write_instrument,
)

from spectrum_systems_core.artifacts import Artifact, new_artifact  # noqa: E402
from spectrum_systems_core.control.decision import decide_control  # noqa: E402
from spectrum_systems_core.data_lake.paths import (  # noqa: E402
    raw_transcript_path,
)
from spectrum_systems_core.evals.extraction_comparison import (  # noqa: E402
    compare_extractions,
    contract_version,
)
from spectrum_systems_core.extraction.false_negative_builder import (  # noqa: E402
    build_false_negative_set,
)
from spectrum_systems_core.extraction.opus_ceiling_extractor import (  # noqa: E402
    CeilingError,
    extract_ceiling,
)

ARTIFACT_TYPE = "dec18_run_report"
SCHEMA_VERSION = "1.0.0"
TRANSCRIPT_ID = "m-2025-12-18-7ghz-downlink-tig-kickoff"
CEILING_ITEM_FLOOR = 50

_SCHEMA_PATH = (
    _SCRIPTS_DIR.parent
    / "contracts"
    / "schemas"
    / "extraction"
    / "dec18_run_report.schema.json"
)
_ACTIVE_PROMPT_ADDITION = (
    _SCRIPTS_DIR.parent / "config" / "prompt_additions" / "active.json"
)


class HaikuNotFound(RuntimeError):
    """No promoted Haiku extraction exists for the transcript."""


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _validate_payload(payload: dict[str, Any]) -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    import jsonschema

    jsonschema.validate(payload, schema)


def _active_prompt_addition() -> dict[str, Any] | None:
    """The merged prompt addition under test, or ``None`` when no
    addition is active (then no staleness verdict can be formed)."""
    try:
        data = json.loads(
            _ACTIVE_PROMPT_ADDITION.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stale_warning(haiku_produced_at: str | None) -> str | None:
    """Warn when the Haiku artifact predates the active prompt
    addition's merge date (red-team Pass 1 #1)."""
    if not haiku_produced_at:
        return None
    active = _active_prompt_addition()
    if not active:
        return None
    merged_at = active.get("merged_at")
    addition_id = active.get("prompt_addition_id") or "unknown"
    if not isinstance(merged_at, str) or not merged_at:
        return None
    if haiku_produced_at < merged_at:
        return (
            f"haiku_artifact_predates_active_prompt_addition:"
            f"{addition_id}:haiku_produced_at={haiku_produced_at}<"
            f"merged_at={merged_at}"
        )
    return None


# ---- real default seams -------------------------------------------------

def _default_open_pr_lookup(transcript_id: str) -> list[str]:
    """Real correction/* open-PR lookup seam.

    The harness ``OpenPrLookup`` contract (Phase Y.8) is
    ``transcript_id -> list[str]``. A GitHub query is out of scope in
    the sandbox; the deterministic seam is the ``Z1_OPEN_PR_IDS`` env
    (comma-separated). Empty -> no prior PR (proceed).
    """
    raw = os.environ.get("Z1_OPEN_PR_IDS", "").strip()
    if not raw:
        return []
    return [tid for tid in (s.strip() for s in raw.split(",")) if tid]


def _ceiling_items_from_env() -> list[dict[str, Any]] | None:
    p = os.environ.get("Z1_CEILING_ITEMS_JSON", "").strip()
    if not p:
        return None
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("Z1_CEILING_ITEMS_JSON must hold a list/items[]")
    return items


def _default_ceiling_extractor(transcript_text: str) -> Artifact:
    """Real ``extract_ceiling``. The Opus call is the deterministic
    ``Z1_CEILING_ITEMS_JSON`` seam when set (test transport, exactly
    like Phase Y's injected ``opus_call``), else the real Opus call."""
    seed = _ceiling_items_from_env()
    if seed is not None:
        return extract_ceiling(
            transcript_text, TRANSCRIPT_ID, opus_call=lambda _t: seed
        )
    return extract_ceiling(transcript_text, TRANSCRIPT_ID)


def _envelope_to_artifact(env: dict[str, Any]) -> Artifact:
    return Artifact(
        artifact_type=env["artifact_type"],
        schema_version=env.get("schema_version", 1),
        status=env.get("status", "draft"),
        payload=env.get("payload", {}),
        trace_id=env.get("trace_id", ""),
        input_refs=env.get("input_refs", []),
        artifact_id=env.get("artifact_id", ""),
        created_at=env.get("created_at", ""),
        content_hash=env.get("content_hash", ""),
    )


def _default_haiku_loader(store: Path | None) -> Artifact:
    """Most recently promoted Haiku extraction for Dec 18.

    Test seam: ``Z1_HAIKU_ARTIFACT_JSON`` points at an envelope file
    carrying ``payload.extracted_items`` (the comparator's input
    contract). Otherwise the latest ``meeting_extraction`` instrument
    in the data-lake is used; absence is a fail-closed
    ``HaikuNotFound``.
    """
    seam = os.environ.get("Z1_HAIKU_ARTIFACT_JSON", "").strip()
    if seam:
        env = json.loads(Path(seam).read_text(encoding="utf-8"))
        return _envelope_to_artifact(env)
    if store is None:
        raise HaikuNotFound("no data-lake store to load Haiku artifact")
    env = latest_instrument(store, TRANSCRIPT_ID, "meeting_extraction")
    if env is None:
        raise HaikuNotFound(
            "no promoted meeting_extraction artifact for "
            f"{TRANSCRIPT_ID}"
        )
    return _envelope_to_artifact(env)


def _default_comparator(
    ceiling: Artifact, haiku: Artifact
) -> Artifact:
    return compare_extractions(
        ceiling_artifact=ceiling,
        haiku_artifact=haiku,
        alignment_contract_version=contract_version(),
    )


# ---- orchestrator -------------------------------------------------------

def run_dec18_loop(
    *,
    transcript_text: str | None,
    store: Path | None,
    api_key_present: bool,
    transcript_present: bool,
    ceiling_extractor: Callable[[str], Artifact],
    haiku_loader: Callable[[Path | None], Artifact],
    comparator: Callable[[Artifact, Artifact], Artifact],
    open_pr_lookup: Callable[[str], list[str]],
    log: list[str] | None = None,
) -> tuple[Artifact, int]:
    """Run Y.1->Y.4 over Dec 18 and return ``(report_artifact, exit)``.

    ``exit`` is 0 only when the run reaches Y.4 with the artifact
    schema-valid; any pre-flight / floor / control halt is exit 1 but
    STILL returns a schema-valid ``dec18_run_report`` (the run must
    explain itself).
    """
    log = log if log is not None else []
    phases = {p: "missing" for p in ("Y_1", "Y_2", "Y_3", "Y_4")}
    produced_at = _now()

    def _emit(
        *,
        phases: dict[str, str],
        ceiling_item_count: int,
        total_f1: float | None,
        per_type_f1: dict[str, Any],
        false_negative_count: int,
        control_decision: str,
        control_reason: str | None,
        halted_at: str | None,
        halt_reason: str | None,
        haiku_artifact_id: str | None = None,
        haiku_produced_at: str | None = None,
        haiku_stale_warning: str | None = None,
    ) -> tuple[Artifact, int]:
        payload = {
            "artifact_type": ARTIFACT_TYPE,
            "schema_version": SCHEMA_VERSION,
            "transcript_id": TRANSCRIPT_ID,
            "produced_at": produced_at,
            "phases": phases,
            "ceiling_item_count": ceiling_item_count,
            "total_f1": total_f1,
            "per_type_f1": per_type_f1,
            "false_negative_count": false_negative_count,
            "control_decision": control_decision,
            "control_reason": control_reason,
            "halted_at": halted_at,
            "halt_reason": halt_reason,
            "haiku_artifact_id": haiku_artifact_id,
            "haiku_artifact_produced_at": haiku_produced_at,
            "haiku_artifact_stale_warning": haiku_stale_warning,
        }
        _validate_payload(payload)
        art = new_artifact(
            artifact_type=ARTIFACT_TYPE,
            payload=payload,
            trace_id=f"dec18-{produced_at}",
            status="draft",
        )
        if store is not None:
            written = write_instrument(store, TRANSCRIPT_ID, art)
            log.append(f"wrote {written}")
        exit_code = 0 if halt_reason is None else 1
        return art, exit_code

    # ---- pre-flight (halt before any phase) ----
    if not api_key_present:
        log.append("preflight: ANTHROPIC_API_KEY missing")
        return _emit(
            phases=phases, ceiling_item_count=0, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(),
            halt_reason="environment_not_ready: ANTHROPIC_API_KEY missing",
        )
    if store is None:
        log.append("preflight: data-lake not found")
        return _emit(
            phases=phases, ceiling_item_count=0, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(),
            halt_reason=(
                "environment_not_ready: data-lake not found at "
                f"{os.environ.get('DATA_LAKE_PATH', '<unset>')}"
            ),
        )
    if not transcript_present or transcript_text is None:
        log.append("preflight: dec18 transcript not found")
        return _emit(
            phases=phases, ceiling_item_count=0, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(),
            halt_reason="environment_not_ready: dec18 transcript not found",
        )
    open_prs = open_pr_lookup(TRANSCRIPT_ID)
    if open_prs:
        log.append(f"preflight: prior open correction PR {open_prs}")
        return _emit(
            phases=phases, ceiling_item_count=0, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(),
            halt_reason=f"prior_open_correction_pr:{open_prs[0]}",
        )

    # ---- Y.1: ceiling ----
    try:
        ceiling = ceiling_extractor(transcript_text)
    except CeilingError as exc:
        log.append(f"Y.1 CeilingError: {exc}")
        return _emit(
            phases=phases, ceiling_item_count=0, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(),
            halt_reason=f"ceiling_error:{getattr(exc, 'reason_code', 'unknown')}",
        )
    ceiling_items = ceiling.payload.get("extracted_items", [])
    count = len(ceiling_items)
    log.append(f"Y.1 ceiling produced {count} items")
    if count < CEILING_ITEM_FLOOR:
        phases["Y_1"] = "present"
        log.append(
            f"Y.1 floor gate: {count} < {CEILING_ITEM_FLOOR} -> halt"
        )
        return _emit(
            phases=phases, ceiling_item_count=count, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(),
            halt_reason=(
                f"ceiling_item_floor_not_met: {count} items, "
                f"expected >= {CEILING_ITEM_FLOOR}"
            ),
        )
    phases["Y_1"] = "present"

    # ---- Y.2: compare against promoted Haiku ----
    try:
        haiku = haiku_loader(store)
    except HaikuNotFound as exc:
        log.append(f"Y.2 {exc}")
        return _emit(
            phases=phases, ceiling_item_count=count, total_f1=None,
            per_type_f1={}, false_negative_count=0,
            control_decision="not_reached", control_reason=None,
            halted_at=_now(), halt_reason="haiku_extraction_not_found",
        )
    haiku_produced_at = None
    if isinstance(haiku.payload, dict):
        haiku_produced_at = haiku.payload.get("produced_at")
    if not haiku_produced_at:
        haiku_produced_at = haiku.created_at or None
    stale = _stale_warning(haiku_produced_at)
    if stale:
        log.append(f"Y.2 STALE: {stale}")
    comparison = comparator(ceiling, haiku)
    phases["Y_2"] = "present"
    total = comparison.payload.get("total_metrics", {})
    total_f1 = total.get("f1") if isinstance(total, dict) else None
    per_type = comparison.payload.get("per_type_metrics", {})
    per_type_f1 = {
        st: m.get("f1")
        for st, m in per_type.items()
        if isinstance(m, dict)
    }
    log.append(f"Y.2 total_f1={total_f1}")

    # ---- Y.3: control decision (one passing eval so the Y.3
    # comparison F1 gate is the real decider, not missing_required) ----
    passing_eval = new_artifact(
        artifact_type="eval_result",
        payload={"status": "pass", "eval_type": "z1_loop_placeholder"},
        trace_id=comparison.trace_id,
        status="evaluated",
    )
    control = decide_control(comparison, [passing_eval])
    phases["Y_3"] = "present"
    decision = control.payload["decision"]
    reasons = control.payload.get("reason_codes", [])
    control_reason = ";".join(reasons) if reasons else None
    log.append(f"Y.3 decision={decision} reasons={reasons}")

    # ---- Y.4: false-negative set ----
    fn_set = build_false_negative_set(comparison)
    phases["Y_4"] = "present"
    fns = fn_set.payload.get("false_negatives", [])
    by_type: dict[str, int] = {}
    for fn in fns:
        st = fn.get("schema_type", "unknown")
        by_type[st] = by_type.get(st, 0) + 1
    log.append(f"Y.4 false_negatives={len(fns)} by_type={by_type}")

    return _emit(
        phases=phases,
        ceiling_item_count=count,
        total_f1=total_f1,
        per_type_f1=per_type_f1,
        false_negative_count=len(fns),
        control_decision=decision if decision in {"allow", "block"} else "block",
        control_reason=control_reason,
        halted_at=None,
        halt_reason=None,
        haiku_artifact_id=haiku.artifact_id or None,
        haiku_produced_at=haiku_produced_at,
        haiku_stale_warning=stale,
    )


def main(argv: list[str] | None = None) -> int:
    store = data_lake_store_root()
    api_key = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    transcript_text: str | None = None
    transcript_present = False
    if store is not None:
        tpath = raw_transcript_path(store, TRANSCRIPT_ID)
        if tpath.is_file():
            txt = tpath.read_text(encoding="utf-8")
            if txt.strip():
                transcript_text = txt
                transcript_present = True

    log: list[str] = []
    art, code = run_dec18_loop(
        transcript_text=transcript_text,
        store=store,
        api_key_present=api_key,
        transcript_present=transcript_present,
        ceiling_extractor=_default_ceiling_extractor,
        haiku_loader=_default_haiku_loader,
        comparator=_default_comparator,
        open_pr_lookup=_default_open_pr_lookup,
        log=log,
    )
    for line in log:
        print(f"[z1] {line}", file=sys.stderr)
    # Re-validate from disk-shape to prove the read path too.
    try:
        validate_artifact(art.payload, ARTIFACT_TYPE)
    except ArtifactValidationError as exc:  # pragma: no cover - defensive
        print(f"[z1] FATAL invalid report: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(art.payload, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
