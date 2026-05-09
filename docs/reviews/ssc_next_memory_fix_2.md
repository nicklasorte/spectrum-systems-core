# SSC-NEXT-MEMORY — Fix Pass #2

Resolves the must_fix and should_fix items from
`docs/reviews/ssc_next_memory_redteam_2.md`.

| ID  | Status | Where the fix landed | Regression test |
| --- | ------ | -------------------- | --------------- |
| M4  | fixed  | Module-level docstrings of `data_lake/run_history.py` and `data_lake/experience.py` now name the distinguishing fields. | `tests/test_redteam_2_fixes.py::test_M4_run_history_records_have_pointer_fields_not_lesson_fields`, `test_M4_experience_history_records_have_lesson_fields_not_pointers` |
| M5  | fixed  | `data_lake/eval_history.py::_coerce_score` rejects strings, bools, and other non-numeric values. | `tests/test_redteam_2_fixes.py::test_M5_score_is_float_or_none_only`, `test_M5_score_coercion_rejects_string_and_bool` |
| S3  | fixed  | `data_lake/debug.py::_INSPECTION_HINTS` covers every known reason code; defended by a presence test. | `tests/test_redteam_2_fixes.py::test_S3_known_reason_codes_have_inspection_hints` |
| S4  | fixed  | The artifact index walker matches `*.json` only and now has an explicit regression that none of the JSONL projections leak into it. | `tests/test_redteam_2_fixes.py::test_S4_jsonl_files_are_not_picked_up_by_artifact_index` |
| D3  | deferred | Append-only run history would break the determinism rule (constitution §9 / contract §9). Defer until incremental reprocessing is a real use case. | n/a |
| D4  | deferred | The "diagnose-in-2-minutes" claim is structural, not measured. Debug → run note → index all surface the same explanation. Defer the timing claim. | n/a |
