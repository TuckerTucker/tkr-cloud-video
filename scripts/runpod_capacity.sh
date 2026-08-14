#!/bin/bash
# Report which GPU types the account can actually rent, and where.
#
# A serverless endpoint whose GPU/data-centre pair has no stock never places a
# worker: the job simply sits in queue with no worker in any state, which is
# indistinguishable from a slow cold start until you look here.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

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

python3 - <<'PY'
import json, os, urllib.error, urllib.request

url = "https://api.runpod.io/graphql?api_key=" + os.environ["RUNPOD_API_KEY"]

query = """
query { gpuTypes {
  id displayName memoryInGb
  lowestPrice(input: {gpuCount: 1}) { stockStatus minimumBidPrice uninterruptablePrice }
} }
"""
request = urllib.request.Request(
    url,
    data=json.dumps({"query": query}).encode(),
    headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(request) as response:
        result = json.load(response)
except urllib.error.HTTPError as error:
    print("query failed:", error.code, error.read().decode()[:400])
    raise SystemExit(1)

if result.get("errors"):
    print("query failed:", json.dumps(result["errors"])[:400])
    raise SystemExit(1)

for gpu in result["data"]["gpuTypes"]:
    price = gpu.get("lowestPrice") or {}
    stock = price.get("stockStatus")
    if "BLACKWELL" in (gpu["id"] or "").upper() or "B200" in (gpu["id"] or ""):
        print(
            f"{gpu['id']:<28} {str(gpu.get('memoryInGb')):>4}GB "
            f"stock={stock} price={price.get('uninterruptablePrice')}"
        )
PY
