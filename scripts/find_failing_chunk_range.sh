#!/usr/bin/env bash
# Binary search for the minimum MAX_CHUNKS value that causes
# failed:required_meeting_minutes_fields on Dec 18.
#
# Usage: scripts/find_failing_chunk_range.sh [SOURCE_ID]
#
# Requirements: gh CLI authenticated, jq installed
#
# The script:
# 1. Starts with low=1, high=138
# 2. Tries mid=(low+high)/2 chunks
# 3. If it fails: high=mid (failure is in [low..mid])
# 4. If it passes: low=mid+1 (failure is in [mid+1..high])
# 5. Repeats until low==high, which is the minimum failing chunk count
# 6. Also identifies the passing threshold (low-1)
set -uo pipefail

SOURCE_ID="${1:-7-ghz-downlink-tig-meeting-kickoff---transcript-20251218}"
REPO="nicklasorte/spectrum-systems-core"
WORKFLOW="debug-llm-extraction.yml"
POLL_INTERVAL=15
TIMEOUT=600  # 10 minutes max per run

run_with_max_chunks() {
    local max_chunks=$1
    echo "==> Trying MAX_CHUNKS=$max_chunks..." >&2

    # Dispatch the workflow
    if ! gh workflow run "$WORKFLOW" \
        -f source_id="$SOURCE_ID" \
        -f max_chunks="$max_chunks" \
        --repo "$REPO" >&2; then
        echo "    ERROR: gh workflow run failed for MAX_CHUNKS=$max_chunks" >&2
        return 2
    fi

    sleep 5  # Wait for run to register

    # Get the most recent run ID for this workflow
    local run_id
    run_id=$(gh run list --workflow="$WORKFLOW" --repo="$REPO" \
        --limit 1 --json databaseId --jq '.[0].databaseId')

    if [ -z "$run_id" ] || [ "$run_id" = "null" ]; then
        echo "    ERROR: could not resolve run ID after dispatch" >&2
        return 2
    fi

    echo "    Run ID: $run_id" >&2

    # Poll until complete
    local elapsed=0
    while [ $elapsed -lt $TIMEOUT ]; do
        local status
        status=$(gh run view "$run_id" --repo="$REPO" \
            --json status,conclusion \
            --jq '{status: .status, conclusion: .conclusion}')

        local run_status
        run_status=$(echo "$status" | jq -r '.status')

        if [ "$run_status" = "completed" ]; then
            local conclusion
            conclusion=$(echo "$status" | jq -r '.conclusion')
            echo "    Completed: $conclusion (run $run_id)" >&2

            if [ "$conclusion" = "success" ]; then
                return 0  # passed
            else
                return 1  # failed
            fi
        fi

        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    echo "    TIMEOUT after ${TIMEOUT}s (run $run_id)" >&2
    return 2  # timeout
}

echo "Binary search for minimum failing chunk count on $SOURCE_ID"
echo "Total chunks: 138"
echo ""

low=1
high=138

# First verify that 138 fails and 1 passes
echo "==> Verifying: MAX_CHUNKS=138 should fail..."
run_with_max_chunks 138
rc=$?
if [ $rc -eq 0 ]; then
    echo "UNEXPECTED: MAX_CHUNKS=138 passed. No failure to find."
    exit 0
elif [ $rc -eq 2 ]; then
    echo "ERROR: MAX_CHUNKS=138 dispatch/poll failed. Aborting."
    exit 2
fi
echo "    Confirmed: 138 fails."

echo "==> Verifying: MAX_CHUNKS=1 should pass..."
run_with_max_chunks 1
rc=$?
if [ $rc -eq 1 ]; then
    echo "UNEXPECTED: MAX_CHUNKS=1 failed. Failure is in chunk 1 itself."
    echo "Result: failure starts at chunk 1"
    exit 0
elif [ $rc -eq 2 ]; then
    echo "ERROR: MAX_CHUNKS=1 dispatch/poll failed. Aborting."
    exit 2
fi
echo "    Confirmed: 1 passes."

# Binary search
while [ $low -lt $high ]; do
    mid=$(( (low + high) / 2 ))

    run_with_max_chunks "$mid"
    rc=$?

    if [ $rc -eq 0 ]; then
        echo "    MAX_CHUNKS=$mid PASSED -- failure is above $mid"
        low=$((mid + 1))
    elif [ $rc -eq 1 ]; then
        echo "    MAX_CHUNKS=$mid FAILED -- failure is at or below $mid"
        high=$mid
    else
        echo "    ERROR: MAX_CHUNKS=$mid dispatch/poll failed. Aborting binary search."
        echo "    Current search range: [$low, $high]"
        exit 2
    fi

    echo "    Search range: [$low, $high]"
    echo ""
done

echo "=============================="
echo "RESULT: Minimum failing chunk count = $low"
echo "  MAX_CHUNKS=$((low - 1)) passes"
echo "  MAX_CHUNKS=$low fails"
echo ""
echo "This means the failure-triggering content is in chunks $((low-1))+1 to $low."
echo "Run with MAX_CHUNKS=$low and DEBUG_CHUNKS=true to see which chunk fails."
echo "=============================="
