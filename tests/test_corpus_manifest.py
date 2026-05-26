"""Unit tests for ``workflows.corpus_manifest``.

The manifest exists to break the Jan-21 pairing tie deterministically.
These tests pin both halves of the keying contract:

  - ``classify_minutes_filename`` returns the same ``meeting_type``
    token the manifest uses for each transcript ``source_id``.
  - ``find_minutes_for_source`` resolves both Jan-21 transcripts to
    DISTINCT minutes files (the original bug).
  - Every entry in ``CORPUS_MANIFEST`` resolves to exactly one minutes
    file when a synthetic minutes directory carries the canonical
    filenames — no silent dead entries.

ZERO LLM calls, no network, no data lake.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from spectrum_systems_core.workflows.corpus_manifest import (
    CORPUS_MANIFEST,
    classify_minutes_filename,
    find_minutes_for_source,
)


# The canonical minutes filename for each source_id in the manifest.
# These are the actual filenames as published by NTIA (or the
# date-normalized form after a .docx -> .txt conversion). Keep in sync
# with the manifest; the resolution test below proves they agree.
CANONICAL_MINUTES_FILENAMES: dict[str, str] = {
    "20251216---p2p-tig-meeting-16dec2025---transcript":
        "P2P TIG Kickoff Meeting Minutes 20251216 FINAL.txt",
    "7-ghz-ul-kickoff-transcript-20251217":
        "7 GHz Uplink TIG Kickoff Meeting Minutes 20251217 FINAL.txt",
    "7-ghz-downlink-tig-meeting-kickoff---transcript-20251218":
        "7 GHz Downlink TIG Kickoff Meeting Minutes 20251218 FINAL.txt",
    "7-ghz-study-working-group-meeting---transcript-20260115":
        "7 GHz WG Meeting Minutes 20260115 - Final.txt",
    "7-ghz-fixed_transportable-point-to-point--p2p--tig-meeting-transcript-20260120":
        "7 GHz P2P TIG Meeting Minutes 20260120 Final.txt",
    "7-ghz-study-plan-comment-adjudication-meeting-with-working-group---transcript-20260121":
        "7 GHz Study Plan Comment Adjudication Meeting Minutes - 20260121 - Final.txt",
    "7-ghz-uplink-tig-meeting-transcript-21jan26":
        "7 GHz Uplink TIG Meeting Minutes 20260121 Final.txt",
    "7-ghz-downlink-tig-meeting-transcript---22jan2026":
        "7 GHz Downlink TIG Meeting Minutes 20260122 Final.txt",
    "7-ghz-study-working-group-meeting-5feb2026---transcript":
        "7 GHz WG Meeting Minutes 20260205 Final.txt",
    "7-ghz-p2p-tig---transcript-2-17-26":
        "7 GHz P2P TIG Meeting Minutes 20260217 Final.txt",
    "7-ghz-uplink-tig---transcript-2-18-26":
        "7 GHz Uplink TIG Meeting Minutes 20260218 Final.txt",
    "7-ghz-downlink-tig-meeting---transcript-2-19-26":
        "7 GHz Downlink TIG Meeting Minutes 20260219 Final.txt",
    "7-ghz-study-working-group-meeting---5mar2026---transcript":
        "7 GHz WG Meeting Minutes 20260305 Final.txt",
}


@pytest.fixture()
def populated_minutes_dir(tmp_path: Path) -> Path:
    """A scratch directory with every canonical minutes filename present."""
    for filename in CANONICAL_MINUTES_FILENAMES.values():
        (tmp_path / filename).write_text("stub\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# classify_minutes_filename


def test_classify_uplink_kickoff():
    assert classify_minutes_filename(
        "7 GHz Uplink TIG Kickoff Meeting Minutes 20251217 FINAL.txt"
    ) == ("20251217", "uplink_tig_kickoff")


def test_classify_downlink_kickoff():
    assert classify_minutes_filename(
        "7 GHz Downlink TIG Kickoff Meeting Minutes 20251218 FINAL.txt"
    ) == ("20251218", "downlink_tig_kickoff")


def test_classify_p2p_kickoff():
    assert classify_minutes_filename(
        "P2P TIG Kickoff Meeting Minutes 20251216 FINAL.txt"
    ) == ("20251216", "p2p_tig_kickoff")


def test_classify_adjudication_beats_working_group():
    # The adjudication file literally embeds 'Meeting with Working
    # Group' — a naive WG check would misroute it. Adjudication wins.
    assert classify_minutes_filename(
        "7 GHz Study Plan Comment Adjudication Meeting Minutes - 20260121 - Final.txt"
    ) == ("20260121", "adjudication_wg")


def test_classify_working_group():
    assert classify_minutes_filename(
        "7 GHz WG Meeting Minutes 20260115 - Final.txt"
    ) == ("20260115", "working_group")


def test_classify_uplink_tig_non_kickoff():
    assert classify_minutes_filename(
        "7 GHz Uplink TIG Meeting Minutes 20260121 Final.txt"
    ) == ("20260121", "uplink_tig")


# ---------------------------------------------------------------------------
# find_minutes_for_source — the Jan 21 ambiguity is the regression case.


def test_jan21_uplink_resolves_to_uplink_minutes(
    populated_minutes_dir: Path,
):
    result = find_minutes_for_source(
        "7-ghz-uplink-tig-meeting-transcript-21jan26",
        populated_minutes_dir,
    )
    assert result is not None
    assert "Uplink" in result.name
    assert "Adjudication" not in result.name


def test_jan21_adjudication_resolves_to_adjudication_minutes(
    populated_minutes_dir: Path,
):
    result = find_minutes_for_source(
        "7-ghz-study-plan-comment-adjudication-meeting-with-working-group"
        "---transcript-20260121",
        populated_minutes_dir,
    )
    assert result is not None
    assert "Adjudication" in result.name
    assert "Uplink" not in result.name


def test_jan21_pair_resolves_to_distinct_files(
    populated_minutes_dir: Path,
):
    uplink = find_minutes_for_source(
        "7-ghz-uplink-tig-meeting-transcript-21jan26",
        populated_minutes_dir,
    )
    adj = find_minutes_for_source(
        "7-ghz-study-plan-comment-adjudication-meeting-with-working-group"
        "---transcript-20260121",
        populated_minutes_dir,
    )
    assert uplink is not None and adj is not None
    assert uplink != adj


def test_all_manifest_entries_resolve(populated_minutes_dir: Path):
    """Every manifest entry pairs with exactly one minutes filename."""
    unresolved: list[str] = []
    for source_id in CORPUS_MANIFEST:
        result = find_minutes_for_source(source_id, populated_minutes_dir)
        if result is None:
            unresolved.append(source_id)
    assert not unresolved, f"unresolved source_ids: {unresolved}"


def test_unknown_source_id_returns_none(populated_minutes_dir: Path):
    assert find_minutes_for_source("not-in-manifest", populated_minutes_dir) is None


def test_missing_minutes_directory_returns_none(tmp_path: Path):
    assert find_minutes_for_source(
        "7-ghz-uplink-tig-meeting-transcript-21jan26",
        tmp_path / "does-not-exist",
    ) is None


def test_no_matching_filename_returns_none(tmp_path: Path):
    # A directory with the wrong filenames should still return None,
    # not silently pick the first .txt it sees.
    (tmp_path / "random-other-doc.txt").write_text("x\n", encoding="utf-8")
    assert find_minutes_for_source(
        "7-ghz-uplink-tig-meeting-transcript-21jan26",
        tmp_path,
    ) is None
