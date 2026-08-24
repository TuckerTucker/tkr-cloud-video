"""Concrete adapter coverage for retention: listing, deleting, bucket rules.

The reconciler's logic is proven elsewhere against fakes. What is proven here
is the seam that made that logic inert until now: that a real listing is
parsed into the shape the reconciler consumes, that a delete is issued, and
that bucket rules round-trip through the provider contract without a field
being silently invented or dropped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tkr_cloud_video.adapters.b2_lifecycle import (
    B2Account,
    B2BucketRuleAdapter,
    BucketConfigurationError,
)
from tkr_cloud_video.adapters.process import CommandResult
from tkr_cloud_video.adapters.rclone import (
    RcloneB2Client,
    RcloneCredentials,
    RcloneLocation,
    RetentionRcloneStore,
)
from tkr_cloud_video.cli import build_parser
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.lifecycle_rules import (
    OUTPUT_PREFIX,
    BucketLifecycleRule,
    RetentionError,
)
from tkr_cloud_video.operations.retention_command import (
    RetentionEnvironment,
    _dry_run_default,
    run_retention,
)

LOCATION = RcloneLocation(
    remote_name="tkr-retention", bucket_name="ca-east-006", base_prefix="tkr/"
)
CREDENTIALS = RcloneCredentials("key-id", "application-key")


class RecordingExecutor:
    """Command executor returning canned output and recording invocations."""

    def __init__(self, stdout: bytes = b"", error: Exception | None = None) -> None:
        """Initialize with the output or failure the provider will produce."""
        self.stdout = stdout
        self.error = error
        self.commands: list[tuple[str, ...]] = []

    async def run(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float = 300,
    ) -> CommandResult:
        """Record the invocation and return the canned result."""
        self.commands.append(arguments)
        if self.error is not None:
            raise self.error
        return CommandResult(self.stdout, b"")


def client(executor: RecordingExecutor) -> RcloneB2Client:
    """Build a client over a recording executor."""
    return RcloneB2Client(executor, CREDENTIALS, LOCATION, executable="/usr/bin/rclone")


@pytest.mark.asyncio
async def test_listing_is_parsed_into_keys_and_times() -> None:
    """A provider listing becomes the shape the reconciler consumes."""
    listing = json.dumps(
        [
            {"Path": "job-1/attempt-1/video.bin", "ModTime": "2026-01-01T00:00:00Z"},
            {
                "Path": "job-1/attempt-1/result.json",
                "ModTime": "2026-01-02T03:04:05.123456789Z",
            },
        ]
    ).encode()
    executor = RecordingExecutor(listing)
    objects = await RetentionRcloneStore(client(executor)).list_objects("outputs/")
    assert [item.key for item in objects] == [
        "outputs/job-1/attempt-1/video.bin",
        "outputs/job-1/attempt-1/result.json",
    ]
    assert objects[0].uploaded_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert objects[1].uploaded_at.microsecond == 123456
    assert "--files-only" in executor.commands[0]
    assert "--recursive" in executor.commands[0]


@pytest.mark.asyncio
async def test_empty_listing_is_no_objects_not_an_error() -> None:
    """An empty namespace lists as nothing rather than failing the sweep."""
    store = RetentionRcloneStore(client(RecordingExecutor(b"")))
    assert await store.list_objects("outputs/") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/outputs/", "outputs", "out//puts/", "../"])
async def test_listing_rejects_a_prefix_that_is_not_a_namespace(prefix: str) -> None:
    """A prefix is validated before it reaches the provider."""
    with pytest.raises(AppError) as caught:
        await client(RecordingExecutor()).list_objects(prefix)
    assert caught.value.code == "invalid_boundary_value"


@pytest.mark.asyncio
async def test_delete_issues_deletefile_for_the_exact_key() -> None:
    """Deleting names one object and no namespace."""
    executor = RecordingExecutor()
    await RetentionRcloneStore(client(executor)).delete(
        "outputs/job-1/attempt-1/video.bin"
    )
    assert executor.commands[0][1] == "deletefile"
    assert executor.commands[0][2].endswith("outputs/job-1/attempt-1/video.bin")


@pytest.mark.asyncio
async def test_deleting_an_absent_object_is_not_a_failure() -> None:
    """An object already gone is the state the caller wanted."""
    absent = AppError("adapter_command_failed", "boom", cause=RuntimeError("not found"))
    store = RetentionRcloneStore(client(RecordingExecutor(error=absent)))
    await store.delete("outputs/job-1/attempt-1/video.bin")


@pytest.mark.asyncio
async def test_a_real_delete_failure_still_propagates() -> None:
    """Only a not-found is swallowed; a refusal must reach the report."""
    refused = AppError(
        "adapter_command_failed", "boom", cause=RuntimeError("access denied")
    )
    store = RetentionRcloneStore(client(RecordingExecutor(error=refused)))
    with pytest.raises(AppError):
        await store.delete("outputs/job-1/attempt-1/video.bin")


class FakeTransport:
    """Native B2 API transport returning scripted responses."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        """Initialize with one response per endpoint suffix."""
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the scripted response for the endpoint."""
        self.calls.append((method, url, payload))
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unscripted endpoint {url}")


AUTHORIZED = {
    "authorizationToken": "token",
    "accountId": "account-1",
    "apiInfo": {"storageApi": {"apiUrl": "https://api001.backblazeb2.com"}},
}


def adapter(responses: dict[str, dict[str, Any]]) -> B2BucketRuleAdapter:
    """Build the bucket rule adapter over a scripted transport."""
    account = B2Account("key-id", "application-key", "bucket-1")
    return B2BucketRuleAdapter(account, FakeTransport(responses))


@pytest.mark.asyncio
async def test_rules_round_trip_through_the_provider_contract() -> None:
    """Applying then reading yields the rules that were declared."""
    declared = (
        BucketLifecycleRule("inputs/", 7, 1),
        BucketLifecycleRule(
            "",
            days_from_hiding_to_deleting=30,
            days_from_starting_to_canceling_unfinished_large_files=2,
        ),
    )
    transport = FakeTransport(
        {
            "b2_authorize_account": AUTHORIZED,
            "b2_update_bucket": {},
        }
    )
    await B2BucketRuleAdapter(
        B2Account("key-id", "application-key", "bucket-1"), transport
    ).apply(declared)
    sent = transport.calls[-1][2]
    assert sent is not None
    wire = sent["lifecycleRules"]
    assert wire[0]["fileNamePrefix"] == "inputs/"
    assert wire[0]["daysFromUploadingToHiding"] == 7
    assert wire[1]["daysFromStartingToCancelingUnfinishedLargeFiles"] == 2

    reading = adapter(
        {
            "b2_authorize_account": AUTHORIZED,
            "b2_list_buckets": {"buckets": [{"lifecycleRules": wire}]},
        }
    )
    assert await reading.read() == declared


@pytest.mark.asyncio
async def test_authorization_response_missing_documented_fields_is_refused() -> None:
    """A shape we do not recognize must not be guessed at.

    The next call writes bucket configuration, so an inferred API URL is not
    an acceptable recovery.
    """
    with pytest.raises(BucketConfigurationError) as caught:
        await adapter({"b2_authorize_account": {"authorizationToken": "t"}}).read()
    assert caught.value.code == "bucket_api_shape_unexpected"


@pytest.mark.asyncio
async def test_unrecognized_rule_field_is_refused() -> None:
    """A rule carrying configuration we cannot reason about fails the check.

    Silently dropping it would let a drift check report agreement it has not
    established.
    """
    reading = adapter(
        {
            "b2_authorize_account": AUTHORIZED,
            "b2_list_buckets": {
                "buckets": [
                    {"lifecycleRules": [{"fileNamePrefix": "x/", "someNewField": 1}]}
                ]
            },
        }
    )
    with pytest.raises(BucketConfigurationError) as caught:
        await reading.read()
    assert caught.value.code == "bucket_rule_field_unknown"
    assert caught.value.context["rule"] == "someNewField"


def test_environment_names_every_missing_value() -> None:
    """Nothing is defaulted: a missing bucket must not resolve elsewhere."""
    with pytest.raises(RetentionError) as caught:
        RetentionEnvironment.from_environment({"B2_BUCKET_ID": "bucket-1"})
    missing = caught.value.context["field"]
    assert "B2_REAPER_KEY_ID" in str(missing)
    assert "B2_BUCKET_NAME" in str(missing)


def test_environment_accepts_a_complete_configuration() -> None:
    """A complete environment yields the validated configuration."""
    configuration = RetentionEnvironment.from_environment(
        {
            "B2_REAPER_KEY_ID": "key-id",
            "B2_REAPER_APPLICATION_KEY": "application-key",
            "B2_BUCKET_ID": "bucket-1",
            "B2_BUCKET_NAME": "ca-east-006",
        }
    )
    assert configuration.bucket_name == "ca-east-006"
    assert configuration.base_prefix.endswith("/")


def test_the_rclone_path_defaults_to_the_pinned_image_binary() -> None:
    """Resolving from PATH inside a worker would be a supply-chain seam.

    No worker sets this, so the default must stay the image's absolute path.
    The sweep is the one caller that runs off-image and needs its own.
    """
    from tkr_cloud_video.adapters.rclone import RCLONE_EXECUTABLE

    base = {
        "B2_REAPER_KEY_ID": "reaper-id",
        "B2_REAPER_APPLICATION_KEY": "reaper-key",
        "B2_BUCKET_NAME": "tkr-bucket",
    }
    assert (
        RetentionEnvironment.from_environment(base, "sweep").rclone_executable
        == RCLONE_EXECUTABLE
    )
    overridden = RetentionEnvironment.from_environment(
        {**base, "B2_RCLONE_EXECUTABLE": "/opt/homebrew/bin/rclone"}, "sweep"
    )
    assert overridden.rclone_executable == "/opt/homebrew/bin/rclone"


def test_a_refusal_names_what_diverged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drift code is useless without the rule it names.

    ``detect_drift`` carries the offending prefix in ``context`` so an operator
    learns which class stopped being enforced. The first live run printed only
    the code and message, so the refusal was correct and anonymous; this pins
    the context into the operator-visible output.
    """
    assert run_retention("check", tmp_path, {}) == 2
    printed = capsys.readouterr().out
    assert "retention_environment_incomplete" in printed
    assert "B2_LIFECYCLE_KEY_ID" in printed


def test_a_sweep_does_not_require_the_lifecycle_credential() -> None:
    """The split is real only if one mode never demands the other's key.

    The provider refuses ``writeBuckets`` on a bucket-restricted key, so the
    credential that rewrites lifecycle rules cannot be the one confined to the
    bucket it sweeps. If a sweep still required both, an operator would hold
    both keys to run either mode and the separation would be a naming
    convention rather than a boundary.
    """
    configuration = RetentionEnvironment.from_environment(
        {
            "B2_REAPER_KEY_ID": "reaper-id",
            "B2_REAPER_APPLICATION_KEY": "reaper-key",
            "B2_BUCKET_NAME": "tkr-bucket",
        },
        "sweep",
    )
    assert configuration.reaper_key_id == "reaper-id"
    assert configuration.lifecycle_key_id == ""


@pytest.mark.parametrize("mode", ["check", "apply"])
def test_bucket_configuration_does_not_require_the_reaper_credential(
    mode: str,
) -> None:
    """Rewriting a rule must not require the key that deletes objects."""
    configuration = RetentionEnvironment.from_environment(
        {
            "B2_LIFECYCLE_KEY_ID": "lifecycle-id",
            "B2_LIFECYCLE_APPLICATION_KEY": "lifecycle-key",
            "B2_BUCKET_ID": "bucket-1",
        },
        mode,
    )
    assert configuration.lifecycle_key_id == "lifecycle-id"
    assert configuration.reaper_key_id == ""


def test_a_mode_names_only_the_values_it_needs() -> None:
    """A check must not report the reaper credential as missing."""
    with pytest.raises(RetentionError) as caught:
        RetentionEnvironment.from_environment({"B2_BUCKET_ID": "bucket-1"}, "check")
    missing = str(caught.value.context["field"])
    assert "B2_LIFECYCLE_KEY_ID" in missing
    assert "B2_REAPER_KEY_ID" not in missing


def test_an_unknown_mode_is_refused_before_any_value_is_read() -> None:
    """A mode with no declared requirements must not resolve to an empty set."""
    with pytest.raises(RetentionError) as caught:
        RetentionEnvironment.from_environment({}, "purge")
    assert caught.value.code == "retention_mode_unknown"


def test_default_base_prefix_is_the_namespace_the_sweep_lists() -> None:
    """A default naming another namespace enforces nothing and reports clean.

    The reconciler lists ``OUTPUT_PREFIX`` through the configured base, so a
    base the workers do not write to yields an empty listing, no deletions and
    a clean report. That is indistinguishable from a bucket with nothing to
    expire, which is why the default is pinned rather than left to an operator
    to discover.
    """
    configuration = RetentionEnvironment.from_environment(
        {
            "B2_REAPER_KEY_ID": "key-id",
            "B2_REAPER_APPLICATION_KEY": "application-key",
            "B2_BUCKET_ID": "bucket-1",
            "B2_BUCKET_NAME": "ca-east-006",
        }
    )
    assert configuration.base_prefix == OUTPUT_PREFIX
    assert OUTPUT_PREFIX.startswith(configuration.base_prefix)


@pytest.mark.parametrize(
    ("value", "expected_dry_run"),
    [
        ({}, True),
        ({"RETENTION_DRY_RUN": "true"}, True),
        ({"RETENTION_DRY_RUN": "yes"}, True),
        ({"RETENTION_DRY_RUN": ""}, True),
        ({"RETENTION_DRY_RUN": "False"}, False),
        ({"RETENTION_DRY_RUN": "false"}, False),
    ],
)
def test_only_an_explicit_false_enables_deletion(
    value: dict[str, str], expected_dry_run: bool
) -> None:
    """A typo must not turn a report into a deletion."""
    assert _dry_run_default(value) is expected_dry_run


def test_cli_exposes_the_lifecycle_modes() -> None:
    """The operator script's three modes exist on the parser it calls."""
    for mode in ("check", "apply", "sweep"):
        parsed = build_parser().parse_args(["lifecycle", mode])
        assert parsed.command == "lifecycle"
        assert parsed.mode == mode


def test_lifecycle_command_fails_closed_without_an_environment(
    tmp_path: Path,
) -> None:
    """Running with nothing configured reports rather than acting."""
    assert run_retention("check", tmp_path, {}) == 2


@pytest.mark.asyncio
async def test_absent_bucket_is_refused() -> None:
    """Configuring a bucket the provider does not report is refused."""
    reading = adapter(
        {"b2_authorize_account": AUTHORIZED, "b2_list_buckets": {"buckets": []}}
    )
    with pytest.raises(BucketConfigurationError) as caught:
        await reading.read()
    assert caught.value.code == "bucket_not_found"
