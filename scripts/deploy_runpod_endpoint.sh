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

IMAGE_DIGEST="sha256:861243d9aaada84cd0501d362c5505476a8d51178fb7a18b19a50e8180bbcc74"
IMAGE="ghcr.io/tuckertucker/tkr-cloud-video@${IMAGE_DIGEST}"

# Deployed release names the commit whose image is actually running.
RELEASE_ID="tkr-cloud-video-0.1.0-8bf1b75"

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

# Every call that leaves this machine runs under the project interpreter rather
# than the system python3. The python.org framework build resolves its trust
# store to a cert.pem its installer never created, so HTTPS from it dies in
# certificate verification before a request is sent. Local parsing below still
# uses python3, which needs neither TLS nor the project package.
#
# Registry auth is only needed while the image is private.
REGISTRY_AUTH_ID=""
if [ -n "${GHCR_PAT:-}" ]; then
    REGISTRY_AUTH_ID=$(
        .venv/bin/python - <<'PY'
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
    .venv/bin/python - <<'PY'
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
#
# Placement follows the territories the ratified licence approval covers, which
# is what bounds where the model set and its outputs may be used. Compute is not
# storage: the bucket, the worker identity and the reviewed publication path
# stay in Canada regardless of where a worker runs, so a Canadian data centre is
# no longer the thing carrying the licence argument and no longer has to be the
# only option. Widening the grant is a change to the approval record, and the
# gate after this create is what proves the two still agree.
#
# The regions below are every registered region of those territories, grouped by
# territory; security/territories.py is the registry that maps each one, and an
# unregistered region fails the gate rather than passing unnoticed. CA-MTL-4
# joins the three Montreal regions already used because it is the same
# territory, the same residency story and the same review as its neighbours.
DATA_CENTRE_IDS="CA-MTL-1,CA-MTL-2,CA-MTL-3,CA-MTL-4"          # Canada
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,EUR-IS-1,EUR-IS-2,EUR-IS-3,EUR-IS-4"  # Iceland
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,EUR-NO-1,EUR-NO-2"           # Norway
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,AP-IN-1,AP-IN-2"             # India
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,AP-JP-1"                     # Japan
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,OC-AU-1"                     # Australia
export DATA_CENTRE_IDS

ENDPOINT_ID=$(
    TEMPLATE_ID="$TEMPLATE_ID" .venv/bin/python - <<'PY'
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
    "dataCenterIds": [r for r in os.environ["DATA_CENTRE_IDS"].split(",") if r],
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

# A created endpoint is not a deployed one until its placement has been seen.
# ADR-001 records this provider dropping a placement field on create and moving
# worker bounds unprompted, and REST omits dataCenterIds from its responses, so
# the create call's own 200 is not evidence that the restriction is in force.
# scripts/runpod_graphql.sh reads the placement back as `locations` and judges it
# against the territories the ratified approval covers.
#
# Invoked through `bash` because runpod_graphql.sh is checked in without its
# executable bit. Its status is captured rather than allowed to end the run, so
# the report still prints and the message below can name what refused.
VERIFY_OUT=$(mktemp "${TMPDIR:-/tmp}/tkr-placement.XXXXXX")
trap 'rm -f "$VERIFY_OUT"' EXIT HUP INT TERM
export VERIFY_OUT

VERIFY_STATUS=0
bash scripts/runpod_graphql.sh "$ENDPOINT_ID" >"$VERIFY_OUT" 2>&1 || VERIFY_STATUS=$?
cat "$VERIFY_OUT"

if [ "$VERIFY_STATUS" -ne 0 ]; then
    echo "deploy failed: endpoint ${ENDPOINT_ID} is placed outside the approval" >&2
    exit 1
fi

# The grant check above cannot catch a dropped field on its own: an endpoint
# with no observed placement has nothing outside the grant in it and passes.
# The create is therefore only accepted when the observed regions are exactly
# the regions sent. This mirrors the check in set_endpoint_datacenters.sh, which
# is the script that repairs a placement; this one only refuses to bless it.
python3 - <<'PY'
import json, os, sys

missing_key = object()
observed_raw = missing_key

# runpod_graphql.sh prints the endpoint record with json.dumps(indent=1), which
# puts the placement on a line of its own as `"locations": "..."`.
with open(os.environ["VERIFY_OUT"], encoding="utf-8") as handle:
    for line in handle:
        stripped = line.strip().rstrip(",")
        if stripped.startswith('"locations":'):
            observed_raw = json.loads("{" + stripped + "}")["locations"]
            break

if observed_raw is missing_key:
    print("no placement observed in the verification report", file=sys.stderr)
    raise SystemExit(1)

observed = sorted(r.strip() for r in (observed_raw or "").split(",") if r.strip())
intended = sorted(r for r in os.environ["DATA_CENTRE_IDS"].split(",") if r)

if not observed:
    print(
        "the provider created the endpoint and stored no placement at all: "
        "it may run in any region it likes",
        file=sys.stderr,
    )
    raise SystemExit(1)

missing = [r for r in intended if r not in observed]
extra = [r for r in observed if r not in intended]
if missing or extra:
    print("placement does not match what was sent", file=sys.stderr)
    if missing:
        print("  dropped by the provider:", ",".join(missing), file=sys.stderr)
    if extra:
        print("  added by the provider  :", ",".join(extra), file=sys.stderr)
    raise SystemExit(1)

print("placement confirmed:", ",".join(observed))
PY
