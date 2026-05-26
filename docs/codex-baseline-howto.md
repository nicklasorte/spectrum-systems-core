# Codex Reference Baseline — How To

## What it is

A second reference baseline alongside the existing Opus baseline.
GPT-5.5 / Codex is run **locally** by the operator on a Mac against
the canonical extraction prompt and pushed to the data lake as a
one-time per-transcript artifact. The result lives next to the Opus
baseline at:

```
<data-lake>/store/processed/meetings/<source_id>/reference_baselines/
    opus_reference_minutes.jsonl    (existing — untouched)
    codex_reference_minutes.jsonl   (this baseline)
```

There is **no** CI workflow, no OpenAI API integration in this repo,
and no autonomous re-extraction. The script is operator-initiated and
the artifact is pushed to the data-lake repo manually.

## How to produce a Codex baseline

1. Open ChatGPT / Codex on a Mac.
2. Paste the canonical extraction system prompt from:
   `src/spectrum_systems_core/workflows/prompts/meeting_minutes_llm.md`
3. Paste the raw transcript text for the target source_id (extract it
   from `data-lake/store/raw/transcripts/<...>.docx` or read the
   `transcript.txt` if one was previously generated).
4. Save the model's JSON response as `codex_output.json` locally.

The JSON must conform to the `meeting_minutes` schema
(`src/spectrum_systems_core/schemas/meeting_minutes.schema.json`).
Both shapes are accepted:

- Flat payload — `{"decisions": [...], "action_items": [...], ...}` —
  what ChatGPT naturally returns from the prompt.
- Wrapped envelope — `{"artifact_type": "meeting_minutes",
  "schema_version": "1.4.0", "payload": {...}}` — what a copy of a
  promoted artifact looks like.

The schema is the gate. If Codex emits a field shape the schema
rejects (e.g. an `artifact_kind` typo), the ingest halts with
`schema_violation` and the artifact never enters the data lake.

## How to ingest it

```bash
python scripts/ingest_codex_baseline.py \
    --input-file codex_output.json \
    --source-id <source_id> \
    --data-lake ../data-lake \
    --operator <your-handle>
```

The script:

1. Reads `codex_output.json`.
2. Validates against `meeting_minutes.schema.json`.
3. Reads `source_record.json` for the canonical transcript UUID.
4. Explodes the payload into the same JSONL row shape as the Opus
   baseline (one row per extracted item).
5. Writes `codex_reference_minutes.jsonl` next to
   `opus_reference_minutes.jsonl`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Ingested (or `--dry-run` validated) |
| 1 | Schema violation, missing source_record, or already_ingested |
| 2 | Input file not found / malformed JSON / data-lake missing |

The data lake is append-only from core's perspective. If a
`codex_reference_minutes.jsonl` already exists for the source the
script halts with `already_ingested`. Removing the existing file is a
deliberate operator action in the data-lake repo, not a side-effect
of the ingest.

The default model string is resolved from
`ai/registry/model_registry.json::codex_reference_baseline.model_id`
so re-keying the registry flows through automatically; an explicit
`--model <id>` overrides for one-off experiments.

## After ingesting

`git -C ../data-lake add store/processed/meetings/<source_id>/reference_baselines/codex_reference_minutes.jsonl`
and commit/push from the data-lake repo. The artifact is now visible
to any downstream consumer.

## How to compare

> **Status:** comparison wiring is intentionally **deferred** to a
> follow-up PR. The current PR delivers the data-lake slot and the
> ingestion gate only.
>
> Once the comparator is wired up, the command will be along the
> lines of `python scripts/compare_opus_haiku.py --baseline both` (or
> a parallel script). Until then, an operator can read the
> `codex_reference_minutes.jsonl` directly to inspect what Codex
> returned.

## Storage location

```
data-lake/store/processed/meetings/<source_id>/
    reference_baselines/
        opus_reference_minutes.jsonl   (existing, untouched)
        codex_reference_minutes.jsonl  (this baseline)
```

The path mirrors the Opus baseline exactly — same directory, same
JSONL shape, distinct filename. The comparison engine's existing
`reference_baselines/` directory convention is reused; no new
directory structure is introduced.

## On the JSONL shape

Each line is one extracted item. The fields match the Opus baseline
row shape:

```json
{
    "pair_id": "<UUID5 over 'codex-ref-<source_id>-<etype>-<index>'>",
    "source_id": "<source_id>",
    "source_artifact_id": "<canonical transcript UUID>",
    "extraction_type": "decisions",
    "ground_truth_text": "...",
    "item_data": { ...the original item verbatim... },
    "human_authored": false,
    "model_authored": true,
    "model_id": "gpt-5.5",
    "verified": false,
    "status": "reference_only",
    "provenance": {
        "produced_by": "codex_reference_baseline_workflow",
        "operator": "<--operator value>"
    },
    "schema_version": "1.4.0",
    "meeting_date": "<from input or null>",
    "created_at": "<UTC ISO at ingest time>",
    "chunking_strategy_version": "speaker_turn_v1"
}
```

The `pair_id` UUID5 namespace is **frozen**: re-ingesting the same
input file over the same source produces identical ids for identical
items. The namespace is distinct from the Opus namespace, so a Codex
row and an Opus row for the same item slot get distinct ids and the
two are never confused.

## Operator etiquette

- Codex baselines are **reference only**. They are never promoted,
  never read back into the governed loop.
- Run Codex on a transcript **once**. The data lake is append-only;
  re-running for the same source is a deliberate act.
- Stamp `--operator` with a real human identifier (your name or
  handle). It lands in every JSONL row's `provenance.operator` for
  audit.
