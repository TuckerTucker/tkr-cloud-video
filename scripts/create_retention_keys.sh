#!/usr/bin/env bash
# Create the two control-plane retention keys and store them in the project vault.
#
# Two keys, because the provider will not issue one that spans both planes:
# writeBuckets is refused on a key restricted to a bucket, so the key that
# rewrites lifecycle rules cannot be confined to the bucket whose objects the
# reaper deletes. See docs/runbooks/retention.md.
#
#   retention-reaper  bucket-restricted, sweeps objects, cannot configure a bucket
#   retention-rules   account-wide, configures buckets, holds no file capability
#
# Requires the master application key, which is read from the terminal and never
# written to disk, echoed, or passed as an argument. The account cache is a
# temporary file removed on exit, so no master credential outlives this script.
#
# Usage:
#   scripts/create_retention_keys.sh                      # prompts on a terminal
#   scripts/create_retention_keys.sh --credentials-file <path>
#                                                         # two lines: keyID, then key
#   scripts/create_retention_keys.sh --rotate             # replace stored keys
#
# The credentials file is deleted when the script exits, however it exits.
set -euo pipefail

VAULT="project:tkr-cloud-video"
ROTATE=""
CREDENTIALS_FILE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --rotate) ROTATE="--rotate"; shift ;;
    --credentials-file) CREDENTIALS_FILE="${2:-}"; shift 2 ;;
    *) echo "usage: $(basename "$0") [--rotate] [--credentials-file <path>]" >&2; exit 2 ;;
  esac
done

command -v b2  >/dev/null || { echo "b2 CLI not found: brew install b2-tools" >&2; exit 2; }
command -v tkr >/dev/null || { echo "tkr not found" >&2; exit 2; }

vault_get() {
  tkr op secrets.get_secret --vaultId "$VAULT" --name "$1" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["output"]["value"])' 2>/dev/null \
    || true
}

vault_set() {
  # NOTE (accepted risk): tkr op has no stdin mode for input, so the value is
  # briefly visible to a same-user `ps`. Local, single-user, same exposure the
  # kit's own keychain write documents.
  tkr op secrets.set_secret --vaultId "$VAULT" --name "$1" --value "$2" --json >/dev/null 2>&1
}

# The bucket restriction comes from the vault rather than from a literal, so the
# reaper cannot be scoped to a bucket the sweep does not address.
BUCKET="$(vault_get TKR_B2_BUCKET_NAME)"
if [ -z "$BUCKET" ]; then
  echo "TKR_B2_BUCKET_NAME is not in $VAULT — set it before creating keys" >&2
  exit 2
fi

if [ "$ROTATE" != "--rotate" ] && [ -n "$(vault_get B2_REAPER_KEY_ID)" ]; then
  echo "retention keys are already stored in $VAULT" >&2
  echo "re-run with --rotate to create replacements (the existing B2 keys are not deleted)" >&2
  exit 2
fi

# Master credential, in order of preference: a file named on the command line,
# the environment, or an interactive prompt. The prompt reads /dev/tty rather
# than stdin because a wrapper that redirects stdin would otherwise make `read`
# hit EOF and this script exit without printing anything — a silent no-op is the
# worst outcome for a provisioning step, so every path below either obtains a
# credential or says why it could not.
prompt_secret() {
  local prompt="$1" target="$2" value=""
  if [ -e /dev/tty ] && (exec 3< /dev/tty) 2>/dev/null; then
    printf '%s' "$prompt" > /dev/tty
    IFS= read -rs value < /dev/tty || value=""
    printf '\n' > /dev/tty
  fi
  printf -v "$target" '%s' "$value"
}

if [ -n "$CREDENTIALS_FILE" ]; then
  [ -r "$CREDENTIALS_FILE" ] || { echo "cannot read $CREDENTIALS_FILE" >&2; exit 2; }
  B2_APPLICATION_KEY_ID="$(sed -n '1p' "$CREDENTIALS_FILE" | tr -d '[:space:]')"
  B2_APPLICATION_KEY="$(sed -n '2p' "$CREDENTIALS_FILE" | tr -d '[:space:]')"
fi

[ -n "${B2_APPLICATION_KEY_ID:-}" ] || prompt_secret "Backblaze master keyID: " B2_APPLICATION_KEY_ID
[ -n "${B2_APPLICATION_KEY:-}" ]    || prompt_secret "Backblaze master applicationKey: " B2_APPLICATION_KEY

if [ -z "${B2_APPLICATION_KEY_ID:-}" ] || [ -z "${B2_APPLICATION_KEY:-}" ]; then
  cat >&2 <<'MSG'
no master credential available, and no terminal to prompt from.

This step needs the Backblaze master application key (it is the only key with
writeKeys). Choose one:

  1. Run this script from a real terminal, where it will prompt:
         scripts/create_retention_keys.sh

  2. Write the two values to a file and name it, so neither reaches your shell
     history. The script deletes the file when it exits:
         printf '%s\n%s\n' '<keyID>' '<applicationKey>' > /tmp/b2-master
         chmod 600 /tmp/b2-master
         scripts/create_retention_keys.sh --credentials-file /tmp/b2-master

  3. Supply them in the environment (they will be in your shell history):
         B2_APPLICATION_KEY_ID=... B2_APPLICATION_KEY=... scripts/create_retention_keys.sh
MSG
  exit 2
fi
export B2_APPLICATION_KEY_ID B2_APPLICATION_KEY

# Point the account cache at a file this script owns and removes.
B2_ACCOUNT_INFO="$(mktemp "${TMPDIR:-/tmp}/b2-retention-XXXXXX")"
export B2_ACCOUNT_INFO
cleanup() {
  rm -f "$B2_ACCOUNT_INFO"
  [ -n "$CREDENTIALS_FILE" ] && rm -f "$CREDENTIALS_FILE"
  unset B2_APPLICATION_KEY B2_APPLICATION_KEY_ID
  return 0
}
trap cleanup EXIT HUP INT TERM

# Each create prints "<keyId> <applicationKey>" once and never again.
store_key() {
  local output id secret
  output="$(b2 key create "$@" | tail -n 1)"
  id="$(printf '%s' "$output" | awk '{print $1}')"
  secret="$(printf '%s' "$output" | awk '{print $2}')"
  if [ -z "$id" ] || [ -z "$secret" ]; then
    echo "b2 key create returned an unexpected shape for $2" >&2
    exit 1
  fi
  printf '%s\t%s' "$id" "$secret"
}

echo "creating retention-reaper (restricted to $BUCKET)" >&2
REAPER="$(store_key --bucket "$BUCKET" retention-reaper listBuckets,listFiles,readFiles,deleteFiles)"
vault_set B2_REAPER_KEY_ID          "${REAPER%%	*}"
vault_set B2_REAPER_APPLICATION_KEY "${REAPER##*	}"
unset REAPER

echo "creating retention-rules (account-wide, no file capability)" >&2
RULES="$(store_key retention-rules listBuckets,writeBuckets)"
vault_set B2_LIFECYCLE_KEY_ID          "${RULES%%	*}"
vault_set B2_LIFECYCLE_APPLICATION_KEY "${RULES##*	}"
unset RULES

echo >&2
echo "stored in $VAULT:" >&2
for name in B2_REAPER_KEY_ID B2_REAPER_APPLICATION_KEY B2_LIFECYCLE_KEY_ID B2_LIFECYCLE_APPLICATION_KEY; do
  if [ -n "$(vault_get "$name")" ]; then echo "  $name" >&2; else echo "  $name  MISSING" >&2; fi
done
echo >&2
echo "next: scripts/provision_lifecycle_rules.sh check" >&2
