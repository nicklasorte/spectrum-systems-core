# Cascade Filter — Per-Item Keep/Drop Judgment

You are evaluating extracted items from a meeting transcript. For each item, decide whether to KEEP or DROP it.

KEEP an item if:
- The group actually committed to or established what the item claims
- The item's text is supported by the transcript chunk
- The item is non-trivial and meaningful (not a transcription artifact)

DROP an item if:
- The item describes a hypothetical, rhetorical question, or self-correction
- The item is a third-party statement not endorsed by the group
- The item is a transcription artifact or speech disfluency
- The item duplicates another item without adding information
- The item is too vague to be actionable

## Input

You receive a transcript chunk and a list of candidate items extracted from it. Each item has:
- An `item_idx` (its position in the input list)
- Its full structured data EXCEPT the `reason` field
- Its grounding (either `source_quote` for verbatim items or `source_turn_ids` for aggregate items)

## Output

Return a JSON array. Each entry: `{"item_idx": <integer>, "decision": "keep" or "drop", "reason": "<one sentence>"}`.

Every input item must appear exactly once. The `reason` for KEEP explains what makes the item meaningful. The `reason` for DROP explains why the item should be excluded.

## Transcript Chunk

<chunk_text>

## Candidate Items

<items_json_without_reason_field>

## Your Response

Return ONLY the JSON array. No preamble, no explanation outside the JSON.
