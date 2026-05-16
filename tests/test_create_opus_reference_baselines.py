"""Unit tests for ``scripts/create_opus_reference_baselines.py``.

Every LLM call is a stubbed in-process client (``(*, system, user) ->
str``) — the SAME structural seam ``workflows/llm_client.py`` defines.
No network, no ``ANTHROPIC_API_KEY``. Each gate is tested on both its
happy path and its rejection path.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import create_opus_reference_baselines as cob  # noqa: E402
from tests.integration.fixtures import make_source_record  # noqa: E402

MODEL = "claude-opus-4-6"
TRANSCRIPT_STEM = "sample-transcript-20251218"
SOURCE_ID = "sample-transcript-20251218"  # _slugify(stem) == stem here

_VALID_RESPONSE = json.dumps(
    {
        "decisions": [
            "The group approved the 7 GHz downlink threshold.",
            "The group deferred the aggregate methodology.",
        ],
        "action_items": ["DoD will submit revised ERP values."],
        "open_questions": ["What is the coordination distance?"],
        "commitments": [
            {
                "commitment_id": "commit-1",
                "owner": "DoD Rep",
                "commitment_text": "DoD will submit revised ERP values.",
                "due": None,
                "source_speaker": "DoD Rep",
            }
        ],
        "risks": [
            {
                "risk_id": "risk-1",
                "risk_text": "Concern about the methodology.",
                "raised_by": "DoD Rep",
                "severity": None,
                "mitigation_mentioned": None,
            }
        ],
        "cross_references": [],
        "attendees": [
            {
                "name": "Chair Smith",
                "agency": "FCC",
                "role": "Chair",
                "present": True,
            }
        ],
        "topics": [],
        "regulatory_references": [],
        "technical_parameters": [
            {
                "param_id": "param-1",
                "parameter_name": "7 GHz downlink threshold",
                "value": "minus 47 dBm per megahertz",
                "unit": "dBm/MHz",
                "context": "approved threshold",
                "speaker": "NTIA Lead",
            }
        ],
        "named_artifacts": [],
        "scheduled_events": [],
    }
)


class SpyClient:
    """Records call count and the exact ``system`` prompt it received."""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0
        self.last_system: str | None = None
        self.last_user: str | None = None

    def __call__(self, *, system: str, user: str) -> str:
        self.calls += 1
        self.last_system = system
        self.last_user = user
        return self.response


def _make_docx(path: Path) -> None:
    from docx import Document

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph("7 GHz Downlink TIG Kickoff Meeting")
    doc.add_paragraph("Meeting Date: 2025-12-18")
    doc.add_paragraph(
        "The group approved the 7 GHz downlink threshold of minus 47 "
        "dBm per megahertz."
    )
    doc.add_paragraph("DoD will submit revised ERP values.")
    doc.save(str(path))


def _seed(tmp_path: Path, *, with_source_record: bool = True) -> Path:
    dl = tmp_path / "data-lake"
    _make_docx(
        dl / "store" / "raw" / "transcripts" / f"{TRANSCRIPT_STEM}.docx"
    )
    if with_source_record:
        proc = dl / "store" / "processed" / "meetings" / SOURCE_ID
        proc.mkdir(parents=True)
        (proc / "source_record.json").write_text(
            json.dumps(make_source_record(SOURCE_ID, str(uuid.uuid4())))
        )
    return dl


def _out_path(dl: Path) -> Path:
    return (
        dl / "store" / "processed" / "meetings" / SOURCE_ID
        / "reference_baselines" / "opus_reference_minutes.jsonl"
    )


# --------------------------------------------------------------------------
# 1. Dry run produces no files
# --------------------------------------------------------------------------
def test_dry_run_produces_no_files(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    spy = SpyClient(_VALID_RESPONSE)
    result = cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=True,
        skip_existing=True,
        model=MODEL,
        client=spy,
    )
    assert result["status"] == "success"
    assert not _out_path(dl).exists()
    t = result["per_transcript"][0]
    assert t["status"] == "dry_run"
    assert t["total"] > 0


# --------------------------------------------------------------------------
# 2. Skip-existing skips a source that already has the JSONL
# --------------------------------------------------------------------------
def test_skip_existing_skips_present_jsonl(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    out = _out_path(dl)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('{"pre":"existing"}\n', encoding="utf-8")

    spy = SpyClient(_VALID_RESPONSE)
    result = cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=False,
        skip_existing=True,
        model=MODEL,
        client=spy,
    )
    assert spy.calls == 0, "skip-existing must not even call the model"
    assert result["per_transcript"][0]["status"] == "skipped"
    assert out.read_text(encoding="utf-8") == '{"pre":"existing"}\n'


def test_no_skip_existing_regenerates(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    out = _out_path(dl)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('{"pre":"existing"}\n', encoding="utf-8")

    spy = SpyClient(_VALID_RESPONSE)
    cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=False,
        skip_existing=False,
        model=MODEL,
        client=spy,
    )
    assert spy.calls == 1
    assert '"pre":"existing"' not in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 3. Valid transcript -> JSONL with correct schema fields
#    7. model_id matches --model    8. authorship flags
# --------------------------------------------------------------------------
def test_valid_transcript_writes_correct_fields(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=False,
        skip_existing=True,
        model=MODEL,
        client=SpyClient(_VALID_RESPONSE),
    )
    lines = [
        json.loads(ln)
        for ln in _out_path(dl).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    # 2 decisions + 1 action + 1 question + 1 commitment + 1 risk
    # + 1 attendee + 1 technical_parameter = 8
    assert len(lines) == 8

    required = {
        "pair_id", "source_id", "source_artifact_id", "extraction_type",
        "ground_truth_text", "item_data", "human_authored",
        "model_authored", "model_id", "verified", "status",
        "provenance", "schema_version", "meeting_date", "created_at",
    }
    for r in lines:
        assert required <= set(r), f"missing fields: {required - set(r)}"
        assert r["model_id"] == MODEL                       # (7)
        assert r["human_authored"] is False                 # (8)
        assert r["model_authored"] is True                  # (8)
        assert r["verified"] is False
        assert r["status"] == "reference_only"
        assert r["provenance"] == {
            "produced_by": "opus_reference_baseline_workflow"
        }
        assert r["schema_version"] == "1.0.0"
        assert r["source_id"] == SOURCE_ID
        assert r["meeting_date"] == "2025-12-18"  # from docx header
        assert r["ground_truth_text"].strip()
        assert r["created_at"].endswith("+00:00")

    by_type: dict[str, list] = {}
    for r in lines:
        by_type.setdefault(r["extraction_type"], []).append(r)
    # String array: ground_truth_text IS the string.
    assert sorted(x["ground_truth_text"] for x in by_type["decisions"]) == [
        "The group approved the 7 GHz downlink threshold.",
        "The group deferred the aggregate methodology.",
    ]
    # Structured: ground_truth_text is the schema-required text field.
    assert by_type["risks"][0]["ground_truth_text"] == (
        "Concern about the methodology."
    )
    assert by_type["attendees"][0]["ground_truth_text"] == "Chair Smith"
    assert by_type["technical_parameters"][0]["ground_truth_text"] == (
        "minus 47 dBm per megahertz"
    )
    # item_data is the full structured object, verbatim.
    assert by_type["risks"][0]["item_data"]["risk_id"] == "risk-1"


# --------------------------------------------------------------------------
# 4. Missing source_record.json -> halt, no partial output
# --------------------------------------------------------------------------
def test_missing_source_record_halts_no_partial(tmp_path: Path) -> None:
    dl = _seed(tmp_path, with_source_record=False)
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=SpyClient(_VALID_RESPONSE),
        )
    assert ei.value.reason == "missing_source_record"
    assert not _out_path(dl).exists()


def _seed_custom_record(tmp_path: Path, record: dict) -> Path:
    """Seed the data-lake with a hand-written source_record.json.

    The minimal-contract gate cases below assert that ONLY a
    valid-UUID ``artifact_id`` is required, so they intentionally do
    NOT route through ``make_source_record`` — they pin the exact
    record shapes the contract must accept or reject.
    """
    dl = _seed(tmp_path, with_source_record=False)
    proc = dl / "store" / "processed" / "meetings" / SOURCE_ID
    proc.mkdir(parents=True)
    (proc / "source_record.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    return dl


def test_source_record_only_artifact_id_passes(tmp_path: Path) -> None:
    """Gate: a record carrying ONLY a valid-UUID artifact_id passes."""
    aid = str(uuid.uuid4())
    dl = _seed_custom_record(tmp_path, {"artifact_id": aid})
    result = cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=False,
        skip_existing=True,
        model=MODEL,
        client=SpyClient(_VALID_RESPONSE),
    )
    assert result["status"] == "success"
    out = _out_path(dl)
    assert out.is_file()
    for ln in out.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            assert json.loads(ln)["source_artifact_id"] == aid


def test_source_record_artifact_id_plus_source_id_passes(
    tmp_path: Path,
) -> None:
    """Gate: artifact_id + a redundant top-level source_id still passes."""
    aid = str(uuid.uuid4())
    dl = _seed_custom_record(
        tmp_path, {"artifact_id": aid, "source_id": SOURCE_ID}
    )
    result = cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=False,
        skip_existing=True,
        model=MODEL,
        client=SpyClient(_VALID_RESPONSE),
    )
    assert result["status"] == "success"
    assert _out_path(dl).is_file()


def test_source_record_missing_artifact_id_halts(tmp_path: Path) -> None:
    """Gate: a record with no artifact_id halts invalid_source_record."""
    dl = _seed_custom_record(tmp_path, {"source_id": SOURCE_ID})
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=SpyClient(_VALID_RESPONSE),
        )
    assert ei.value.reason == "invalid_source_record"
    assert not _out_path(dl).exists()


def test_source_record_non_uuid_artifact_id_halts(tmp_path: Path) -> None:
    """A present but non-UUID artifact_id is still a fail-closed halt."""
    dl = _seed_custom_record(tmp_path, {"artifact_id": "x"})
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=SpyClient(_VALID_RESPONSE),
        )
    assert ei.value.reason == "invalid_source_record"
    assert not _out_path(dl).exists()


# --------------------------------------------------------------------------
# 5. Missing prompt -> halt with missing_extraction_prompt BEFORE the LLM
# --------------------------------------------------------------------------
def test_missing_prompt_halts_before_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dl = _seed(tmp_path)
    spy = SpyClient(_VALID_RESPONSE)
    monkeypatch.setattr(
        cob, "_PROMPT_PATH", tmp_path / "does-not-exist.md"
    )
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=spy,
        )
    assert ei.value.reason == "missing_extraction_prompt"
    assert spy.calls == 0, "must halt BEFORE any model call"
    assert not _out_path(dl).exists()


def test_empty_prompt_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dl = _seed(tmp_path)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    spy = SpyClient(_VALID_RESPONSE)
    monkeypatch.setattr(cob, "_PROMPT_PATH", empty)
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=spy,
        )
    assert ei.value.reason == "missing_extraction_prompt"
    assert spy.calls == 0


# --------------------------------------------------------------------------
# 6. Malformed LLM response -> halt, no write
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "this is not json at all",
        "[1, 2, 3]",                       # JSON but not an object
        '{"decisions": "oops not a list"}',  # content array not a list
        '{"risks": [{"risk_id": "r1"}]}',  # missing required risk_text
        "",                                 # empty text
    ],
)
def test_malformed_llm_response_halts_no_write(
    tmp_path: Path, bad: str
) -> None:
    dl = _seed(tmp_path)
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=SpyClient(bad),
        )
    assert ei.value.reason == "malformed_llm_response"
    assert not _out_path(dl).exists(), "no partial JSONL on malformed"


# --------------------------------------------------------------------------
# 9. UUID5 pair_ids deterministic across two SEPARATE calls
# --------------------------------------------------------------------------
def test_uuid5_pair_ids_deterministic(tmp_path: Path) -> None:
    def run(root: Path) -> list[str]:
        dl = _seed(root)
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=SpyClient(_VALID_RESPONSE),
        )
        return [
            json.loads(ln)["pair_id"]
            for ln in _out_path(dl).read_text(
                encoding="utf-8"
            ).splitlines()
            if ln.strip()
        ]

    first = run(tmp_path / "a")
    second = run(tmp_path / "b")
    assert first == second, "identical input must yield identical pair_ids"
    # Sanity: ids are real, distinct UUID5 values.
    assert len(set(first)) == len(first)
    for pid in first:
        assert uuid.UUID(pid).version == 5


# --------------------------------------------------------------------------
# 10. The prompt passed to the LLM IS the canonical file (no substitution)
# --------------------------------------------------------------------------
def test_prompt_passed_matches_canonical_file(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    spy = SpyClient(_VALID_RESPONSE)
    cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=True,
        skip_existing=True,
        model=MODEL,
        client=spy,
    )
    canonical = cob._PROMPT_PATH.read_text(encoding="utf-8")
    assert spy.last_system == canonical, (
        "the system prompt sent to the model must be the verbatim "
        "meeting_minutes_llm.md — no silent substitution"
    )
    # And it is the SAME file the Haiku pipeline workflow loads.
    from spectrum_systems_core.workflows import meeting_minutes_llm
    assert cob._PROMPT_PATH == meeting_minutes_llm._PROMPT_PATH


# --------------------------------------------------------------------------
# Model string discipline: --model required, never hardcoded
# --------------------------------------------------------------------------
def test_empty_model_halts(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=True,
            skip_existing=True,
            model="   ",
            client=SpyClient(_VALID_RESPONSE),
        )
    assert ei.value.reason == "missing_model"


def test_no_hardcoded_model_string_in_script() -> None:
    """Zero ``claude-<tier>-<n>`` literals in the script body."""
    import re

    src = (
        SCRIPTS_DIR / "create_opus_reference_baselines.py"
    ).read_text(encoding="utf-8")
    hits = re.findall(r"claude-(?:opus|sonnet|haiku|mythos)-\d[\w-]*", src)
    assert not hits, f"hardcoded model string(s) in script: {hits}"


# --------------------------------------------------------------------------
# Step 4: git-ignored artifact is refused, not silently written
# --------------------------------------------------------------------------
def test_gitignored_artifact_is_refused(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    subprocess.run(
        ["git", "init", "-q", str(dl)], check=True
    )
    # Mirror the real data-lake repo: file-level **/processed/** bulk
    # ignore with NO negation -> the baseline is shadowed.
    (dl / ".gitignore").write_text(
        "**/processed/**\n", encoding="utf-8"
    )
    with pytest.raises(cob.ReferenceBaselineError) as ei:
        cob.create_baselines(
            data_lake=dl,
            source_id=None,
            dry_run=False,
            skip_existing=True,
            model=MODEL,
            client=SpyClient(_VALID_RESPONSE),
        )
    assert ei.value.reason == "gitignore_blocks_artifact"


def test_gitignore_negation_allows_artifact(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    subprocess.run(["git", "init", "-q", str(dl)], check=True)
    # Mirror exactly what the workflow's "Ensure data-lake .gitignore
    # negation" step writes on top of the data-lake repo's file-glob
    # bulk ignore: re-include the directory chain first (git cannot
    # re-include a file under an excluded parent), then the file.
    (dl / ".gitignore").write_text(
        "**/processed/**\n"
        "!**/processed/**/\n"
        "!**/processed/**/reference_baselines/"
        "opus_reference_minutes.jsonl\n",
        encoding="utf-8",
    )
    result = cob.create_baselines(
        data_lake=dl,
        source_id=None,
        dry_run=False,
        skip_existing=True,
        model=MODEL,
        client=SpyClient(_VALID_RESPONSE),
    )
    assert result["status"] == "success"
    assert _out_path(dl).is_file()


# --------------------------------------------------------------------------
# source_id filter selects exactly one transcript
# --------------------------------------------------------------------------
def test_source_id_filter(tmp_path: Path) -> None:
    dl = _seed(tmp_path)
    _make_docx(
        dl / "store" / "raw" / "transcripts" / "other-transcript-2026.docx"
    )
    other_proc = (
        dl / "store" / "processed" / "meetings"
        / "other-transcript-2026"
    )
    other_proc.mkdir(parents=True)
    (other_proc / "source_record.json").write_text(
        json.dumps(
            make_source_record("other-transcript-2026", str(uuid.uuid4()))
        )
    )
    result = cob.create_baselines(
        data_lake=dl,
        source_id=SOURCE_ID,
        dry_run=True,
        skip_existing=True,
        model=MODEL,
        client=SpyClient(_VALID_RESPONSE),
    )
    assert result["transcripts"] == 1
    assert result["per_transcript"][0]["source_id"] == SOURCE_ID
