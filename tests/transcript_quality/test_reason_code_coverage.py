"""Phase 2R — every reason code the validator/CLI emits must appear in
``spectrum_systems_core.reason_codes``."""
from __future__ import annotations

from spectrum_systems_core import reason_codes
from spectrum_systems_core.transcript_quality.checks import (
    reason_codes_emitted,
)


def test_validator_reason_codes_declared() -> None:
    for code in reason_codes_emitted():
        assert code in reason_codes.PHASE_2R_REASON_CODES, code


def test_cli_layer_reason_codes_declared() -> None:
    """The CLI layer emits two additional codes (``transcript_not_found``
    / ``transcript_unreadable``) plus the extraction-CLI hook code."""
    for needle in (
        "transcript_not_found",
        "transcript_unreadable",
        "transcript_quality_check_failed",
    ):
        assert needle in reason_codes.PHASE_2R_REASON_CODES


def test_phase_2r_set_is_exactly_the_declared_constants() -> None:
    """Belt-and-braces: every constant defined as a public string in
    the module is included in PHASE_2R_REASON_CODES."""
    public = {
        v
        for k, v in vars(reason_codes).items()
        if k.isupper() and isinstance(v, str)
    }
    assert reason_codes.PHASE_2R_REASON_CODES == frozenset(public)
