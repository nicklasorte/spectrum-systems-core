"""Phase W: agenda detection + hierarchical chunk metadata.

Implements Rec 2a from transcript_extraction_research_2026.pdf:
chunk -> agenda-item -> meeting. Detection runs over the first 20% of
speaker turns and emits agenda_item artifacts that chunks then reference
via the optional ``agenda_item_id`` field.

Public API::

    from spectrum_systems_core.agenda import AgendaDetector, AgendaReferenceError
"""
from .agenda_detector import (
    AgendaDetector,
    AgendaReferenceError,
    UNCATEGORIZED_LABEL,
    build_chunk_to_agenda_mapping,
    validate_agenda_references,
)
from .pipeline_integration import (
    apply_phase_w_if_enabled,
    make_phase_w_agenda_resolver,
    write_agenda_artifact,
)

__all__ = [
    "AgendaDetector",
    "AgendaReferenceError",
    "UNCATEGORIZED_LABEL",
    "apply_phase_w_if_enabled",
    "build_chunk_to_agenda_mapping",
    "make_phase_w_agenda_resolver",
    "validate_agenda_references",
    "write_agenda_artifact",
]
