# Few-Shot Example Review Checklist

Phase X2.2 — review each candidate below before it can reach the
extraction prompt. Examples are loaded ONLY when `verified: true`.

- Source meeting: `7-ghz-downlink-tig-meeting-kickoff---transcript-20251218`
- Transcript reference: `/home/runner/work/spectrum-systems-core/spectrum-systems-core/data-lake/store/processed/meetings/7-ghz-downlink-tig-meeting-kickoff---transcript-20251218`

## Review instructions

1. Open the source transcript and locate each referenced `source_turn_ids`.
2. Confirm the `expected_output.decision_text` matches what was actually said.
3. Confirm the `decision_outcome` is correct
   (`approval`, `deferral`, or `action_required`).
4. If correct, run:

   ```
   python scripts/verify_example.py \
       --example-id <example_id> \
       --reviewer-id <your-name> \
       --data-lake <path>
   ```

Reviewer policy: the reviewer MUST be a different person from
the operator who ran the extraction. The system cannot enforce
this technically; the audit_log records the reviewer_id.

## Candidates

### `21f8b17f-0b9d-4dca-aa29-bc4ba9c4fde7`

- outcome: `approval`
- source_turn_ids: `['12aec35e-1ba1-4689-8d77-9c643cd5766c']`
- confidence: `1.0`

Decision text:

> Meeting agenda approved: scope confirmation, sharing-criteria approach, and action items for the next session

### `5d9a57fd-e8b1-4e57-90e1-2f767ef16130`

- outcome: `deferral`
- source_turn_ids: `['fcb5679e-13f9-4e84-80e2-273f5f82a6e4']`
- confidence: `1.0`

Decision text:

> Decision on Course of Action (COA) selection deferred to the next working group meeting pending completion of working paper from Nick

### `51424f03-7239-45d9-98e9-0724c54a7c5f`

- outcome: `approval`
- source_turn_ids: `['fcdf449e-ccdd-4a2c-84a3-b592cbcf5ae1']`
- confidence: `1.0`

Decision text:

> Group approved application of ITU two-point criteria for FSS protection analysis: negative 10.5 dB at 80th percentile and negative 6 dB at 0.03 percent

