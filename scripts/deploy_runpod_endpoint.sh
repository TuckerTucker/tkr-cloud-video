#!/bin/bash
# Create the RunPod serverless endpoint for the validated worker image.
#
# Reads every credential from the project vault at run time; no secret is ever
# written to disk, passed as an argument, or echoed.
#
# The GHCR package is public, so no registry auth is needed. Set GHCR_PAT to a
# token with read:packages to pull a private image instead.
#
# Usage: scripts/deploy_runpod_endpoint.sh
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

IMAGE_DIGEST="sha256:a33a743f9774c8c5689d51e023ad1a43129775bad6ac3efae5773af4ffb5a798"
IMAGE="ghcr.io/tuckertucker/tkr-cloud-video@${IMAGE_DIGEST}"

# Deployed release names the commit whose image is actually running.
RELEASE_ID="tkr-cloud-video-0.1.0-f6d97e4"

VAULT="project:tkr-cloud-video"

# The vault opens from the credential chain. Prefer an already-exported
# password; otherwise fall back to the untracked local operator file.
export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    if [ ! -r .env_temp ]; then
        echo "no vault credential: export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD" >&2
        exit 2
    fi
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

vault_get() {
    tkr op secrets.get_secret --vaultId "$VAULT" --name "$1" --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
}

RUNPOD_API_KEY=$(vault_get RUNPOD_API_KEY)
export RUNPOD_API_KEY

# Registry auth is only needed while the image is private.
REGISTRY_AUTH_ID=""
if [ -n "${GHCR_PAT:-}" ]; then
    REGISTRY_AUTH_ID=$(
        python3 - <<'PY'
import json, os, urllib.request

body = json.dumps({
    "name": "ghcr-tuckertucker-read",
    "username": "TuckerTucker",
    "password": os.environ["GHCR_PAT"],
}).encode()
request = urllib.request.Request(
    "https://rest.runpod.io/v1/containerregistryauth",
    data=body,
    headers={
        "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(request) as response:
    print(json.load(response)["id"])
PY
    )
    echo "registry auth: ${REGISTRY_AUTH_ID}"
fi

# The worker reads each B2 secret directly from its own environment by name.
TEMPLATE_ID=$(
    B2_MODEL_KEY_ID=$(vault_get B2_MODEL_KEY_ID) \
    B2_MODEL_APPLICATION_KEY=$(vault_get B2_MODEL_APPLICATION_KEY) \
    B2_INPUT_KEY_ID=$(vault_get B2_INPUT_KEY_ID) \
    B2_INPUT_APPLICATION_KEY=$(vault_get B2_INPUT_APPLICATION_KEY) \
    B2_OUTPUT_KEY_ID=$(vault_get B2_OUTPUT_KEY_ID) \
    B2_OUTPUT_APPLICATION_KEY=$(vault_get B2_OUTPUT_APPLICATION_KEY) \
    TKR_B2_BUCKET_NAME=$(vault_get TKR_B2_BUCKET_NAME) \
    TKR_WORKER_ID=$(vault_get TKR_WORKER_ID) \
    TKR_MODEL_SET_ID=$(vault_get TKR_MODEL_SET_ID) \
    TKR_MANIFEST_DIGEST=$(vault_get TKR_MANIFEST_DIGEST) \
    RELEASE_ID="$RELEASE_ID" \
    IMAGE="$IMAGE" \
    REGISTRY_AUTH_ID="$REGISTRY_AUTH_ID" \
    python3 - <<'PY'
import json, os, urllib.request

environment = {
    "TKR_RELEASE_ID": os.environ["RELEASE_ID"],
    "TKR_WORKER_ID": os.environ["TKR_WORKER_ID"],
    "TKR_MODEL_SET_ID": os.environ["TKR_MODEL_SET_ID"],
    "TKR_MANIFEST_DIGEST": os.environ["TKR_MANIFEST_DIGEST"],
    "TKR_B2_BUCKET_NAME": os.environ["TKR_B2_BUCKET_NAME"],
    "B2_MODEL_KEY_ID": os.environ["B2_MODEL_KEY_ID"],
    "B2_MODEL_APPLICATION_KEY": os.environ["B2_MODEL_APPLICATION_KEY"],
    "B2_INPUT_KEY_ID": os.environ["B2_INPUT_KEY_ID"],
    "B2_INPUT_APPLICATION_KEY": os.environ["B2_INPUT_APPLICATION_KEY"],
    "B2_OUTPUT_KEY_ID": os.environ["B2_OUTPUT_KEY_ID"],
    "B2_OUTPUT_APPLICATION_KEY": os.environ["B2_OUTPUT_APPLICATION_KEY"],
}
template = {
    "name": "tkr-cloud-video-worker",
    "imageName": os.environ["IMAGE"],
    "isServerless": True,
    # 42.5 GB of model artifacts, the 10 GB configured disk-safety margin,
    # and room for one workspace and its outputs.
    "containerDiskInGb": 120,
    "env": environment,
}
if os.environ.get("REGISTRY_AUTH_ID"):
    template["containerRegistryAuthId"] = os.environ["REGISTRY_AUTH_ID"]
body = json.dumps(template).encode()
request = urllib.request.Request(
    "https://rest.runpod.io/v1/templates",
    data=body,
    headers={
        "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(request) as response:
    print(json.load(response)["id"])
PY
)
echo "template: ${TEMPLATE_ID}"

# Blackwell only: the text encoder is NVFP4 and the image pins the cu128 build.
# Canadian data centres only: the bucket, the worker identity, and the reviewed
# publication path all place this workload in Canada.
ENDPOINT_ID=$(
    TEMPLATE_ID="$TEMPLATE_ID" python3 - <<'PY'
import json, os, urllib.request

body = json.dumps({
    "name": "tkr-cloud-video",
    "templateId": os.environ["TEMPLATE_ID"],
    "computeType": "GPU",
    "gpuCount": 1,
    "gpuTypeIds": [
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "NVIDIA B200",
    ],
    "allowedCudaVersions": ["12.8", "12.9", "13.0"],
    "dataCenterIds": ["CA-MTL-1", "CA-MTL-2", "CA-MTL-3"],
    "workersMin": 0,
    "workersMax": 1,
    # Cold start re-hydrates 42.5 GB, so hold a finished worker briefly to make
    # a payload-level retry cheap. Nothing is charged once it scales down.
    "idleTimeout": 300,
    # Generation alone is bounded at 1800s by the worker; leave headroom.
    "executionTimeoutMs": 2_700_000,
    "flashboot": False,
}).encode()
request = urllib.request.Request(
    "https://rest.runpod.io/v1/endpoints",
    data=body,
    headers={
        "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(request) as response:
    print(json.load(response)["id"])
PY
)
echo "endpoint: ${ENDPOINT_ID}"
