#!/bin/bash
# Check a license approval record against the artifacts it claims to cover.
#
# A record that merely parses proves nothing: it asserts a model set, a
# manifest digest and a territory set, and each of those describes something
# that exists elsewhere. Every claim is compared against that thing here, and
# the fields only a reviewer can supply are reported until they are supplied.
#
# Usage: scripts/validate_license_approval.sh [record.json] [manifest.json]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

RECORD="${1:-release-assets/minimax-h3-t2v/license-approval.json}"
MANIFEST="${2:-dist/minimax-h3-t2v.model-set.json}"
export RECORD MANIFEST

.venv/bin/python - <<'PY'
import json
import os
import sys

from tkr_cloud_video.artifacts.models import ModelSetManifest
from tkr_cloud_video.security.release_gate import LicenseApproval
from tkr_cloud_video.security.territories import (
    LICENSE_EXCLUDED_TERRITORIES,
    license_permitted_territories,
)

record = json.load(open(os.environ["RECORD"]))
record.pop("_draft", None)
manifest = ModelSetManifest.model_validate(json.load(open(os.environ["MANIFEST"])))

blocking: list[str] = []
findings: list[str] = []

# Fields a reviewer owns. Their absence is what keeps the record unratified.
unsigned = [name for name in ("reviewer_id", "approved_at", "expires_at")
            if record.get(name) is None]
for name in unsigned:
    blocking.append(f"{name} is unset and only a named reviewer can supply it")

# Claims about artifacts, checked against the artifacts themselves.
if record.get("model_set_id") != manifest.model_set_id:
    blocking.append(
        f"model_set_id {record.get('model_set_id')!r} does not name the manifest's "
        f"{manifest.model_set_id!r}"
    )
if record.get("manifest_digest") != str(manifest.digest()):
    blocking.append(
        f"manifest_digest {record.get('manifest_digest')!r} is not the manifest's "
        f"own digest {manifest.digest()}"
    )

# Claims about territory, checked against the license exclusions.
territories = record.get("territories") or []
if not territories:
    blocking.append("territories is empty; an approval must name what it covers")
excluded = sorted(set(territories) & LICENSE_EXCLUDED_TERRITORIES)
if excluded:
    blocking.append(f"territories name licence-excluded states: {','.join(excluded)}")

permitted = license_permitted_territories()
unreviewed = sorted(set(permitted) - set(territories))
if unreviewed:
    findings.append(
        f"licence-permitted but not covered by this approval: {','.join(unreviewed)}"
    )

if record.get("deployment_use") != "internal":
    findings.append(
        f"deployment_use is {record.get('deployment_use')!r}; public or commercial "
        "service carries obligations beyond this record"
    )

if not blocking:
    approval = LicenseApproval.model_validate(record)
    print(f"ratified: {approval.approval_id}")
    print(f"  reviewer    : {approval.reviewer_id}")
    print(f"  model set   : {approval.model_set_id}")
    print(f"  digest      : {approval.manifest_digest}")
    print(f"  use         : {approval.deployment_use.value}")
    print(f"  territories : {','.join(approval.territories)}")
    print(f"  valid       : {approval.approved_at} to {approval.expires_at}")
else:
    print(f"DRAFT — not an approval ({len(blocking)} blocking)")
    for item in blocking:
        print(" -", item)

for item in findings:
    print(" note:", item)

sys.exit(1 if blocking else 0)
PY
