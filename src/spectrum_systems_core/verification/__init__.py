"""Phase O verification utilities.

Read-only inspection of pipeline state. Implements:

- ``state_scanner``: scan SDL_ROOT, classify artifacts, validate schemas,
  surface ``next_required_actions``.
- ``findings_compiler``: aggregate pipeline_state_record + eval_summary
  into a verification_findings artifact.

Neither module ever writes to the data lake outside of
``$SDL_ROOT/verifications/``. Neither raises.
"""
from .state_scanner import (
    scan_pipeline_state,
    write_pipeline_state_record,
    emit_actions_summary,
)
from .findings_compiler import (
    compile_findings,
    write_verification_findings,
    format_findings_markdown,
)
from .next_phase_handoff import (
    build_next_phase_briefing,
    write_next_phase_briefing,
)
from .post_hoc_verifier import (
    EARLY_HALT_SAMPLE_SIZE,
    EARLY_HALT_UNSUPPORTED_THRESHOLD,
    PostHocVerifier,
)
from .model_registry import ModelRegistry, ModelRegistryError
from .verification_gate import GateDecision, VerificationGate
from .pipeline_integration import (
    VerificationIncompleteError,
    apply_phase_v_if_enabled,
    write_verification_result,
)

__all__ = [
    "scan_pipeline_state",
    "write_pipeline_state_record",
    "emit_actions_summary",
    "compile_findings",
    "write_verification_findings",
    "format_findings_markdown",
    "build_next_phase_briefing",
    "write_next_phase_briefing",
    "PostHocVerifier",
    "EARLY_HALT_SAMPLE_SIZE",
    "EARLY_HALT_UNSUPPORTED_THRESHOLD",
    "ModelRegistry",
    "ModelRegistryError",
    "GateDecision",
    "VerificationGate",
    "VerificationIncompleteError",
    "apply_phase_v_if_enabled",
    "write_verification_result",
]
