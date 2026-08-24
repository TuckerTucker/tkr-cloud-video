"""Control-plane entrypoint for retention: rule provisioning and sweeping.

Composed here rather than in the worker composition root because every
dependency it builds carries a capability a worker must not have. Constructing
the reaper credential in this module and nowhere else keeps that boundary
mechanical: a worker cannot delete because nothing in its process ever builds
a client that can.

Two credentials, not one, because the provider will not issue one that spans
both planes: ``writeBuckets`` is refused on a key restricted to a bucket, so a
key able to rewrite lifecycle rules cannot also be confined to the bucket whose
objects the sweep deletes. Splitting them is the stronger arrangement anyway.
The lifecycle credential holds no file capability, so it cannot read or delete
a single object; the reaper holds no bucket capability, so it cannot alter the
rules that bound it. Each mode requires only its own pair, so an operator
running one mode is never made to hold the other's key.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from tkr_cloud_video.adapters.b2_lifecycle import (
    B2Account,
    B2BucketRuleAdapter,
    UrllibHttpTransport,
)
from tkr_cloud_video.adapters.process import SubprocessCommandExecutor
from tkr_cloud_video.adapters.rclone import (
    RCLONE_EXECUTABLE,
    RcloneB2Client,
    RcloneCredentials,
    RcloneLocation,
    RetentionRcloneStore,
)
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.lifecycle import FileConfigSource, LifecyclePolicyLoader
from tkr_cloud_video.delivery.lifecycle_rules import (
    OUTPUT_PREFIX,
    LifecycleRuleSet,
    RetentionError,
    detect_drift,
)
from tkr_cloud_video.delivery.reconciler import RetentionReconciler

POLICY_RELATIVE_PATH = "config/lifecycle.json"


#: Values each mode requires, by the environment variable that supplies it.
#: ``check`` and ``apply`` reconfigure a bucket and never touch an object;
#: ``sweep`` deletes objects and never reads a rule. Requiring only the mode's
#: own pair is what keeps the split real rather than decorative.
MODE_REQUIREMENTS: Final[dict[str, dict[str, str]]] = {
    "check": {
        "B2_LIFECYCLE_KEY_ID": "lifecycle_key_id",
        "B2_LIFECYCLE_APPLICATION_KEY": "lifecycle_application_key",
        "B2_BUCKET_ID": "bucket_id",
    },
    "apply": {
        "B2_LIFECYCLE_KEY_ID": "lifecycle_key_id",
        "B2_LIFECYCLE_APPLICATION_KEY": "lifecycle_application_key",
        "B2_BUCKET_ID": "bucket_id",
    },
    "sweep": {
        "B2_REAPER_KEY_ID": "reaper_key_id",
        "B2_REAPER_APPLICATION_KEY": "reaper_application_key",
        "B2_BUCKET_NAME": "bucket_name",
    },
}


@dataclass(frozen=True, slots=True)
class RetentionEnvironment:
    """Every value the control plane needs, read once and validated."""

    lifecycle_key_id: str
    lifecycle_application_key: str
    reaper_key_id: str
    reaper_application_key: str
    bucket_id: str
    bucket_name: str
    remote_name: str
    base_prefix: str
    rclone_executable: str

    @classmethod
    def from_environment(
        cls, environ: dict[str, str], mode: str = "sweep"
    ) -> RetentionEnvironment:
        """Read and validate the control-plane environment for one mode.

        Args:
            environ: Process environment.
            mode: The operation about to run. Only that mode's values are
                required; the others are read when present and left empty
                otherwise, so holding one key does not require holding both.

        Returns:
            The validated configuration.

        Raises:
            RetentionError: If a required value is absent. Nothing is
                defaulted: a missing bucket must not resolve to some other
                bucket, and a missing credential must not fall back to an
                ambient one.

        Note:
            The rclone path defaults to the absolute path the worker image
            pins, which is deliberate: resolving a binary from PATH inside a
            worker would be a supply-chain seam, and no worker sets this. The
            sweep is the one caller that runs off-image, on an operator machine
            where that path does not exist, so it is the one caller that needs
            to name its own.

            The base prefix defaults to the namespace the reconciler actually
            sweeps. It cannot default to the bucket root, which
            ``RcloneLocation`` rejects, and it must not name a namespace the
            workers do not write to: the sweep lists ``outputs/`` through this
            prefix, so a base naming anything else lists nothing, deletes
            nothing, and reports a clean run. A retention sweep that enforces
            nothing while reporting success is the exact failure this feature
            exists to prevent, so the default is pinned to the prefix the
            runtime writes rather than to a value an operator must know to
            override.

        """
        required = MODE_REQUIREMENTS.get(mode)
        if required is None:
            raise RetentionError(
                "retention_mode_unknown",
                "the requested retention mode is not one this entrypoint runs",
                context={"field": mode},
            )
        optional = {
            "B2_LIFECYCLE_KEY_ID": "lifecycle_key_id",
            "B2_LIFECYCLE_APPLICATION_KEY": "lifecycle_application_key",
            "B2_REAPER_KEY_ID": "reaper_key_id",
            "B2_REAPER_APPLICATION_KEY": "reaper_application_key",
            "B2_BUCKET_ID": "bucket_id",
            "B2_BUCKET_NAME": "bucket_name",
        }
        values = {attribute: "" for attribute in optional.values()}
        for variable, attribute in optional.items():
            values[attribute] = environ.get(variable, "").strip()
        missing = [variable for variable in required if not values[required[variable]]]
        if missing:
            raise RetentionError(
                "retention_environment_incomplete",
                "the control-plane environment is missing a required value",
                context={"field": ",".join(sorted(missing))},
            )
        return cls(
            remote_name=environ.get("B2_REMOTE_NAME", "tkr-retention").strip(),
            base_prefix=environ.get("B2_BASE_PREFIX", "").strip() or OUTPUT_PREFIX,
            rclone_executable=(
                environ.get("B2_RCLONE_EXECUTABLE", "").strip() or RCLONE_EXECUTABLE
            ),
            **values,
        )


def _dry_run_default(environ: dict[str, str]) -> bool:
    """Return whether the sweep should report rather than delete.

    Deleting is opt-in. Any value other than an explicit false leaves the
    sweep in reporting mode, so a typo cannot turn a report into a deletion.
    """
    return environ.get("RETENTION_DRY_RUN", "true").strip().lower() != "false"


def _report(error: AppError) -> None:
    """Print a typed failure with the fields that name what diverged.

    ``detect_drift`` raises with the offending prefix in ``context`` precisely
    so an operator learns which class stopped being enforced rather than only
    that something did. Printing the code and message alone discarded that,
    leaving a correct refusal that was anonymous — the drift codes are useless
    without the rule they name.
    """
    detail = ", ".join(f"{field}={value}" for field, value in error.context.items())
    suffix = f" ({detail})" if detail else ""
    print(f"retention: {error.code}: {error.safe_message}{suffix}")


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
        configuration = RetentionEnvironment.from_environment(settings, mode)
        policy = LifecyclePolicyLoader().load(
            FileConfigSource(root / POLICY_RELATIVE_PATH, root)
        )
    except AppError as error:
        _report(error)
        return 2
    declared = LifecycleRuleSet().project(policy)

    def _rules() -> B2BucketRuleAdapter:
        """Build the bucket-configuration client, on the lifecycle credential.

        Built inside the mode that uses it so a sweep never constructs a client
        from a credential it was not given, which is what lets the sweep run
        with the reaper key alone.
        """
        return B2BucketRuleAdapter(
            B2Account(
                key_id=configuration.lifecycle_key_id,
                application_key=configuration.lifecycle_application_key,
                bucket_id=configuration.bucket_id,
            ),
            UrllibHttpTransport(),
        )

    async def _run() -> int:
        if mode == "apply":
            await _rules().apply(declared)
            print(
                f"retention: applied {len(declared)} rules from policy {policy.version}"
            )
            return 0
        if mode == "check":
            detect_drift(declared, await _rules().read())
            print(f"retention: rules in force match policy {policy.version}")
            return 0
        store = RetentionRcloneStore(
            RcloneB2Client(
                SubprocessCommandExecutor(),
                RcloneCredentials(
                    configuration.reaper_key_id,
                    configuration.reaper_application_key,
                ),
                RcloneLocation(
                    remote_name=configuration.remote_name,
                    bucket_name=configuration.bucket_name,
                    base_prefix=configuration.base_prefix,
                ),
                executable=configuration.rclone_executable,
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
        _report(error)
        return 1
