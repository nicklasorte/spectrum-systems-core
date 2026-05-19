"""Correction miner (System 2 of the self-improvement loop).

Reads every ``comparison_result`` for a ``source_id``, mines the
systematic patterns in what Haiku misses, asks Opus for targeted,
ADDITIVE prompt additions, re-evaluates each candidate by running the
real Haiku governed loop with the candidate prompt, and promotes the
best candidate via a PR — but ONLY when it improves F1 vs Opus by
STRICTLY more than 0.05.

Invariants (enforced here and by tests):

* The pattern classifier is a pure keyword heuristic — NO model call.
* Candidate generation uses the Opus model resolved from
  ``ai/registry/model_registry.json`` (``complex_reasoning`` key).
* Candidate EVALUATION runs Haiku (``extraction`` key) — we improve
  Haiku, not Opus. Scoring with Opus would be meaningless.
* Prompt edits are ADDITIVE: the current prompt text is never
  rewritten, only appended to.
* The current prompt is backed up BEFORE it is modified.
* Promotion ALWAYS opens a PR — the prompt is never silently changed.
* The comparison metric is imported from ``compare_opus_haiku`` and
  never reimplemented here.
* No model string is hardcoded — all resolve from the registry.

Fail-closed reason codes:

* ``no_comparisons``        — no comparison_result artifacts for source
* ``missing_baseline_f1``   — no haiku_vs_opus_comparison eval_history
                              row to gate against
* ``invalid_comparison``    — a comparison_result failed its schema
* ``data_lake_not_a_directory``
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _artifact_validator import (  # noqa: E402
    ArtifactValidationError,
    validate_artifact,
)
import compare_opus_haiku as cmp  # noqa: E402

_REPO_ROOT = _SCRIPTS_DIR.parent
_DEFAULT_REGISTRY = (
    _REPO_ROOT / "ai" / "registry" / "model_registry.json"
)
_PROMPT_PATH = (
    _REPO_ROOT
    / "src"
    / "spectrum_systems_core"
    / "workflows"
    / "prompts"
    / "meeting_minutes_llm.md"
)

# Registry keys — NEVER a hardcoded model string. The generator is an
# Opus-tier reasoning model; the evaluator is the Haiku extraction
# model (the pipeline model we are improving).
GENERATOR_MODEL_KEY = "complex_reasoning"
EVALUATOR_MODEL_KEY = "extraction"

# Offline transport seams — the SAME pattern
# ``create_opus_reference_baselines._STUB_ENV`` uses. When set, the
# value is returned verbatim by the corresponding client instead of
# constructing the real Anthropic transport, so the integration
# contract test (and a key-free dry-run smoke) exercise the real
# orchestration with no API key. These are TRANSPORT stubs only — the
# model string is still resolved from the registry and stamped into
# ``generated_by``; the stub never overrides it. They only activate
# when explicitly set, so they cannot silently shadow a production run.
_OPUS_STUB_ENV = "CORRECTION_MINER_OPUS_STUB_RESPONSE"
_HAIKU_STUB_ENV = "CORRECTION_MINER_HAIKU_STUB_RESPONSE"

PROMOTION_THRESHOLD = 0.05  # strictly greater than this to promote
# delta_f1 is f1 - baseline_f1, both IEEE floats, so a mathematically
# exact 0.05 delta (e.g. 0.55 - 0.50) lands at 0.050000000000000044.
# Fail-closed reading of "strictly MORE than 5 points": a delta within
# float noise of the threshold is NOT a promotion. Only a delta that
# clears the threshold by more than this epsilon promotes.
_PROMO_EPS = 1e-9
MAX_CANDIDATES_CAP = 3


def exceeds_promotion_threshold(delta_f1: float) -> bool:
    """True iff ``delta_f1`` is strictly above 0.05 beyond float noise.

    Centralised so the gate and ``better_than_baseline`` cannot drift,
    and so a 0.05-exact delta provably does NOT promote.
    """
    return delta_f1 > (PROMOTION_THRESHOLD + _PROMO_EPS)

# Marker delimiting an appended correction block so a human reviewer
# (and a rollback) can see exactly what was added and that nothing
# above it changed.
_ADDITION_MARKER = (
    "\n\n<!-- correction-miner addition: {pattern} "
    "({ts}) — ADDITIVE, do not edit above -->\n"
)

PATTERN_TYPES: Dict[str, str] = {
    "implicit_decision": (
        "decision not stated as a decision; stated as a direction, "
        "agreement, or outcome"
    ),
    "cross_turn_item": (
        "item requires reading across multiple speaker turns to fully "
        "understand"
    ),
    "attributed_indirectly": (
        "owner or actor is implicit, not explicitly named in the same "
        "sentence"
    ),
    "technical_detail": (
        "fine-grained technical parameter (frequency, threshold, band) "
        "stated without explicit label"
    ),
    "procedural_commitment": (
        "commitment stated as procedure or next-step rather than as an "
        "action item"
    ),
    "deferred_item": (
        "deferral mistaken for no-decision; the deferral itself is the "
        "decision"
    ),
    # Phase 1 — surfaced from grounding_rejection_report artifacts. The
    # miner reads these and attributes each rejected item to one of the
    # four reason codes the gate emits.
    "hallucination_paraphrase": (
        "the model emitted a source_quote that is a paraphrase or "
        "fabrication of a real transcript span — the canonical "
        "hallucination signal (reason_code "
        "grounding_exact_text_not_in_transcript)"
    ),
    "hallucination_offset_drift": (
        "the model emitted a real transcript quote with a wrong byte "
        "offset (reason_code grounding_offset_mismatch); the gate "
        "rejects because the offset assertion is part of the contract"
    ),
    "hallucination_missing_field": (
        "the model emitted an item without the required source_quote / "
        "quote_offset_normalized / source_turn_ids field (reason_code "
        "grounding_missing_field); the item cannot be grounded at all"
    ),
    "hallucination_unknown_turn": (
        "the model emitted a turn_aggregate item referencing a "
        "turn_id that is not in the transcript turn index (reason_code "
        "grounding_unknown_turn_id)"
    ),
}

# Map from grounding gate reason_code -> miner pattern_type. Used to
# fold grounding_rejection_report artifacts into the same failure
# pattern bucket the rest of the miner already understands.
_GROUNDING_REASON_TO_PATTERN: Dict[str, str] = {
    "grounding_exact_text_not_in_transcript": "hallucination_paraphrase",
    "grounding_offset_mismatch": "hallucination_offset_drift",
    "grounding_missing_field": "hallucination_missing_field",
    "grounding_unknown_turn_id": "hallucination_unknown_turn",
}

_GENERATION_INSTRUCTION = (
    "Generate a targeted addition to this prompt that addresses this "
    "failure pattern. The addition must be:\n"
    "1. Additive only — do not modify existing instructions\n"
    "2. Include an explicit example from the 7 GHz downlink domain\n"
    "3. Include hallucination defense: if not in transcript, omit\n"
    "4. Under 150 words\n"
    "Return only the addition text, no preamble."
)


class CorrectionMinerError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclasses.dataclass
class FailurePattern:
    pattern_type: str
    frequency: int
    percentage_of_fns: float
    example_items: List[Dict[str, Any]]


@dataclasses.dataclass
class PromptCandidate:
    candidate_id: str
    pattern_addressed: str
    pattern_frequency: int
    prompt_addition: str
    full_prompt: str
    generated_by: str
    generated_at: str


@dataclasses.dataclass
class CandidateScore:
    candidate_id: str
    f1_vs_opus: float
    recall_vs_opus: float
    precision_vs_opus: float
    gt_recall: float
    baseline_f1: float
    delta_f1: float
    better_than_baseline: bool


# --------------------------------------------------------------------------
# Model registry — never a hardcoded model string.
# --------------------------------------------------------------------------
def load_model_registry(
    registry_path: Optional[Path] = None,
) -> Dict[str, Any]:
    path = Path(registry_path) if registry_path else _DEFAULT_REGISTRY
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorrectionMinerError(
            "missing_model_registry",
            f"cannot read model registry at {path}: {exc}",
        ) from exc
    if not isinstance(reg, dict) or not isinstance(
        reg.get("models"), dict
    ):
        raise CorrectionMinerError(
            "invalid_model_registry",
            f"model registry at {path} has no 'models' object",
        )
    return reg


def resolve_model(registry: Dict[str, Any], key: str) -> str:
    model = (registry.get("models") or {}).get(key)
    if not isinstance(model, str) or not model.strip():
        raise CorrectionMinerError(
            "model_key_missing",
            f"model registry has no usable '{key}' entry",
        )
    return model.strip()


def _now_utc_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


# --------------------------------------------------------------------------
# Step 2-A — pattern analyzer (PURE; no model call).
# --------------------------------------------------------------------------
def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


_DEFER_KW = (
    "defer",
    "deferred",
    "postpone",
    "table this",
    "tabled",
    "revisit",
    "pending further",
    "hold off",
    "punt",
)
_TECH_KW = (
    "dbm",
    "mhz",
    "ghz",
    "khz",
    " db ",
    "erp",
    "threshold",
    "frequency",
    "bandwidth",
    "spectral",
    "power level",
    "-47",
    "dbm/mhz",
)
_PROC_KW = (
    "will ",
    "next step",
    "follow up",
    "follow-up",
    "circulate",
    "submit",
    "send ",
    "schedule",
    "by next",
    "to do",
    "action item",
)
_CROSS_KW = (
    "as discussed",
    "as mentioned",
    "per the earlier",
    "building on",
    "following up on",
    "referring back",
    "as noted earlier",
    "earlier point",
)
_INDIRECT_KW = (
    "they ",
    "someone",
    "the team",
    "it was agreed",
    "it was decided",
    "will be handled",
    "to be assigned",
    "tbd",
)
# Explicit decision verbs — their ABSENCE on a decision-type FN is the
# "implicit_decision" signal.
_EXPLICIT_DECISION_VERBS = (
    "approved",
    "rejected",
    "decided",
    "adopted",
    "ratified",
    "denied",
    "voted",
)


def classify_false_negative(text: str, extraction_type: str) -> str:
    """Deterministic keyword classifier. NO model call.

    Priority order is fixed so the same FN always maps to the same
    pattern (replay-stable analysis).
    """
    t = _norm(text)
    et = (extraction_type or "").lower()
    if any(k in t for k in _DEFER_KW):
        return "deferred_item"
    if any(k in t for k in _TECH_KW):
        return "technical_detail"
    if "action" in et or "commitment" in et:
        if any(k in t for k in _PROC_KW):
            return "procedural_commitment"
    if any(k in t for k in _CROSS_KW):
        return "cross_turn_item"
    if any(k in t for k in _INDIRECT_KW):
        return "attributed_indirectly"
    if "decision" in et:
        # A decision-type FN with no explicit decision verb is the
        # canonical implicit_decision; even WITH a verb the default
        # for an un-othered decision miss is implicit_decision.
        return "implicit_decision"
    if any(k in t for k in _PROC_KW):
        return "procedural_commitment"
    # Generic fallback — an unlabelled miss reads as an implicit item.
    return "implicit_decision"


def analyze_failure_patterns(
    comparison_results: List[Dict[str, Any]],
    grounding_rejections: Optional[List[Dict[str, Any]]] = None,
) -> List[FailurePattern]:
    """Classify every false_negative across all comparisons.

    Pure: no I/O, no model. Returns patterns sorted by frequency
    (desc), then pattern_type (asc) for a stable order.

    Phase 1: when ``grounding_rejections`` is supplied, each rejected
    item is folded into a ``hallucination_*`` pattern keyed by its
    gate ``reason_code``. The grounded-rejection counts add to the
    total denominator so the percentage_of_fns figure stays meaningful.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {
        p: [] for p in PATTERN_TYPES
    }
    total = 0
    for comp in comparison_results:
        for fn in comp.get("false_negatives") or []:
            text = (
                fn.get("text_preview")
                or fn.get("ground_truth_text")
                or ""
            )
            etype = fn.get("extraction_type", "")
            ptype = classify_false_negative(text, etype)
            buckets[ptype].append(
                {
                    "text_preview": str(text)[:200],
                    "extraction_type": etype,
                }
            )
            total += 1
    for report in grounding_rejections or []:
        for rejection in report.get("rejected_items") or []:
            reason = rejection.get("reason_code", "")
            ptype = _GROUNDING_REASON_TO_PATTERN.get(reason)
            if ptype is None:
                # Unknown reason — skip rather than mis-classify.
                continue
            quote = (
                rejection.get("expected_quote_normalized")
                or (
                    rejection.get("item", {}).get("source_quote", "")
                    if isinstance(rejection.get("item"), dict)
                    else ""
                )
            )
            buckets[ptype].append(
                {
                    "text_preview": str(quote)[:200],
                    "extraction_type": rejection.get("item_type", ""),
                    "reason_code": reason,
                }
            )
            total += 1
    patterns: List[FailurePattern] = []
    for ptype, items in buckets.items():
        if not items:
            continue
        patterns.append(
            FailurePattern(
                pattern_type=ptype,
                frequency=len(items),
                percentage_of_fns=(
                    len(items) / total if total else 0.0
                ),
                example_items=items[:3],
            )
        )
    patterns.sort(key=lambda p: (-p.frequency, p.pattern_type))
    return patterns


def load_grounding_rejection_reports(
    data_lake: Path, source_id: str
) -> List[Dict[str, Any]]:
    """Read every grounding_rejection_report__*.json under the source's
    diagnostics directory.

    A missing diagnostics directory is NOT an error: pre-Phase-1
    artifacts simply have no such reports. The miner falls back to the
    legacy comparison-only pattern analysis.

    The schema is validated before any field is read (read-path
    co-requirement).
    """
    diag_dir = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "diagnostics"
    )
    if not diag_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(
        diag_dir.glob("grounding_rejection_report__*.json")
    ):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            validate_artifact(
                doc, "grounding_rejection_report", str(path)
            )
        except ArtifactValidationError:
            continue
        out.append(doc)
    return out


# --------------------------------------------------------------------------
# Step 2-B — candidate generator (uses Opus from the registry).
# --------------------------------------------------------------------------
def _opus_client(model: str) -> Callable[..., str]:
    """Default Opus transport. Lazily built so tests inject a stub and
    never touch the network. Reuses the SAME structural client seam the
    rest of the repo uses."""
    from spectrum_systems_core.workflows.llm_client import (
        AnthropicJSONClient,
    )

    return AnthropicJSONClient(model=model)


def _stub_client(response: str) -> Callable[..., str]:
    def _c(*, system: str, user: str) -> str:  # noqa: ARG001
        return response

    return _c


def _resolve_generator_client(
    injected: Optional[Callable[..., str]], model: str
) -> Callable[..., str]:
    if injected is not None:
        return injected
    stub = os.environ.get(_OPUS_STUB_ENV)
    if stub is not None:
        return _stub_client(stub)
    return _opus_client(model)


def _resolve_evaluator_client(
    injected: Optional[Callable[..., str]], model: str
) -> Callable[..., str]:
    if injected is not None:
        return injected
    stub = os.environ.get(_HAIKU_STUB_ENV)
    if stub is not None:
        return _stub_client(stub)
    return cmp_haiku_client(model)


def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    return body.strip()


def generate_candidates(
    patterns: List[FailurePattern],
    current_prompt_path: Path,
    model_registry: Dict[str, Any],
    *,
    client: Optional[Callable[..., str]] = None,
    max_candidates: int = MAX_CANDIDATES_CAP,
) -> List[PromptCandidate]:
    """One ADDITIVE candidate per top failure pattern, via Opus.

    The current prompt is read but NEVER rewritten: ``full_prompt`` is
    the current text plus a clearly-marked appended block.
    """
    max_candidates = max(0, min(int(max_candidates), MAX_CANDIDATES_CAP))
    if max_candidates == 0 or not patterns:
        return []
    current_prompt = current_prompt_path.read_text(encoding="utf-8")
    opus_model = resolve_model(model_registry, GENERATOR_MODEL_KEY)
    active = _resolve_generator_client(client, opus_model)

    candidates: List[PromptCandidate] = []
    for pattern in patterns[:max_candidates]:
        examples = "\n".join(
            f"- ({ex.get('extraction_type')}) {ex.get('text_preview')}"
            for ex in pattern.example_items
        )
        user = (
            "CURRENT EXTRACTION PROMPT:\n"
            "----------------------------------------\n"
            f"{current_prompt}\n"
            "----------------------------------------\n\n"
            f"FAILURE PATTERN: {pattern.pattern_type}\n"
            f"DEFINITION: {PATTERN_TYPES.get(pattern.pattern_type, '')}\n"
            f"FREQUENCY: {pattern.frequency} "
            f"({pattern.percentage_of_fns:.0%} of all misses)\n"
            f"EXAMPLE MISSED ITEMS:\n{examples}\n\n"
            f"{_GENERATION_INSTRUCTION}"
        )
        raw = active(
            system=(
                "You are improving a deterministic extraction prompt. "
                "Output ONLY an additive instruction block."
            ),
            user=user,
        )
        addition = _strip_fence(raw)
        ts = _now_utc_iso()
        marker = _ADDITION_MARKER.format(
            pattern=pattern.pattern_type, ts=ts
        )
        full_prompt = current_prompt.rstrip() + marker + addition + "\n"
        candidates.append(
            PromptCandidate(
                candidate_id=str(uuid.uuid4()),
                pattern_addressed=pattern.pattern_type,
                pattern_frequency=pattern.frequency,
                prompt_addition=addition,
                full_prompt=full_prompt,
                generated_by=opus_model,
                generated_at=ts,
            )
        )
    return candidates


# --------------------------------------------------------------------------
# Step 2-C — candidate evaluator (runs HAIKU through the governed loop).
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _prompt_override(text: str):
    """Point the LLM workflow at a temp prompt file for the duration.

    This reuses the ENTIRE governed loop + every LLM eval gate + the
    real writer with a different system prompt — nothing is bypassed
    and the comparison metric is not reimplemented. The canonical
    ``meeting_minutes_llm.md`` on disk is never touched.
    """
    import tempfile

    from spectrum_systems_core.workflows import meeting_minutes_llm

    original = meeting_minutes_llm._PROMPT_PATH
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(text)
        tmp.close()
        meeting_minutes_llm._PROMPT_PATH = Path(tmp.name)
        yield
    finally:
        meeting_minutes_llm._PROMPT_PATH = original
        with contextlib.suppress(OSError):
            Path(tmp.name).unlink()


def read_baseline_f1(data_lake: Path, source_id: str) -> float:
    """Most-recent ``haiku_vs_opus_comparison`` F1 from eval_history.

    Fail-closed: a candidate cannot be gated without a baseline, so a
    missing row halts rather than defaulting to 0.0 (which would let
    any candidate look like a win).
    """
    path = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "eval_history.jsonl"
    )
    if not path.is_file():
        raise CorrectionMinerError(
            "missing_baseline_f1",
            f"no eval_history.jsonl at {path}",
        )
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(rec, dict)
            and rec.get("eval_type") == "haiku_vs_opus_comparison"
            and isinstance(rec.get("haiku_f1_vs_opus"), (int, float))
        ):
            rows.append(rec)
    if not rows:
        raise CorrectionMinerError(
            "missing_baseline_f1",
            f"no haiku_vs_opus_comparison row in {path}",
        )
    rows.sort(key=lambda r: str(r.get("timestamp", "")))
    return float(rows[-1]["haiku_f1_vs_opus"])


def evaluate_candidate(
    candidate: PromptCandidate,
    source_id: str,
    data_lake: Path,
    model_registry: Dict[str, Any],
    *,
    client: Optional[Callable[..., str]] = None,
    baseline_f1: Optional[float] = None,
    transcript_path: Optional[Path] = None,
) -> CandidateScore:
    """Run HAIKU (registry ``extraction`` key) with the candidate
    prompt through the real governed loop, then score it with the
    imported comparison metric (never reimplemented).
    """
    haiku_model = resolve_model(model_registry, EVALUATOR_MODEL_KEY)
    client = _resolve_evaluator_client(client, haiku_model)

    from spectrum_systems_core.workflows.meeting_minutes_llm import (
        run_meeting_minutes_llm_workflow,
    )

    # Read the transcript the SAME way the pipeline does so the
    # candidate is evaluated on the real input.
    transcript = _load_transcript(data_lake, source_id, transcript_path)

    with _prompt_override(candidate.full_prompt):
        result = run_meeting_minutes_llm_workflow(
            transcript,
            client=client,
            meeting_id=source_id,
            source_id=source_id,
            lake_root=data_lake / "store",
        )

    haiku_payload = (
        result.meeting_minutes.payload
        if result.meeting_minutes is not None
        else {}
    )
    baseline_rows = cmp.load_opus_baseline(data_lake, source_id)
    gt_pairs = cmp.load_gt_pairs(data_lake, source_id)
    types = cmp.extraction_types()
    metrics = cmp.compute_comparison(
        baseline_rows=baseline_rows,
        haiku_payload=haiku_payload,
        gt_pairs=gt_pairs,
        types=types,
    )["summary"]

    if baseline_f1 is None:
        baseline_f1 = read_baseline_f1(data_lake, source_id)
    f1 = metrics["haiku_f1_vs_opus"]
    delta = f1 - baseline_f1
    return CandidateScore(
        candidate_id=candidate.candidate_id,
        f1_vs_opus=f1,
        recall_vs_opus=metrics["haiku_recall_vs_opus"],
        precision_vs_opus=metrics["haiku_precision_vs_opus"],
        gt_recall=metrics["gt_recall_haiku"],
        baseline_f1=baseline_f1,
        delta_f1=delta,
        better_than_baseline=exceeds_promotion_threshold(delta),
    )


def cmp_haiku_client(model: str) -> Callable[..., str]:
    """Default Haiku transport (registry-resolved model). Separate
    named function so a test can assert the evaluator built a client
    with the HAIKU model string, never Opus."""
    from spectrum_systems_core.workflows.llm_client import (
        AnthropicJSONClient,
    )

    return AnthropicJSONClient(model=model)


# Transcript-resolution constants. The processed-meeting directory
# holds the transcript ALONGSIDE the JSON product artifacts; only the
# real transcript extensions are eligible and the two well-known JSON
# products are excluded by name as a second line of defence (they are
# ``.json`` so the extension filter already excludes them — the name
# guard documents intent and survives a future extension list change).
_TRANSCRIPT_EXTS = (".txt", ".md", ".docx")
_MINUTES_PREFIX = "meeting_minutes__"
_NON_TRANSCRIPT_NAMES = frozenset({"source_record.json"})


def _read_transcript_file(path: Path) -> str:
    """Read one transcript file to non-empty plain text.

    ``.docx`` goes through the real ``DocxExtractor`` (no LLM call);
    ``.txt`` / ``.md`` are read directly. Fail-closed: a corrupt,
    unreadable, or empty file raises ``missing_transcript`` with the
    actual path and the underlying read error so the operator can see
    exactly which file failed and why.
    """
    suffix = path.suffix.lower()
    if suffix == ".docx":
        import tempfile

        from spectrum_systems_core.ingestion.docx_extractor import (
            DocxExtractor,
        )

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "t.txt"
            res = DocxExtractor().extract(
                str(path), output_path=str(out)
            )
            if res.get("status") != "success":
                raise CorrectionMinerError(
                    "missing_transcript",
                    f"docx at {path} is unreadable: "
                    f"{res.get('reason')}",
                )
            text = out.read_text(encoding="utf-8")
            if not text.strip():
                raise CorrectionMinerError(
                    "missing_transcript",
                    f"docx at {path} extracted to empty text",
                )
            return text
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorrectionMinerError(
            "missing_transcript",
            f"transcript at {path} is unreadable: {exc}",
        ) from exc
    if not text.strip():
        raise CorrectionMinerError(
            "missing_transcript",
            f"transcript at {path} is empty",
        )
    return text


def _transcript_from_source_record(
    store: Path, source_id: str
) -> Optional[Path]:
    """Resolve the transcript via the AUTHORITATIVE ``source_record.json``.

    ``store/processed/meetings/<source_id>/source_record.json`` is
    written by the ingestion pipeline (``SourceLoader``) and records the
    transcript it actually processed in ``payload.raw_path`` — a path
    relative to the data-lake ``store/`` root (e.g.
    ``raw/meetings/<source_id>/source.txt``). That is the canonical
    pointer back to the input; the directory globs that follow are
    heuristic fallbacks for sources whose record predates this field or
    is incomplete.

    Non-fatal at every intermediate step (returns ``None`` so the caller
    falls through, NEVER raises):

    * no ``source_record.json`` on disk;
    * the file is unreadable or not valid JSON / not an object;
    * ``payload`` or ``payload.raw_path`` is missing/empty;
    * the recorded path resolves to nothing on this machine.

    A WARNING is logged on every fall-through so an operator can see why
    the authoritative pointer was not used. Only when the file the
    record points to EXISTS is it returned — and then the caller reads
    it strictly (a corrupt authoritative transcript is a real error the
    operator must see, exactly as for a processed-dir transcript). No
    LLM call: pure filesystem + JSON.
    """
    record_path = (
        store / "processed" / "meetings" / source_id / "source_record.json"
    )
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"WARNING: source_record.json at {record_path} is "
            f"unreadable/not JSON ({exc}); falling through to "
            f"transcript auto-detection",
            file=sys.stderr,
        )
        return None
    if not isinstance(record, dict):
        print(
            f"WARNING: source_record.json at {record_path} is not a "
            f"JSON object; falling through to transcript auto-detection",
            file=sys.stderr,
        )
        return None
    payload = record.get("payload")
    raw_path = (
        payload.get("raw_path") if isinstance(payload, dict) else None
    )
    if not isinstance(raw_path, str) or not raw_path.strip():
        print(
            f"WARNING: source_record.json at {record_path} has no "
            f"payload.raw_path transcript pointer; falling through to "
            f"transcript auto-detection",
            file=sys.stderr,
        )
        return None
    raw_path = raw_path.strip()

    # ``raw_path`` is normally relative to the data-lake ``store/``
    # root. ``store / rel`` resolves it; if the record stored an
    # ABSOLUTE path (e.g. written on a different machine), Path-join
    # keeps it absolute so we try it as-is first, then re-root the
    # segment after the last ``store`` component under THIS store/ so a
    # cross-machine but identically-laid-out path still resolves. All
    # deterministic, no network.
    candidates: List[Path] = []
    rp = Path(raw_path)
    if rp.is_absolute():
        candidates.append(rp)
        parts = rp.parts
        if "store" in parts:
            last_store = max(
                i for i, p in enumerate(parts) if p == "store"
            )
            tail = parts[last_store + 1:]
            if tail:
                candidates.append(store.joinpath(*tail))
    else:
        candidates.append(store / rp)

    for cand in candidates:
        if cand.is_file():
            return cand

    print(
        f"WARNING: source_record.json at {record_path} points to "
        f"{raw_path!r} but no such file exists (checked "
        f"{[str(c) for c in candidates]}); falling through to "
        f"transcript auto-detection",
        file=sys.stderr,
    )
    return None


def _find_transcript_in_dir(directory: Path) -> Optional[Path]:
    """First transcript-like file directly in ``directory``.

    Non-recursive (subdirectories such as ``markdown/``,
    ``comparisons/``, ``reference_baselines/`` are never descended) and
    deterministic (alphabetical). The ``meeting_minutes__*`` and
    ``source_record.json`` product artifacts that share the
    processed-meeting directory are excluded. Multiple matches do not
    fail — the first alphabetically is used and a warning is logged.
    """
    if not directory.is_dir():
        return None
    candidates: List[Path] = []
    for child in sorted(directory.iterdir()):
        if not child.is_file():
            continue
        if child.suffix.lower() not in _TRANSCRIPT_EXTS:
            continue
        if child.name.startswith(_MINUTES_PREFIX):
            continue
        if child.name in _NON_TRANSCRIPT_NAMES:
            continue
        candidates.append(child)
    if not candidates:
        return None
    if len(candidates) > 1:
        print(
            f"WARNING: multiple transcript files in {directory}: "
            f"{[c.name for c in candidates]}; "
            f"using {candidates[0].name} (first alphabetically)",
            file=sys.stderr,
        )
    return candidates[0]


def _load_transcript(
    data_lake: Path,
    source_id: str,
    transcript_path: Optional[Path] = None,
) -> str:
    """Resolve and read the raw transcript text for ``source_id``.

    Resolution order (priority — first match wins, NOT a fallback
    chain that masks errors). No LLM call anywhere in this path:

    1. ``transcript_path`` override — when given, the search is skipped
       ENTIRELY and this exact path is read. A missing override fails
       closed immediately, before any model call.
    2. ``source_record.json`` — the AUTHORITATIVE pointer written by
       the ingestion pipeline at
       ``store/processed/meetings/<source_id>/source_record.json``. Its
       ``payload.raw_path`` records the transcript the pipeline actually
       processed (relative to the data-lake ``store/`` root). Checked
       BEFORE the directory globs because transcripts are NOT co-located
       with the processed artifacts in the common case. A missing
       record / missing field / dangling path is non-fatal: a WARNING
       is logged and resolution continues (older records may be
       incomplete). A file the record DOES point to is read strictly.
    3. ``store/processed/meetings/<source_id>/`` — glob fallback for
       transcripts co-located with the ``meeting_minutes__*.json`` and
       ``source_record.json`` product artifacts (both excluded). A
       transcript found here is read strictly: a corrupt/empty file
       raises rather than silently falling through to the raw path.
    4. ``store/raw/transcripts/`` — the original location, preserved
       as a fallback (legacy flat ``<source_id>.txt`` /
       ``<source_id>.docx`` names, then a ``<source_id>/`` subdir).

    Fail-closed: if no readable transcript is found, raise
    ``missing_transcript`` listing every location checked.
    """
    if transcript_path is not None:
        override = Path(transcript_path)
        if not override.is_file():
            raise CorrectionMinerError(
                "missing_transcript",
                f"--transcript-path {override} does not exist or is "
                f"not a file",
            )
        return _read_transcript_file(override)

    store = data_lake / "store"
    processed_dir = store / "processed" / "meetings" / source_id
    raw_dir = store / "raw" / "transcripts"
    record_path = processed_dir / "source_record.json"

    # 2. Authoritative pointer: source_record.json. First match wins;
    #    a found file is read strictly (corrupt authoritative input is
    #    a real error, not a reason to silently glob elsewhere).
    found = _transcript_from_source_record(store, source_id)
    if found is not None:
        return _read_transcript_file(found)

    found = _find_transcript_in_dir(processed_dir)
    if found is not None:
        # A transcript exists in the processed dir: that IS the input.
        # Read it strictly — a corrupt file is a real error the
        # operator must see, not a reason to look elsewhere.
        return _read_transcript_file(found)

    # Fallback: the original raw/transcripts location. The legacy flat
    # filenames are read leniently (empty/unreadable -> try the next
    # legacy candidate) to preserve the pre-existing behaviour, then
    # the <source_id>/ subdirectory form.
    for name in (f"{source_id}.txt", f"{source_id}.docx"):
        legacy = raw_dir / name
        if legacy.is_file():
            try:
                return _read_transcript_file(legacy)
            except CorrectionMinerError:
                continue
    found = _find_transcript_in_dir(raw_dir / source_id)
    if found is not None:
        return _read_transcript_file(found)

    raise CorrectionMinerError(
        "missing_transcript",
        f"no readable transcript for {source_id}; checked "
        f"(1) source_record.json {record_path}, "
        f"(2) processed dir {processed_dir}, "
        f"(3) raw flat {raw_dir / (source_id + '.txt')} / "
        f"{raw_dir / (source_id + '.docx')}, "
        f"(4) raw subdir {raw_dir / source_id}{os.sep}",
    )


# --------------------------------------------------------------------------
# Step 2-D — promotion gate (STRICTLY > 0.05; PR always).
# --------------------------------------------------------------------------
def _default_pr_opener(
    *, branch: str, title: str, body: str, files: List[str]
) -> Dict[str, Any]:
    """Create a branch, commit the prompt change + backup, push, and
    open a PR via the GitHub CLI. Replaceable in tests with a stub."""

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            check=True,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )

    _git("checkout", "-b", branch)
    for f in files:
        _git("add", f)
    _git("commit", "-m", title)
    _git("push", "-u", "origin", branch)
    res = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            "main",
            "--head",
            branch,
            "--draft",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return {
        "returncode": res.returncode,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "branch": branch,
    }


def _repo_rel(p: Path) -> str:
    """Repo-relative path for ``git add`` when under the repo (the
    production case); the absolute path otherwise (tests use a temp
    prompt and a stub pr_opener that ignores this)."""
    try:
        return str(p.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(p)


def _backup_prompt(prompt_path: Path, timestamp: str) -> Path:
    # Backups land in a gitignored ``backups/`` sibling of the prompt,
    # never in the prompt source directory itself. The source tree is
    # canonical; backups are ephemeral rollback artifacts. The
    # ``backups/`` directory and the bare backup-file pattern are both
    # gitignored at ``src/spectrum_systems_core/workflows/prompts/.gitignore``
    # so a stray ``git add`` cannot re-commit them.
    safe_ts = timestamp.replace(":", "").replace("+", "")
    backup_dir = prompt_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"meeting_minutes_llm_backup_{safe_ts}.md"
    backup.write_text(
        prompt_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return backup


def promote_best_candidate(
    candidates: List[CandidateScore],
    candidate_prompts: List[PromptCandidate],
    current_prompt_path: Path,
    data_lake: Path,
    *,
    pr_opener: Optional[Callable[..., Dict[str, Any]]] = None,
    on_backup: Optional[Callable[[Path], None]] = None,
) -> Dict[str, Any]:
    """Promote the best candidate IFF it beats baseline by > 0.05.

    Selection: highest ``f1_vs_opus``. Gate: ``delta_f1`` strictly
    above 0.05 beyond float noise (0.05 exactly does NOT promote — see
    ``exceeds_promotion_threshold``). The backup file is fully written
    and closed BEFORE the live prompt is overwritten; ``on_backup``
    (used by tests) is invoked AFTER the backup exists and BEFORE the
    prompt is rewritten so the ordering is provable.
    """
    if not candidates:
        return {"promoted": False, "reason": "no_candidates"}

    best = max(candidates, key=lambda c: c.f1_vs_opus)
    if not exceeds_promotion_threshold(best.delta_f1):
        return {
            "promoted": False,
            "reason": (
                "no candidate improved F1 by threshold (>0.05); "
                "no promotion"
            ),
            "best_candidate_id": best.candidate_id,
            "best_delta_f1": best.delta_f1,
        }

    prompt = next(
        (
            p
            for p in candidate_prompts
            if p.candidate_id == best.candidate_id
        ),
        None,
    )
    if prompt is None:
        raise CorrectionMinerError(
            "candidate_prompt_missing",
            f"no PromptCandidate for winning id {best.candidate_id}",
        )

    timestamp = _now_utc_iso()
    # 1. Backup BEFORE any modification.
    backup_path = _backup_prompt(current_prompt_path, timestamp)
    if on_backup is not None:
        # Ordering proof hook: at this point the live prompt MUST still
        # be the original (backup created, prompt not yet rewritten).
        on_backup(backup_path)
    # 2. Write the new (additive) prompt.
    current_prompt_path.write_text(
        prompt.full_prompt, encoding="utf-8"
    )

    # 3. Open the PR — promotion is NEVER a silent prompt change.
    #    (The append-only correction_miner_promotion eval_history row
    #    is written by run_correction_miner, which holds the real
    #    source_id — keeping this function free of CLI state.)
    opener = pr_opener or _default_pr_opener
    branch = (
        f"claude/correction-{prompt.pattern_addressed}-"
        f"{best.candidate_id[:8]}"
    )
    title = (
        f"prompt(correction): address {prompt.pattern_addressed} "
        f"(+{best.delta_f1:.1%} F1 vs Opus)"
    )
    body = _pr_body(best, candidates, prompt, backup_path)
    # Only the prompt change is committed. The backup is an out-of-tree
    # rollback artifact (see ``_backup_prompt``); committing it would
    # leak runner state into source control.
    pr_result = opener(
        branch=branch,
        title=title,
        body=body,
        files=[_repo_rel(current_prompt_path)],
    )
    return {
        "promoted": True,
        "candidate_id": best.candidate_id,
        "pattern_addressed": prompt.pattern_addressed,
        "delta_f1": best.delta_f1,
        "baseline_f1": best.baseline_f1,
        "new_f1": best.f1_vs_opus,
        "backup_path": str(backup_path),
        "branch": branch,
        "pr_result": pr_result,
    }


def _pr_body(
    best: CandidateScore,
    candidates: List[CandidateScore],
    prompt: PromptCandidate,
    backup_path: Path,
) -> str:
    rows = [
        "| candidate_id | pattern | f1 | recall | precision | "
        "delta_f1 | promoted |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        rows.append(
            f"| `{c.candidate_id[:8]}` | "
            f"{'(this)' if c.candidate_id == best.candidate_id else ''} "
            f"| {c.f1_vs_opus:.3f} | {c.recall_vs_opus:.3f} | "
            f"{c.precision_vs_opus:.3f} | {c.delta_f1:+.3f} | "
            f"{'YES' if c.candidate_id == best.candidate_id else 'no'} |"
        )
    table = "\n".join(rows)
    return (
        f"## Correction miner promotion\n\n"
        f"**Pattern addressed:** `{prompt.pattern_addressed}` — "
        f"{PATTERN_TYPES.get(prompt.pattern_addressed, '')}\n\n"
        f"**Why:** this pattern was the most frequent systematic miss "
        f"in the Haiku-vs-Opus comparison "
        f"(frequency {prompt.pattern_frequency}).\n\n"
        f"### Prompt addition (additive only — nothing above it "
        f"changed)\n\n```diff\n+ "
        + "\n+ ".join(prompt.prompt_addition.splitlines())
        + "\n```\n\n"
        f"### Candidate scores\n\n{table}\n\n"
        f"### Before / after\n\n"
        f"- baseline F1 vs Opus: {best.baseline_f1:.3f}\n"
        f"- new F1 vs Opus: {best.f1_vs_opus:.3f} "
        f"(Δ {best.delta_f1:+.3f}, strictly > 0.05 gate)\n"
        f"- new recall vs Opus: {best.recall_vs_opus:.3f}\n"
        f"- new precision vs Opus: {best.precision_vs_opus:.3f}\n\n"
        f"### Rollback\n\n"
        f"```\ncp {backup_path} {_PROMPT_PATH}\n```\n"
    )


# --------------------------------------------------------------------------
# Loader for comparison_result artifacts (validated before any read).
# --------------------------------------------------------------------------
def load_comparison_results(
    data_lake: Path, source_id: str
) -> List[Dict[str, Any]]:
    comp_dir = (
        data_lake
        / "store"
        / "processed"
        / "meetings"
        / source_id
        / "comparisons"
    )
    if not comp_dir.is_dir():
        raise CorrectionMinerError(
            "no_comparisons",
            f"no comparisons directory at {comp_dir}",
        )
    out: List[Dict[str, Any]] = []
    for path in sorted(comp_dir.glob("haiku_vs_opus_*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorrectionMinerError(
                "invalid_comparison",
                f"comparison_result at {path} unreadable/!json: {exc}",
            ) from exc
        # Read-path co-requirement: validate before reading fields.
        try:
            validate_artifact(doc, "comparison_result", str(path))
        except ArtifactValidationError as exc:
            raise CorrectionMinerError(
                "invalid_comparison",
                f"comparison_result at {path} failed schema: {exc}",
            ) from exc
        out.append(doc)
    if not out:
        raise CorrectionMinerError(
            "no_comparisons",
            f"no comparison_result artifacts under {comp_dir}",
        )
    return out


# --------------------------------------------------------------------------
# Orchestration + CLI.
# --------------------------------------------------------------------------
def run_correction_miner(
    *,
    data_lake: Path,
    source_id: str,
    dry_run: bool,
    max_candidates: int,
    registry_path: Optional[Path] = None,
    transcript_path: Optional[Path] = None,
    opus_client: Optional[Callable[..., str]] = None,
    haiku_client: Optional[Callable[..., str]] = None,
    pr_opener: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    registry = load_model_registry(registry_path)
    comparisons = load_comparison_results(data_lake, source_id)
    grounding_rejections = load_grounding_rejection_reports(
        data_lake, source_id
    )
    patterns = analyze_failure_patterns(
        comparisons, grounding_rejections=grounding_rejections
    )
    pattern_report = [dataclasses.asdict(p) for p in patterns]

    candidates = generate_candidates(
        patterns,
        _PROMPT_PATH,
        registry,
        client=opus_client,
        max_candidates=max_candidates,
    )
    candidate_report = [
        {
            "candidate_id": c.candidate_id,
            "pattern_addressed": c.pattern_addressed,
            "pattern_frequency": c.pattern_frequency,
            "generated_by": c.generated_by,
            "prompt_addition": c.prompt_addition,
        }
        for c in candidates
    ]

    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "source_id": source_id,
            "patterns": pattern_report,
            "candidates": candidate_report,
            "scores": [],
            "promotion": {
                "promoted": False,
                "reason": "dry_run (no evaluation, no PR)",
            },
        }

    baseline_f1 = read_baseline_f1(data_lake, source_id)
    scores: List[CandidateScore] = []
    for cand in candidates:
        scores.append(
            evaluate_candidate(
                cand,
                source_id,
                data_lake,
                registry,
                client=haiku_client,
                baseline_f1=baseline_f1,
                transcript_path=transcript_path,
            )
        )

    promotion = promote_best_candidate(
        scores,
        candidates,
        _PROMPT_PATH,
        data_lake,
        pr_opener=pr_opener,
    )
    if promotion.get("promoted"):
        # The promotion eval_history row is keyed by the real source.
        cmp._append_eval_history(
            data_lake,
            source_id,
            {
                "eval_type": "correction_miner_promotion",
                "candidate_id": promotion["candidate_id"],
                "delta_f1": promotion["delta_f1"],
                "pattern_addressed": promotion["pattern_addressed"],
                "baseline_f1": promotion["baseline_f1"],
                "new_f1": promotion["new_f1"],
                "backup_path": promotion["backup_path"],
                "promoted_at": _now_utc_iso(),
            },
        )

    return {
        "status": "success",
        "dry_run": False,
        "source_id": source_id,
        "patterns": pattern_report,
        "candidates": candidate_report,
        "scores": [dataclasses.asdict(s) for s in scores],
        "promotion": promotion,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Dry run (default true): analyze + generate, no eval/PR.",
    )
    parser.add_argument(
        "--max-candidates", type=int, default=MAX_CANDIDATES_CAP
    )
    parser.add_argument(
        "--transcript-path",
        dest="transcript_path",
        default="",
        help=(
            "Explicit path to the transcript file. When set, skips "
            "auto-detection entirely and reads this exact path "
            "(fail-closed if it does not exist)."
        ),
    )
    args = parser.parse_args(argv)
    for attr in vars(args):
        val = getattr(args, attr)
        if isinstance(val, str):
            setattr(args, attr, val.strip())

    data_lake = Path(args.data_lake)
    if not data_lake.is_dir():
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": "data_lake_not_a_directory",
                    "detail": str(data_lake),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    transcript_path = (
        Path(args.transcript_path) if args.transcript_path else None
    )

    try:
        result = run_correction_miner(
            data_lake=data_lake,
            source_id=args.source_id,
            dry_run=args.dry_run,
            max_candidates=args.max_candidates,
            transcript_path=transcript_path,
        )
    except CorrectionMinerError as exc:
        print(
            json.dumps(
                {
                    "status": "failure",
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
                indent=2,
                sort_keys=True,
            )
        )
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
