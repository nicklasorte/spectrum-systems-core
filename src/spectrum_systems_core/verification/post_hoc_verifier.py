"""PostHocVerifier: re-verify extracted items against their cited source turns.

Phase V. Per Rec 8b (transcript_extraction_research_2026.pdf): for each
extracted item, fetch its cited turn text, then verify the item's content
is supported by that text. Items that fail verification are returned with
an explicit ``verification_status`` other than ``"verified"``.

The verifier never raises. API failures degrade to
``verification_status="verification_failed"`` per item so the pipeline
gate (VerificationGate, Phase V Part F) can fail closed on any
non-``verified`` status.

Why the verifier uses a different task_type (``generation``) than the
extractor (``extraction``): a self-grading loop where the same model
judges its own output collapses to "looks-good-to-me". Asking a
Sonnet-class model to verify Haiku-class output reduces that risk per
*Governed AI Pipelines*.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from .model_registry import ModelRegistry

_LOG = logging.getLogger(__name__)


# When the first N items show >=THRESHOLD unsupported, the verifier
# concludes its prompt or model is mis-wired and halts. This protects
# against burning an entire run on a misconfigured verifier.
EARLY_HALT_SAMPLE_SIZE = 5
EARLY_HALT_UNSUPPORTED_THRESHOLD = 0.95


_ALLOWED_STATUSES = {
    "verified",
    "unsupported",
    "contradicted",
    "insufficient_evidence",
}

# Maps the LLM-emitted status to the on-artifact status. Two extra
# pipeline-only values (verification_failed, halted_sanity_check) are
# emitted by the verifier itself, not the model.


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def _parse_json_response(text: str) -> dict[str, Any]:
    """Robust JSON-from-LLM parse. Returns ``{}`` on any failure."""
    if not isinstance(text, str) or not text.strip():
        return {}
    candidates: list[str] = [text]
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped[3:]
        if body.startswith("json"):
            body = body[4:]
        body = body.lstrip("\n").rstrip()
        if body.endswith("```"):
            body = body[:-3].rstrip()
        if body:
            candidates.append(body)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


class PostHocVerifier:
    """Verify extracted items against their cited source turns.

    Parameters
    ----------
    model_registry:
        ModelRegistry-like; must expose ``.get(task_type)`` returning a
        dict with at least ``model`` and ``version`` keys.
    sdl_root:
        Logical data-lake root. Stored on the instance for downstream
        callers; not used by the verifier itself.
    api_caller:
        Optional callable ``(prompt: str) -> dict``. When provided the
        verifier uses it instead of the live anthropic SDK. Tests inject
        a mock here.
    """

    EARLY_HALT_SAMPLE_SIZE = EARLY_HALT_SAMPLE_SIZE
    EARLY_HALT_UNSUPPORTED_THRESHOLD = EARLY_HALT_UNSUPPORTED_THRESHOLD

    def __init__(
        self,
        model_registry: ModelRegistry,
        sdl_root: str | None = None,
        api_caller: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.model_registry = model_registry
        self.sdl_root = sdl_root
        # Must request the ``generation`` task type, not ``extraction`` --
        # the asymmetry is the whole point of the post-hoc pass.
        self._model_spec = model_registry.get("generation")
        self._api_caller = api_caller

    @property
    def model_version(self) -> str:
        return f"{self._model_spec['model']}@{self._model_spec.get('version', 'default')}"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def verify_extraction(
        self,
        meeting_extraction: dict[str, Any],
        chunks_by_id: dict[str, dict[str, Any]],
        pipeline_run_id: str,
        *,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify every decision/claim/action_item in ``meeting_extraction``.

        Returns a source_verification_result artifact dict.
        """
        items: list[tuple[dict[str, Any], str]] = []
        for item in meeting_extraction.get("decisions", []) or []:
            items.append((item, "decision"))
        for item in meeting_extraction.get("claims", []) or []:
            items.append((item, "claim"))
        for item in meeting_extraction.get("action_items", []) or []:
            items.append((item, "action_item"))

        item_verifications: list[dict[str, Any]] = []
        halted = False
        for index, (item, item_type) in enumerate(items):
            verification = self._verify_single_item(
                item, item_type, chunks_by_id,
            )
            item_verifications.append(verification)

            # Early halt check fires after EARLY_HALT_SAMPLE_SIZE items.
            if index + 1 == self.EARLY_HALT_SAMPLE_SIZE:
                unsupported = sum(
                    1 for v in item_verifications
                    if v["verification_status"] == "unsupported"
                )
                rate = unsupported / float(self.EARLY_HALT_SAMPLE_SIZE)
                if rate >= self.EARLY_HALT_UNSUPPORTED_THRESHOLD:
                    halted = True
                    _LOG.warning(
                        "post_hoc_verifier_early_halt: "
                        "unsupported_rate=%.2f after %d items",
                        rate, self.EARLY_HALT_SAMPLE_SIZE,
                    )
                    break

        summary = self._compute_summary(item_verifications, halted=halted)
        source_id = meeting_extraction.get("source_id") or meeting_extraction.get(
            "source_artifact_id"
        ) or "unknown"

        # Both fields are uuid-typed in the schema. Coerce non-uuid inputs
        # (e.g. the runner's "tex-<hex>" extraction_run_id) to a stable
        # uuid5 so a strict format-checking validator can never break the
        # write post-hoc (RT1 Sev-2 fix).
        link_id = (
            meeting_extraction.get("meeting_extraction_id")
            or meeting_extraction.get("source_artifact_id")
        )
        return {
            "source_verification_result_id": str(uuid.uuid4()),
            "artifact_type": "source_verification_result",
            "schema_version": "1.0.0",
            "created_at": _now_iso(),
            "trace_id": _coerce_uuid(trace_id, allow_none=True),
            "pipeline_run_id": _coerce_uuid(pipeline_run_id),
            "meeting_extraction_artifact_id": _coerce_uuid(link_id),
            "source_id": str(source_id),
            "item_verifications": item_verifications,
            "summary": summary,
            "provenance": {"produced_by": "PostHocVerifier", "phase": "V"},
        }

    # ------------------------------------------------------------------ #
    # Per-item verification
    # ------------------------------------------------------------------ #

    def _verify_single_item(
        self,
        item: dict[str, Any],
        item_type: str,
        chunks_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        item_id = _coerce_item_id(item)
        item_text = _item_text(item, item_type)
        cited = list(item.get("source_turn_ids") or [])

        # Resolve cited turn texts. A turn id that doesn't exist in the
        # chunk index is recorded as text="<unknown turn>" so the
        # verifier prompt is still well-formed; the verifier will then
        # almost certainly return insufficient_evidence/unsupported.
        cited_turns = [
            {
                "turn_id": str(tid),
                "speaker": (chunks_by_id.get(str(tid), {}) or {}).get("speaker", ""),
                "timestamp": (chunks_by_id.get(str(tid), {}) or {}).get("timestamp", ""),
                "text": (chunks_by_id.get(str(tid), {}) or {}).get("text") or "<missing turn>",
            }
            for tid in cited
        ]

        if not cited_turns:
            # No cited turns -- treat as unsupported (we cannot verify
            # against nothing). This is fail-closed: never auto-verify.
            return _build_verification_entry(
                item_id=item_id,
                item_type=item_type,
                item_text=item_text,
                cited_source_turn_ids=[],
                status="unsupported",
                excerpts=[],
                confidence=0.0,
                rationale="no source_turn_ids cited; cannot verify.",
                model_version=self.model_version,
            )

        prompt = self._build_verification_prompt(item_text, item_type, cited_turns)

        try:
            response = self._call_model(prompt)
        except Exception as exc:  # never re-raise; degrade
            _LOG.warning(
                "post_hoc_verifier_api_failed: %s: %s",
                type(exc).__name__, exc,
            )
            return _build_verification_entry(
                item_id=item_id,
                item_type=item_type,
                item_text=item_text,
                cited_source_turn_ids=[str(t) for t in cited],
                status="verification_failed",
                excerpts=[],
                confidence=0.0,
                rationale=f"verifier_api_error: {type(exc).__name__}: {exc}",
                model_version=self.model_version,
            )

        status = response.get("verification_status")
        excerpts_raw = response.get("supporting_text_excerpts")
        rationale = response.get("verifier_rationale") or ""
        confidence_raw = response.get("verifier_confidence")

        excerpts = [
            str(e) for e in excerpts_raw if isinstance(e, str) and e.strip()
        ] if isinstance(excerpts_raw, list) else []

        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if status not in _ALLOWED_STATUSES:
            # Treat as verification_failed so the gate blocks.
            return _build_verification_entry(
                item_id=item_id,
                item_type=item_type,
                item_text=item_text,
                cited_source_turn_ids=[str(t) for t in cited],
                status="verification_failed",
                excerpts=[],
                confidence=0.0,
                rationale=f"verifier_returned_unparseable_status: {status!r}",
                model_version=self.model_version,
            )

        # Defense against silent self-grading collapse:
        # a "verified" status with no excerpts cannot stand.
        if status == "verified" and not excerpts:
            return _build_verification_entry(
                item_id=item_id,
                item_type=item_type,
                item_text=item_text,
                cited_source_turn_ids=[str(t) for t in cited],
                status="insufficient_evidence",
                excerpts=[],
                confidence=confidence,
                rationale=(
                    "verifier returned verified but supplied no excerpts; "
                    "downgraded to insufficient_evidence."
                ),
                model_version=self.model_version,
            )

        return _build_verification_entry(
            item_id=item_id,
            item_type=item_type,
            item_text=item_text,
            cited_source_turn_ids=[str(t) for t in cited],
            status=status,
            excerpts=excerpts,
            confidence=confidence,
            rationale=str(rationale)[:1000],
            model_version=self.model_version,
        )

    # ------------------------------------------------------------------ #
    # Prompt + model call
    # ------------------------------------------------------------------ #

    def _build_verification_prompt(
        self,
        item_text: str,
        item_type: str,
        cited_turn_texts: Sequence[dict[str, Any]],
    ) -> str:
        turn_blocks: list[str] = []
        for t in cited_turn_texts:
            turn_blocks.append(
                f"  - turn_id: {t['turn_id']}\n"
                f"    speaker: {t.get('speaker', '')}\n"
                f"    timestamp: {t.get('timestamp', '')}\n"
                f"    text: |\n"
                f"      {t.get('text', '').strip()}"
            )
        turns_yaml = "\n".join(turn_blocks)

        return (
            f"You are verifying whether an extracted {item_type} is supported "
            f"by the source transcript text it cites.\n\n"
            f"EXTRACTED ITEM:\n```\n{item_text}\n```\n\n"
            f"CITED SOURCE TURNS:\n{turns_yaml}\n\n"
            f"Your task:\n"
            f"1. Read the cited source turns word-for-word.\n"
            f"2. Decide whether the extracted item's content is directly "
            f"supported by what was actually said in these turns.\n"
            f"3. Return ONE of these statuses:\n"
            f"   - \"verified\": the item's content appears in the cited turns "
            f"(paraphrasing OK; meaning must match)\n"
            f"   - \"unsupported\": the item's content does NOT appear in the "
            f"cited turns (something was added that wasn't said)\n"
            f"   - \"contradicted\": the cited turns say the opposite of the item\n"
            f"   - \"insufficient_evidence\": the cited turns are too vague to "
            f"confirm or deny the item\n"
            f"4. Provide supporting_text_excerpts: verbatim quotes from the "
            f"cited turns that support your status. REQUIRED if status="
            f"\"verified\" (at least one quote). Empty array OK for other "
            f"statuses.\n"
            f"5. Provide verifier_confidence (0.0-1.0): your confidence in the "
            f"status.\n"
            f"6. Provide verifier_rationale: one sentence why.\n\n"
            f"Return JSON only:\n"
            f"{{\n"
            f"  \"verification_status\": "
            f"\"verified|unsupported|contradicted|insufficient_evidence\",\n"
            f"  \"supporting_text_excerpts\": [\"...\"],\n"
            f"  \"verifier_confidence\": 0.95,\n"
            f"  \"verifier_rationale\": \"...\"\n"
            f"}}\n"
        )

    def _call_model(self, prompt: str) -> dict[str, Any]:
        if self._api_caller is not None:
            response = self._api_caller(prompt)
            if isinstance(response, dict):
                return response
            return {}

        import anthropic  # local import: SDK not required for tests
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=self._model_spec["model"],
            max_tokens=2000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        parts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return _parse_json_response("\n".join(parts))

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #

    def _compute_summary(
        self,
        item_verifications: Sequence[dict[str, Any]],
        *,
        halted: bool,
    ) -> dict[str, Any]:
        total = len(item_verifications)
        verified = 0
        unsupported = 0
        contradicted = 0
        insufficient = 0
        failed = 0
        for v in item_verifications:
            status = v.get("verification_status")
            if status == "verified":
                verified += 1
            elif status == "unsupported":
                unsupported += 1
            elif status == "contradicted":
                contradicted += 1
            elif status == "insufficient_evidence":
                insufficient += 1
            elif status == "verification_failed":
                failed += 1
        spurious_add_rate = (
            (unsupported + contradicted) / float(total) if total > 0 else 0.0
        )
        return {
            "total_items_count": total,
            "verified_count": verified,
            "unsupported_count": unsupported,
            "contradicted_count": contradicted,
            "insufficient_evidence_count": insufficient,
            "verification_failed_count": failed,
            "spurious_add_rate": round(spurious_add_rate, 6),
            "status": "halted_sanity_check" if halted else "complete",
        }


# ---------------------------------------------------------------------- #
# Module-level helpers
# ---------------------------------------------------------------------- #

_VALID_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _coerce_uuid(value: Any, allow_none: bool = False) -> str | None:
    """Return ``value`` if it parses as a UUID; otherwise mint a stable
    uuid5 from its string form.

    The verifier schema declares ``format: uuid`` on several link fields
    (pipeline_run_id, trace_id, meeting_extraction_artifact_id). Some
    callers pass opaque correlation tokens ("tex-<hex16>") that match
    that field's intent but not its format. We deterministically map
    non-uuid strings to uuid5 so the artifact is always schema-valid
    AND a re-run produces the same id (auditability preserved).
    """
    if value is None or value == "":
        if allow_none:
            return None
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "phase-v-anon"))
    s = str(value)
    if _VALID_UUID_RE.match(s):
        return s
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "phase-v-corr:" + s))


def _coerce_item_id(item: dict[str, Any]) -> str:
    """Return a UUID-shaped id for ``item``.

    Items produced by today's extractors do not carry an ``id`` field --
    the schema only requires text + source_turn_ids. Phase V mints a
    deterministic uuid5 from the item's primary text + source_turn_ids
    if no id is present so re-running the pipeline yields stable ids.
    """
    raw = item.get("id")
    if isinstance(raw, str) and _VALID_UUID_RE.match(raw):
        return raw
    seed = (
        (item.get("decision_text") or item.get("claim_text") or item.get("action") or "")
        + "|"
        + ",".join(str(x) for x in (item.get("source_turn_ids") or []))
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "phase-v-item:" + seed))


def _item_text(item: dict[str, Any], item_type: str) -> str:
    if item_type == "decision":
        return str(item.get("decision_text") or "").strip() or "<empty>"
    if item_type == "claim":
        return str(item.get("claim_text") or "").strip() or "<empty>"
    if item_type == "action_item":
        return str(item.get("action") or "").strip() or "<empty>"
    return str(item.get("text") or "").strip() or "<empty>"


def _build_verification_entry(
    *,
    item_id: str,
    item_type: str,
    item_text: str,
    cited_source_turn_ids: list[str],
    status: str,
    excerpts: list[str],
    confidence: float,
    rationale: str,
    model_version: str,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "item_type": item_type,
        "original_item_text": item_text,
        "cited_source_turn_ids": list(cited_source_turn_ids),
        "verification_status": status,
        "supporting_text_excerpts": list(excerpts),
        "verifier_confidence": float(confidence),
        "verifier_rationale": rationale,
        "verifier_model_version": model_version,
        "verified_at": _now_iso(),
    }
