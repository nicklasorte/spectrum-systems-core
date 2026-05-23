"""Phase 6 integration contract — the cascade dispatch MUST NOT mutate
the source `meeting_minutes` artifact.

The trust property defended here (NOT ceremony):

* The Stage-2 cascade reads the just-promoted `meeting_minutes`
  artifact and writes a separate `meeting_minutes_filtered__*.json`
  alongside it. The source artifact must remain byte-identical to a
  run with `--disable-cascade-filter`. Specifically, the cascade must
  NOT:
  - flip `provenance.extraction_config.prompt_variant` from
    `production_haiku` to `production_haiku_with_cascade_filter`
    (the new variant is stamped on the FILTERED envelope only);
  - re-evaluate the source artifact under the new variant in a way
    that introduces a missing-required-fields cascade
    (the operator-reported failure mode);
  - mutate `mm.payload` in place between extraction and the next eval.

The cascade dispatch is invoked through the production CLI entry point
(`spectrum_systems_core.cli.meeting_minutes_llm`) with deterministic
stubs for the workflow client and the cascade api_client, so the test
exercises the real wiring end-to-end without a network call.

Co-defended: the comparison engine selects the cascade artifact via
`mdir.glob("meeting_minutes_filtered__*.json")`, NOT the source
artifact via `mdir.glob("meeting_minutes__*.json")`, so the filename
families must stay disjoint.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from spectrum_systems_core.cli import meeting_minutes_llm
from tests.cascade._helpers import DeterministicFilterClient, always_keep_rule
from tests.llm_stub import (
    DEC18_ACTION_ITEMS,
    DEC18_DECISIONS,
    DEC18_OPEN_QUESTIONS,
    DEC18_TECHNICAL_PARAMETERS,
    _auto_grounding,
    json_stub,
    load_fixture,
)

MEETING_ID = "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218"


def _seed_store_lake(tmp_path: Path) -> Path:
    """Build the `<lake>/store/raw/meetings/<sid>/source.txt` layout
    `meeting_minutes_llm()` expects."""
    lake = tmp_path / "lake"
    staged = lake / "store" / "raw" / "meetings" / MEETING_ID / "source.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(load_fixture("dec18_transcript.txt"), encoding="utf-8")
    return lake


def _processed_dir(lake: Path) -> Path:
    return lake / "store" / "processed" / "meetings" / MEETING_ID


def _read_one(paths: list[Path]) -> dict:
    assert len(paths) == 1, [str(p) for p in paths]
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _make_extraction_client():
    return json_stub(
        decisions=DEC18_DECISIONS,
        action_items=DEC18_ACTION_ITEMS,
        open_questions=DEC18_OPEN_QUESTIONS,
        technical_parameters=DEC18_TECHNICAL_PARAMETERS,
    )


def test_cascade_dispatch_leaves_source_artifact_passing_required_fields(
    tmp_path,
):
    """The cascade dispatch never blocks the source artifact with
    `failed:required_meeting_minutes_fields` (the operator-reported
    regression). Run `meeting_minutes_llm` with cascade enabled and
    verify the promoted source artifact carries every field
    `REQUIRED_MEETING_MINUTES_FIELDS` enforces."""
    lake = _seed_store_lake(tmp_path)
    out = io.StringIO()
    rc = meeting_minutes_llm(
        source_id=MEETING_ID,
        data_lake=str(lake),
        model_token="haiku",
        enable_cascade_filter=True,
        confirm_cost=True,
        client=_make_extraction_client(),
        cascade_api_client=DeterministicFilterClient(
            decision_rule=always_keep_rule
        ),
        env={"ANTHROPIC_API_KEY": "sk-test"},
        out_stream=out,
    )
    log = out.getvalue()
    assert rc == 0, (rc, log)
    # The CLI emits one OK line for the workflow AND one CASCADE OK
    # line — both fired, the source artifact passed every eval, and
    # the cascade ran cleanly on top.
    assert "BLOCKED" not in log, log
    assert "failed:required_meeting_minutes_fields" not in log, log
    assert "OK produced_by=meeting_minutes_llm" in log, log
    assert "CASCADE OK" in log, log

    proc = _processed_dir(lake)
    source_artifacts = sorted(proc.glob("meeting_minutes__*.json"))
    filtered_artifacts = sorted(proc.glob("meeting_minutes_filtered__*.json"))

    # Exactly one source AND one filtered — the filename families are
    # disjoint by the `_` vs `__` boundary; assert both glob bodies
    # so a future writer that accidentally collides is caught.
    source = _read_one(source_artifacts)
    filtered = _read_one(filtered_artifacts)

    # Every field `REQUIRED_MEETING_MINUTES_FIELDS` enforces, plus
    # `schema_version` (1.1.0 branch). A missing key here is the exact
    # failure shape the operator reported.
    payload = source["payload"]
    for required in (
        "title",
        "summary",
        "decisions",
        "action_items",
        "open_questions",
        "schema_version",
    ):
        assert required in payload, (
            f"source artifact payload missing required field {required!r}; "
            f"keys = {sorted(payload.keys())}"
        )

    # The source artifact's prompt_variant stays `production_haiku` —
    # the cascade discriminator is on the FILTERED envelope only.
    prov = payload["provenance"]
    ec = prov.get("extraction_config") or {}
    assert ec.get("prompt_variant") == "production_haiku", ec
    assert filtered.get("extraction_config", {}).get("prompt_variant") == (
        "production_haiku_with_cascade_filter"
    ), filtered.get("extraction_config")


def test_cascade_dispatch_does_not_mutate_source_artifact_bytes(tmp_path):
    """Disabled vs enabled cascade: the source artifact's bytes are
    IDENTICAL. The cascade artifact is the only on-disk difference."""
    lake_a = _seed_store_lake(tmp_path / "a")
    lake_b = _seed_store_lake(tmp_path / "b")

    # Run a: cascade DISABLED. Run b: cascade ENABLED.
    for lake, cascade in ((lake_a, False), (lake_b, True)):
        rc = meeting_minutes_llm(
            source_id=MEETING_ID,
            data_lake=str(lake),
            model_token="haiku",
            enable_cascade_filter=cascade,
            confirm_cost=True,
            client=_make_extraction_client(),
            cascade_api_client=(
                DeterministicFilterClient(decision_rule=always_keep_rule)
                if cascade
                else None
            ),
            env={"ANTHROPIC_API_KEY": "sk-test"},
            out_stream=io.StringIO(),
        )
        assert rc == 0, (cascade, rc)

    source_a = _read_one(
        sorted(_processed_dir(lake_a).glob("meeting_minutes__*.json"))
    )
    source_b = _read_one(
        sorted(_processed_dir(lake_b).glob("meeting_minutes__*.json"))
    )

    # The payload — every field the required-fields eval reads —
    # is byte-identical across the two runs. The cascade did not
    # mutate the source artifact in place; if it had, this assertion
    # would expose the exact field that drifted.
    assert source_a["payload"] == source_b["payload"], (
        f"source artifact payload diverged when cascade was enabled; "
        f"diff candidates: "
        f"{set(source_a['payload']) ^ set(source_b['payload'])}"
    )
    # Cascade artifact exists ONLY in lake_b.
    assert (
        not list(
            _processed_dir(lake_a).glob("meeting_minutes_filtered__*.json")
        )
    )
    assert (
        len(
            list(
                _processed_dir(lake_b).glob(
                    "meeting_minutes_filtered__*.json"
                )
            )
        )
        == 1
    )


def test_blocked_strict_schema_surfaces_detail_under_cascade(tmp_path):
    """When the upstream extraction blocks on strict_schema, the BLOCKED
    CLI line MUST carry the per-eval ``schema_violation:...`` detail —
    not just the flattened ``failed:llm_extraction_strict_schema`` token
    ``control.decide_control`` emits.

    Why this matters: the operator-reported failure
    ``reason_codes=failed:llm_extraction_strict_schema,
    failed:tlc_routed_extraction`` under ``--enable-cascade-filter``
    cannot be diagnosed from the BLOCKED line alone — the model is
    non-deterministic, so reproducing it requires the SPECIFIC schema
    violation (e.g. ``schema_violation:None is not of type 'string' at
    ['scheduled_events', N, 'date']``, the exact bug class commits
    d2e23d7 + #182 partially fixed). Surfacing the per-eval reason
    codes on the BLOCKED line is the only way the operator can
    self-diagnose without re-clicking through ``debug__*.json`` files.

    Also pins the cascade-source-artifact-invariant contract: when the
    upstream extraction fails, the cascade dispatch MUST NOT have run
    (no ``CASCADE OK`` token, no filtered artifact on disk). The block
    happens at ``cli.py:3377`` BEFORE ``cli.py:3414`` would dispatch
    cascade — exactly the same gate ordering as a no-cascade run.
    """
    lake = _seed_store_lake(tmp_path)
    out = io.StringIO()

    # A stub that returns a scheduled_events item with
    # ``event_id: null`` — the schema declares ``event_id`` as a bare
    # ``type: "string"`` (no null branch, minLength: 1), so jsonschema
    # rejects it with ``schema_violation:None is not of type
    # 'string'``. Same bug class as the d2e23d7 attendees.agency fix:
    # the model emitted a faithful null where the schema required a
    # non-null string. We need the BLOCKED line to NAME the field so
    # the operator can decide whether to widen the schema (the d2e23d7
    # pattern) or correct the prompt.
    #
    # Grounding mirrors the well-behaved-model auto-grounding (one
    # entry per content item citing the real turn_ids in the user
    # message) so the grounding_coverage gate passes and the run
    # blocks on strict_schema specifically — the exact failure shape
    # the operator reported.
    def _invalid_schema_stub(*, system: str, user: str) -> str:  # noqa: ARG001
        doc = {
            "decisions": list(DEC18_DECISIONS),
            "action_items": list(DEC18_ACTION_ITEMS),
            "open_questions": list(DEC18_OPEN_QUESTIONS),
            "commitments": [],
            "risks": [],
            "cross_references": [],
            "attendees": [],
            "topics": [],
            "regulatory_references": [],
            "technical_parameters": list(DEC18_TECHNICAL_PARAMETERS),
            "named_artifacts": [],
            "scheduled_events": [
                {
                    "event_id": None,
                    "title": "Working group review",
                    "date": "2026-01-15",
                }
            ],
        }
        doc["grounding"] = _auto_grounding(doc, user)
        return json.dumps(doc)

    rc = meeting_minutes_llm(
        source_id=MEETING_ID,
        data_lake=str(lake),
        model_token="haiku",
        enable_cascade_filter=True,
        confirm_cost=True,
        client=_invalid_schema_stub,
        cascade_api_client=DeterministicFilterClient(
            decision_rule=always_keep_rule
        ),
        env={"ANTHROPIC_API_KEY": "sk-test"},
        out_stream=out,
    )
    log = out.getvalue()

    # Upstream block; cascade never dispatched. Mirrors the
    # operator-reported BLOCKED line shape.
    assert rc == 1, (rc, log)
    assert "BLOCKED produced_by=meeting_minutes_llm" in log, log
    assert "failed:llm_extraction_strict_schema" in log, log
    assert "failed:tlc_routed_extraction" in log, log
    # Cascade did NOT run — same gate ordering as the no-cascade path.
    assert "CASCADE OK" not in log, log
    assert "filtered_written=" not in log, log
    proc = _processed_dir(lake)
    if proc.exists():
        assert not list(proc.glob("meeting_minutes_filtered__*.json"))

    # The new per-eval BLOCKED-detail lines surface the SPECIFIC
    # schema_violation reason — what the operator needs to diagnose
    # whether the block is a real regression or a flaky-model sample.
    assert (
        "BLOCKED detail llm_extraction_strict_schema:" in log
    ), log
    assert "schema_violation" in log, log
    # The detail line for tlc_routed_extraction carries the
    # subeval-fail marker that points back at strict_schema, so the
    # operator sees the two codes are the same root cause.
    assert "BLOCKED detail tlc_routed_extraction:" in log, log
    assert "tlc_subeval_failed:llm_extraction_strict_schema" in log, log


def test_source_glob_excludes_filtered_filename(tmp_path):
    """The comparison engine globs `meeting_minutes__*.json` to find
    the source artifact and `meeting_minutes_filtered__*.json` to find
    the cascade output. The single-underscore `meeting_minutes_filtered`
    must NOT match the `meeting_minutes__*` glob — otherwise a stale
    cascade artifact could shadow the source on the comparison side.

    Pin the property with a real glob against on-disk files written by
    a successful cascade run.
    """
    lake = _seed_store_lake(tmp_path)
    rc = meeting_minutes_llm(
        source_id=MEETING_ID,
        data_lake=str(lake),
        model_token="haiku",
        enable_cascade_filter=True,
        confirm_cost=True,
        client=_make_extraction_client(),
        cascade_api_client=DeterministicFilterClient(
            decision_rule=always_keep_rule
        ),
        env={"ANTHROPIC_API_KEY": "sk-test"},
        out_stream=io.StringIO(),
    )
    assert rc == 0

    proc = _processed_dir(lake)
    source_glob = sorted(proc.glob("meeting_minutes__*.json"))
    filtered_glob = sorted(
        proc.glob("meeting_minutes_filtered__*.json")
    )
    assert len(source_glob) == 1, [p.name for p in source_glob]
    assert len(filtered_glob) == 1, [p.name for p in filtered_glob]
    # The two globs MUST be disjoint sets of paths.
    assert set(source_glob).isdisjoint(set(filtered_glob))
