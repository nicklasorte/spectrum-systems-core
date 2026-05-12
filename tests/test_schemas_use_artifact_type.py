"""Phase V schemas must declare ``artifact_type`` exclusively.

The generic "no new schema uses artifact_kind" scan lives in
``tests/ci/test_no_artifact_kind_in_schemas.py`` (added by the infra
phase). This file pins the Phase V schemas specifically so a future
regression that adds ``artifact_kind`` to the new schemas fails with a
Phase-V-named test.
"""
from __future__ import annotations

import pathlib


SCHEMAS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "contracts" / "schemas"
)


def test_every_phase_v_schema_declares_artifact_type() -> None:
    """Phase V schemas must use ``artifact_type`` and not ``artifact_kind``."""
    targets = [
        SCHEMAS_DIR / "verification" / "source_verification_result.schema.json",
        SCHEMAS_DIR / "extraction" / "meeting_extraction.v2.schema.json",
    ]
    for path in targets:
        content = path.read_text(encoding="utf-8")
        assert '"artifact_type"' in content, f"{path.name} missing artifact_type"
        assert '"artifact_kind"' not in content, (
            f"{path.name} reintroduced artifact_kind"
        )
