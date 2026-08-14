#!/bin/bash
# Run one validation job on a freshly started worker and verify its evidence.
#
# Waits for existing workers to retire first: a worker already up is running the
# image from before the template changed, so submitting into it would exercise
# the previous release rather than the one under test.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

ENDPOINT_ID="${ENDPOINT_ID:-176tpna3ogl94t}"
REQUEST="${1:-validation-request.json}"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

RUNPOD_API_KEY=$(
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video \
        --name RUNPOD_API_KEY --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
)
export RUNPOD_API_KEY

worker_total() {
    curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" \
        "https://api.runpod.ai/v2/${ENDPOINT_ID}/health" \
        | python3 -c 'import json,sys; print(sum(json.load(sys.stdin)["workers"].values()))'
}

echo "waiting for stale workers to retire"
for _ in $(seq 1 60); do
    total=$(worker_total || echo 1)
    if [ "${total:-1}" -eq 0 ]; then
        echo "no workers remain; the next one starts on the current template"
        break
    fi
    sleep 15
done

echo "submitting validation job"
bash scripts/submit_validation_job.sh "$REQUEST"
submit_status=$?

echo
echo "verifying committed evidence"
bash scripts/verify_evidence.sh "$REQUEST"
verify_status=$?

if [ "$submit_status" -ne 0 ] || [ "$verify_status" -ne 0 ]; then
    exit 1
fi
