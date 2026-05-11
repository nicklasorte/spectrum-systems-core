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

__all__ = [
    "scan_pipeline_state",
    "write_pipeline_state_record",
    "emit_actions_summary",
    "compile_findings",
    "write_verification_findings",
    "format_findings_markdown",
]
