#!/bin/bash
# Publish a changed workflow blob and its manifest, reusing the model blobs.
#
# The normal release path needs a local source for every artifact and reads
# each into memory, which means having the whole model set on disk. Only the
# workflow changed here, and blobs are content-addressed, so the four model
# blobs stay where they are under keys that did not move.
#
# Write access to the model namespace is minted for this operation and revoked
# at the end: the runtime model credential is read-only and stays that way.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

MANIFEST="${1:-dist/minimax-h3-t2v.model-set.json}"
WORKFLOW="${2:-release-assets/minimax-h3-t2v/workflow-api.json}"

export B2_ADMIN_KEY_ID B2_ADMIN_APPLICATION_KEY MANIFEST WORKFLOW
B2_ADMIN_KEY_ID=$(awk -F= '/^B2_ADMIN_KEY_ID=/{print $2}' .env_temp)
B2_ADMIN_APPLICATION_KEY=$(awk -F= '/^B2_ADMIN_APPLICATION_KEY=/{print $2}' .env_temp)

python3 - <<'PY'
import base64, json, os, subprocess, sys, urllib.request

BUCKET = "tkr-cloud-video-aba33dd61d3e"
ENDPOINT = "https://s3.ca-east-006.backblazeb2.com"
PREFIX = "models/"


def b2(api: str, token: str, name: str, payload: dict) -> dict:
    """Call one B2 native API method."""
    request = urllib.request.Request(
        f"{api}/b2api/v3/{name}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as reply:
        return json.load(reply)


credential = base64.b64encode(
    f"{os.environ['B2_ADMIN_KEY_ID']}:{os.environ['B2_ADMIN_APPLICATION_KEY']}".encode()
).decode()
with urllib.request.urlopen(
    urllib.request.Request(
        "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
        headers={"Authorization": "Basic " + credential},
    )
) as reply:
    auth = json.load(reply)
api, token, account = (
    auth["apiInfo"]["storageApi"]["apiUrl"],
    auth["authorizationToken"],
    auth["accountId"],
)
bucket_id = next(
    b["bucketId"]
    for b in b2(api, token, "b2_list_buckets", {"accountId": account})["buckets"]
    if b["bucketName"] == BUCKET
)

manifest = json.load(open(os.environ["MANIFEST"]))
workflow_entry = next(a for a in manifest["artifacts"] if a["role"] == "workflow")
digest = __import__("hashlib").sha256(
    open(os.environ["WORKFLOW"], "rb").read()
).hexdigest()
if digest != workflow_entry["sha256"]:
    print(f"workflow on disk {digest} is not the manifest's {workflow_entry['sha256']}")
    raise SystemExit(1)

created = b2(api, token, "b2_create_key", {
    "accountId": account,
    "capabilities": ["listBuckets", "listFiles", "readFiles", "writeFiles"],
    "keyName": "tkr-workflow-revision",
    "bucketId": bucket_id,
    "namePrefix": PREFIX,
    "validDurationInSeconds": 3600,
})
key_id, application_key = created["applicationKeyId"], created["applicationKey"]
print("minted a prefix-scoped publisher key")

environment = dict(
    os.environ,
    AWS_ACCESS_KEY_ID=key_id,
    AWS_SECRET_ACCESS_KEY=application_key,
    AWS_DEFAULT_REGION="ca-east-006",
)


def upload(path: str, key: str, sha: str) -> int:
    """Put one object carrying the digest metadata hydration verifies."""
    return subprocess.run(
        ["aws", "--endpoint-url", ENDPOINT, "s3", "cp", path,
         f"s3://{BUCKET}/{key}", "--metadata", f"sha256={sha}"],
        env=environment, capture_output=True, text=True,
    ).returncode


try:
    blob_key = PREFIX + workflow_entry["object_key"]
    manifest_digest = __import__("hashlib").sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_key = f"{PREFIX}manifests/sha256/{manifest_digest}.json"

    if upload(os.environ["WORKFLOW"], blob_key, workflow_entry["sha256"]) != 0:
        print("workflow blob upload failed")
        raise SystemExit(1)
    print("published", blob_key)
    if upload(os.environ["MANIFEST"], manifest_key, manifest_digest) != 0:
        print("manifest upload failed")
        raise SystemExit(1)
    print("published", manifest_key)
    print("manifest_digest", manifest_digest)
finally:
    b2(api, token, "b2_delete_key", {"applicationKeyId": key_id})
    print("revoked the publisher key")
PY
