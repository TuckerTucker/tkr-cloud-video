#!/bin/bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

run_gate() {
    gate_name="$1"
    shift
    echo "quality_gate=${gate_name} outcome=started"
    "$@"
    echo "quality_gate=${gate_name} outcome=succeeded"
}

if [ "${1:-}" = "--ci" ]; then
    shift
fi

if [ "$#" -ne 0 ]; then
    echo "usage: scripts/check.sh [--ci]" >&2
    exit 2
fi

run_gate lock_check uv lock --check
run_gate format_check uv run ruff format --check .
run_gate lint uv run ruff check .
run_gate type_check uv run mypy
run_gate unit_test uv run pytest --cov=tkr_cloud_video --cov-report=term-missing
run_gate package_smoke uv build

temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/tkr-cloud-video-smoke.XXXXXX")"
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
(
    cd "$temporary_directory"
    uv run --project "$project_root" --frozen python -c \
        'import tkr_cloud_video; print(tkr_cloud_video.__version__)'
)
run_gate doctor uv run python -m tkr_cloud_video doctor --json
