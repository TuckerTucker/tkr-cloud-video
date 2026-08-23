#!/bin/bash
# Serve the local operator console.
#
# The console holds the RunPod bearer token and the delivery-read credential so
# a browser never has to. This script is the one place that knows where those
# secrets live: it resolves them exactly as scripts/submit_validation_job.sh and
# scripts/fetch_result.sh do, exports them, and hands the process nothing else.
#
# Usage: scripts/console.sh [--port N]
# Env:
#   ENDPOINT_ID       the Serverless endpoint to submit to
#   TKR_PRINCIPAL_ID  must equal the endpoint's own principal, because a job's
#                     identity is derived from it; configured differently, the
#                     console looks for every result under the wrong key
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

vault_get() {
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video --name "$1" --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
}

RUNPOD_API_KEY=$(vault_get RUNPOD_API_KEY)
B2_DELIVERY_KEY_ID=$(vault_get B2_DELIVERY_KEY_ID)
B2_DELIVERY_APPLICATION_KEY=$(vault_get B2_DELIVERY_APPLICATION_KEY)
export RUNPOD_API_KEY B2_DELIVERY_KEY_ID B2_DELIVERY_APPLICATION_KEY

export TKR_CONSOLE_ENDPOINT_ID="${ENDPOINT_ID:-176tpna3ogl94t}"
export TKR_B2_BUCKET_NAME="${TKR_B2_BUCKET_NAME:-$(vault_get TKR_B2_BUCKET_NAME)}"
export TKR_PRINCIPAL_ID="${TKR_PRINCIPAL_ID:-runpod-endpoint}"

exec uv run python -m tkr_cloud_video console "$@"
