#!/bin/bash
# Take HEAD to a verified validation run in one invocation.
#
# Pushes the branch, waits for the image, points the template at it, waits for
# stale workers to retire, submits, follows to a terminal state, and verifies
# the committed evidence. Every step that can silently do the wrong thing is
# read back rather than trusted.
#
# Usage: scripts/iterate.sh [request.json]
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

REQUEST="${1:-validation-request.json}"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
SHA=$(git rev-parse --short HEAD)

echo "=== pushing ${BRANCH} at ${SHA} ==="
git push origin "$BRANCH"

echo "=== waiting for the image ==="
# The run is matched by commit so a stale run is never mistaken for this one.
RUN=""
for _ in $(seq 1 30); do
    RUN=$(gh run list --branch "$BRANCH" --limit 5 \
        --json databaseId,headSha --jq \
        "[.[] | select(.headSha | startswith(\"${SHA}\"))][0].databaseId" 2>/dev/null || true)
    [ -n "$RUN" ] && [ "$RUN" != "null" ] && break
    sleep 10
done
if [ -z "$RUN" ] || [ "$RUN" = "null" ]; then
    echo "no workflow run found for ${SHA}" >&2
    exit 1
fi
echo "run ${RUN}"
gh run watch "$RUN" --exit-status --interval 30 >/dev/null

JOB=$(gh run view "$RUN" --json jobs --jq '.jobs[0].databaseId')
DIGEST=$(gh run view --job="$JOB" --log 2>/dev/null \
    | grep -oE 'latest@sha256:[a-f0-9]{64}' | tail -1 | cut -d@ -f2)
if [ -z "$DIGEST" ]; then
    echo "no image digest in the build log" >&2
    exit 1
fi
echo "image ${DIGEST}"

echo "=== rolling the template ==="
bash scripts/update_template_image.sh "$DIGEST" "tkr-cloud-video-0.1.0-${SHA}"

echo "=== confirming placement ==="
bash scripts/runpod_graphql.sh >/dev/null && echo "placement within the granted territory"

echo "=== running validation ==="
bash scripts/run_validation.sh "$REQUEST"
