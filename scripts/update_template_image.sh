#!/bin/bash
# Point the serverless template at a new worker image.
#
# Reads the template first and merges, because a patch that omitted `env` would
# drop the injected credentials. Reads it back afterwards rather than trusting
# the write: this provider has already been observed accepting a field it does
# not persist.
#
# Usage: scripts/update_template_image.sh <image-digest> <release-id> [template-id]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

IMAGE_DIGEST="${1:?usage: scripts/update_template_image.sh <image-digest> <release-id> [template-id]}"
RELEASE_ID="${2:?release id required}"
TEMPLATE_ID="${3:-j2hflqaoiq}"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

RUNPOD_API_KEY=$(
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video \
        --name RUNPOD_API_KEY --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
)
export RUNPOD_API_KEY TEMPLATE_ID IMAGE_DIGEST RELEASE_ID

python3 - <<'PY'
import json, os, urllib.error, urllib.request

base = "https://rest.runpod.io/v1/templates/" + os.environ["TEMPLATE_ID"]
headers = {
    "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
    "Content-Type": "application/json",
}
image = "ghcr.io/tuckertucker/tkr-cloud-video@" + os.environ["IMAGE_DIGEST"]


def read() -> dict:
    """Return the template as the provider currently holds it."""
    with urllib.request.urlopen(urllib.request.Request(base, headers=headers)) as reply:
        return json.load(reply)


current = read()
environment = dict(current.get("env") or {})
environment["TKR_RELEASE_ID"] = os.environ["RELEASE_ID"]

body = json.dumps({"imageName": image, "env": environment}).encode()
try:
    request = urllib.request.Request(base, data=body, headers=headers, method="PATCH")
    with urllib.request.urlopen(request) as reply:
        json.load(reply)
except urllib.error.HTTPError as error:
    print("PATCH failed:", error.code, error.read().decode()[:400])
    raise SystemExit(1)

after = read()
after_env = after.get("env") or {}
print("imageName     :", after.get("imageName"))
print("TKR_RELEASE_ID:", after_env.get("TKR_RELEASE_ID"))
print("env keys      :", len(after_env))

secrets = (
    "B2_MODEL_KEY_ID", "B2_MODEL_APPLICATION_KEY",
    "B2_INPUT_KEY_ID", "B2_INPUT_APPLICATION_KEY",
    "B2_OUTPUT_KEY_ID", "B2_OUTPUT_APPLICATION_KEY",
)
missing = [name for name in secrets if not after_env.get(name)]
if after.get("imageName") != image:
    print("image was not persisted")
    raise SystemExit(1)
if missing:
    print("credentials lost by the patch:", missing)
    raise SystemExit(1)
print("template updated with every credential intact")
PY
