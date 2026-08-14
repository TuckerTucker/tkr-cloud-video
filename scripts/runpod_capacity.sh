#!/bin/bash
# Report which GPU types the account can actually rent, and where.
#
# A serverless endpoint whose GPU/data-centre pair has no stock never places a
# worker: the job simply sits in queue with no worker in any state, which is
# indistinguishable from a slow cold start until you look here.
#
# The account-wide view answers "does this GPU exist anywhere", which is the
# wrong question once placement is pinned to a territory: a card can be plentiful
# in Texas and absent from every region the license permits. The per-data-centre
# section below is therefore the one that decides a placement expansion.
#
# Usage: scripts/runpod_capacity.sh [data-centre ...]
# With no arguments it surveys every registered region whose territory the
# license permits, so the surveyed set tracks the registry instead of a list
# that has to be remembered separately.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

export TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD
if [ -z "${TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD:-}" ]; then
    TKR_VAULT_PROJECT_TKR_CLOUD_VIDEO_PASSWORD=$(awk -F= '/^vault_key=/{print $2}' .env_temp)
fi

RUNPOD_API_KEY=$(
    tkr op secrets.get_secret --vaultId project:tkr-cloud-video \
        --name RUNPOD_API_KEY --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])'
)
export RUNPOD_API_KEY

# Regions to survey arrive through the environment rather than as script
# arguments because the Python body is a quoted heredoc, which is what keeps the
# key out of the process argument list.
REQUESTED_DATA_CENTRES="$*"
export REQUESTED_DATA_CENTRES

# The provider call runs under the project interpreter rather than the system
# python3 because the python.org framework build resolves its trust store to a
# cert.pem that its installer never created, so every HTTPS call from it dies in
# certificate verification. The venv interpreter uses the macOS store, and the
# territory registry below has to be importable from the interpreter anyway.
.venv/bin/python - <<'PY'
import json, os, urllib.error, urllib.request

from tkr_cloud_video.security.territories import (
    DATA_CENTRE_TERRITORIES,
    license_permitted_territories,
    territory_for,
)

url = "https://api.runpod.io/graphql?api_key=" + os.environ["RUNPOD_API_KEY"]

# Every entry holds the 42.5 GB model set with room for activations, so this is
# the set worth asking about; a region offering only smaller cards is no more
# useful than a region offering nothing. Kept in the same rent-preference order
# as set_endpoint_gpus.sh so the two lists can be read against each other.
GPU_TYPES = [
    "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    "NVIDIA B200",
    "NVIDIA H200",
    "NVIDIA H100 80GB HBM3",
    "NVIDIA A100 80GB PCIe",
]
SHORT = {
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": "RTXPRO6000",
    "NVIDIA B200": "B200",
    "NVIDIA H200": "H200",
    "NVIDIA H100 80GB HBM3": "H100SXM",
    "NVIDIA A100 80GB PCIe": "A100PCIe",
}


def graphql(query):
    """Send one query, returning the data block or exiting on any failure."""
    # The key rides in the query string and a browser-shaped agent is sent
    # because the GraphQL host sits behind a bot filter that rejects the bare
    # client.
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
    return result["data"]


# --- account-wide, unchanged: does this class of card exist anywhere at all ---
print("=== account-wide Blackwell stock (no data-centre dimension) ===")
account_wide = graphql("""
query { gpuTypes {
  id displayName memoryInGb
  lowestPrice(input: {gpuCount: 1}) { stockStatus minimumBidPrice uninterruptablePrice }
} }
""")
for gpu in account_wide["gpuTypes"]:
    price = gpu.get("lowestPrice") or {}
    stock = price.get("stockStatus")
    if "BLACKWELL" in (gpu["id"] or "").upper() or "B200" in (gpu["id"] or ""):
        print(
            f"{gpu['id']:<28} {str(gpu.get('memoryInGb')):>4}GB "
            f"stock={stock} price={price.get('uninterruptablePrice')}"
        )

# --- the provider's own region catalogue ---
# Asked for first so a region identifier that the provider does not recognise is
# named as unknown. Without this check a typo and a genuinely empty region both
# come back as "nothing in stock", which is the one confusion this whole report
# exists to prevent.
catalogue = graphql("""
query { dataCenters {
  id name location listed
  gpuAvailability { gpuTypeId displayName available stockStatus }
} }
""")
regions = {d["id"]: d for d in catalogue["dataCenters"]}

requested = os.environ.get("REQUESTED_DATA_CENTRES", "").split()
if requested:
    survey = requested
else:
    permitted = frozenset(license_permitted_territories())
    survey = sorted(
        region
        for region, territory in DATA_CENTRE_TERRITORIES.items()
        if territory in permitted
    )

print()
print("=== stock per (data centre, GPU type) ===")
print("legend: availability from DataCenter.gpuAvailability, price from")
print("        GpuType.lowestPrice(input: {dataCenterId: ...}); '-' means the")
print("        region does not list the card at all, 'none' means listed but")
print("        no free capacity right now.")
print()
header = "".join(f"{SHORT[g]:>12}" for g in GPU_TYPES)
print(f"{'data centre':<12}{'terr':<6}{'listed':<8}{header}")

# Prices are fetched per region rather than per (region, card) because the
# gpuTypes field returns every card for one dataCenterId in a single response,
# which keeps this to one request per region.
findings = []
for region in survey:
    territory = territory_for(region) or "?"
    known = regions.get(region)
    if known is None:
        print(f"{region:<12}{territory:<6}{'UNKNOWN — provider does not list this region':<8}")
        continue

    availability = {
        g["gpuTypeId"]: g for g in (known.get("gpuAvailability") or [])
    }
    priced = graphql(
        'query { gpuTypes { id lowestPrice(input: {gpuCount: 1, dataCenterId: "%s"})'
        " { stockStatus minimumBidPrice uninterruptablePrice } } }" % region
    )
    prices = {g["id"]: (g.get("lowestPrice") or {}) for g in priced["gpuTypes"]}

    cells = []
    for gpu in GPU_TYPES:
        listed_here = availability.get(gpu)
        price = prices.get(gpu, {})
        if listed_here is None:
            cells.append("-")
            continue
        if listed_here.get("available"):
            cell = listed_here.get("stockStatus") or "yes"
            findings.append((region, territory, gpu, cell, price))
        else:
            cell = "none"
        cells.append(cell)
    row = "".join(f"{c:>12}" for c in cells)
    print(f"{region:<12}{territory:<6}{str(known.get('listed')):<8}{row}")

print()
print("=== rentable now, with price ===")
if not findings:
    print("nothing: no permitted region currently has any of these cards free")
for region, territory, gpu, stock, price in findings:
    # The two signals are sampled seconds apart against a live market, so a card
    # reported available with no price is a real state worth showing rather than
    # smoothing over: it means stock moved between the two calls.
    on_demand = price.get("uninterruptablePrice")
    spot = price.get("minimumBidPrice")
    note = "" if on_demand is not None else "   (price signal disagreed; stock is moving)"
    print(
        f"{region:<12}{territory:<4}{gpu:<46}stock={stock:<8}"
        f"on-demand={on_demand} spot={spot}{note}"
    )

# --- registry drift ---
# The registry is the thing that decides whether a region is inside the license
# grant, so a region the provider offers in a permitted territory but that the
# registry has never heard of is a placement option being silently left on the
# table. It is reported here rather than in the table above because it is a gap
# in our own data, not in the provider's.
print()
print("=== regions the provider offers that the registry does not map ===")
unmapped = sorted(set(regions) - set(DATA_CENTRE_TERRITORIES))
if not unmapped:
    print("none: every region the provider lists is mapped to a territory")
for region in unmapped:
    print(f"{region:<12}{regions[region].get('location')}")
PY
