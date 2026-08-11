"""Command-line entrypoints for local diagnostics and worker operations."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from structlog.typing import FilteringBoundLogger

from tkr_cloud_video.adapters.process import SubprocessCommandExecutor
from tkr_cloud_video.adapters.rclone import (
    RCLONE_EXECUTABLE,
    ArtifactRcloneStore,
    RcloneB2Client,
    RcloneCredentials,
    RcloneLocation,
)
from tkr_cloud_video.artifacts.streaming_publisher import StreamingArtifactPublisher
from tkr_cloud_video.bootstrap import DoctorResult, run_doctor
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.core.logging import configure_logging, default_event_sink
from tkr_cloud_video.core.storage import B2_S3_ENDPOINT, B2_S3_REGION
from tkr_cloud_video.release.catalog import prepare_model_set
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
    release_parser = subparsers.add_parser(
        "release", help="prepare or publish an immutable model-set release"
    )
    release_commands = release_parser.add_subparsers(
        dest="release_command", required=True
    )
    for name, help_text in (
        ("prepare", "validate and render the immutable model-set manifest"),
        ("publish", "stream model artifacts and publish the manifest last"),
    ):
        command = release_commands.add_parser(name, help=help_text)
        command.add_argument("--catalog", required=True, type=Path)
        command.add_argument("--license-approval-id", required=True)
        command.add_argument("--output", type=Path)
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
    if arguments.command == "release":
        if arguments.release_command == "prepare":
            return run_release_prepare(
                arguments.catalog,
                arguments.license_approval_id,
                arguments.output,
            )
        if arguments.release_command == "publish":
            return run_release_publish(
                arguments.catalog,
                arguments.license_approval_id,
                arguments.output,
            )
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


def run_release_prepare(
    catalog_path: Path, license_approval_id: str, output_path: Path | None
) -> int:
    """Validate a reviewed catalog and render its deterministic manifest."""
    logger = configure_logging(default_event_sink())
    correlation_id = "release-prepare"
    try:
        prepared = prepare_model_set(catalog_path, license_approval_id)
        if output_path is not None:
            _write_release_file(output_path, prepared.manifest.canonical_bytes())
        logger.info(
            "release.catalog.prepared",
            correlation_id=correlation_id,
            resource_id=prepared.catalog.model_set_id,
            outcome="succeeded",
            safe_context={
                "artifact_count": len(prepared.manifest.artifacts),
                "total_bytes": sum(
                    artifact.size_bytes for artifact in prepared.manifest.artifacts
                ),
            },
        )
        print(_release_summary(prepared.manifest))
    except Exception as error:
        _log_release_failure(logger, correlation_id, error)
        return 1
    return 0


def run_release_publish(
    catalog_path: Path, license_approval_id: str, output_path: Path | None
) -> int:
    """Stream a reviewed catalog to B2 using an offline publisher credential."""
    logger = configure_logging(default_event_sink())
    correlation_id = "release-publish"
    try:
        prepared = prepare_model_set(catalog_path, license_approval_id)
        bucket_name = _required_environment("TKR_B2_BUCKET_NAME")
        credentials = RcloneCredentials(
            _required_environment("B2_MODEL_PUBLISHER_KEY_ID"),
            _required_environment("B2_MODEL_PUBLISHER_APPLICATION_KEY"),
        )
        store = ArtifactRcloneStore(
            RcloneB2Client(
                SubprocessCommandExecutor(),
                credentials,
                RcloneLocation(
                    "tkr-publisher",
                    bucket_name,
                    "models/",
                    os.environ.get("TKR_B2_S3_ENDPOINT", B2_S3_ENDPOINT),
                    os.environ.get("TKR_B2_S3_REGION", B2_S3_REGION),
                ),
                executable=RCLONE_EXECUTABLE,
            )
        )
        receipt = asyncio.run(
            StreamingArtifactPublisher(store).publish(
                prepared.manifest, prepared.sources
            )
        )
        if output_path is not None:
            _write_release_file(output_path, prepared.manifest.canonical_bytes())
        logger.info(
            "release.catalog.published",
            correlation_id=correlation_id,
            resource_id=prepared.catalog.model_set_id,
            outcome="succeeded",
            safe_context={
                "reused_blobs": receipt.reused_blobs,
                "uploaded_blobs": receipt.uploaded_blobs,
            },
        )
        print(
            json.dumps(
                {
                    **json.loads(_release_summary(prepared.manifest)),
                    "manifest_key": receipt.manifest_key,
                    "manifest_version_id": receipt.manifest_version_id,
                    "reused_blobs": receipt.reused_blobs,
                    "uploaded_blobs": receipt.uploaded_blobs,
                },
                sort_keys=True,
            )
        )
    except Exception as error:
        _log_release_failure(logger, correlation_id, error)
        return 1
    return 0


def _release_summary(manifest: object) -> str:
    from tkr_cloud_video.artifacts.models import ModelSetManifest

    if not isinstance(manifest, ModelSetManifest):
        raise TypeError("release summary requires a model-set manifest")
    return json.dumps(
        {
            "artifact_count": len(manifest.artifacts),
            "manifest_digest": str(manifest.digest()),
            "model_set_id": manifest.model_set_id,
            "total_bytes": sum(artifact.size_bytes for artifact in manifest.artifacts),
        },
        sort_keys=True,
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise AppError(
            "release_environment_missing",
            "Release publication environment is incomplete.",
            context={"field": name},
        )
    return value


def _write_release_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _log_release_failure(
    logger: FilteringBoundLogger, correlation_id: str, error: Exception
) -> None:
    code = error.code if isinstance(error, AppError) else "release_catalog_invalid"
    logger.error(
        "release.catalog.failed",
        correlation_id=correlation_id,
        outcome="failed",
        error_code=code,
    )


if __name__ == "__main__":
    sys.exit(main())
