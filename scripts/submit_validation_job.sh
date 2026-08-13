#!/bin/bash
# Submit one text-to-video validation job and follow it to a terminal state.
#
# Uses the async run endpoint because a cold worker hydrates the full 42.5 GB
# model set before it accepts work.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

ENDPOINT_ID="${ENDPOINT_ID:-176tpna3ogl94t}"
REQUEST="${1:?usage: scripts/submit_validation_job.sh <request.json> [job-id]}"
JOB_ID="${2:-}"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

RUNPOD_API_KEY=$(
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video \
        --name RUNPOD_API_KEY --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
)
export RUNPOD_API_KEY ENDPOINT_ID REQUEST JOB_ID

python3 - <<'PY'
import json, os, time, urllib.error, urllib.request

endpoint = os.environ["ENDPOINT_ID"]
base = "https://api.runpod.ai/v2/" + endpoint
headers = {
    "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
    "Content-Type": "application/json",
}


def call(path: str, body: dict | None = None) -> dict:
    """Call one RunPod job route, surfacing HTTP errors as data."""
    request = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return {"httpError": error.code, "body": error.read().decode()[:400]}


job_id = os.environ.get("JOB_ID") or ""
if not job_id:
    payload = json.load(open(os.environ["REQUEST"]))
    submitted = call("/run", payload)
    if "httpError" in submitted or "id" not in submitted:
        print("submit failed:", json.dumps(submitted)[:400])
        raise SystemExit(1)
    job_id = submitted["id"]
    print("job:", job_id, "status:", submitted.get("status"))

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
last = None
for _ in range(720):  # bounded follow; the endpoint caps a run at 45 minutes
    state = call("/status/" + job_id)
    status = state.get("status", state.get("httpError"))
    if status != last:
        print(f"status: {status}", flush=True)
        last = status
    if status in TERMINAL:
        print(json.dumps(state, indent=1)[:4000])
        raise SystemExit(0 if status == "COMPLETED" else 1)
    time.sleep(10)

print("still running after the follow window; job:", job_id)
PY
