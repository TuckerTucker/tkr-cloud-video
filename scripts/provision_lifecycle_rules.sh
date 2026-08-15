#!/usr/bin/env bash
# Apply, verify, or run the declared object retention policy.
#
# Operator-invoked only. Every mode here needs a capability no worker holds:
# check and apply configure the bucket, sweep deletes objects. Nothing in the
# worker image calls this.
#
# Usage:
#   scripts/provision_lifecycle_rules.sh check    # report drift, change nothing
#   scripts/provision_lifecycle_rules.sh apply    # write the declared rules
#   scripts/provision_lifecycle_rules.sh sweep    # expire aged-out objects
#
# Sweeping reports rather than deletes unless RETENTION_DRY_RUN=false is set
# explicitly, so deleting is always a deliberate act.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-check}"

case "$MODE" in
  check|apply|sweep) ;;
  *)
    echo "usage: $(basename "$0") [check|apply|sweep]" >&2
    exit 2
    ;;
esac

for required in B2_REAPER_KEY_ID B2_REAPER_APPLICATION_KEY B2_BUCKET_ID B2_BUCKET_NAME; do
  eval "value=\${$required:-}"
  if [ -z "$value" ]; then
    echo "$required is required" >&2
    exit 2
  fi
done

# Policy lives in typed Python; this script chooses the mode and surfaces the
# exit status, so the rules applied can never diverge from the declared file.
exec "${PYTHON:-python3}" -m tkr_cloud_video.cli lifecycle "$MODE" --root "$ROOT"
