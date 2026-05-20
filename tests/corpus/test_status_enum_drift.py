"""Phase 4 — state / recommendation enum drift tests.

The status_report schema lists allowed values for ``state`` and
``recommendation``. The :mod:`spectrum_systems_core.corpus.status`
module also defines them as Python constants. The two MUST agree —
red-team Pass 1 #6 and #7 explicitly call this out as the drift
class to defend against.

These tests load the schema directly and assert set-equality against
the Python constants.
"""
from __future__ import annotations

import json

from spectrum_systems_core.corpus.status import (
    ALL_RECOMMENDATIONS,
    ALL_STATES,
)
from spectrum_systems_core.schemas import schema_path


def _load_schema() -> dict:
    return json.loads(
        schema_path("status_report").read_text(encoding="utf-8")
    )


def test_state_enum_matches_python_constants() -> None:
    schema = _load_schema()
    enum_values = set(
        schema["$defs"]["row"]["properties"]["state"]["enum"]
    )
    assert enum_values == set(ALL_STATES), (
        f"state enum drift: schema={enum_values}, "
        f"code={set(ALL_STATES)}"
    )


def test_recommendation_enum_matches_python_constants() -> None:
    schema = _load_schema()
    enum_values = set(
        schema["$defs"]["row"]["properties"]["recommendation"]["enum"]
    )
    assert enum_values == set(ALL_RECOMMENDATIONS), (
        f"recommendation enum drift: schema={enum_values}, "
        f"code={set(ALL_RECOMMENDATIONS)}"
    )
