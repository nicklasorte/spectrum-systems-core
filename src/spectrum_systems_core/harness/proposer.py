"""Phase AA.4 — the Meta-Harness proposer agent.

The proposer is an Opus agent reached through an injected seam (the
same pattern Phase Y.5's correction miner uses for its Opus call). It
NEVER touches the filesystem itself: the outer-loop driver (AA.7)
assembles a :class:`ProposerContext` from disk and hands it in.

**The proposer never self-validates.** This module deliberately does
not import ``harness_mutation_validator``. A proposer that could see
the validator's verdict could learn to game it; allowlist validation
is exclusively the outer-loop driver's job (AA.7), with a
defense-in-depth recheck in the code-candidate evaluator (AA.5).
``build_harness_code_candidate`` therefore *embeds* an
``allowlist_validation_result`` that the driver computed and passed in
— it does not compute one.

Output is one of:

* Type A — a ``correction_candidate`` (schema 1.1.0,
  ``candidate_source: "proposer"``) routed to the existing Phase Y
  candidate-evaluator pipeline. No new evaluation code.
* Type B — a ``harness_code_candidate`` (schema 1.0.0) routed to AA.5.
* "none" — an explicit statement that every obvious improvement is
  already on the Pareto frontier. The proposer must say this rather
  than emit a dominated candidate.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from ..artifacts import Artifact, new_artifact
from ..data_lake.serialize import canonical_json

CORRECTION_CANDIDATE_TYPE = "correction_candidate"
CORRECTION_CANDIDATE_SCHEMA_VERSION = "1.1.0"
CODE_CANDIDATE_TYPE = "harness_code_candidate"
CODE_CANDIDATE_SCHEMA_VERSION = "1.0.0"

_CC_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "correction_candidate.schema.json"
)
_HCC_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "schemas"
    / "harness"
    / "harness_code_candidate.schema.json"
)

# (system_prompt, context) -> structured proposal dict.
ProposeCall = Callable[[str, "ProposerContext"], dict]
Clock = Callable[[], str]


class ProposerError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class ProposerContext:
    transcript_id: str
    current_trial_id: str
    score_summaries: list[dict] = field(default_factory=list)
    experience_rows: list[dict] = field(default_factory=list)
    harness_snapshot_paths: list[str] = field(default_factory=list)
    current_harness_files: dict[str, str] = field(default_factory=dict)
    false_negative_set: dict = field(default_factory=dict)
    pareto_frontier: list[dict] = field(default_factory=list)

    def known_trial_ids(self) -> list[str]:
        """Every trial id discoverable from the context, sorted. Used to
        guarantee a proposal records the trials it had access to even
        if the model omits some from ``trial_ids_read``."""
        ids: set[str] = set()
        if self.current_trial_id:
            ids.add(self.current_trial_id)
        for s in self.score_summaries:
            tid = s.get("trial_id")
            if isinstance(tid, str) and tid:
                ids.add(tid)
        return sorted(ids)


@dataclass(frozen=True)
class ProposerProposal:
    candidate_type: str  # "A" | "B" | "none"
    trial_ids_read: list[str]
    proposer_reasoning: str
    hypothesis: str
    predicted_improvement: str
    proposed_prompt_addition: str | None = None
    schema_type: str | None = None
    proposed_diff: str | None = None
    frontier_statement: str | None = None


SYSTEM_PROMPT_TEMPLATE = """\
You are the Spectrum Systems harness proposer. Your goal is to improve
extraction F1 on {transcript_id} by analyzing prior extraction trials
and proposing a targeted change to the harness.

You have access to:
- All prior score summaries (F1 scores by type, false negative counts)
- All prior execution traces (per-chunk prompts, outputs, extraction results)
- All prior harness snapshots (the actual code at each trial)
- The current false negative set (items the harness missed)
- The current Pareto frontier (what has and hasn't worked)

You may propose ONE of:
A) A prompt/context change (a correction_candidate) — for changes to
   what the model is asked to extract or how examples are presented
B) A code change (a harness_code_candidate) — for changes to chunking
   logic, context assembly, or retrieval logic in allowlisted files only

For whichever you propose, you must state:
1. Which prior trials you examined and what you learned from them
2. A specific, falsifiable hypothesis about WHY the current harness fails
3. The minimum change that tests that hypothesis
4. What you predict will happen to F1 if you are correct

Do not propose changes you have already tried. Check the Pareto frontier
first. Do not propose changes to governance files, schemas, or control
logic. If all obvious improvements are already on the frontier, say so
explicitly rather than proposing a dominated candidate.
"""


def build_system_prompt(context: ProposerContext) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(transcript_id=context.transcript_id)


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _default_opus_call(system_prompt: str, context: ProposerContext) -> dict:
    """Single real Opus call. Fail-closed; never returns a default.

    Raises on a missing key or any SDK/parse error so the caller records
    a ``proposer_error`` — there is no silent fallback proposal.
    """
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not api_key.strip():
        raise ProposerError(
            "missing_credentials:ANTHROPIC_API_KEY",
            reason_code="proposer_no_credentials",
        )
    from anthropic import Anthropic

    from ..extraction.llm_opus import OPUS_MODEL

    user = (
        "Analyze the following harness state and return ONLY a single "
        "JSON object with keys: candidate_type ('A'|'B'|'none'), "
        "trial_ids_read (list of strings), proposer_reasoning, "
        "hypothesis, predicted_improvement, and either "
        "proposed_prompt_addition+schema_type (type A), proposed_diff "
        "(type B, unified diff), or frontier_statement (none).\n\n"
        f"SCORE_SUMMARIES:\n{json.dumps(context.score_summaries)}\n\n"
        f"PARETO_FRONTIER:\n{json.dumps(context.pareto_frontier)}\n\n"
        f"FALSE_NEGATIVE_SET:\n{json.dumps(context.false_negative_set)}\n\n"
        f"HARNESS_SNAPSHOTS:\n{json.dumps(context.harness_snapshot_paths)}"
    )
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user}],
    )
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    raw = "\n".join(parts).strip()
    if not raw:
        raise ProposerError(
            "opus_proposal_empty", reason_code="proposer_empty_output"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProposerError(
            f"opus_proposal_not_json: {exc}",
            reason_code="proposer_bad_output",
        ) from exc


def propose(
    context: ProposerContext,
    *,
    opus_call: ProposeCall | None = None,
    clock: Clock | None = None,
) -> ProposerProposal:
    """Ask the proposer for ONE proposal. No validation, no write.

    The returned proposal carries the raw narrative + (for Type B) the
    proposed diff. The driver validates and writes; this function never
    imports the allowlist validator.
    """
    call = opus_call or _default_opus_call
    system_prompt = build_system_prompt(context)
    out = call(system_prompt, context)
    if not isinstance(out, dict):
        raise ProposerError(
            f"proposer returned {type(out).__name__}, expected dict",
            reason_code="proposer_bad_output",
        )

    ctype = out.get("candidate_type")
    if ctype not in {"A", "B", "none"}:
        raise ProposerError(
            f"invalid candidate_type {ctype!r}",
            reason_code="proposer_bad_output",
        )

    for narrative in (
        "proposer_reasoning",
        "hypothesis",
        "predicted_improvement",
    ):
        if ctype != "none" and not str(out.get(narrative) or "").strip():
            raise ProposerError(
                f"proposer omitted required field {narrative!r}",
                reason_code="proposer_bad_output",
            )

    # Union model-declared trial_ids_read with every trial discoverable
    # from the context, so the proposal always records the trials it
    # actually had access to (Red-Team: a proposer cannot under-report
    # what it inspected).
    declared = out.get("trial_ids_read")
    declared_list = [
        str(t) for t in declared if isinstance(t, str) and t
    ] if isinstance(declared, list) else []
    trial_ids_read = sorted(
        set(declared_list) | set(context.known_trial_ids())
    )

    if ctype == "A":
        addition = str(out.get("proposed_prompt_addition") or "").strip()
        schema_type = str(out.get("schema_type") or "").strip()
        if not addition or not schema_type:
            raise ProposerError(
                "Type-A proposal missing proposed_prompt_addition/"
                "schema_type",
                reason_code="proposer_bad_output",
            )
        return ProposerProposal(
            candidate_type="A",
            trial_ids_read=trial_ids_read,
            proposer_reasoning=str(out["proposer_reasoning"]).strip(),
            hypothesis=str(out["hypothesis"]).strip(),
            predicted_improvement=str(
                out["predicted_improvement"]
            ).strip(),
            proposed_prompt_addition=addition,
            schema_type=schema_type,
        )
    if ctype == "B":
        diff = str(out.get("proposed_diff") or "").strip()
        if not diff:
            raise ProposerError(
                "Type-B proposal missing proposed_diff",
                reason_code="proposer_bad_output",
            )
        return ProposerProposal(
            candidate_type="B",
            trial_ids_read=trial_ids_read,
            proposer_reasoning=str(out["proposer_reasoning"]).strip(),
            hypothesis=str(out["hypothesis"]).strip(),
            predicted_improvement=str(
                out["predicted_improvement"]
            ).strip(),
            proposed_diff=diff,
        )
    statement = str(out.get("frontier_statement") or "").strip()
    if not statement:
        raise ProposerError(
            "'none' proposal must carry an explicit frontier_statement",
            reason_code="proposer_bad_output",
        )
    return ProposerProposal(
        candidate_type="none",
        trial_ids_read=trial_ids_read,
        proposer_reasoning=str(
            out.get("proposer_reasoning") or statement
        ).strip(),
        hypothesis=str(out.get("hypothesis") or "").strip(),
        predicted_improvement=str(
            out.get("predicted_improvement") or ""
        ).strip(),
        frontier_statement=statement,
    )


def _candidate_id(context: ProposerContext, kind: str, body: str) -> str:
    seed = f"{context.transcript_id}|{context.current_trial_id}|{kind}|{body}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def build_correction_candidate(
    proposal: ProposerProposal,
    context: ProposerContext,
    *,
    clock: Clock | None = None,
) -> Artifact:
    """Type A — a ``correction_candidate`` with
    ``candidate_source: "proposer"`` routed to the existing Phase Y
    pipeline. Validated against the correction_candidate schema before
    the artifact is returned so a shape regression fails here."""
    if proposal.candidate_type != "A":
        raise ProposerError(
            "build_correction_candidate requires a Type-A proposal",
            reason_code="proposer_bad_output",
        )
    now = (clock or _now)()
    payload = {
        "artifact_type": CORRECTION_CANDIDATE_TYPE,
        "schema_version": CORRECTION_CANDIDATE_SCHEMA_VERSION,
        "correction_candidate_id": _candidate_id(
            context, "A", proposal.proposed_prompt_addition or ""
        ),
        "source_id": context.transcript_id,
        "created_at": now,
        "status": "pending",
        "candidate_source": "proposer",
        "target_transcript_id": context.transcript_id,
        "schema_type": proposal.schema_type,
        "proposed_prompt_addition": proposal.proposed_prompt_addition,
    }
    schema = json.loads(_CC_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)
    return new_artifact(
        artifact_type=CORRECTION_CANDIDATE_TYPE,
        payload=payload,
        trace_id=f"proposer-A-{uuid.uuid4().hex[:16]}",
        status="draft",
        input_refs=[context.current_trial_id],
    )


def build_harness_code_candidate(
    proposal: ProposerProposal,
    context: ProposerContext,
    *,
    allowlist_validation_result: dict,
    clock: Clock | None = None,
) -> Artifact:
    """Type B — assemble the ``harness_code_candidate`` envelope.

    ``allowlist_validation_result`` is computed by the OUTER-LOOP
    DRIVER (AA.7) and passed in. This function only embeds it; it never
    computes one (the proposer never self-validates). The driver must
    NOT call this when ``allowlist_validation_result["valid"]`` is
    false — that path emits ``proposer_rejected_invalid_diff`` and
    writes nothing.
    """
    if proposal.candidate_type != "B":
        raise ProposerError(
            "build_harness_code_candidate requires a Type-B proposal",
            reason_code="proposer_bad_output",
        )
    now = (clock or _now)()
    payload = {
        "artifact_type": CODE_CANDIDATE_TYPE,
        "schema_version": CODE_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": _candidate_id(
            context, "B", proposal.proposed_diff or ""
        ),
        "produced_at": now,
        "transcript_id": context.transcript_id,
        "trial_ids_read": list(proposal.trial_ids_read),
        "proposed_diff": proposal.proposed_diff,
        "proposer_reasoning": proposal.proposer_reasoning,
        "hypothesis": proposal.hypothesis,
        "predicted_improvement": proposal.predicted_improvement,
        "allowlist_validation_result": allowlist_validation_result,
    }
    schema = json.loads(_HCC_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)
    return new_artifact(
        artifact_type=CODE_CANDIDATE_TYPE,
        payload=payload,
        trace_id=f"proposer-B-{uuid.uuid4().hex[:16]}",
        status="draft",
        input_refs=[context.current_trial_id],
    )


def candidate_canonical_json(artifact: Artifact) -> str:
    """Deterministic on-disk form for a proposer candidate envelope."""
    from ..data_lake.serialize import artifact_to_dict

    return canonical_json(artifact_to_dict(artifact))


__all__ = [
    "CORRECTION_CANDIDATE_TYPE",
    "CODE_CANDIDATE_TYPE",
    "ProposerError",
    "ProposerContext",
    "ProposerProposal",
    "SYSTEM_PROMPT_TEMPLATE",
    "build_system_prompt",
    "propose",
    "build_correction_candidate",
    "build_harness_code_candidate",
    "candidate_canonical_json",
]
