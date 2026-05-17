"""Class 5: stale artifact detection."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spectrum_systems_core.health.stale_artifact import (
    DEFAULT_MAX_ARTIFACT_AGE_HOURS,
    audit_bundle_freshness,
    check_artifact_freshness,
    load_max_artifact_age_hours,
    majority_stale,
)

NOW = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)


def _art(*, age_hours: float, artifact_id: str = "a") -> dict:
    created = NOW - timedelta(hours=age_hours)
    return {
        "artifact_id": artifact_id,
        "artifact_type": "meeting_extraction",
        "created_at": created.isoformat(),
    }


def test_fresh_artifact_no_finding(tmp_path: Path) -> None:
    art = _art(age_hours=1.0)
    finding = check_artifact_freshness(art, NOW, max_age_hours=48.0)
    assert finding is None


def test_stale_artifact_emits_warn(tmp_path: Path) -> None:
    art = _art(age_hours=72.0)
    finding = check_artifact_freshness(art, NOW, max_age_hours=48.0)
    assert finding is not None
    assert finding.finding_code == "stale_artifact_in_bundle"
    assert finding.severity == "warn"
    assert finding.context["age_hours"] == 72.0
    assert finding.context["max_age_hours"] == 48.0
    assert finding.context["artifact_id"] == "a"
    assert finding.remediation


def test_majority_stale_escalates_to_halt(tmp_path: Path) -> None:
    """Red Team 2: majority-stale escalation is a separate halt."""
    bundle = [
        _art(age_hours=72, artifact_id="s1"),
        _art(age_hours=72, artifact_id="s2"),
        _art(age_hours=72, artifact_id="s3"),
        _art(age_hours=1, artifact_id="f1"),
    ]
    result = audit_bundle_freshness(
        bundle, NOW, data_lake_path=tmp_path
    )
    assert majority_stale(result) is True
    halt_findings = [f for f in result.findings if f.severity == "halt"]
    assert len(halt_findings) == 1
    halt = halt_findings[0]
    assert halt.context.get("aggregate") is True
    assert halt.context["stale"] == 3
    assert halt.context["total"] == 4


def test_minority_stale_only_warns(tmp_path: Path) -> None:
    """Red Team 2: minority stale is warn, no halt."""
    bundle = [
        _art(age_hours=72, artifact_id="s1"),
        _art(age_hours=1, artifact_id="f1"),
        _art(age_hours=1, artifact_id="f2"),
        _art(age_hours=1, artifact_id="f3"),
    ]
    result = audit_bundle_freshness(
        bundle, NOW, data_lake_path=tmp_path
    )
    assert majority_stale(result) is False
    halts = [f for f in result.findings if f.severity == "halt"]
    assert halts == []
    warns = [f for f in result.findings if f.severity == "warn"]
    assert len(warns) == 1


def test_max_age_read_from_config_not_hardcoded(tmp_path: Path) -> None:
    config_dir = tmp_path / "store" / "artifacts" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "health.json").write_text(
        json.dumps({"max_artifact_age_hours": 24.0})
    )
    assert load_max_artifact_age_hours(tmp_path) == 24.0


def test_max_age_default_when_no_config(tmp_path: Path) -> None:
    assert load_max_artifact_age_hours(tmp_path) == DEFAULT_MAX_ARTIFACT_AGE_HOURS
