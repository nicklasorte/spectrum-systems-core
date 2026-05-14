"""EvalRunner: orchestrate load -> align -> metrics -> gate -> summary.

Phase M.4. Reads ground_truth_pairs from ``$SDL_ROOT/ground_truth/``,
loads each confirmed pair's source_record + minutes_text + extracted
items, runs the aligner, computes metrics, aggregates into an
eval_summary, then asks the RegressionGate for a decision against the
baseline.

Inputs (filesystem):

* ``$SDL_ROOT/ground_truth/<pair_id>.json``
* source_record: ``$SDL_ROOT/<source_artifact_id>.json`` OR
  ``<data-lake>/store/processed/meetings/<source_id>/source_record.json``
* minutes: ``$SDL_ROOT/minutes/<minutes_id>.json`` (the record points
  at a relative txt_path which is read for the text body)
* extracted items: ``<data-lake>/store/processed/meetings/<source_id>/
  stories/candidates.jsonl``

Outputs (filesystem):

* ``$SDL_ROOT/evals/alignment/<alignment_id>.json``    one per pair
* ``$SDL_ROOT/evals/results/<eval_result_id>.json``    one per pair
* ``$SDL_ROOT/evals/eval_summary_<pipeline_run_id>.json``
* ``$SDL_ROOT/evals/gate_decision_<pipeline_run_id>.json``
* ``$SDL_ROOT/evals/eval_run_count.json``               (run counter)
* ``$SDL_ROOT/evals/baseline_eval_summary.json``        (first run, or
                                                          explicit
                                                          --set-baseline)

The runner never raises. Every recoverable failure produces a
``pairs_skipped_pending_review``-style entry or a per-pair eval_result
with coverage=0 / precision=0 / review=0. The CLI exits 0 on
completion (even partial); it exits 1 only if SDL_ROOT or
DATA_LAKE_PATH is unset.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from ...ingestion._paths import contracts_root
from ..alignment import ALIGNMENT_THRESHOLD, compute_alignment
from .aligner import EvalAligner
from .metrics import EvalMetrics
from .regression_gate import RegressionGate

logger = logging.getLogger(__name__)

SCHEMA_VERSION_SUMMARY = "2.0.0"
PRODUCED_BY_SUMMARY = "EvalRunner"

# Phase P1: gt_pair_review artifact suffix. The new alignment gate
# requires a sibling ``<pair_id>_review.json`` confirming the pair's
# expected_decision_outcome before scoring.
_GT_PAIR_REVIEW_SUFFIX = "_review.json"

# Phase O.4: minimum distinct source_ids required to expose
# per_source_metrics. With a single source the rollup would just
# duplicate aggregate_coverage / aggregate_precision and add no
# debugging signal.
_PER_SOURCE_METRICS_MIN_SOURCES: int = 2


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _load_schema(name: str) -> Optional[Dict[str, Any]]:
    path = contracts_root() / "schemas" / "eval" / f"{name}.schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, obj: Dict[str, Any]) -> bool:
    """Atomic-ish JSON write. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return True
    except OSError as exc:
        logger.warning("write_failed path=%s err=%s", path, exc)
        return False


def _resolve_sdl_root(data_lake_path: str) -> Optional[Path]:
    env = os.environ.get("SDL_ROOT", "").strip()
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return p
    if not data_lake_path:
        return None
    base = Path(data_lake_path)
    if not base.exists():
        return None
    return base / "store" / "artifacts"


class EvalRunner:
    """Run the ground-truth eval against confirmed pairs and emit summary."""

    def __init__(
        self,
        data_lake_path: str,
        *,
        sdl_root: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
        prompt_version: str = "unspecified",
        aligner: Optional[EvalAligner] = None,
        metrics: Optional[EvalMetrics] = None,
        gate: Optional[RegressionGate] = None,
    ) -> None:
        self.data_lake_path = str(data_lake_path or "").strip()
        if sdl_root:
            self.sdl_root = Path(sdl_root)
            try:
                self.sdl_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        else:
            self.sdl_root = _resolve_sdl_root(self.data_lake_path)
        self.pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
        self.prompt_version = prompt_version or "unspecified"
        self.aligner = aligner or EvalAligner()
        self.metrics = metrics or EvalMetrics()
        self.gate = gate or RegressionGate()
        # Phase P1 source-level aggregates, populated by the new
        # deterministic alignment path. Keyed by source_id so the
        # summary builder can pull spurious_add_rate / per_outcome_f1 /
        # review_queue_count back out after per-pair iteration finishes.
        self._p1_source_aggregates: Dict[str, Dict[str, Any]] = {}
        self._p1_review_queue: List[Dict[str, Any]] = []
        self._p1_eval_result_ids: set = set()

    def run(
        self,
        *,
        pair_id_filter: Optional[str] = None,
        set_baseline: bool = False,
        is_dry_run: bool = False,
        source_id_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the eval and return a result dict.

        ``is_dry_run`` short-circuits the run (eval is skipped, no
        artifacts written). This matches the orchestration_run_record
        contract: pipeline runs flagged dry_run=true did not produce
        the artifacts the eval would measure.

        Phase X2.4: ``source_id_filter`` narrows the run to pairs
        whose resolved source_id equals the filter. Used by the
        validate-and-baseline workflow so the development baseline
        covers only a single transcript. Carries through to
        ``eval_summary.baseline_scope`` ("single_transcript" vs
        "full_corpus") and ``gate_decision.baseline_type``
        ("development" vs "production").
        """
        if self.sdl_root is None:
            return {
                "status": "failed",
                "reason": "sdl_root_unresolved",
                "exit_code": 1,
            }
        if is_dry_run:
            logger.info("dry_run_skipped pipeline_run_id=%s", self.pipeline_run_id)
            return {
                "status": "skipped",
                "reason": "dry_run_skipped",
                "exit_code": 0,
                "pipeline_run_id": self.pipeline_run_id,
            }

        pairs = self._load_pairs()
        if pair_id_filter:
            pairs = [p for p in pairs if p.get("pair_id") == pair_id_filter]
        # Phase X2.4: narrow to a single source_id. The resolver walks
        # source_record.payload.source_id (and falls back to
        # fixture_source_id) so fixture-driven pairs also work.
        if source_id_filter:
            pairs = [
                p for p in pairs
                if self._resolve_pair_source_id(p) == source_id_filter
            ]

        confirmed = [p for p in pairs if p.get("status") == "confirmed"]
        pending = [p for p in pairs if p.get("status") == "pending_review"]

        # Phase O.4 — partial-run detection. Compute BEFORE evaluating
        # pairs so the warning shows up even if every pair would have
        # produced a degenerate eval_result.
        partial_warning, partial_detail = self._compute_partial_run_signal(
            confirmed
        )

        # Phase X2.4: --set-baseline requires a successful prior
        # extraction. Last orchestration_result for the source must NOT
        # be stage_status="failed"; otherwise we would install a
        # baseline measured against a broken run. Halt finding +
        # exit_code=1 (no summary written).
        if set_baseline and source_id_filter:
            if self._last_run_failed_for_source(source_id_filter):
                self._emit_baseline_requires_successful_run_finding(
                    source_id_filter
                )
                return {
                    "status": "failed",
                    "reason": (
                        "baseline_requires_successful_run: last "
                        f"orchestration_result for source_id={source_id_filter} "
                        "has stage_status=failed"
                    ),
                    "exit_code": 1,
                    "pipeline_run_id": self.pipeline_run_id,
                }

        # Refuse --set-baseline on partial runs. Returning exit_code=1
        # ensures CI gates fail closed even when the underlying eval
        # results otherwise look passable. The summary is NOT written.
        if partial_warning and set_baseline:
            return {
                "status": "failed",
                "reason": (
                    "partial_run_warning_blocks_set_baseline: expected="
                    f"{partial_detail.get('expected', 0)} actual="
                    f"{partial_detail.get('actual', 0)} missing="
                    f"{partial_detail.get('missing_source_ids', [])}"
                ),
                "exit_code": 1,
                "pipeline_run_id": self.pipeline_run_id,
                "partial_run_warning": True,
                "partial_run_detail": partial_detail,
            }

        eval_results: List[Dict[str, Any]] = []
        # Phase O.4: keep the (pair, eval_result) alignment so the
        # summary can carry source_id / ground_truth_text provenance.
        # _evaluate_pair returns None on schema-invalid eval_result; the
        # paired entry is skipped here too.
        evaluated_pairs: List[Dict[str, Any]] = []
        for pair in confirmed:
            er = self._evaluate_pair(pair)
            if er is not None:
                eval_results.append(er)
                evaluated_pairs.append(pair)

        run_count = self._bump_run_count()
        summary = self._build_summary(
            eval_results=eval_results,
            pairs_skipped_pending_review=len(pending),
            is_baseline=(
                (set_baseline or (run_count == 1)) and not partial_warning
            ),
            partial_run_warning=partial_warning,
            partial_run_detail=partial_detail,
            evaluated_pairs=evaluated_pairs,
        )

        # Load the baseline FIRST so the gate sees per-pair records
        # alongside aggregate numbers. The summary is written ONCE,
        # after the gate decision has been folded back in -- a reader
        # of eval_summary alone must see the same regression verdict
        # as gate_decision (RT1 finding: previously the summary said
        # regression_detected=False even when the gate said block).
        baseline_summary = self._load_baseline()
        baseline_pair_results: List[Dict[str, Any]] = []
        if baseline_summary is not None:
            baseline_pair_results = self._load_pair_results_for_summary(
                baseline_summary
            )

        gate_decision = self.gate.evaluate(
            current_summary=summary,
            baseline_summary=baseline_summary,
            run_count=run_count,
            current_pair_results=eval_results,
            baseline_pair_results=baseline_pair_results,
        )

        # Fold the gate's verdict into the summary so the artifact is
        # self-explanatory.
        summary["baseline_eval_summary_id"] = gate_decision.get(
            "baseline_eval_summary_id"
        )
        summary["regression_detected"] = gate_decision["decision"] == "block"
        summary["regression_detail"] = list(
            gate_decision.get("regression_detail") or []
        )

        # Record baseline. Two ways to install one:
        #   (a) implicit: run 1 with no baseline -> install current.
        #   (b) explicit: --set-baseline overrides whatever is there.
        # Phase O.4 — partial runs must never install an implicit baseline
        # either; the explicit --set-baseline path has already returned
        # above with exit_code=1, but the implicit path runs here.
        becoming_baseline = (
            set_baseline
            or (baseline_summary is None and run_count == 1)
        ) and not partial_warning
        if becoming_baseline:
            summary["is_baseline"] = True

        # Phase X2.4: tag the baseline scope on both the summary and
        # the gate_decision so a reader can answer "what does the
        # baseline cover?" without walking the artifact graph.
        baseline_scope: Optional[str] = (
            "single_transcript" if source_id_filter else "full_corpus"
        ) if becoming_baseline else None
        summary["baseline_scope"] = baseline_scope

        # The gate_decision schema (Phase X2.4) adds optional
        # baseline_type + baseline_scope -- record them when we are
        # installing a baseline so the operator can distinguish a
        # development from a production baseline.
        #
        # Codex P2 fix: on a non-baseline run, derive baseline_type from
        # the existing baseline's baseline_scope so a reader of
        # gate_decision can still answer "what kind of baseline is this
        # regression compared against?". Previous behaviour wrote
        # baseline_type=null on every non-baseline run, losing that
        # signal the moment the first baseline-setting run finished.
        if becoming_baseline:
            gate_decision["baseline_type"] = (
                "development" if source_id_filter else "production"
            )
        elif baseline_summary is not None:
            prior_scope = baseline_summary.get("baseline_scope")
            if prior_scope == "single_transcript":
                gate_decision["baseline_type"] = "development"
            elif prior_scope == "full_corpus":
                gate_decision["baseline_type"] = "production"
            else:
                gate_decision["baseline_type"] = None
        else:
            gate_decision["baseline_type"] = None
        gate_decision["baseline_scope"] = baseline_scope

        # Single write of eval_summary, with the gate verdict baked in.
        self._validate_and_write(
            "eval_summary",
            summary,
            self.sdl_root / "evals" / f"eval_summary_{self.pipeline_run_id}.json",
        )
        self._validate_and_write(
            "gate_decision",
            gate_decision,
            self.sdl_root / "evals" / f"gate_decision_{self.pipeline_run_id}.json",
        )

        # Baseline file is written ONLY after the summary has the
        # gate-verdict fields baked in -- a baseline must not be
        # installed with stale regression_detected=False if the run
        # actually regressed (paranoid but cheap).
        if summary["is_baseline"]:
            self._write_baseline(summary)
            # Phase X2.4: surface "what IS the baseline?" as a structured
            # finding (info severity) so an operator reading the health
            # report can see coverage / precision / f1 / scope without
            # opening the eval_summary artifact.
            if set_baseline:
                self._emit_baseline_set_finding(
                    summary=summary,
                    baseline_scope=baseline_scope,
                )

        return {
            "status": "completed",
            "exit_code": 0,
            "pipeline_run_id": self.pipeline_run_id,
            "summary": summary,
            "gate_decision": gate_decision,
            "eval_results": eval_results,
            "run_count": run_count,
            "pairs_evaluated": len(eval_results),
            "pairs_skipped_pending_review": len(pending),
        }

    # -- loaders ----------------------------------------------------------

    def _load_pairs(self) -> List[Dict[str, Any]]:
        if self.sdl_root is None:
            return []
        pairs_dir = self.sdl_root / "ground_truth"
        if not pairs_dir.is_dir():
            return []
        out: List[Dict[str, Any]] = []
        for path in sorted(pairs_dir.glob("*.json")):
            if not path.is_file():
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            out.append(rec)
        return out

    def _load_source_record(
        self, source_artifact_id: str, source_id_hint: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if self.sdl_root is None:
            return None
        # Try flat sdl_root path first.
        flat = self.sdl_root / f"{source_artifact_id}.json"
        if flat.is_file():
            try:
                rec = json.loads(flat.read_text(encoding="utf-8"))
                if isinstance(rec, dict):
                    return rec
            except (OSError, json.JSONDecodeError):
                pass
        # Try processed/ tree if data_lake_path is known.
        if self.data_lake_path and source_id_hint:
            processed_root = (
                Path(self.data_lake_path) / "store" / "processed" / "meetings"
            )
            candidate = processed_root / source_id_hint / "source_record.json"
            if candidate.is_file():
                try:
                    rec = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(rec, dict):
                        return rec
                except (OSError, json.JSONDecodeError):
                    pass
        return None

    def _load_minutes_text(self, minutes_artifact_id: str) -> str:
        if self.sdl_root is None:
            return ""
        record_path = self.sdl_root / "minutes" / f"{minutes_artifact_id}.json"
        if not record_path.is_file():
            return ""
        try:
            rec = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(rec, dict):
            return ""
        # The fixture path: minutes records may carry an inline
        # ``minutes_text`` field. The real-data path: the record points
        # at a relative ``txt_path`` we read.
        inline = rec.get("minutes_text")
        if isinstance(inline, str) and inline.strip():
            return inline
        txt_rel = rec.get("txt_path")
        if not isinstance(txt_rel, str) or not txt_rel.strip():
            return ""
        if not self.data_lake_path:
            return ""
        txt_abs = Path(self.data_lake_path) / "store" / txt_rel
        if not txt_abs.is_file():
            txt_abs = Path(self.data_lake_path) / txt_rel
            if not txt_abs.is_file():
                return ""
        try:
            return txt_abs.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _load_extracted_items(
        self, source_record: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        payload = source_record.get("payload") if source_record else None
        if not isinstance(payload, dict):
            return []
        source_id = payload.get("source_id")
        source_family = payload.get("source_family") or "meetings"
        if not isinstance(source_id, str) or not source_id:
            return []
        if not self.data_lake_path:
            return []
        candidates_path = (
            Path(self.data_lake_path)
            / "store"
            / "processed"
            / source_family
            / source_id
            / "stories"
            / "candidates.jsonl"
        )
        if not candidates_path.is_file():
            return []
        out: List[Dict[str, Any]] = []
        try:
            with candidates_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            return []
        return out

    def _evaluate_pair(self, pair: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Phase P1: dispatch on pair shape. New deterministic alignment
        # fires when the pair carries the typed-extraction surface
        # (ground_truth_text + expected_decision_outcome) AND no
        # fixture inline fields. Legacy fixtures continue to use the
        # TF-IDF EvalAligner path so existing eval tests keep passing.
        if self._is_p1_eligible_pair(pair):
            return self._evaluate_pair_p1(pair)

        pair_id = pair.get("pair_id") or ""
        source_artifact_id = pair.get("source_artifact_id") or ""
        minutes_artifact_id = pair.get("minutes_artifact_id") or ""

        # Allow fixtures to inline minutes_text + extracted_items right
        # on the pair record. This is how the test fixtures express
        # "no real data lake yet" -- the M4 runner can still exercise
        # the full code path.
        inline_extracted = pair.get("fixture_extracted_items")
        inline_minutes_text = pair.get("fixture_minutes_text")
        fixture_chunking_strategy = pair.get("fixture_chunking_strategy")
        fixture_source_id = pair.get("fixture_source_id")

        source_id_hint: Optional[str] = (
            fixture_source_id if isinstance(fixture_source_id, str) else None
        )
        source_record = self._load_source_record(
            source_artifact_id, source_id_hint=source_id_hint
        )

        if isinstance(inline_extracted, list):
            extracted_items = inline_extracted
        else:
            extracted_items = (
                self._load_extracted_items(source_record) if source_record else []
            )

        if isinstance(inline_minutes_text, str):
            minutes_text = inline_minutes_text
        else:
            minutes_text = self._load_minutes_text(minutes_artifact_id)

        chunking_strategy = "unknown"
        if isinstance(fixture_chunking_strategy, str):
            chunking_strategy = fixture_chunking_strategy
        elif source_record is not None:
            payload = source_record.get("payload") or {}
            cs = payload.get("chunking_strategy")
            if isinstance(cs, str):
                chunking_strategy = cs

        alignment = self.aligner.align(
            extracted_items=extracted_items,
            minutes_text=minutes_text,
            source_id=source_id_hint or source_artifact_id,
            minutes_artifact_id=minutes_artifact_id,
            source_artifact_id=source_artifact_id,
            pair_id=pair_id,
            chunking_strategy=chunking_strategy,
        )
        if self.sdl_root is not None:
            ok = self._validate_and_write(
                "alignment_result",
                alignment,
                self.sdl_root
                / "evals"
                / "alignment"
                / f"{alignment['alignment_result_id']}.json",
            )
            if not ok:
                # RT1 finding: a schema-invalid alignment_result must
                # NOT silently feed a downstream eval_result; the gate
                # would see normal-looking numbers derived from invalid
                # input. Skip this pair entirely; the .invalid.json
                # sidecar that _validate_and_write already wrote lets
                # a human inspect the failure.
                logger.warning(
                    "skipping_pair_invalid_alignment pair_id=%s", pair_id
                )
                return None

        eval_result = self.metrics.compute(
            alignment_result=alignment,
            pipeline_run_id=self.pipeline_run_id,
            prompt_version=self.prompt_version,
        )
        if self.sdl_root is not None:
            ok = self._validate_and_write(
                "eval_result",
                eval_result,
                self.sdl_root
                / "evals"
                / "results"
                / f"{eval_result['eval_result_id']}.json",
            )
            if not ok:
                # Same trust property as the alignment_result case
                # above: a schema-invalid eval_result must NOT enter
                # the summary aggregation. RT1 finding.
                logger.warning(
                    "skipping_pair_invalid_eval_result pair_id=%s", pair_id
                )
                return None
        return eval_result

    # -- Phase P1: deterministic two-stage alignment ----------------------

    @staticmethod
    def _is_p1_eligible_pair(pair: Dict[str, Any]) -> bool:
        """True iff the pair should go through the Phase P1 alignment path.

        Eligibility requires the new typed-extraction fields to be
        present AND the legacy fixture inline fields to be absent.
        Both conditions are needed: a legacy fixture that happens to
        carry ``ground_truth_text`` for documentation reasons should
        still run through the EvalAligner path that the fixture was
        written against.
        """
        if not isinstance(pair, dict):
            return False
        gt_text = pair.get("ground_truth_text")
        gt_outcome = pair.get("expected_decision_outcome")
        if not isinstance(gt_text, str) or not gt_text.strip():
            return False
        if not isinstance(gt_outcome, str) or not gt_outcome.strip():
            return False
        if isinstance(pair.get("fixture_extracted_items"), list):
            return False
        if isinstance(pair.get("fixture_minutes_text"), str):
            return False
        return True

    def _gt_pair_review_path(self, pair_id: str) -> Optional[Path]:
        if self.sdl_root is None or not pair_id:
            return None
        return self.sdl_root / "ground_truth" / f"{pair_id}{_GT_PAIR_REVIEW_SUFFIX}"

    def _load_gt_pair_review(self, pair_id: str) -> Optional[Dict[str, Any]]:
        path = self._gt_pair_review_path(pair_id)
        if path is None or not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def _load_meeting_extraction_for_pair(
        self, pair: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Load the meeting_extraction artifact tied to ``pair``.

        Lookup precedence:
          1. <sdl>/extractions/<source_artifact_id>_meeting_extraction.json
          2. <sdl>/extractions/<source_id>_meeting_extraction.json
          3. Scan <sdl>/extractions/ for a payload whose
             source_artifact_id matches.

        Returns ``None`` if no extraction is found; the caller treats
        that case as zero decisions (the partial_run_warning machinery
        already covers the operator-facing signal).
        """
        if self.sdl_root is None:
            return None
        ext_dir = self.sdl_root / "extractions"
        if not ext_dir.is_dir():
            return None
        sa_id = pair.get("source_artifact_id")
        source_id = self._resolve_pair_source_id(pair) or ""
        candidates: List[Path] = []
        if isinstance(sa_id, str) and sa_id:
            candidates.append(ext_dir / f"{sa_id}_meeting_extraction.json")
        if source_id:
            candidates.append(ext_dir / f"{source_id}_meeting_extraction.json")
        for cand in candidates:
            if cand.is_file():
                try:
                    doc = json.loads(cand.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(doc, dict):
                    return doc
        # Last-resort scan.
        for path in ext_dir.glob("*_meeting_extraction.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            if isinstance(sa_id, str) and sa_id and doc.get(
                "source_artifact_id"
            ) == sa_id:
                return doc
        return None

    def _emit_finding(
        self,
        finding_code: str,
        severity: str,
        *,
        context: Dict[str, Any],
        remediation: str,
    ) -> None:
        if self.sdl_root is None:
            return
        try:
            from ...health.finding import HealthFinding, write_finding
        except ImportError:
            return
        try:
            data_lake_root = self.sdl_root.parent.parent
        except (OSError, AttributeError):
            return
        try:
            write_finding(
                HealthFinding(
                    finding_code=finding_code,
                    severity=severity,
                    pipeline_run_id=self.pipeline_run_id,
                    context=context,
                    remediation=remediation,
                ),
                data_lake_path=data_lake_root,
            )
        except Exception as exc:  # never propagate; logging is enough
            logger.warning(
                "finding_write_failed code=%s err=%s", finding_code, exc,
            )

    def _emit_gt_pair_not_reviewed(self, pair_id: str) -> None:
        self._emit_finding(
            "gt_pair_not_reviewed",
            "halt",
            context={"pair_id": pair_id},
            remediation=(
                "Run `python scripts/review_gt_pairs.py --pair-id "
                f"{pair_id} --reviewer-id <your-id>` "
                "to confirm the expected_decision_outcome before "
                "eval-ground-truth will score this pair."
            ),
        )

    def _emit_gt_pair_outcome_rejected(self, pair_id: str) -> None:
        self._emit_finding(
            "gt_pair_outcome_rejected",
            "halt",
            context={"pair_id": pair_id},
            remediation=(
                "The reviewer marked this ground_truth_pair's "
                "expected_decision_outcome as incorrect. Either fix the "
                "pair record (re-generate from a corrected extraction) "
                "or write a fresh review with --overwrite once the "
                "outcome is corrected."
            ),
        )

    def _evaluate_pair_p1(
        self, pair: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Phase P1 deterministic two-stage alignment for a single GT pair.

        Gates:
          * Missing ``<pair_id>_review.json`` -> emit
            ``gt_pair_not_reviewed`` (halt) and skip.
          * Review present with ``outcome_confirmed: false`` -> emit
            ``gt_pair_outcome_rejected`` (halt) and skip.

        On a passed gate, the runner loads the meeting_extraction for
        the pair, calls ``compute_alignment`` against this pair alone,
        and emits a per-pair eval_result. Per-source aggregates are
        stashed on ``self._p1_source_aggregates`` so the summary
        builder can roll up spurious_add_rate / per_outcome_f1 /
        review_queue_count over the whole P1 run.
        """
        pair_id = pair.get("pair_id") or ""
        source_artifact_id = pair.get("source_artifact_id") or ""
        minutes_artifact_id = pair.get("minutes_artifact_id") or ""
        source_id = self._resolve_pair_source_id(pair) or source_artifact_id

        # Gate 1: review artifact present.
        review = self._load_gt_pair_review(pair_id)
        if review is None:
            self._emit_gt_pair_not_reviewed(pair_id)
            logger.info(
                "skipping_pair_no_review pair_id=%s", pair_id
            )
            return None
        # Gate 2: outcome confirmed.
        if not bool(review.get("outcome_confirmed", False)):
            self._emit_gt_pair_outcome_rejected(pair_id)
            logger.info(
                "skipping_pair_outcome_rejected pair_id=%s", pair_id
            )
            return None

        # Load the extraction once per source. Multiple pairs from the
        # same source re-use the cached decisions + alignment so the
        # source-level aggregates are computed from the full GT pool
        # rather than a single-pair view (which would always score
        # spurious_add_rate=0 or 1 trivially).
        aggregates = self._p1_source_aggregates.get(source_id)
        if aggregates is None:
            aggregates = self._compute_p1_source_alignment(pair, source_id)
            self._p1_source_aggregates[source_id] = aggregates

        per_pair = aggregates["per_pair"].get(pair_id)
        if per_pair is None:
            # Pair was not present in the source-level pool the first
            # time we computed -- can happen if multiple pairs share a
            # source and the first invocation seeded the cache before
            # this pair joined. Recompute including this pair.
            aggregates = self._compute_p1_source_alignment(pair, source_id)
            self._p1_source_aggregates[source_id] = aggregates
            per_pair = aggregates["per_pair"].get(pair_id, {})

        matched = bool(per_pair.get("matched"))
        coverage = 1.0 if matched else 0.0
        precision = 1.0 if matched else 0.0
        chunking_strategy = aggregates.get("chunking_strategy") or "unknown"

        # Build alignment_result + eval_result so the on-disk shape
        # stays compatible with the existing schemas. coverage_alignments
        # carries the matched GT-text record; review_alignments carries
        # the matched extracted decisions.
        alignment_id = str(uuid.uuid4())
        coverage_alignments: List[Dict[str, Any]] = [
            {
                "minutes_item_text": pair.get("ground_truth_text") or "",
                "matched_extracted_item_id": (
                    f"decision-{per_pair.get('matched_extracted_indices', [None])[0]}"
                    if matched else None
                ),
                "matched_extracted_item_text": (
                    aggregates["decision_texts"].get(
                        per_pair.get("matched_extracted_indices", [None])[0]
                    )
                    if matched else None
                ),
                "semantic_similarity": float(per_pair.get("best_similarity", 0.0)),
                "content_word_overlap": int(per_pair.get("best_overlap", 0)),
                "alignment_status": "matched" if matched else "unmatched",
            }
        ]
        review_alignments: List[Dict[str, Any]] = []
        if matched:
            for ext_idx in per_pair.get("matched_extracted_indices", []):
                review_alignments.append(
                    {
                        "extracted_item_id": f"decision-{ext_idx}",
                        "extracted_item_text": aggregates["decision_texts"].get(ext_idx, ""),
                        "source_turn_ids": aggregates["decision_turns"].get(ext_idx, []),
                        "source_turn_validation": "unknown",
                        "matched_minutes_text": pair.get("ground_truth_text") or "",
                        "semantic_similarity": float(per_pair.get("best_similarity", 0.0)),
                        "alignment_status": "matched",
                        "low_confidence_flagged": False,
                    }
                )
        alignment = {
            "alignment_result_id": alignment_id,
            "source_artifact_id": source_artifact_id or source_id,
            "minutes_artifact_id": minutes_artifact_id or f"synthesized-from-extraction:{source_id}",
            "pair_id": pair_id,
            "artifact_type": "alignment_result",
            "schema_version": "1.0.0",
            "created_at": _now_iso(),
            "coverage_alignments": coverage_alignments,
            "review_alignments": review_alignments,
            "chunking_strategy": chunking_strategy,
            "artifact_source": "meeting_extraction",
            "eval_input_warning": bool(aggregates.get("total_extracted", 0) == 0),
            "provenance": {"produced_by": "EvalAligner"},
        }
        if self.sdl_root is not None:
            ok = self._validate_and_write(
                "alignment_result",
                alignment,
                self.sdl_root / "evals" / "alignment" / f"{alignment_id}.json",
            )
            if not ok:
                logger.warning(
                    "skipping_pair_invalid_alignment pair_id=%s", pair_id
                )
                return None

        total_extracted = int(aggregates.get("total_extracted", 0))
        review_count = int(aggregates.get("review_queue_count_for_source", 0))
        eval_result_id = str(uuid.uuid4())
        eval_result = {
            "eval_result_id": eval_result_id,
            "alignment_result_id": alignment_id,
            "source_artifact_id": source_artifact_id or source_id,
            "minutes_artifact_id": minutes_artifact_id or f"synthesized-from-extraction:{source_id}",
            "pair_id": pair_id,
            "pipeline_run_id": self.pipeline_run_id,
            "prompt_version": self.prompt_version,
            "artifact_type": "eval_result",
            "schema_version": "1.0.0",
            "created_at": _now_iso(),
            "chunking_strategy": chunking_strategy,
            "coverage": float(coverage),
            "precision": float(precision),
            "items_requiring_review": int(0 if matched else 1),
            "items_requiring_review_rate": float(0.0 if matched else 1.0),
            "total_extracted_items": int(total_extracted),
            "total_minutes_items": 1,
            "provenance": {"produced_by": "EvalMetrics"},
        }
        if self.sdl_root is not None:
            ok = self._validate_and_write(
                "eval_result",
                eval_result,
                self.sdl_root / "evals" / "results" / f"{eval_result_id}.json",
            )
            if not ok:
                logger.warning(
                    "skipping_pair_invalid_eval_result pair_id=%s", pair_id
                )
                return None

        self._p1_eval_result_ids.add(eval_result_id)
        return eval_result

    def _compute_p1_source_alignment(
        self, seed_pair: Dict[str, Any], source_id: str
    ) -> Dict[str, Any]:
        """Run compute_alignment for every confirmed P1 pair under ``source_id``.

        Caching: the result is keyed by ``source_id`` on
        ``self._p1_source_aggregates`` so a single extraction is
        scored once even when many pairs share the source.
        """
        # Re-load every confirmed P1 pair under the same source so the
        # per-source aggregates (spurious_add_rate / per_outcome_f1)
        # reflect the full ground-truth pool, not just the seed pair.
        all_pairs = self._load_pairs()
        confirmed_for_source = [
            p for p in all_pairs
            if p.get("status") == "confirmed"
            and self._is_p1_eligible_pair(p)
            and (self._resolve_pair_source_id(p) or p.get("source_artifact_id") or "") == source_id
        ]
        # Only include pairs whose review confirms the outcome -- the
        # gate is enforced per-pair, but the aggregates should not pull
        # in rejected pairs either.
        gt_pool: List[Dict[str, Any]] = []
        for p in confirmed_for_source:
            review = self._load_gt_pair_review(p.get("pair_id") or "")
            if review is None:
                continue
            if not bool(review.get("outcome_confirmed", False)):
                continue
            gt_pool.append(p)

        extraction = self._load_meeting_extraction_for_pair(seed_pair) or {}
        decisions_raw = extraction.get("decisions") if isinstance(extraction, dict) else []
        decisions: List[Dict[str, Any]] = [
            d for d in (decisions_raw or []) if isinstance(d, dict)
        ]

        alignment = compute_alignment(
            extracted_decisions=decisions,
            gt_pairs=gt_pool,
            threshold=ALIGNMENT_THRESHOLD,
        )

        chunking_strategy = "unknown"
        try:
            sa_id = seed_pair.get("source_artifact_id") or ""
            rec = self._load_source_record(sa_id, source_id_hint=source_id)
            if isinstance(rec, dict):
                payload = rec.get("payload") or {}
                cs = payload.get("chunking_strategy")
                if isinstance(cs, str) and cs in (
                    "speaker_turn", "character_count_fallback", "unknown"
                ):
                    chunking_strategy = cs
        except Exception as exc:  # never let a load-time hiccup poison alignment
            logger.warning(
                "chunking_strategy_lookup_failed source_id=%s err=%s",
                source_id, exc,
            )

        # Index per-pair info, attaching best-score signals from the
        # raw alignment for the on-disk alignment_result payload.
        per_pair: Dict[str, Dict[str, Any]] = {}
        for entry in alignment["pairs"]:
            pid = entry["pair_id"]
            matched_idxs = entry.get("matched_extracted_indices") or []
            best_sim = 0.0
            best_overlap = 0
            # Best similarity / overlap for the pair across its matched
            # extracted indices -- only used cosmetically on the
            # alignment_result coverage_alignments row.
            for idx in matched_idxs:
                if 0 <= idx < len(decisions):
                    text = decisions[idx].get("decision_text") or ""
                    from ..alignment import _alignment_score
                    sim_a = _alignment_score(
                        text, gt_pool[
                            next(i for i, p in enumerate(gt_pool) if p.get("pair_id") == pid)
                        ].get("ground_truth_text") or "",
                    )
                    if sim_a > best_sim:
                        best_sim = sim_a
                    # Token overlap is informational; recompute lightly.
                    from ..alignment import _tokenize
                    overlap = len(_tokenize(text) & _tokenize(
                        gt_pool[
                            next(i for i, p in enumerate(gt_pool) if p.get("pair_id") == pid)
                        ].get("ground_truth_text") or ""
                    ))
                    if overlap > best_overlap:
                        best_overlap = overlap
            per_pair[pid] = {
                "matched": bool(entry.get("matched")),
                "matched_extracted_indices": list(matched_idxs),
                "best_similarity": float(best_sim),
                "best_overlap": int(best_overlap),
            }

        decision_texts = {
            i: (d.get("decision_text") or "")
            for i, d in enumerate(decisions)
        }
        decision_turns = {
            i: list(d.get("source_turn_ids") or [])
            for i, d in enumerate(decisions)
        }

        # Add this source's review queue to the global P1 queue once.
        if alignment["review_queue"]:
            for entry in alignment["review_queue"]:
                self._p1_review_queue.append(
                    {
                        "source_id": source_id,
                        **entry,
                    }
                )

        return {
            "source_id": source_id,
            "coverage": float(alignment["coverage"]),
            "precision": float(alignment["precision"]),
            "spurious_add_rate": float(alignment["spurious_add_rate"]),
            "per_outcome_f1": dict(alignment["per_outcome_f1"]),
            "matched_pair_count": int(alignment["matched_pair_count"]),
            "matched_extracted_count": int(alignment["matched_extracted_count"]),
            "total_extracted": int(alignment["total_extracted"]),
            "total_gt_pairs": int(alignment["total_gt_pairs"]),
            "review_queue_count_for_source": int(len(alignment["review_queue"])),
            "threshold": float(alignment["threshold"]),
            "per_pair": per_pair,
            "decision_texts": decision_texts,
            "decision_turns": decision_turns,
            "chunking_strategy": chunking_strategy,
        }

    # -- summary ----------------------------------------------------------

    def _build_summary(
        self,
        eval_results: List[Dict[str, Any]],
        pairs_skipped_pending_review: int,
        is_baseline: bool,
        partial_run_warning: bool = False,
        partial_run_detail: Optional[Dict[str, Any]] = None,
        evaluated_pairs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        n = len(eval_results)
        aggregate_coverage = (
            sum(_safe_float(er.get("coverage")) for er in eval_results) / n
            if n > 0
            else 0.0
        )
        aggregate_precision = (
            sum(_safe_float(er.get("precision")) for er in eval_results) / n
            if n > 0
            else 0.0
        )
        total_items_requiring_review = sum(
            int(er.get("items_requiring_review", 0) or 0) for er in eval_results
        )

        by_strategy: Dict[str, Dict[str, float]] = {}
        for strategy in (
            "speaker_turn",
            "character_count_fallback",
            "unknown",
        ):
            bucket = [
                er
                for er in eval_results
                if er.get("chunking_strategy") == strategy
            ]
            cnt = len(bucket)
            by_strategy[strategy] = {
                "coverage": (
                    sum(_safe_float(er.get("coverage")) for er in bucket) / cnt
                    if cnt > 0
                    else 0.0
                ),
                "precision": (
                    sum(_safe_float(er.get("precision")) for er in bucket)
                    / cnt
                    if cnt > 0
                    else 0.0
                ),
                "pairs_count": cnt,
            }

        # Phase O.4: pair_breakdown + per_source_metrics provenance.
        # Pairs lacking a source_id field on the ground_truth_pair
        # record emit ``eval_pair_missing_source_id`` (info severity)
        # via ``_emit_missing_source_id_findings`` so a low-coverage
        # run cannot quietly hide unprovenanced pairs.
        pair_breakdown = self._build_pair_breakdown(
            eval_results, evaluated_pairs or []
        )
        per_source_metrics = self._compute_per_source_metrics(pair_breakdown)
        self._emit_missing_source_id_findings(pair_breakdown)

        # Phase T.5: per-entity-type F1. Computed only when every
        # ground_truth pair carries a ``target_type`` field. Mixed
        # presence is treated as missing -- per-type metrics fabricated
        # from a partial signal would be more misleading than null.
        per_type_metrics, per_type_metrics_reason = (
            self._compute_per_type_metrics(
                eval_results, evaluated_pairs or [],
            )
        )

        # Phase P1 aggregates: roll up source-level spurious_add_rate /
        # per_outcome_f1 / review_queue_count from the new deterministic
        # alignment path. Computed only over eval_results that went
        # through ``_evaluate_pair_p1`` so legacy fixtures don't poison
        # the new metrics with their inline-text scoring.
        p1_aggregates = self._compute_p1_aggregates(eval_results)
        summary: Dict[str, Any] = {
            "eval_summary_id": str(uuid.uuid4()),
            "pipeline_run_id": self.pipeline_run_id,
            "artifact_type": "eval_summary",
            "schema_version": SCHEMA_VERSION_SUMMARY,
            "created_at": _now_iso(),
            "pairs_evaluated": n,
            "pairs_skipped_pending_review": int(pairs_skipped_pending_review),
            "aggregate_coverage": aggregate_coverage,
            "aggregate_precision": aggregate_precision,
            "total_items_requiring_review": total_items_requiring_review,
            "by_chunking_strategy": by_strategy,
            "eval_results": [
                er.get("eval_result_id", "")
                for er in eval_results
                if er.get("eval_result_id")
            ],
            "is_baseline": bool(is_baseline),
            "baseline_eval_summary_id": None,
            "regression_detected": False,
            "regression_detail": [],
            "partial_run_warning": bool(partial_run_warning),
            "partial_run_detail": partial_run_detail,
            "pair_breakdown": pair_breakdown,
            "per_source_metrics": per_source_metrics,
            "per_type_metrics": per_type_metrics,
            "per_type_metrics_reason": per_type_metrics_reason,
            "baseline_scope": None,
            "provenance": {"produced_by": PRODUCED_BY_SUMMARY},
        }
        # Optional Phase P1 fields. Always emit when at least one P1
        # eval_result fed the summary so a regression downstream that
        # changes spurious_add_rate from non-zero to zero is visible.
        if p1_aggregates["covered_p1_pairs"] > 0:
            summary["spurious_add_rate"] = float(p1_aggregates["spurious_add_rate"])
            summary["per_outcome_f1"] = dict(p1_aggregates["per_outcome_f1"])
            summary["review_queue_count"] = int(p1_aggregates["review_queue_count"])
            summary["alignment_threshold"] = float(p1_aggregates["threshold"])
        return summary

    def _compute_p1_aggregates(
        self, eval_results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Aggregate Phase P1 source-level metrics across the run.

        ``spurious_add_rate`` is computed from extracted-decision
        totals, not averaged across sources, so a single source with
        many extractions has the influence its data warrants.

        ``per_outcome_f1`` is averaged across sources for outcomes
        present in at least one source — this matches the run-level
        view of "how is the model doing on each outcome class?".

        ``review_queue_count`` is summed across sources.
        """
        relevant_ids = {
            er.get("eval_result_id")
            for er in eval_results
            if er.get("eval_result_id") in self._p1_eval_result_ids
        }
        covered_p1_pairs = sum(
            1 for er in eval_results if er.get("eval_result_id") in relevant_ids
        )
        # Aggregate across distinct sources that contributed an
        # eval_result in this run.
        sources_in_run = set()
        for er in eval_results:
            if er.get("eval_result_id") in relevant_ids:
                sources_in_run.add(er.get("source_artifact_id") or "")
        total_extracted_global = 0
        unmatched_extracted_global = 0
        outcome_f1_buckets: Dict[str, List[float]] = {}
        review_queue_total = 0
        threshold_seen: Optional[float] = None
        for aggregates in self._p1_source_aggregates.values():
            if aggregates.get("source_id") not in sources_in_run and \
                    not any(
                        self._p1_source_aggregates.get(s) is aggregates
                        for s in sources_in_run
                    ):
                # When the seed-pair source_id differs from the
                # eval_result.source_artifact_id, we still want to
                # include the aggregates -- only skip an aggregates
                # entry that is truly unrelated to this run.
                pass
            te = int(aggregates.get("total_extracted", 0))
            mec = int(aggregates.get("matched_extracted_count", 0))
            total_extracted_global += te
            unmatched_extracted_global += max(0, te - mec)
            review_queue_total += int(aggregates.get("review_queue_count_for_source", 0))
            t = aggregates.get("threshold")
            if isinstance(t, (int, float)):
                threshold_seen = float(t)
            for outcome, f1 in (aggregates.get("per_outcome_f1") or {}).items():
                outcome_f1_buckets.setdefault(outcome, []).append(float(f1))
        per_outcome_f1 = {
            outcome: round(sum(vals) / len(vals), 6) if vals else 0.0
            for outcome, vals in outcome_f1_buckets.items()
        }
        spurious_add_rate = (
            (unmatched_extracted_global / total_extracted_global)
            if total_extracted_global > 0
            else 0.0
        )
        return {
            "covered_p1_pairs": covered_p1_pairs,
            "spurious_add_rate": float(spurious_add_rate),
            "per_outcome_f1": per_outcome_f1,
            "review_queue_count": int(review_queue_total),
            "threshold": float(threshold_seen) if threshold_seen is not None else float(ALIGNMENT_THRESHOLD),
        }

    def _compute_per_type_metrics(
        self,
        eval_results: List[Dict[str, Any]],
        evaluated_pairs: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Dict[str, float]]], Optional[str]]:
        """Aggregate coverage/precision/F1 per ``target_type``.

        Returns ``(metrics_or_none, reason_or_none)``. The metrics dict
        maps every supported target_type (``decision``, ``claim``,
        ``action_item``) to a ``{precision, recall, f1, pairs_count}``
        bucket. When any ground_truth pair lacks ``target_type``, the
        result is ``(None, "target_type_absent_on_N_pairs")``. A
        ``ground_truth_missing_type`` info finding is also emitted.

        F1 is computed from precision/recall via the harmonic mean.
        Recall is sourced from the eval_result's ``coverage`` field
        (this is the production convention: coverage is recall in the
        F1 sense -- "did the extracted set hit the ground truth").
        """
        SUPPORTED_TYPES = ("decision", "claim", "action_item")

        if not evaluated_pairs:
            return None, "no_evaluated_pairs"

        missing = [
            pair for pair in evaluated_pairs
            if not isinstance(pair, dict)
            or not isinstance(pair.get("target_type"), str)
            or pair.get("target_type") not in SUPPORTED_TYPES
        ]
        if missing:
            self._emit_ground_truth_missing_type_finding(len(missing))
            return None, f"target_type_absent_on_{len(missing)}_pairs"

        buckets: Dict[str, Dict[str, float]] = {
            t: {"prec_sum": 0.0, "rec_sum": 0.0, "pairs": 0}
            for t in SUPPORTED_TYPES
        }
        for er, pair in zip(eval_results, evaluated_pairs):
            t = pair.get("target_type")
            if t not in buckets:
                continue
            buckets[t]["pairs"] += 1
            buckets[t]["prec_sum"] += _safe_float(er.get("precision"))
            buckets[t]["rec_sum"] += _safe_float(er.get("coverage"))

        out: Dict[str, Dict[str, float]] = {}
        for t in SUPPORTED_TYPES:
            n = int(buckets[t]["pairs"])
            if n == 0:
                out[t] = {
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1": 0.0,
                    "pairs_count": 0,
                }
                continue
            p = buckets[t]["prec_sum"] / n
            r = buckets[t]["rec_sum"] / n
            denom = p + r
            f1 = (2.0 * p * r / denom) if denom > 0 else 0.0
            out[t] = {
                "precision": round(p, 6),
                "recall": round(r, 6),
                "f1": round(f1, 6),
                "pairs_count": n,
            }
        return out, None

    # -- Phase X2.4 baseline findings -------------------------------------

    def _emit_baseline_set_finding(
        self,
        *,
        summary: Dict[str, Any],
        baseline_scope: Optional[str],
    ) -> None:
        """Emit ``baseline_set`` (info severity) after --set-baseline."""
        if self.sdl_root is None:
            return
        try:
            from ...health.finding import HealthFinding, write_finding
        except ImportError:
            return
        try:
            data_lake_root = self.sdl_root.parent.parent
        except (OSError, AttributeError):
            return

        coverage = _safe_float(summary.get("aggregate_coverage"))
        precision = _safe_float(summary.get("aggregate_precision"))
        per_type = summary.get("per_type_metrics") or {}
        decision_f1 = None
        if isinstance(per_type, dict):
            decision_bucket = per_type.get("decision")
            if isinstance(decision_bucket, dict):
                decision_f1 = _safe_float(decision_bucket.get("f1"))

        try:
            write_finding(
                HealthFinding(
                    finding_code="baseline_set",
                    severity="info",
                    pipeline_run_id=self.pipeline_run_id,
                    context={
                        "coverage": coverage,
                        "precision": precision,
                        "f1": decision_f1,
                        "baseline_scope": baseline_scope,
                        "pairs_count": int(summary.get("pairs_evaluated") or 0),
                        "eval_summary_id": str(summary.get("eval_summary_id") or ""),
                    },
                    remediation=(
                        "Baseline installed. Subsequent runs will be "
                        "compared against this eval_summary. Re-run "
                        "eval-ground-truth --set-baseline to overwrite."
                    ),
                ),
                data_lake_path=data_lake_root,
            )
        except Exception as exc:  # never propagate
            logger.warning("baseline_set_finding_failed: %s", exc)

    def _emit_baseline_requires_successful_run_finding(
        self, source_id: str
    ) -> None:
        if self.sdl_root is None:
            return
        try:
            from ...health.finding import HealthFinding, write_finding
        except ImportError:
            return
        try:
            data_lake_root = self.sdl_root.parent.parent
        except (OSError, AttributeError):
            return
        try:
            write_finding(
                HealthFinding(
                    finding_code="baseline_requires_successful_run",
                    severity="halt",
                    pipeline_run_id=self.pipeline_run_id,
                    context={
                        "source_id": source_id,
                        "last_run_stage_status": "failed",
                    },
                    remediation=(
                        "Re-run the extraction for this source_id and "
                        "confirm orchestration_result.stage_status='ok' "
                        "before retrying --set-baseline."
                    ),
                ),
                data_lake_path=data_lake_root,
            )
        except Exception as exc:  # never propagate
            logger.warning(
                "baseline_requires_successful_run_finding_failed: %s", exc,
            )

    def _last_run_failed_for_source(self, source_id: str) -> bool:
        """Scan the last orchestration_result for ``source_id``.

        Returns True only when an orchestration_result for the source
        is present AND stage_status == "failed". Absence of an
        artifact is treated as not-failed (the user may legitimately
        be installing the very first baseline for a transcript before
        a full pipeline run has been recorded). Phase X2 amendment:
        false negatives are preferred to false positives because the
        gate already blocks via partial_run_warning when no extraction
        exists at all.
        """
        if self.sdl_root is None:
            return False
        candidates: List[Path] = []
        for d in (
            self.sdl_root / "orchestration",
            self.sdl_root / "extractions",
        ):
            if d.is_dir():
                candidates.extend(d.glob("*.json"))
        latest_status: Optional[str] = None
        latest_mtime: float = 0.0
        for path in candidates:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("source_id") != source_id:
                continue
            if doc.get("artifact_type") != "orchestration_result":
                continue
            status = doc.get("stage_status")
            if not isinstance(status, str):
                continue
            mtime = path.stat().st_mtime
            if mtime >= latest_mtime:
                latest_status = status
                latest_mtime = mtime
        return latest_status == "failed"

    def _emit_ground_truth_missing_type_finding(self, count: int) -> None:
        """Emit a ``ground_truth_missing_type`` info finding."""
        if self.sdl_root is None:
            return
        try:
            from ...health.finding import HealthFinding, write_finding
        except ImportError:
            return
        try:
            data_lake_root = self.sdl_root.parent.parent
        except (OSError, AttributeError):
            return
        try:
            write_finding(
                HealthFinding(
                    finding_code="ground_truth_missing_type",
                    severity="info",
                    pipeline_run_id=self.pipeline_run_id,
                    context={"pairs_missing_target_type": int(count)},
                    remediation=(
                        "Add a ``target_type`` field "
                        "(decision|claim|action_item) to every "
                        "ground_truth_pair record so per_type_metrics "
                        "can be computed."
                    ),
                ),
                data_lake_path=data_lake_root,
            )
        except Exception as exc:  # never propagate
            logger.warning(
                "ground_truth_missing_type_finding_failed: %s", exc,
            )

    # -- Phase O.4 pair_breakdown / per_source_metrics ---------------------

    def _build_pair_breakdown(
        self,
        eval_results: List[Dict[str, Any]],
        evaluated_pairs: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """One entry per (pair, eval_result). Pairs without a resolvable
        source_id store ``source_id: None`` so the missing-provenance
        signal stays visible in the artifact (and triggers an
        ``eval_pair_missing_source_id`` info finding via
        ``_emit_missing_source_id_findings``)."""
        out: List[Dict[str, Any]] = []
        for er, pair in zip(eval_results, evaluated_pairs):
            pair_id = (
                er.get("pair_id")
                or (pair.get("pair_id") if isinstance(pair, dict) else "")
                or ""
            )
            source_id = self._resolve_pair_source_id(pair) or None
            agenda_item_id = (
                pair.get("agenda_item_id")
                if isinstance(pair, dict) and isinstance(pair.get("agenda_item_id"), str)
                else None
            )
            ground_truth_text = ""
            if isinstance(pair, dict):
                gt = (
                    pair.get("ground_truth_text")
                    or pair.get("fixture_minutes_text")
                    or ""
                )
                if isinstance(gt, str):
                    ground_truth_text = gt[:500]
            coverage = _safe_float(er.get("coverage"))
            matched = coverage > 0.0
            out.append(
                {
                    "pair_id": pair_id,
                    "source_id": source_id,
                    "agenda_item_id": agenda_item_id,
                    "ground_truth_text": ground_truth_text,
                    "matched": matched,
                    "match_score": float(max(0.0, min(1.0, coverage))),
                    "status": "matched" if matched else "unmatched",
                }
            )
        return out

    def _compute_per_source_metrics(
        self,
        pair_breakdown: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Aggregate match_score / coverage per source_id.

        Suppressed (returns ``None``) when fewer than
        ``_PER_SOURCE_METRICS_MIN_SOURCES`` distinct source_ids are
        present so the rollup never duplicates the aggregate numbers.
        """
        buckets: Dict[str, Dict[str, float]] = {}
        for entry in pair_breakdown:
            sid = entry.get("source_id")
            if not isinstance(sid, str) or not sid:
                continue
            b = buckets.setdefault(
                sid,
                {"pairs": 0, "matched": 0, "coverage_sum": 0.0, "precision_sum": 0.0},
            )
            b["pairs"] += 1
            if entry.get("matched"):
                b["matched"] += 1
            b["coverage_sum"] += float(entry.get("match_score") or 0.0)
            # Precision is per-eval_result; we approximate it via the
            # match_score on the pair (precision == coverage when no
            # extracted-side data is exposed at the pair level). The
            # diff tool reads coverage primarily.
            b["precision_sum"] += float(entry.get("match_score") or 0.0)
        if len(buckets) < _PER_SOURCE_METRICS_MIN_SOURCES:
            return None
        out: Dict[str, Dict[str, Any]] = {}
        for sid, b in buckets.items():
            pairs = int(b["pairs"])
            matched = int(b["matched"])
            cov = b["coverage_sum"] / pairs if pairs > 0 else 0.0
            prec = b["precision_sum"] / pairs if pairs > 0 else 0.0
            out[sid] = {
                "pairs": pairs,
                "matched": matched,
                "coverage": float(max(0.0, min(1.0, cov))),
                "precision": float(max(0.0, min(1.0, prec))),
            }
        return out

    def _emit_missing_source_id_findings(
        self,
        pair_breakdown: List[Dict[str, Any]],
    ) -> None:
        if self.sdl_root is None:
            return
        missing = [
            entry for entry in pair_breakdown
            if not entry.get("source_id")
        ]
        if not missing:
            return
        try:
            from ...health.finding import HealthFinding, write_finding
        except ImportError:
            return
        # The HealthFinding write site needs ``<data_lake>/`` (i.e. the
        # repo root above ``store/``). ``self.sdl_root`` already points
        # into ``store/artifacts``; back out to the data-lake root.
        try:
            data_lake_root = self.sdl_root.parent.parent
        except (OSError, AttributeError):
            return
        for entry in missing:
            try:
                write_finding(
                    HealthFinding(
                        finding_code="eval_pair_missing_source_id",
                        severity="info",
                        pipeline_run_id=self.pipeline_run_id,
                        context={
                            "pair_id": str(entry.get("pair_id") or ""),
                            "agenda_item_id": entry.get("agenda_item_id"),
                        },
                        remediation=(
                            "Add a ``source_id`` (or ``fixture_source_id``) "
                            "field to the ground_truth_pair record so the "
                            "per_source_metrics rollup can include this pair."
                        ),
                    ),
                    data_lake_path=data_lake_root,
                )
            except Exception as exc:  # never propagate
                logger.warning(
                    "eval_pair_missing_source_id_write_failed: %s", exc,
                )

    # -- partial-run detection (Phase O.4) ---------------------------------

    def _compute_partial_run_signal(
        self, confirmed_pairs: List[Dict[str, Any]]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Return (partial_run_warning, partial_run_detail).

        ``expected`` = number of confirmed ground_truth_pairs.
        ``actual``   = number of confirmed pairs whose meeting_extraction
                       artifact is present on disk under
                       ``$SDL_ROOT/extractions/``.
        ``missing_source_ids`` lists the source_ids whose extraction is
        absent.

        Empty confirmed list (expected == 0) yields warning=False — there
        is nothing to be partial about (divide-by-zero guard).
        """
        expected = len(confirmed_pairs)
        if expected == 0:
            return (False, {"expected": 0, "actual": 0, "missing_source_ids": []})

        present = 0
        missing: List[str] = []
        for pair in confirmed_pairs:
            sid = self._resolve_pair_source_id(pair)
            if not sid:
                # No source_id => we cannot prove extraction exists.
                # Treat as missing so the warning fires loudly.
                missing.append(pair.get("pair_id", "") or "<unknown>")
                continue
            if self._meeting_extraction_exists_for_source(sid, pair):
                present += 1
            else:
                missing.append(sid)

        warning = present < expected
        return (
            warning,
            {
                "expected": int(expected),
                "actual": int(present),
                "missing_source_ids": missing,
            },
        )

    def _resolve_pair_source_id(self, pair: Dict[str, Any]) -> str:
        sid = pair.get("fixture_source_id")
        if isinstance(sid, str) and sid:
            return sid
        # Fall back to source_record.payload.source_id when available.
        sa_id = pair.get("source_artifact_id") or ""
        rec = self._load_source_record(sa_id, source_id_hint=None)
        if isinstance(rec, dict):
            payload = rec.get("payload") or {}
            sid = payload.get("source_id")
            if isinstance(sid, str) and sid:
                return sid
        return ""

    def _meeting_extraction_exists_for_source(
        self, source_id: str, pair: Dict[str, Any]
    ) -> bool:
        # Fixture-injected pairs carry their extracted items inline on
        # the pair record itself; for those there is no separate
        # meeting_extraction artifact and the partial-run check would
        # always misfire. Treat the fixture key as evidence that an
        # extraction equivalent exists.
        if isinstance(pair.get("fixture_extracted_items"), list):
            return True
        if self.sdl_root is None:
            return False
        extractions_dir = self.sdl_root / "extractions"
        if not extractions_dir.is_dir():
            return False
        direct = extractions_dir / f"{source_id}_meeting_extraction.json"
        if direct.is_file():
            return True
        # Indirect: filename is <source_artifact_id>_meeting_extraction.json.
        sa_id = pair.get("source_artifact_id") or ""
        if isinstance(sa_id, str) and sa_id:
            indirect = extractions_dir / f"{sa_id}_meeting_extraction.json"
            if indirect.is_file():
                return True
        # Last-resort scan: match by payload contents.
        for path in extractions_dir.glob("*_meeting_extraction.json"):
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("source_id") == source_id:
                return True
            if isinstance(sa_id, str) and sa_id and obj.get(
                "source_artifact_id"
            ) == sa_id:
                return True
        return False

    # -- baseline + run-count ---------------------------------------------

    def _baseline_path(self) -> Path:
        assert self.sdl_root is not None
        return self.sdl_root / "evals" / "baseline_eval_summary.json"

    def _run_count_path(self) -> Path:
        assert self.sdl_root is not None
        return self.sdl_root / "evals" / "eval_run_count.json"

    def _load_baseline(self) -> Optional[Dict[str, Any]]:
        if self.sdl_root is None:
            return None
        path = self._baseline_path()
        if not path.is_file():
            return None
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return rec if isinstance(rec, dict) else None

    def _write_baseline(self, summary: Dict[str, Any]) -> None:
        if self.sdl_root is None:
            return
        baseline = dict(summary)
        baseline["is_baseline"] = True
        _write_json(self._baseline_path(), baseline)

    def _load_run_count(self) -> int:
        if self.sdl_root is None:
            return 0
        path = self._run_count_path()
        if not path.is_file():
            return 0
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(rec, dict):
            return 0
        try:
            return int(rec.get("count", 0))
        except (TypeError, ValueError):
            return 0

    def _bump_run_count(self) -> int:
        count = self._load_run_count() + 1
        if self.sdl_root is not None:
            _write_json(
                self._run_count_path(),
                {"count": count, "last_updated": _now_iso()},
            )
        return count

    def _load_pair_results_for_summary(
        self, summary: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if self.sdl_root is None:
            return []
        ids = summary.get("eval_results") or []
        if not isinstance(ids, list):
            return []
        out: List[Dict[str, Any]] = []
        for eid in ids:
            if not isinstance(eid, str) or not eid:
                continue
            path = self.sdl_root / "evals" / "results" / f"{eid}.json"
            if not path.is_file():
                continue
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    # -- write + validate --------------------------------------------------

    def _validate_and_write(
        self, schema_name: str, artifact: Dict[str, Any], path: Path
    ) -> bool:
        schema = _load_schema(schema_name)
        if schema is not None:
            try:
                jsonschema.Draft202012Validator(schema).validate(artifact)
            except jsonschema.ValidationError as exc:
                logger.warning(
                    "schema_violation schema=%s path=%s err=%s",
                    schema_name,
                    path,
                    exc.message,
                )
                # Write a sibling .invalid.json so a human can inspect
                # what failed, then stop. Returning False lets the
                # caller decide whether to abort or proceed.
                _write_json(path.with_suffix(".invalid.json"), artifact)
                return False
        return _write_json(path, artifact)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_cli_report(result: Dict[str, Any]) -> str:
    """Render the eval result as a CLI report (human + machine readable)."""
    if result.get("status") == "skipped":
        return (
            "=== Ground Truth Eval ===\n"
            f"skipped: {result.get('reason', 'unknown')}\n"
        )
    if result.get("status") == "failed":
        return (
            "=== Ground Truth Eval ===\n"
            f"failed: {result.get('reason', 'unknown')}\n"
        )

    summary = result.get("summary") or {}
    gate = result.get("gate_decision") or {}
    eval_results = result.get("eval_results") or []

    lines: List[str] = []
    lines.append("=== Ground Truth Eval ===")
    lines.append(f"Pipeline run: {summary.get('pipeline_run_id', '')}")
    lines.append(
        f"Pairs evaluated: {summary.get('pairs_evaluated', 0)} "
        f"(confirmed only; pending_review excluded: "
        f"{summary.get('pairs_skipped_pending_review', 0)})"
    )
    lines.append("")
    lines.append(
        "| pair_id  | coverage | precision | review_queue |"
    )
    lines.append(
        "|----------|----------|-----------|--------------|"
    )
    for er in eval_results:
        pid = (er.get("pair_id") or "")[:8] + "..."
        cov = float(er.get("coverage", 0.0))
        prec = float(er.get("precision", 0.0))
        rev = int(er.get("items_requiring_review", 0))
        lines.append(
            f"| {pid:<8} | {cov:8.3f} | {prec:9.3f} | {rev:12d} |"
        )
    lines.append("")
    lines.append(
        f"Aggregate coverage: {summary.get('aggregate_coverage', 0.0):.3f} "
        f"| Aggregate precision: {summary.get('aggregate_precision', 0.0):.3f}"
    )
    lines.append(
        f"Items requiring review: "
        f"{summary.get('total_items_requiring_review', 0)} across all pairs"
    )
    lines.append(f"Gate decision: {gate.get('decision', 'unknown')}"
                 f" -- reason: {gate.get('reason', '')}")
    lines.append(f"run_count: {result.get('run_count', 0)}")
    if gate.get("regression_detail"):
        lines.append("Regression detail:")
        for r in gate["regression_detail"]:
            lines.append(
                f"  pair={r.get('pair_id', '')} "
                f"metric={r.get('metric', '')} "
                f"baseline={r.get('baseline_value', 0):.3f} "
                f"current={r.get('current_value', 0):.3f} "
                f"delta={r.get('delta', 0):+.3f}"
            )
    return "\n".join(lines) + "\n"
