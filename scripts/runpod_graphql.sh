#!/bin/bash
# Report the serverless endpoint's effective placement configuration.
#
# The REST API advertises `dataCenterIds` in its response schema but omits the
# field, so REST cannot confirm the territory restriction is in force. GraphQL
# returns it as `locations`, which makes this the verification path for the
# reviewed Canadian placement.
#
# Placement is judged against the territories a license approval covers, passed
# in explicitly so the legal decision is visible at the call site rather than
# assumed by this script.
#
# Usage: scripts/runpod_graphql.sh [endpoint-id] [granted-territories]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

ENDPOINT_ID="${1:-176tpna3ogl94t}"
APPROVAL="${APPROVAL:-release-assets/minimax-h3-t2v/license-approval.json}"

# Territories come from the ratified approval rather than the command line, so
# the record is what decides where the model may run. An unratified record
# grants nothing: the run stops instead of falling back to a permissive guess.
if [ -n "${2:-}" ]; then
    GRANTED_TERRITORIES="$2"
else
    GRANTED_TERRITORIES=$(
        APPROVAL="$APPROVAL" .venv/bin/python -c '
import json, os, sys

from tkr_cloud_video.security.release_gate import LicenseApproval

try:
    record = json.load(open(os.environ["APPROVAL"]))
    approval = LicenseApproval.model_validate(record)
except (OSError, ValueError) as error:
    print(f"no ratified approval to read territories from: {error}"[:200], file=sys.stderr)
    raise SystemExit(1)
print(",".join(approval.territories))
'
    ) || exit 1
fi

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

RUNPOD_API_KEY=$(
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video \
        --name RUNPOD_API_KEY --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
)
export RUNPOD_API_KEY ENDPOINT_ID GRANTED_TERRITORIES

report_placement() {
    OBSERVED_LOCATIONS="$1" .venv/bin/python -c '
import os

from tkr_cloud_video.security.territories import (
    license_permitted_territories,
    review_placement,
)

observed = [p for p in os.environ["OBSERVED_LOCATIONS"].split(",") if p]
granted = [t for t in os.environ["GRANTED_TERRITORIES"].split(",") if t]
review = review_placement(observed, granted)

print("granted territories:", ",".join(granted))
print("permitted regions  :", ",".join(review.permitted) or "none")
if review.excluded:
    print("OUTSIDE THE GRANT  :", ",".join(review.excluded))
if review.unmapped:
    print("UNREGISTERED REGION:", ",".join(review.unmapped))
print(
    "license permits (needs approval before use):",
    ",".join(license_permitted_territories()),
)
raise SystemExit(0 if review.approved else 1)
'
}

LOCATIONS_OUT=$(mktemp "${TMPDIR:-/tmp}/tkr-locations.XXXXXX")
trap 'rm -f "$LOCATIONS_OUT"' EXIT HUP INT TERM
export LOCATIONS_OUT

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
with open(os.environ["LOCATIONS_OUT"], "w", encoding="utf-8") as handle:
    handle.write((current.get("locations") or "").strip())
PY

report_placement "$(cat "$LOCATIONS_OUT")"
