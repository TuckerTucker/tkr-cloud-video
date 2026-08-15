"""Control-plane entrypoint for retention: rule provisioning and sweeping.

Composed here rather than in the worker composition root because every
dependency it builds carries a capability a worker must not have. Constructing
the reaper credential in this module and nowhere else keeps that boundary
mechanical: a worker cannot delete because nothing in its process ever builds
a client that can.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tkr_cloud_video.adapters.b2_lifecycle import (
    B2Account,
    B2BucketRuleAdapter,
    UrllibHttpTransport,
)
from tkr_cloud_video.adapters.process import SubprocessCommandExecutor
from tkr_cloud_video.adapters.rclone import (
    RcloneB2Client,
    RcloneCredentials,
    RcloneLocation,
    RetentionRcloneStore,
)
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.lifecycle import FileConfigSource, LifecyclePolicyLoader
from tkr_cloud_video.delivery.lifecycle_rules import (
    LifecycleRuleSet,
    RetentionError,
    detect_drift,
)
from tkr_cloud_video.delivery.reconciler import RetentionReconciler

POLICY_RELATIVE_PATH = "config/lifecycle.json"


@dataclass(frozen=True, slots=True)
class RetentionEnvironment:
    """Every value the control plane needs, read once and validated."""

    key_id: str
    application_key: str
    bucket_id: str
    bucket_name: str
    remote_name: str
    base_prefix: str

    @classmethod
    def from_environment(cls, environ: dict[str, str]) -> RetentionEnvironment:
        """Read and validate the control-plane environment.

        Args:
            environ: Process environment.

        Returns:
            The validated configuration.

        Raises:
            RetentionError: If a required value is absent. Nothing is
                defaulted: a missing bucket must not resolve to some other
                bucket, and a missing credential must not fall back to an
                ambient one.

        """
        required = {
            "B2_REAPER_KEY_ID": "key_id",
            "B2_REAPER_APPLICATION_KEY": "application_key",
            "B2_BUCKET_ID": "bucket_id",
            "B2_BUCKET_NAME": "bucket_name",
        }
        values: dict[str, str] = {}
        missing = []
        for variable, attribute in required.items():
            value = environ.get(variable, "").strip()
            if not value:
                missing.append(variable)
                continue
            values[attribute] = value
        if missing:
            raise RetentionError(
                "retention_environment_incomplete",
                "the control-plane environment is missing a required value",
                context={"field": ",".join(sorted(missing))},
            )
        return cls(
            remote_name=environ.get("B2_REMOTE_NAME", "tkr-retention").strip(),
            base_prefix=environ.get("B2_BASE_PREFIX", "").strip() or "tkr/",
            **values,
        )


def _dry_run_default(environ: dict[str, str]) -> bool:
    """Return whether the sweep should report rather than delete.

    Deleting is opt-in. Any value other than an explicit false leaves the
    sweep in reporting mode, so a typo cannot turn a report into a deletion.
    """
    return environ.get("RETENTION_DRY_RUN", "true").strip().lower() != "false"


def run_retention(mode: str, root: Path, environ: dict[str, str] | None = None) -> int:
    """Run one retention operation and return a process exit status.

    Args:
        mode: ``check`` to report drift, ``apply`` to write the declared rules,
            or ``sweep`` to expire aged-out objects.
        root: Project root holding the declared policy document.
        environ: Process environment, injected for testability.

    Returns:
        Zero on success, non-zero when drift is found or a sweep could not
        complete cleanly.

    """
    settings = environ if environ is not None else dict(os.environ)
    try:
        configuration = RetentionEnvironment.from_environment(settings)
        policy = LifecyclePolicyLoader().load(
            FileConfigSource(root / POLICY_RELATIVE_PATH, root)
        )
    except AppError as error:
        print(f"retention: {error.code}: {error.safe_message}")
        return 2
    declared = LifecycleRuleSet().project(policy)
    account = B2Account(
        key_id=configuration.key_id,
        application_key=configuration.application_key,
        bucket_id=configuration.bucket_id,
    )
    rules = B2BucketRuleAdapter(account, UrllibHttpTransport())

    async def _run() -> int:
        if mode == "apply":
            await rules.apply(declared)
            print(
                f"retention: applied {len(declared)} rules from policy {policy.version}"
            )
            return 0
        if mode == "check":
            detect_drift(declared, await rules.read())
            print(f"retention: rules in force match policy {policy.version}")
            return 0
        store = RetentionRcloneStore(
            RcloneB2Client(
                SubprocessCommandExecutor(),
                RcloneCredentials(configuration.key_id, configuration.application_key),
                RcloneLocation(
                    remote_name=configuration.remote_name,
                    bucket_name=configuration.bucket_name,
                    base_prefix=configuration.base_prefix,
                ),
            )
        )
        report = await RetentionReconciler(store).run(
            policy,
            datetime.now(UTC),
            dry_run=_dry_run_default(settings),
        )
        verb = "would delete" if report.dry_run else "deleted"
        deleted = sum(report.deleted_by_class.values())
        print(
            f"retention: evaluated {report.evaluated} attempts, "
            f"{verb} {deleted} objects, {report.failures} unresolved"
        )
        for retained_class in report.classes_with_no_objects:
            print(f"retention: no objects matched class {retained_class.value}")
        return 0 if report.clean else 1

    try:
        return asyncio.run(_run())
    except AppError as error:
        print(f"retention: {error.code}: {error.safe_message}")
        return 1
