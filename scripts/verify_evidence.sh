#!/bin/bash
# Verify a committed result against the durable result contract.
#
# Checks the evidence rather than the exit status: result.json is the sole
# completion signal, so every field it declares is re-derived from the objects
# actually in the bucket, and the request hash is recomputed from the request
# that was submitted. Any mismatch exits non-zero.
#
# Usage: scripts/verify_evidence.sh [request.json]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

REQUEST="${1:-validation-request.json}"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

vault_get() {
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video --name "$1" --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
}

# The delivery credential is the declared reader for committed deliverables.
AWS_ACCESS_KEY_ID=$(vault_get B2_DELIVERY_KEY_ID)
AWS_SECRET_ACCESS_KEY=$(vault_get B2_DELIVERY_APPLICATION_KEY)
BUCKET=$(vault_get TKR_B2_BUCKET_NAME)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY BUCKET REQUEST
export AWS_DEFAULT_REGION=ca-east-006
export ENDPOINT=https://s3.ca-east-006.backblazeb2.com

# The expected request hash comes from the project's own parser, not by hand.
EXPECTED_HASH=$(
    .venv/bin/python -c "
import json
from tkr_cloud_video.jobs.contracts import parse_generation_request
payload = json.load(open('${REQUEST}'))['input']
print(parse_generation_request(payload).request_hash())
"
)
EXPECTED_MODEL_SET=$(
    python3 -c "
import json
print(json.load(open('dist/minimax-h3-t2v.model-set.json'))['model_set_id'])
"
)
export EXPECTED_HASH EXPECTED_MODEL_SET

python3 - <<'PY'
import hashlib, json, os, re, subprocess, sys

bucket = os.environ["BUCKET"]
endpoint = os.environ["ENDPOINT"]
failures = []
findings: list[str] = []


def aws(*args: str, empty_ok: bool = False) -> str:
    """Run one aws s3api/s3 call against the reviewed endpoint."""
    result = subprocess.run(
        ["aws", "--endpoint-url", endpoint, *args],
        capture_output=True, text=True, check=False,
    )
    # `s3 ls` exits 1 on a prefix that matches nothing, which is an answer
    # rather than a fault; a real failure still writes to stderr.
    if result.returncode != 0 and not (empty_ok and not result.stderr.strip()):
        raise RuntimeError(result.stderr.strip()[:300] or f"exit {result.returncode}")
    return result.stdout


listing = aws("s3", "ls", f"s3://{bucket}/outputs/", "--recursive", empty_ok=True)
keys = [line.split(None, 3)[3] for line in listing.splitlines() if len(line.split(None, 3)) == 4]
results = [k for k in keys if k.endswith("/result.json")]
if not results:
    print("no committed result.json under outputs/ — nothing to verify")
    raise SystemExit(2)

key = sorted(results)[-1]
print("result:", key)
body = aws("s3", "cp", f"s3://{bucket}/{key}", "-")
result = json.loads(body)

REQUIRED = [
    "schema_version", "job_id", "attempt_id", "workflow_digest",
    "model_set_id", "request_hash", "committed_at", "artifacts",
]
for field in REQUIRED:
    if field not in result:
        failures.append(f"result.json missing required field {field}")

if result.get("schema_version") != "1":
    failures.append(f"schema_version is {result.get('schema_version')!r}, expected '1'")

for field in ("workflow_digest", "request_hash"):
    value = result.get(field, "")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value)):
        failures.append(f"{field} is not a sha256 hex digest: {value!r}")

if result.get("request_hash") != os.environ["EXPECTED_HASH"]:
    failures.append(
        f"request_hash {result.get('request_hash')} != locally derived "
        f"{os.environ['EXPECTED_HASH']}"
    )
else:
    print("request_hash matches the submitted request")

if result.get("model_set_id") != os.environ["EXPECTED_MODEL_SET"]:
    failures.append(
        f"model_set_id {result.get('model_set_id')} != pinned "
        f"{os.environ['EXPECTED_MODEL_SET']}"
    )
else:
    print("model_set_id matches the pinned manifest")

artifacts = result.get("artifacts") or []
roles = sorted(a.get("role") for a in artifacts)
if roles != ["generation", "video", "workflow"]:
    failures.append(f"artifact roles are {roles}, expected generation/video/workflow")

# Re-derive every declared digest from the object actually stored.
for artifact in artifacts:
    role, remote = artifact.get("role"), artifact.get("remote_key")
    try:
        head = json.loads(aws(
            "s3api", "head-object", "--bucket", bucket, "--key", remote,
        ))
    except RuntimeError as error:
        failures.append(f"{role}: cannot head {remote}: {error}")
        continue

    if head.get("ContentLength") != artifact.get("size_bytes"):
        failures.append(
            f"{role}: stored size {head.get('ContentLength')} != declared "
            f"{artifact.get('size_bytes')}"
        )
    # The uploader records a provider version when the provider reports one and
    # falls back to a digest-derived value when it does not. The fallback is
    # not the provider's version and cannot retrieve a specific one, so it is
    # reported rather than compared, and anything else must still match.
    version = head.get("VersionId")
    recorded = artifact.get("provider_version_id") or ""
    if recorded == f"sha256-{artifact.get('sha256')}":
        findings.append(
            f"{role}: no provider version recorded; the digest-derived "
            f"fallback stands in for one"
        )
    elif version and recorded != version:
        failures.append(
            f"{role}: provider_version_id {recorded} != stored {version}"
        )

    raw = subprocess.run(
        ["aws", "--endpoint-url", endpoint, "s3", "cp", f"s3://{bucket}/{remote}", "-"],
        capture_output=True, check=False,
    ).stdout
    digest = hashlib.sha256(raw).hexdigest()
    if digest != artifact.get("sha256"):
        failures.append(f"{role}: recomputed sha256 {digest} != declared {artifact.get('sha256')}")
    else:
        print(f"{role}: {artifact.get('size_bytes')} bytes, sha256 verified")

for finding in findings:
    print(" note:", finding)

if failures:
    print("\nEVIDENCE VERIFICATION FAILED")
    for failure in failures:
        print(" -", failure)
    sys.exit(1)
print("\nevidence chain verified end to end")
PY
