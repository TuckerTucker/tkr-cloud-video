#!/bin/bash
# Fetch one committed artifact out of the delivery bucket, digest-checked.
#
# result.json is the sole completion signal, so the artifact is located through
# it rather than by constructing a key, and the bytes are checked against the
# digest that record declares. A mismatch leaves nothing behind: the download
# lands in a temporary file and is moved into place only once it verifies, so a
# corrupt copy never sits next to a good one under a plausible name.
#
# Usage: scripts/fetch_result.sh [destination]
#   destination  a directory, in which case a name is derived from the job and
#                the role, or a file path used verbatim; defaults to "."
# Env:
#   JOB_ID  fetch this job's result rather than the most recently committed one
#   ROLE    which artifact to fetch: video (default), workflow, generation
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

DEST="${1:-.}"
JOB_ID="${JOB_ID:-}"
ROLE="${ROLE:-video}"

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
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY BUCKET DEST JOB_ID ROLE
export AWS_DEFAULT_REGION=ca-east-006
export ENDPOINT=https://s3.ca-east-006.backblazeb2.com

python3 - <<'PY'
import hashlib, json, os, subprocess, tempfile

bucket = os.environ["BUCKET"]
endpoint = os.environ["ENDPOINT"]
role = os.environ["ROLE"]
wanted_job = os.environ["JOB_ID"]


def aws(*args: str, empty_ok: bool = False) -> str:
    """Run one aws s3api/s3 call against the reviewed endpoint."""
    result = subprocess.run(
        ["aws", "--endpoint-url", endpoint, *args],
        capture_output=True, text=True, check=False,
    )
    # `s3 ls` exits 1 on a prefix that matches nothing, which is an answer
    # rather than a fault; a real failure still writes to stderr.
    if result.returncode != 0 and not (empty_ok and not result.stderr.strip()):
        print("aws call failed:", result.stderr.strip()[:300] or f"exit {result.returncode}")
        raise SystemExit(1)
    return result.stdout


listing = aws("s3", "ls", f"s3://{bucket}/outputs/", "--recursive", empty_ok=True)
# Rows are "<date> <time> <size> <key>"; the timestamp is what orders them,
# because a lexical sort of keys orders by job id, which is a digest.
rows = []
for line in listing.splitlines():
    parts = line.split(None, 3)
    if len(parts) == 4 and parts[3].endswith("/result.json"):
        rows.append((parts[0] + " " + parts[1], parts[3]))

if wanted_job:
    # Accept the id with or without the prefix the key carries.
    prefix = wanted_job if wanted_job.startswith("job-") else "job-" + wanted_job
    rows = [row for row in rows if row[1].startswith(f"outputs/{prefix}/")]
    if not rows:
        print(f"no committed result for {prefix} under outputs/")
        raise SystemExit(2)
elif not rows:
    print("no committed result.json under outputs/ — nothing to fetch")
    raise SystemExit(2)

committed_at, key = max(rows)
result = json.loads(aws("s3", "cp", f"s3://{bucket}/{key}", "-"))
print("result:", key, f"(committed {committed_at})")

artifacts = result.get("artifacts") or []
artifact = next((a for a in artifacts if a.get("role") == role), None)
if artifact is None:
    available = ", ".join(sorted(str(a.get("role")) for a in artifacts)) or "none"
    print(f"no artifact with role {role!r} in this result; available: {available}")
    raise SystemExit(1)

remote = artifact["remote_key"]
declared = artifact.get("sha256")

# Download beside the destination so the move into place cannot cross a
# filesystem and become a copy that can be interrupted half-written.
destination = os.path.abspath(os.environ["DEST"])
staging_dir = destination if os.path.isdir(destination) else os.path.dirname(destination)
handle, staged = tempfile.mkstemp(prefix=".fetch-result-", dir=staging_dir or ".")
os.close(handle)
try:
    aws("s3", "cp", f"s3://{bucket}/{remote}", staged)

    digest = hashlib.sha256()
    size = 0
    with open(staged, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    if digest.hexdigest() != declared:
        print("\nDIGEST MISMATCH — nothing written")
        print(f" - recomputed {digest.hexdigest()}")
        print(f" - declared   {declared}")
        raise SystemExit(1)
    if size != artifact.get("size_bytes"):
        print("\nSIZE MISMATCH — nothing written")
        print(f" - fetched  {size}")
        print(f" - declared {artifact.get('size_bytes')}")
        raise SystemExit(1)

    if os.path.isdir(destination):
        # Every artifact is stored as .bin because the pipeline names by role,
        # not by container. The extension is read off the bytes rather than
        # assumed, so a role that changes container still lands named honestly.
        with open(staged, "rb") as stream:
            head = stream.read(16)
        if head[4:8] == b"ftyp":
            suffix = ".mp4"
        elif head[:1] in (b"{", b"["):
            suffix = ".json"
        else:
            suffix = ".bin"
        job = result.get("job_id", "result")
        destination = os.path.join(destination, f"{job}-{role}{suffix}")

    os.replace(staged, destination)
    staged = ""
finally:
    if staged and os.path.exists(staged):
        os.remove(staged)

print(f"{role}: {size} bytes, sha256 verified")
print(destination)
PY
