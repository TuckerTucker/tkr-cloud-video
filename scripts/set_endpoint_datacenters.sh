#!/bin/bash
# Set the endpoint's data-centre placement to the licence-approved regions.
#
# Placement is the only thing keeping the model set inside its Applicable
# Territory, so the regions sent here are the registered regions of the
# territories the ratified licence approval covers, and nothing else. The
# approval record decides which territories those are, read at run time, so
# widening the grant is a change to the record rather than a change to a list
# hidden in a script.
#
# Verification cannot use the pattern the sibling scripts use. The REST API
# advertises `dataCenterIds` in its response schema but omits the field, so a
# GET after the PATCH proves nothing about placement; scripts/runpod_graphql.sh
# reads it as `locations` and is the only path that observes it. That script
# answers "is what I can see inside the grant", which an empty placement passes
# trivially, so this script additionally requires the observed regions to equal
# the regions sent. A provider that accepts the write and drops the field is
# then an error here rather than a silent narrowing nobody notices.
#
# Usage: scripts/set_endpoint_datacenters.sh [endpoint-id]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

ENDPOINT_ID="${1:-176tpna3ogl94t}"
# Exported rather than kept local because runpod_graphql.sh reads the same
# variable when it verifies the write, and the two halves of this script must
# judge the placement against one record rather than two.
APPROVAL="${APPROVAL:-release-assets/minimax-h3-t2v/license-approval.json}"
export APPROVAL

# Every registered region of the six territories the licence permits, grouped by
# territory so a reviewer can see the placement decision one country at a time.
# The list is written out rather than derived from the registry because what
# gets sent to a provider should be readable at the call site; the pre-flight
# below is what makes writing it out safe, since it refuses any region whose
# territory the ratified approval does not name.
#
# Four registered regions are absent: CA-MTL-4, EUR-IS-4, EUR-NO-2 and AP-IN-2.
# They are licence-permitted and the provider's own GraphQL `dataCenters` query
# reports all four, but the REST PATCH schema pins dataCenterIds to a narrower
# enum that rejects them with a 400. This is the same REST surface that omits
# dataCenterIds from its responses entirely: it is not authoritative about
# placement in either direction. Add them when the enum catches up; nothing in
# the licence or the registry excludes them.
#
# Every territory the approval covers still has at least one region here, so the
# grant and the placement agree even though the placement is thinner.
DATA_CENTRE_IDS="CA-MTL-1,CA-MTL-2,CA-MTL-3"                   # Canada
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,EUR-IS-1,EUR-IS-2,EUR-IS-3"  # Iceland
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,EUR-NO-1"                    # Norway
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,AP-IN-1"                     # India
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,AP-JP-1"                     # Japan
DATA_CENTRE_IDS="$DATA_CENTRE_IDS,OC-AU-1"                     # Australia
export DATA_CENTRE_IDS

# Territories come from the ratified approval rather than from this file, so the
# record is what decides where the model may run. An unratified or expired
# record grants nothing and the run stops here, before anything is written.
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
export GRANTED_TERRITORIES

# The write is checked before it happens as well as after. Verifying only
# afterwards would leave a placement outside the grant already configured on a
# live endpoint by the time the failure is reported, which is the wrong order
# for a control whose whole purpose is to keep the model set inside its
# territory.
.venv/bin/python -c '
import os, sys

from tkr_cloud_video.security.territories import review_placement

intended = [r for r in os.environ["DATA_CENTRE_IDS"].split(",") if r]
granted = [t for t in os.environ["GRANTED_TERRITORIES"].split(",") if t]
review = review_placement(intended, granted)

print("granted territories:", ",".join(granted))
print("regions to send    :", ",".join(review.permitted) or "none")
if review.excluded:
    print("OUTSIDE THE GRANT  :", ",".join(review.excluded), file=sys.stderr)
if review.unmapped:
    print("UNREGISTERED REGION:", ",".join(review.unmapped), file=sys.stderr)
if not review.permitted:
    print("nothing to send: the approval covers none of these regions", file=sys.stderr)
raise SystemExit(0 if review.approved and review.permitted else 1)
' || {
    echo "refusing to write a placement the approval does not cover" >&2
    exit 1
}

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

# The write goes over GraphQL, not REST. REST is not merely unable to report this
# field, it is unable to set it: a PATCH carrying ten regions returned 200 and
# stored three, silently discarding every non-Canadian one. That is the failure
# ADR-001 predicted, observed rather than inferred, and it is why this script
# refuses to treat a success status as evidence. GraphQL's saveEndpoint is the
# one surface that both reads and writes `locations`.
#
# saveEndpoint takes a whole endpoint rather than a patch, so the current
# configuration is read first and only `locations` is replaced. Sending a partial
# input would silently reset whatever was omitted — the same class of damage this
# script exists to catch.
#
# The provider call runs under the project interpreter rather than the system
# python3 because the python.org framework build resolves its trust store to a
# cert.pem that its installer never created, so every HTTPS call from it dies in
# certificate verification. This is the same reason runpod_graphql.sh uses it.
.venv/bin/python - <<'PY'
import json, os, urllib.error, urllib.request

url = "https://api.runpod.io/graphql?api_key=" + os.environ["RUNPOD_API_KEY"]
endpoint_id = os.environ["ENDPOINT_ID"]
target = ",".join(r for r in os.environ["DATA_CENTRE_IDS"].split(",") if r)

# A browser-shaped agent is sent because the GraphQL host sits behind a bot
# filter that rejects the bare client.
def call(payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
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
        try:
            result = json.loads(error.read().decode())
        except ValueError:
            print("graphql call failed:", error.code)
            raise SystemExit(1) from error
    if result.get("errors"):
        for item in result["errors"]:
            print("graphql error:", str(item.get("message"))[:300])
        raise SystemExit(1)
    return result["data"]

FIELDS = "id name templateId gpuIds locations idleTimeout scalerType scalerValue workersMin workersMax"
endpoints = call({"query": "query { myself { endpoints { %s } } }" % FIELDS})
current = next(
    (e for e in endpoints["myself"]["endpoints"] if e["id"] == endpoint_id), None
)
if current is None:
    print("endpoint not visible:", endpoint_id)
    raise SystemExit(1)

print("locations before:", current.get("locations") or "none")

# Every field is carried forward untouched; only the placement changes. `name`
# is the one field saveEndpoint requires, and dropping any other would reset it.
payload = {key: current[key] for key in current if key != "__typename"}
payload["locations"] = target

mutation = (
    "mutation($input: EndpointInput!) { saveEndpoint(input: $input) { %s } }" % FIELDS
)
saved = call({"query": mutation, "variables": {"input": payload}})["saveEndpoint"]
print("locations after :", saved.get("locations") or "none")

# Report any field the provider altered while handling a placement write. It has
# rewritten gpuTypeIds into architecture classes unprompted before, so a change
# here is worth naming even when the placement itself lands.
for key in ("gpuIds", "workersMin", "workersMax", "idleTimeout", "templateId"):
    if saved.get(key) != current.get(key):
        print(f"PROVIDER ALTERED {key}: {current.get(key)!r} -> {saved.get(key)!r}")
PY

# The GraphQL report is captured rather than streamed so its observed placement
# can be compared with what was sent. Its exit status is kept instead of being
# allowed to end the run, so the report is printed either way and the failure
# below can say which of the two checks refused.
VERIFY_OUT=$(mktemp "${TMPDIR:-/tmp}/tkr-placement.XXXXXX")
trap 'rm -f "$VERIFY_OUT"' EXIT HUP INT TERM
export VERIFY_OUT

# Invoked through `bash` because runpod_graphql.sh is checked in without its
# executable bit, so calling it by path alone would fail on permissions.
VERIFY_STATUS=0
bash scripts/runpod_graphql.sh "$ENDPOINT_ID" >"$VERIFY_OUT" 2>&1 || VERIFY_STATUS=$?
cat "$VERIFY_OUT"

if [ "$VERIFY_STATUS" -ne 0 ]; then
    echo "placement verification refused the observed configuration" >&2
    exit 1
fi

# A grant-shaped check alone cannot catch a dropped field: an endpoint with no
# observed placement has nothing outside the grant in it and passes. The write
# is therefore only accepted when the observed regions are exactly the regions
# sent, so silence and success stop being the same answer.
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
        "the provider accepted the PATCH and stored no placement at all: "
        "the endpoint may now run in any region it likes",
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
