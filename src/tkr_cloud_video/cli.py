"""Command-line entrypoints for local diagnostics and worker operations."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Sequence
from typing import Protocol, cast

from tkr_cloud_video.bootstrap import DoctorResult, run_doctor
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.logging import default_event_sink
from tkr_cloud_video.release.serverless import compose_serverless_deployment
from tkr_cloud_video.worker import compose_worker_application


class RunPodServerlessSdk(Protocol):
    """Narrow official SDK surface used by the process entrypoint."""

    def register_fitness_check(self, check: object) -> object:
        """Register one startup readiness check."""
        ...

    def start(self, config: dict[str, object]) -> None:
        """Start the RunPod worker loop."""
        ...


def build_parser() -> argparse.ArgumentParser:
    """Build the side-effect-free command parser."""
    parser = argparse.ArgumentParser(prog="tkr-cloud-video")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser(
        "doctor", help="validate local wiring without provider access"
    )
    doctor_parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON output"
    )
    subparsers.add_parser("worker", help="run the hydrated supervised GPU worker")
    subparsers.add_parser(
        "serverless", help="run the contract-compatible RunPod Serverless worker"
    )
    return parser


def render_doctor(result: DoctorResult, *, as_json: bool) -> str:
    """Render the safe doctor result for a human or machine caller."""
    if as_json:
        return json.dumps(result.model_dump(mode="json"), sort_keys=True)
    status = "OK" if result.outcome == "succeeded" else "FAILED"
    boundaries = ", ".join(
        key for key, present in result.boundary_directories.items() if not present
    )
    suffix = f"; missing: {boundaries}" if boundaries else ""
    return f"tkr-cloud-video doctor: {status}{suffix}"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected command and return a process exit status."""
    arguments = build_parser().parse_args(argv)
    if arguments.command == "doctor":
        result = run_doctor()
        print(render_doctor(result, as_json=bool(arguments.as_json)))
        return 0 if result.outcome == "succeeded" else 1
    if arguments.command == "worker":
        return run_worker()
    if arguments.command == "serverless":
        return run_serverless()
    return 2


def run_worker() -> int:
    """Compose and run the production worker with safe startup failures."""
    sink = default_event_sink()
    try:
        application = compose_worker_application(os.environ, sink)
        asyncio.run(application.run())
    except Exception as error:
        code = error.code if isinstance(error, AppError) else "worker_config_invalid"
        payload = {
            "event": "worker.startup.failed",
            "correlation_id": "worker-startup",
            "outcome": "failed",
            "error_code": code,
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 1
    return 0


def run_serverless() -> int:
    """Register the async deployment with the pinned official RunPod SDK."""
    sink = default_event_sink()
    try:
        deployment = compose_serverless_deployment(os.environ, sink)
        runpod = importlib.import_module("runpod")
        sdk = cast(RunPodServerlessSdk, runpod.serverless)
        sdk.register_fitness_check(deployment.ensure_started)
        sdk.start({"handler": deployment.handle})
    except Exception as error:
        code = (
            error.code if isinstance(error, AppError) else "serverless_config_invalid"
        )
        payload = {
            "event": "serverless.startup.failed",
            "correlation_id": "serverless-startup",
            "outcome": "failed",
            "error_code": code,
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
