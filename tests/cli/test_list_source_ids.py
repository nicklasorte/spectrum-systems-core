"""Tests for ``cli list-source-ids`` and ``cli run-single``.

Phase Perf. The matrix workflow consumes the JSON output of
list-source-ids. A mismatch between the slugs emitted here and the slugs
the orchestrator uses would cause matrix jobs to process source_ids that
don't exist on disk -- so we verify that this CLI re-uses the
orchestrator's ``_slugify`` and that the matrix wrapper ``run-single``
delegates to ``run_pipeline`` with ``--specific-source-id``.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spectrum_systems_core import cli
from spectrum_systems_core.orchestration.pipeline_orchestrator import _slugify


def _make_lake(*filenames: str) -> str:
    tmp = tempfile.mkdtemp()
    transcripts = Path(tmp) / "store" / "raw" / "transcripts"
    transcripts.mkdir(parents=True)
    for fn in filenames:
        (transcripts / fn).touch()
    return tmp


class ListSourceIdsTests(unittest.TestCase):
    def test_returns_all_transcripts_text_format(self) -> None:
        lake = _make_lake(
            "Meeting Alpha.docx",
            "Meeting Beta.docx",
            "Meeting Gamma.docx",
        )
        buf = io.StringIO()
        rc = cli.list_source_ids(data_lake=lake, fmt="text", out_stream=buf)
        self.assertEqual(rc, 0)
        ids = [line for line in buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(ids), 3)
        self.assertEqual(
            sorted(ids),
            sorted(["meeting-alpha", "meeting-beta", "meeting-gamma"]),
        )

    def test_json_format_is_valid_json_array(self) -> None:
        lake = _make_lake("Meeting Alpha.docx", "Meeting Beta.docx")
        buf = io.StringIO()
        rc = cli.list_source_ids(data_lake=lake, fmt="json", out_stream=buf)
        self.assertEqual(rc, 0)
        out = buf.getvalue().strip()
        ids = json.loads(out)
        self.assertIsInstance(ids, list)
        self.assertEqual(sorted(ids), sorted(["meeting-alpha", "meeting-beta"]))

    def test_empty_transcripts_returns_empty_array(self) -> None:
        lake = _make_lake()  # no .docx
        buf = io.StringIO()
        rc = cli.list_source_ids(data_lake=lake, fmt="json", out_stream=buf)
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "[]")

    def test_minutes_filenames_filtered_out(self) -> None:
        # PipelineOrchestrator.scan() filters .docx/.txt files containing
        # "minutes". list-source-ids must mirror that filter exactly so
        # the matrix never tries to process a slug the orchestrator
        # would have skipped.
        lake = _make_lake("Meeting Alpha.docx", "feb-19-minutes.docx")
        buf = io.StringIO()
        rc = cli.list_source_ids(data_lake=lake, fmt="json", out_stream=buf)
        self.assertEqual(rc, 0)
        ids = json.loads(buf.getvalue().strip())
        self.assertEqual(ids, ["meeting-alpha"])

    def test_slug_matches_orchestrator_slugify_exactly(self) -> None:
        # Critical invariant: the source_ids emitted by list-source-ids
        # MUST match what PipelineOrchestrator._slugify() produces; otherwise
        # the matrix job ID won't resolve to a real transcript on disk.
        # Use a stem that exercises every slugify edge case the
        # orchestrator handles: case folding, spaces -> '-', slashes
        # would cause a real OS-level failure, so we use the dot-form
        # date that actually appears in real transcript filenames.
        stem = "Working Group 7-GHz Downlink TIG Meeting -- Transcript 2.19.26"
        lake = _make_lake(stem + ".docx")
        buf = io.StringIO()
        rc = cli.list_source_ids(data_lake=lake, fmt="json", out_stream=buf)
        self.assertEqual(rc, 0)
        emitted = json.loads(buf.getvalue().strip())
        expected_slug = _slugify(stem)
        self.assertEqual(emitted, [expected_slug])

    def test_dedupes_docx_and_txt_for_same_slug(self) -> None:
        # When DocxExtractor has run, both .docx and .txt exist for the
        # same transcript. The matrix must NOT process the source_id
        # twice -- once is enough.
        lake = _make_lake("Meeting Alpha.docx", "Meeting Alpha.txt")
        buf = io.StringIO()
        rc = cli.list_source_ids(data_lake=lake, fmt="json", out_stream=buf)
        self.assertEqual(rc, 0)
        ids = json.loads(buf.getvalue().strip())
        self.assertEqual(ids, ["meeting-alpha"])

    def test_missing_data_lake_returns_error(self) -> None:
        buf = io.StringIO()
        rc = cli.list_source_ids(
            data_lake="/nonexistent/path/does/not/exist",
            fmt="json", out_stream=buf,
        )
        self.assertEqual(rc, 1)


class RunSingleTests(unittest.TestCase):
    def test_run_single_delegates_to_run_pipeline_with_specific_source_id(self) -> None:
        # run-single must NOT reimplement orchestration; it must thinly
        # forward to run_pipeline with --specific-source-id pinned.
        with mock.patch.object(
            cli, "run_pipeline", return_value=0,
        ) as mock_rp:
            rc = cli.run_single(
                source_id="meeting-alpha",
                data_lake="/tmp/lake",
                force=True,
                skip_existing=True,
            )
        self.assertEqual(rc, 0)
        mock_rp.assert_called_once()
        kwargs = mock_rp.call_args.kwargs
        self.assertEqual(kwargs.get("specific_source_id"), "meeting-alpha")
        self.assertEqual(kwargs.get("force"), True)
        self.assertEqual(kwargs.get("force_only_missing"), True)
        self.assertEqual(kwargs.get("dry_run"), False)

    def test_run_single_requires_source_id(self) -> None:
        buf = io.StringIO()
        rc = cli.run_single(source_id="", out_stream=buf)
        self.assertEqual(rc, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
