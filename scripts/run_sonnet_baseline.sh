#!/usr/bin/env bash
# Phase 5 — Sonnet baseline runbook.
#
# Usage: scripts/run_sonnet_baseline.sh [SOURCE_ID] [VARIANT]
#   VARIANT: "haiku-prompt" (apples-to-apples) or "opus-prompt" (Sonnet's capability)
#
# Exit codes:
#   0  -- full success: extraction + comparison + result printed
#   1  -- VARIANT argument unrecognised
#   10 -- extraction failed; no comparison attempted
#   20 -- extraction succeeded but comparison failed
#   30 -- comparison succeeded but result formatting failed (raw artifact at PATH printed)
#
# The script is operator-driven: it dispatches GitHub workflows and
# waits for the operator to confirm completion before proceeding to
# the next step. This is intentional: the runbook is a measurement
# instrument, not an autonomous loop. A future PR may add a
# `--poll` mode that watches the Actions UI directly.
set -euo pipefail

SOURCE_ID="${1:-7-ghz-downlink-tig-meeting-kickoff---transcript-20251218}"
VARIANT="${2:-haiku-prompt}"
DATA_LAKE_PATH="${DATA_LAKE_PATH:-$PWD/data-lake}"

if [ "$VARIANT" = "haiku-prompt" ]; then
    MODEL="sonnet"
elif [ "$VARIANT" = "opus-prompt" ]; then
    MODEL="sonnet-unconstrained"
else
    echo "ERROR: VARIANT must be 'haiku-prompt' or 'opus-prompt', got '$VARIANT'"
    exit 1
fi

# The two dispatched workflows write to the remote nicklasorte/data-lake
# repo. The local clone at $DATA_LAKE_PATH starts out stale relative to
# what just landed remotely; we MUST git-pull after each dispatch
# before reading any artifact off disk. Without this, Step 3 reads
# either a missing file or an older comparison and silently lies
# about the Sonnet measurement (Codex review P1, Phase 5 follow-up).
sync_data_lake() {
    local label="$1"
    if [ ! -d "${DATA_LAKE_PATH}/.git" ]; then
        echo "    WARNING: ${DATA_LAKE_PATH} is not a git clone; cannot sync (${label})."
        echo "    Set DATA_LAKE_PATH to your local data-lake clone, or clone"
        echo "    nicklasorte/data-lake there before re-running."
        return 0
    fi
    echo "    Syncing ${DATA_LAKE_PATH} with origin/main (${label})..."
    if ! git -C "${DATA_LAKE_PATH}" fetch --quiet origin main; then
        echo "    ERROR: git fetch failed (${label}). Aborting before reading stale data."
        return 1
    fi
    if ! git -C "${DATA_LAKE_PATH}" reset --hard --quiet origin/main; then
        echo "    ERROR: git reset --hard origin/main failed (${label})."
        return 1
    fi
}

echo "==> Step 1/3: Dispatching Sonnet extraction for $SOURCE_ID (variant: $VARIANT, model: $MODEL)"
if ! gh workflow run debug-llm-extraction.yml \
    -f source_id="$SOURCE_ID" \
    -f model="$MODEL" \
    --repo nicklasorte/spectrum-systems-core; then
    echo "ERROR: Step 1/3 (extraction dispatch) failed"
    exit 10
fi

echo "    Wait for completion in Actions UI, then press enter..."
read -r
sync_data_lake "post-extraction" || exit 10

echo "==> Step 2/3: Dispatching three-way comparison for $SOURCE_ID"
if ! gh workflow run compare-opus-haiku.yml \
    -f source_id="$SOURCE_ID" \
    -f include_sonnet=true \
    --repo nicklasorte/spectrum-systems-core; then
    echo "ERROR: Step 2/3 (comparison dispatch) failed"
    exit 20
fi

echo "    Wait for completion in Actions UI, then press enter..."
read -r
sync_data_lake "post-comparison" || exit 20

echo "==> Step 3/3: Reading three-way comparison artifact"
if ! python scripts/print_three_way_delta.py \
    --source-id "$SOURCE_ID" \
    --variant "$VARIANT" \
    --data-lake "$DATA_LAKE_PATH"; then
    echo "ERROR: Step 3/3 (result formatting) failed; raw artifact in data lake"
    exit 30
fi

echo "==> Done. Sonnet ($VARIANT) F1 measurement complete."
