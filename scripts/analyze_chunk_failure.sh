#!/usr/bin/env bash
# After find_failing_chunk_range.sh identifies the failing chunk count N,
# run this with MAX_CHUNKS=N and debug flags to see what's different.
#
# Usage: scripts/analyze_chunk_failure.sh <MAX_CHUNKS> [SOURCE_ID]
set -euo pipefail

MAX_CHUNKS="${1:?Usage: analyze_chunk_failure.sh <MAX_CHUNKS> [SOURCE_ID]}"
SOURCE_ID="${2:-7-ghz-downlink-tig-meeting-kickoff---transcript-20251218}"
REPO="nicklasorte/spectrum-systems-core"

echo "==> Dispatching debug run with MAX_CHUNKS=$MAX_CHUNKS..."
gh workflow run debug-llm-extraction.yml \
    -f source_id="$SOURCE_ID" \
    -f max_chunks="$MAX_CHUNKS" \
    -f debug_chunk_decomposition=true \
    --repo "$REPO"

echo "Watch the Actions UI for the debug output."
echo "Look for which chunk triggers the failure."
