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


def _call_extract(
    extract: Callable, input_text: str, chunks: list[dict] | None
) -> dict:
    """Call ``extract`` with chunks only when chunks is non-None.

    Phase Y adds an optional ``chunks`` argument to extract functions.
    Existing test helpers monkeypatch single-arg ``extract`` callables;
    those still work because we only pass chunks when we have them.
    """
    if chunks is None:
        return extract(input_text)
    return extract(input_text, chunks)


def run_governed_loop(
    *,
    input_text: str,
    artifact_type: str,
    extract: Callable,
    chunks: list[dict] | None = None,
    extra_evals: list[Callable[[Artifact], Artifact]] | None = None,
    debug_hook: Callable[[Artifact, list[Artifact]], None] | None = None,
) -> GovernedRun:
    """One Produce -> Evaluate -> Decide -> Promote pass for any artifact_type.

    ``chunks`` is the optional Phase Y turn-chunked transcript. When
    provided, the extract function is called with ``(input_text, chunks)``
    and is expected to emit a ``schema_version`` 1.1.0 payload with
    ``source_turns`` on every extracted item. When omitted, the extract
    function is called with ``(input_text,)`` and emits the original
    1.0.0-style payload (backward-compatible — see runner.py).

    ``extra_evals`` is an optional list of ``(target) -> eval_result``
    callables appended to the required evals BEFORE the control decision.
    It is how the live-LLM workflow attaches its workflow-scoped gates
    (strict schema, non-empty, within-source, GT-coverage) without
    polluting the global ``run_required_evals`` sequence — the regex
    path passes ``None`` and is byte-for-byte unchanged. The extra eval
    results flow through the SAME ``decide_control`` / ``promote`` gate,
    so a failed extra eval blocks promotion exactly like a required one.

    ``debug_hook`` is an optional ``(target, eval_results) -> None``
    observability callback invoked AFTER every eval has run but BEFORE
    ``decide_control`` aggregates them into a decision. It is read-only
    by contract (the caller must not mutate ``target`` / ``eval_results``)
    and its return value is ignored, so it cannot influence control or
    promotion. Default ``None`` means the call is skipped entirely —
    the regex path passes ``None`` and stays byte-for-byte unchanged.
    """
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
        payload=_call_extract(extract, input_text, chunks),
        trace_id=trace_id,
        status="draft",
        input_refs=[context_bundle.artifact_id],
    )
    store.put(target)

    eval_results = run_required_evals(target)
    if extra_evals:
        eval_results = eval_results + [e(target) for e in extra_evals]
    for r in eval_results:
        store.put(r)

    if debug_hook is not None:
        # Observe-only, BEFORE aggregation. Decomposes the about-to-be
        # aggregated eval_results back to their per-chunk sources so an
        # operator can see WHICH chunk produced a blocking item. Never
        # alters target / eval_results / the decision.
        debug_hook(target, eval_results)

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
