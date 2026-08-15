#!/bin/bash
# Fetch a committed attempt out of the delivery bucket, digest-checked.
#
# result.json is the sole completion signal, so each artifact is located through
# it rather than by constructing a key, and the bytes are checked against the
# digest that record declares. A mismatch leaves nothing behind: the download
# lands in a temporary file and is moved into place only once it verifies, so a
# corrupt copy never sits next to a good one under a plausible name.
#
# By default the whole attempt is fetched as one directory rather than as loose
# files. An artifact only means something alongside the workflow that produced
# it and the record that vouches for it, and three files spilled into a shared
# downloads folder lose that association the moment anything else lands there.
#
# Usage: scripts/fetch_result.sh [destination]
#   destination  a directory to place the attempt directory in; defaults to "."
#                With ROLE set, a directory in which a name is derived from the
#                job and the role, or a file path used verbatim.
# Env:
#   JOB_ID  fetch this job's result rather than the most recently committed one
#   ROLE    fetch only this artifact - video, workflow or generation - as a
#           single file instead of the whole attempt
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

DEST="${1:-.}"
JOB_ID="${JOB_ID:-}"
ROLE="${ROLE:-}"

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
import hashlib, json, os, shutil, subprocess, tempfile

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
if role:
    selected = [a for a in artifacts if a.get("role") == role]
    if not selected:
        available = ", ".join(sorted(str(a.get("role")) for a in artifacts)) or "none"
        print(f"no artifact with role {role!r} in this result; available: {available}")
        raise SystemExit(1)
else:
    selected = artifacts
    if not selected:
        print("this result declares no artifacts")
        raise SystemExit(1)


def suffix_for(path: str) -> str:
    """Return the extension the bytes themselves imply.

    Every artifact is stored as .bin because the pipeline names by role, not by
    container. The extension is read off the bytes rather than assumed, so a
    role that changes container still lands named honestly.
    """
    with open(path, "rb") as stream:
        head = stream.read(16)
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:1] in (b"{", b"["):
        return ".json"
    return ".bin"


def fetch_verified(artifact: dict, staging_dir: str) -> tuple[str, int]:
    """Download one artifact and check it against what result.json declares.

    Args:
        artifact: One entry from the result's artifact list.
        staging_dir: Directory the temporary file is created in, so the later
            move into place stays on one filesystem and cannot become a copy
            that is interrupted half-written.

    Returns:
        The staged path and its byte count. The caller owns the staged file.

    Raises:
        SystemExit: The bytes do not match the declared digest or size. The
            staged file is removed first, so a failed fetch leaves nothing.

    """
    handle, staged = tempfile.mkstemp(prefix=".fetch-result-", dir=staging_dir)
    os.close(handle)
    try:
        aws("s3", "cp", f"s3://{bucket}/{artifact['remote_key']}", staged)

        digest = hashlib.sha256()
        size = 0
        with open(staged, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != artifact.get("sha256"):
            print(f"\nDIGEST MISMATCH on {artifact.get('role')} — nothing written")
            print(f" - recomputed {digest.hexdigest()}")
            print(f" - declared   {artifact.get('sha256')}")
            raise SystemExit(1)
        if size != artifact.get("size_bytes"):
            print(f"\nSIZE MISMATCH on {artifact.get('role')} — nothing written")
            print(f" - fetched  {size}")
            print(f" - declared {artifact.get('size_bytes')}")
            raise SystemExit(1)
    except BaseException:
        os.remove(staged)
        raise
    return staged, size


destination = os.path.abspath(os.environ["DEST"])
job = result.get("job_id", "result")

if role:
    # Download beside the destination so the move into place cannot cross a
    # filesystem and become a copy that can be interrupted half-written.
    staging_dir = (
        destination if os.path.isdir(destination) else os.path.dirname(destination)
    )
    staged, size = fetch_verified(selected[0], staging_dir or ".")
    if os.path.isdir(destination):
        destination = os.path.join(destination, f"{job}-{role}{suffix_for(staged)}")
    os.replace(staged, destination)
    print(f"{role}: {size} bytes, sha256 verified")
    print(destination)
    raise SystemExit(0)

if os.path.exists(destination) and not os.path.isdir(destination):
    print(f"destination {destination} is a file; an attempt needs a directory")
    raise SystemExit(1)
os.makedirs(destination, exist_ok=True)

# The attempt is assembled complete in a staging directory and swapped into
# place in one move, so a partial package never appears under the name a
# complete one would have.
package = os.path.join(destination, job)
staging = tempfile.mkdtemp(prefix=".fetch-result-", dir=destination)
previous = ""
try:
    for artifact in selected:
        staged, size = fetch_verified(artifact, staging)
        name = f"{artifact.get('role')}{suffix_for(staged)}"
        os.replace(staged, os.path.join(staging, name))
        print(f"  {str(artifact.get('role')):<10} {size:>12,} bytes  sha256 verified")

    # The record that vouches for the artifacts travels with them, so the
    # package can be re-verified later without reaching for the bucket.
    with open(os.path.join(staging, "result.json"), "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)

    # Job and attempt are content-addressed, so re-fetching yields identical
    # bytes and replacing an existing package is safe. The old one is moved
    # aside rather than deleted first, so a failure here cannot leave the
    # destination holding neither copy.
    if os.path.exists(package):
        previous = tempfile.mkdtemp(prefix=".fetch-result-old-", dir=destination)
        os.replace(package, os.path.join(previous, "package"))
    os.replace(staging, package)
    staging = ""
finally:
    if staging and os.path.isdir(staging):
        shutil.rmtree(staging)
    if previous and os.path.isdir(previous):
        shutil.rmtree(previous)

print(f"\n{package}")
PY
