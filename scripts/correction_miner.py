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
) -> List[FailurePattern]:
    """Classify every false_negative across all comparisons.

    Pure: no I/O, no model. Returns patterns sorted by frequency
    (desc), then pattern_type (asc) for a stable order.
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
    transcript = _load_transcript(data_lake, source_id)

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


def _load_transcript(data_lake: Path, source_id: str) -> str:
    """Best-effort raw transcript text for the source.

    Looks at the contract path first; falls back to the raw docx via
    the pipeline extractor. Fail-closed: no transcript -> halt (we
    never evaluate a candidate on an empty input).
    """
    txt = (
        data_lake
        / "store"
        / "raw"
        / "transcripts"
        / f"{source_id}.txt"
    )
    if txt.is_file():
        text = txt.read_text(encoding="utf-8")
        if text.strip():
            return text
    docx = (
        data_lake
        / "store"
        / "raw"
        / "transcripts"
        / f"{source_id}.docx"
    )
    if docx.is_file():
        import tempfile

        from spectrum_systems_core.ingestion.docx_extractor import (
            DocxExtractor,
        )

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "t.txt"
            res = DocxExtractor().extract(
                str(docx), output_path=str(out)
            )
            if res.get("status") == "success":
                text = out.read_text(encoding="utf-8")
                if text.strip():
                    return text
    raise CorrectionMinerError(
        "missing_transcript",
        f"no readable transcript for {source_id} under "
        f"{data_lake}/store/raw/transcripts/",
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
    safe_ts = timestamp.replace(":", "").replace("+", "")
    backup = (
        prompt_path.parent
        / f"meeting_minutes_llm_backup_{safe_ts}.md"
    )
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
    pr_result = opener(
        branch=branch,
        title=title,
        body=body,
        files=[
            _repo_rel(current_prompt_path),
            _repo_rel(backup_path),
        ],
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
    opus_client: Optional[Callable[..., str]] = None,
    haiku_client: Optional[Callable[..., str]] = None,
    pr_opener: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    registry = load_model_registry(registry_path)
    comparisons = load_comparison_results(data_lake, source_id)
    patterns = analyze_failure_patterns(comparisons)
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

    try:
        result = run_correction_miner(
            data_lake=data_lake,
            source_id=args.source_id,
            dry_run=args.dry_run,
            max_candidates=args.max_candidates,
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
