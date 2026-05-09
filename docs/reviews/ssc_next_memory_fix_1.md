# SSC-NEXT-MEMORY — Fix Pass #1

Resolves the must_fix and should_fix items from
`docs/reviews/ssc_next_memory_redteam_1.md`.

| ID  | Status | Where the fix landed | Regression test |
| --- | ------ | -------------------- | --------------- |
| M1  | fixed  | `data_lake/markdown.py::_canonical_json_relpath_from_artifact_md` returns `(unwritten)` sentinel; the body Links section explains it. | `tests/test_redteam_1_fixes.py::test_M1_canonical_json_path_uses_unwritten_sentinel_when_unknown` |
| M2  | fixed  | `_BACKLINK_NOTE` now reads "JSON is the canonical source of truth ... core never reads it back." | `tests/test_redteam_1_fixes.py::test_M2_artifact_markdown_says_json_is_canonical` |
| M3  | fixed  | `render_index_markdown` emits a body line "JSON is canonical. ... regenerated views." right under the meeting metadata. | `tests/test_redteam_1_fixes.py::test_M3_index_body_states_canonical` |
| S1  | fixed  | Agency and topic notes now show "Original agency string: `<value>`" / "Original topic string: `<value>`" so the slug→value mapping is explicit. | `tests/test_redteam_1_fixes.py::test_S1_agency_note_includes_original_string`, `test_S1_topic_note_includes_original_string` |
| S2  | fixed by docstring + constant | Added `_ARTIFACT_MD_TO_INDEX_RELPATH = "../index.md"` constant in `markdown.py` and a comment explaining the assumed depth. The relative path is also defended by `tests/test_cli_process_meeting.py::test_no_broken_relative_links_in_artifact_markdown`. | existing |
| D1  | deferred | Cross-meeting agency index would expand the lake contract beyond per-meeting writers; defer until vault usage shows the need. | n/a |
| D2  | deferred | The run-note Markdown (`runs/<run_id>.md`, SSC-031) already gives a friendly view of `manifest__*` and `debug__*`; an additional Markdown rendering of those JSON files would be redundant. | n/a |

All `pytest` runs after these fixes pass.
