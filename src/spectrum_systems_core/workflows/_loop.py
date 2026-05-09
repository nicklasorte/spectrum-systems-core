from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from ..artifacts import Artifact, ArtifactStore, new_artifact
from ..context import build_context_bundle
from ..control import decide_control
from ..evals import run_required_evals
from ..promotion import promote_if_allowed


@dataclass
class GovernedRun:
    context_bundle: Artifact
    target: Artifact
    eval_results: list[Artifact]
    control_decision: Artifact
    promoted: bool
    store: ArtifactStore


def derive_trace_id(input_text: str) -> str:
    digest = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
    return f"trace-{digest[:16]}"


def run_governed_loop(
    *,
    input_text: str,
    artifact_type: str,
    extract: Callable[[str], dict],
) -> GovernedRun:
    """One Produce -> Evaluate -> Decide -> Promote pass for any artifact_type."""
    store = ArtifactStore()
    trace_id = derive_trace_id(input_text)

    context_bundle = build_context_bundle(
        input_text=input_text,
        purpose=artifact_type,
        trace_id=trace_id,
    )
    store.put(context_bundle)

    target = new_artifact(
        artifact_type=artifact_type,
        payload=extract(input_text),
        trace_id=trace_id,
        status="draft",
        input_refs=[context_bundle.artifact_id],
    )
    store.put(target)

    eval_results = run_required_evals(target)
    for r in eval_results:
        store.put(r)

    decision = decide_control(target, eval_results)
    store.put(decision)

    promote_if_allowed(store, target, decision)

    return GovernedRun(
        context_bundle=context_bundle,
        target=target,
        eval_results=eval_results,
        control_decision=decision,
        promoted=target.status == "promoted",
        store=store,
    )
