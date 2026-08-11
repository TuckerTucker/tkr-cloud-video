#!/bin/bash
set -euo pipefail

if [ "$#" -eq 0 ]; then
    set -- serverless
fi

exec python -m tkr_cloud_video "$@"
