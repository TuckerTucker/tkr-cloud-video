#!/bin/bash
# Set the endpoint's acceptable GPU types, ordered by rent preference.
#
# Blackwell stays first because the model set's text encoder is NVFP4 and that
# format is Blackwell-native; Hopper follows so the endpoint can actually place
# a worker while no Canadian Blackwell is in stock.
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

base = "https://rest.runpod.io/v1/endpoints/" + os.environ["ENDPOINT_ID"]
headers = {
    "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
    "Content-Type": "application/json",
}
body = json.dumps({
    # Ordered by rent preference. Every entry holds the 42.5 GB model set with
    # room for activations; 48 GB and smaller cards are left out because the
    # weights alone would leave nothing to generate in.
    "gpuTypeIds": [
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "NVIDIA B200",
        "NVIDIA H200",
        "NVIDIA H100 80GB HBM3",
        "NVIDIA A100 80GB PCIe",
    ],
    # CUDA minor-version compatibility carries the cu128 build on any 12.x
    # host, so the floor only has to exclude CUDA 11. Blackwell hosts report
    # 12.8+ regardless; pinning 12.8 here excluded every Hopper host instead.
    "allowedCudaVersions": [
        "12.4", "12.5", "12.6", "12.7", "12.8", "12.9", "13.0",
    ],
}).encode()
request = urllib.request.Request(base, data=body, headers=headers, method="PATCH")
try:
    with urllib.request.urlopen(request) as response:
        json.load(response)
except urllib.error.HTTPError as error:
    print("PATCH failed:", error.code, error.read().decode()[:400])
    raise SystemExit(1)

with urllib.request.urlopen(urllib.request.Request(base, headers=headers)) as response:
    current = json.load(response)
print("gpuTypeIds:", current.get("gpuTypeIds"))
PY
