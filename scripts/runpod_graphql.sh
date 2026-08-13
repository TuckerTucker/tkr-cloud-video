#!/bin/bash
# Report the serverless endpoint's effective placement configuration.
#
# The REST API advertises `dataCenterIds` in its response schema but omits the
# field, so REST cannot confirm the territory restriction is in force. GraphQL
# returns it as `locations`, which makes this the verification path for the
# reviewed Canadian placement.
#
# Usage: scripts/runpod_graphql.sh [endpoint-id]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

ENDPOINT_ID="${1:-176tpna3ogl94t}"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

RUNPOD_API_KEY=$(
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video \
        --name RUNPOD_API_KEY --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
)
export RUNPOD_API_KEY ENDPOINT_ID

python3 - <<'PY'
import json, os, urllib.error, urllib.request

# The key rides in the query string and a browser-shaped agent is sent because
# the GraphQL host sits behind a bot filter that rejects the bare client.
url = "https://api.runpod.io/graphql?api_key=" + os.environ["RUNPOD_API_KEY"]
endpoint = os.environ["ENDPOINT_ID"]

query = """
query { myself { endpoints {
  id name locations templateId gpuIds idleTimeout
  scalerType scalerValue workersMin workersMax
} } }
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

endpoints = result["data"]["myself"]["endpoints"]
current = next((e for e in endpoints if e["id"] == endpoint), None)
if current is None:
    print("endpoint not visible:", [e["id"] for e in endpoints])
    raise SystemExit(1)

print(json.dumps(current, indent=1))

locations = current.get("locations") or ""
outside = [part for part in locations.split(",") if part and not part.startswith("CA-")]
if outside:
    print("WARNING: non-Canadian placement permitted:", outside)
    raise SystemExit(1)
print("placement confined to Canadian data centres:", locations)
PY
