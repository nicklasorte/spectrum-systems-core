#!/usr/bin/env bash
#
# Phase 3 — operator measurement runbook for the glossary production
# wiring. NOT a CI job. Dispatches the production extraction (with the
# Phase-3 default-enabled glossary), waits for the operator to confirm
# completion, dispatches the matching comparison, and then prints the
# F1 delta against the post-Phase-2 baseline of 0.395.
#
# Usage:
#   scripts/run_glossary_measurement.sh [SOURCE_ID]
#
# The default SOURCE_ID is the 7-GHz Dec-18 transcript that was the
# Phase 2 calibration target. Pass any other source_id to measure
# the impact on a different transcript.
set -euo pipefail

SOURCE_ID="${1:-7-ghz-downlink-tig-meeting-kickoff---transcript-20251218}"
BASELINE_F1="${BASELINE_F1:-0.395}"
REPO="${REPO:-nicklasorte/spectrum-systems-core}"

echo "==> Step 1: Dispatching extraction for $SOURCE_ID (glossary default: enabled)"
gh workflow run debug-llm-extraction.yml \
    -f source_id="$SOURCE_ID" \
    -f debug_chunks=false \
    --repo "$REPO"
echo "    Wait for completion in Actions UI, then continue"
read -r -p "Press enter once extraction completes... " _

echo "==> Step 2: Dispatching comparison for $SOURCE_ID"
gh workflow run compare-opus-haiku.yml \
    -f source_id="$SOURCE_ID" \
    --repo "$REPO"
echo "    Wait for completion in Actions UI, then continue"
read -r -p "Press enter once comparison completes... " _

echo "==> Step 3: Reading latest comparison artifact"
python scripts/print_comparison_delta.py \
    --source-id "$SOURCE_ID" \
    --baseline-f1 "$BASELINE_F1"
