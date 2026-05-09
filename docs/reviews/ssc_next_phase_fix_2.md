# Fix Pass #2 — Response to Red Team Review #2

Document ID: SSC-FIX-002
Scope: Resolutions for findings in `ssc_next_phase_redteam_2.md`.

Each finding lists status, code change, and the test(s) that lock it in.

---

## must_fix

### KW1. Keyword search matched JSON encoding structure — FIXED

**Change**: `data_lake/query.py::_keyword_matches` now collects string
leaves from the payload via `_collect_string_leaves` (a flat traversal
that yields only `str` leaves, never field names, brackets, escapes, or
booleans). The substring check runs against those leaves, one at a time.
The first matching leaf returns `("payload_text",)` so the existing
matched-fields semantics remain.

**Tests**:
- `test_keyword_does_not_match_json_field_names`
- `test_keyword_does_not_match_meeting_id_field_name`
- `test_keyword_still_matches_real_string_content`

---

## should_fix

### DT1. Date filter inputs were not validated — FIXED

**Change**: `query` now calls `_validate_date_input` on `date_from` and
`date_to` before running. Anything that does not match the contract's
`YYYY-MM-DD` regex raises `QueryError` with the rejected value in the
message.

**Tests**:
- `test_query_rejects_non_yyyy_mm_dd_date_from`
- `test_query_rejects_non_yyyy_mm_dd_date_to`
- `test_query_accepts_valid_yyyy_mm_dd_dates`

### IDX1. Index could be silently stale — FIXED

**Change**: `query` rebuilds the index file on demand when it does not
exist. The module docstring documents the policy: an existing index is
trusted; a missing index is rebuilt. Mutations to `processed/` after a
query has run are not detected — callers that change `processed/` must
call `write_artifact_index` themselves.

**Tests**: `test_query_builds_index_on_demand_when_missing`.

---

## defer_with_reason

### KW2. Substring keyword has no word boundary — DEFERRED

The current behavior (`fcc` matches `fcco`) is documented in the module
docstring: case-insensitive substring against string content. Word-
boundary matching needs Unicode tokenization rules and adds complexity
that no real failure has yet justified. Reason: the constitution prefers
boring code; defer until a concrete miss happens.

### SEM. Semantic search creep — DEFERRED

None observed. Recorded so future drift is checked against this baseline.
Reason: the constitution prohibits semantic search until deterministic
index/query is real and useful. The deterministic path is now real and
useful; the prohibition stands.

### ECP. Grounding eval substring laxity — DEFERRED

All current extractors emit the full transcript line as the source
excerpt, so the laxity does not produce wrong promotions today. Adding a
length or identity rule defends against a producer that does not exist.
Re-examine when a future workflow synthesizes excerpts.

---

## Verdict

KW1 closes the only correctness bug found. DT1 fails closed on input the
contract didn't allow. IDX1 removes a "you forgot to rebuild" footgun.
KW2/SEM/ECP remain documented choices, not bugs. Run count: 133 tests
pass.
