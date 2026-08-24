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
# Each mode's values are read from the project vault when they are not already
# exported, so a credential lives in one place rather than in an operator's
# shell history. check and apply use the lifecycle credential, sweep uses the
# reaper credential, and neither mode requires the other's key. An explicit
# export still wins, which is what makes a one-off run against a different
# bucket possible without editing the vault. Because that override is also how
# the wrong bucket gets addressed, the resolved source of every value is
# reported before any mode runs.
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

VAULT="project:tkr-cloud-video"

# Reads one secret by name. The name reaches argv; the value never does, so it
# is not visible to a same-user `ps` the way a `--value` would be. A failure to
# open the vault is not distinguished from an absent name here: both mean the
# value is unresolved, and the caller reports it by variable.
vault_get() {
  tkr op secrets.get_secret --vaultId "$VAULT" --name "$1" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])' 2>/dev/null \
    || true
}

# Each required environment variable and the vault name that supplies it. The
# bucket is TKR_B2_BUCKET_NAME in the vault because that is what the worker
# deployment and the evidence verifier already call it — one bucket must not
# acquire a second name that can disagree with the first.
resolve() {
  local variable="$1" secret="$2" current source value
  eval "current=\${$variable:-}"
  if [ -n "$current" ]; then
    source="environment"
  else
    value="$(vault_get "$secret")"
    if [ -n "$value" ]; then
      eval "$variable=\$value"
      source="vault ($secret)"
    else
      source=""
    fi
  fi
  RESOLVED_SOURCE="$source"
}

# Only the running mode's values are required. check and apply reconfigure a
# bucket on the lifecycle credential and never touch an object; sweep deletes
# objects on the reaper credential and never reads a rule. The provider will
# not issue one key for both — writeBuckets is refused on a bucket-restricted
# key — and requiring only the mode's own pair is what keeps that split real
# rather than a naming convention.
case "$MODE" in
  check|apply)
    REQUIRED=( \
      "B2_LIFECYCLE_KEY_ID:B2_LIFECYCLE_KEY_ID" \
      "B2_LIFECYCLE_APPLICATION_KEY:B2_LIFECYCLE_APPLICATION_KEY" \
      "B2_BUCKET_ID:TKR_B2_BUCKET_ID" \
    )
    SUBJECT="B2_BUCKET_ID"
    ;;
  sweep)
    REQUIRED=( \
      "B2_REAPER_KEY_ID:B2_REAPER_KEY_ID" \
      "B2_REAPER_APPLICATION_KEY:B2_REAPER_APPLICATION_KEY" \
      "B2_BUCKET_NAME:TKR_B2_BUCKET_NAME" \
    )
    SUBJECT="B2_BUCKET_NAME"
    ;;
esac

MISSING=""
REPORT=""
for pair in "${REQUIRED[@]}"
do
  variable="${pair%%:*}"
  secret="${pair##*:}"
  resolve "$variable" "$secret"
  if [ -z "$RESOLVED_SOURCE" ]; then
    MISSING="${MISSING:+$MISSING,}$variable"
  else
    REPORT="${REPORT}retention: $variable from $RESOLVED_SOURCE
"
  fi
done

# Every missing name at once, rather than the first one found: an operator
# fixing these one exit at a time learns the requirement one round-trip at a
# time. This mirrors retention_environment_incomplete in the typed layer.
if [ -n "$MISSING" ]; then
  echo "retention: required values unresolved from environment or vault: $MISSING" >&2
  echo "retention: set them with: tkr op secrets.set_secret --vaultId $VAULT --name <name> --value <value>" >&2
  exit 2
fi

# The sweep shells out to rclone. The typed default is the absolute path the
# worker image pins, which does not exist on an operator machine, so resolve the
# operator's own and report which binary will run. An explicit setting wins, as
# everywhere else here.
if [ "$MODE" = "sweep" ] && [ -z "${B2_RCLONE_EXECUTABLE:-}" ]; then
  if resolved="$(command -v rclone 2>/dev/null)" && [ -n "$resolved" ]; then
    B2_RCLONE_EXECUTABLE="$resolved"
    export B2_RCLONE_EXECUTABLE
    REPORT="${REPORT}retention: rclone at $resolved
"
  else
    echo "retention: rclone not found on PATH; install it or set B2_RCLONE_EXECUTABLE" >&2
    exit 2
  fi
fi

# Only the bucket is named. The credential's source is reported, never its value.
printf '%s' "$REPORT" >&2
eval "subject=\${$SUBJECT}"
echo "retention: addressing bucket $subject ($MODE)" >&2

for pair in "${REQUIRED[@]}"; do export "${pair%%:*}"; done

# Policy lives in typed Python; this script chooses the mode and surfaces the
# exit status, so the rules applied can never diverge from the declared file.
exec "${PYTHON:-python3}" -m tkr_cloud_video.cli lifecycle "$MODE" --root "$ROOT"
