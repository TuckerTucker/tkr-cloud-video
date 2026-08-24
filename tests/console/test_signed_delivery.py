"""Presigning, bounded object reads, and the committed-result repository."""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest

from tests.console.conftest import (
    CREDENTIALS,
    SIGNING_INSTANT,
    FakeObjectReader,
    SigningClock,
)
from tkr_cloud_video.console.objects import MAX_MARKER_BYTES, PresignedObjectReader
from tkr_cloud_video.console.presign import SigV4Presigner
from tkr_cloud_video.console.results import (
    AttemptResultRepository,
    JobRegistry,
    RegisteredJobAuthorizer,
    derive_identity,
)
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.delivery.access_policy import ResultState
from tkr_cloud_video.delivery.contracts import (
    RemoteArtifact,
    ResultManifest,
    ResultRole,
)
from tkr_cloud_video.delivery.signed_links import SignedLinkService

BUCKET = "tkr-video-ca"
ENDPOINT = "https://s3.ca-east-006.backblazeb2.com"
REGION = "ca-east-006"
VIDEO_KEY = "outputs/job-2ea577062383db85fd880d4adc8030c4/attempt-abc/video.mp4"

# Verified byte for byte against botocore's own generate_presigned_url for the
# same credential, bucket, key, region, lifetime and instant. Pinned here rather
# than recomputed so a change to the signing implementation has to restate the
# signature it produces, and cannot quietly start issuing links B2 refuses.
EXPECTED_URL = (
    "https://s3.ca-east-006.backblazeb2.com/tkr-video-ca/outputs/"
    "job-2ea577062383db85fd880d4adc8030c4/attempt-abc/video.mp4"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=0026abcdef0123456789abcd%2F20260823%2Fca-east-006%2Fs3%2F"
    "aws4_request"
    "&X-Amz-Date=20260823T123456Z"
    "&X-Amz-Expires=900"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature="
    "c4635e902cbfae478223fb2a4e309ef8dd0ae24b7da528a5d9a12377b74066f9"
)


def build_presigner(**overrides: Any) -> SigV4Presigner:
    """Build a presigner scoped to the reviewed Canadian delivery bucket."""
    values: dict[str, Any] = {
        "bucket_name": BUCKET,
        "endpoint": ENDPOINT,
        "region": REGION,
    }
    values.update(overrides)
    return SigV4Presigner(CREDENTIALS, SigningClock(), **values)


def manifest(job_id: str, attempt_id: str) -> ResultManifest:
    """Build a committed manifest for one attempt."""
    prefix = f"outputs/{job_id}/{attempt_id}/"
    return ResultManifest(
        job_id=job_id,
        attempt_id=attempt_id,
        workflow_digest="a" * 64,
        model_set_id="minimax-h3-t2v-int8-20260809",
        request_hash="b" * 64,
        committed_at=SIGNING_INSTANT,
        artifacts=(
            RemoteArtifact(
                role=ResultRole.VIDEO,
                remote_key=f"{prefix}video.mp4",
                size_bytes=1024,
                sha256="c" * 64,
                provider_version_id="version-1",
            ),
            RemoteArtifact(
                role=ResultRole.WORKFLOW,
                remote_key=f"{prefix}workflow.json",
                size_bytes=32,
                sha256="d" * 64,
                provider_version_id="version-2",
            ),
            RemoteArtifact(
                role=ResultRole.GENERATION,
                remote_key=f"{prefix}generation.bin",
                size_bytes=16,
                sha256="e" * 64,
                provider_version_id="version-3",
            ),
        ),
    )


def test_the_presigned_url_matches_its_pinned_vector() -> None:
    """One exact signature, for one exact credential, key, region and instant."""
    assert build_presigner().presign_get(VIDEO_KEY, 900) == EXPECTED_URL


def test_an_endpoint_outside_a_reviewed_canadian_region_cannot_be_signed_for() -> None:
    """The pair is refused at construction, not at the first issued link."""
    with pytest.raises(ValueError, match="Canadian"):
        build_presigner(endpoint="https://s3.us-west-004.backblazeb2.com")
    with pytest.raises(ValueError, match="Canadian"):
        build_presigner(region="us-west-004")


@pytest.mark.parametrize("ttl", [0, -1, 604_801])
def test_an_unsignable_lifetime_is_refused(ttl: int) -> None:
    """A lifetime a signature cannot express never reaches the provider."""
    with pytest.raises(ValueError, match="signable range"):
        build_presigner().presign_get(VIDEO_KEY, ttl)


@pytest.mark.asyncio
async def test_the_delivery_policy_still_caps_what_the_presigner_would_sign() -> None:
    """The one-hour delivery cap sits above signing and narrows it."""
    links = SignedLinkService(build_presigner(), SigningClock(), 3600)

    link = await links.issue(VIDEO_KEY, 86_400)

    assert "X-Amz-Expires=3600" in link.url
    assert link.expires_at == SIGNING_INSTANT + timedelta(seconds=3600)


class _Response:
    """Minimal urlopen context manager returning fixed bytes."""

    def __init__(self, payload: bytes) -> None:
        self._buffer = BytesIO(payload)

    def __enter__(self) -> BytesIO:
        return self._buffer

    def __exit__(self, *_: object) -> None:
        return None


def _raise(error: Exception) -> Any:
    """Return a urlopen replacement that raises."""

    def _open(*_: object, **__: object) -> Any:
        raise error

    return _open


@pytest.mark.asyncio
async def test_an_absent_object_reads_as_absent_rather_than_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job with nothing committed yet is a state, not an error."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _raise(urllib.error.HTTPError(ENDPOINT, 404, "Not Found", {}, None)),  # type: ignore[arg-type]
    )
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    assert await reader.get(VIDEO_KEY) is None


def _refusal(status: int, provider_code: str) -> urllib.error.HTTPError:
    """Return one provider refusal carrying its own error document.

    The bodies are the ones `s3.ca-east-006.backblazeb2.com` actually returns,
    captured against the delivery credential itself. The distinction they carry
    is the whole point of the test: a narrow credential and a wrong one are both
    403, and only the provider's own code separates them.
    """
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<Error>\n    <Code>{provider_code}</Code>\n"
        "    <Message>Cannot access bucket</Message>\n</Error>"
    ).encode()
    return urllib.error.HTTPError(
        ENDPOINT,
        status,
        provider_code,
        {},  # type: ignore[arg-type]
        BytesIO(body),
    )


@pytest.mark.asyncio
async def test_a_key_the_read_credential_may_not_enumerate_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation still sampling is a state, not an error.

    The delivery-read credential holds no bucket listing right, so the provider
    refuses to say whether an uncommitted key exists and answers 403
    AccessDenied rather than 404. Read as a failure, that is every poll from
    submission until the marker lands, and the console shows the operator
    nothing for the whole of a generation that is succeeding.
    """
    monkeypatch.setattr(
        urllib.request, "urlopen", _raise(_refusal(403, "AccessDenied"))
    )
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    assert await reader.get(VIDEO_KEY) is None


@pytest.mark.parametrize(
    "provider_code", ["SignatureDoesNotMatch", "InvalidAccessKeyId"]
)
@pytest.mark.asyncio
async def test_a_credential_that_is_wrong_rather_than_narrow_stays_a_failure(
    monkeypatch: pytest.MonkeyPatch, provider_code: str
) -> None:
    """Forgiving absence must not forgive a misconfigured console.

    Both of these are 403 as well. If they were read as absence, a console
    holding the wrong delivery secret would report every generation as
    permanently in progress instead of naming what is wrong.
    """
    monkeypatch.setattr(urllib.request, "urlopen", _raise(_refusal(403, provider_code)))
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    with pytest.raises(AppError, match="Reading a committed result marker failed"):
        await reader.get(VIDEO_KEY)


@pytest.mark.asyncio
async def test_a_refusal_carrying_no_error_document_stays_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is forgiven only where the provider said so."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _raise(urllib.error.HTTPError(ENDPOINT, 403, "Forbidden", {}, None)),  # type: ignore[arg-type]
    )
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    with pytest.raises(AppError):
        await reader.get(VIDEO_KEY)


@pytest.mark.asyncio
async def test_a_provider_failure_is_reported_without_naming_the_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL is a secret for its lifetime and never enters an error."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _raise(urllib.error.HTTPError(ENDPOINT, 503, "Unavailable", {}, None)),  # type: ignore[arg-type]
    )
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    with pytest.raises(AppError) as raised:
        await reader.get(VIDEO_KEY)

    assert raised.value.retryable is True
    assert "X-Amz-Signature" not in str(raised.value)
    assert "X-Amz-Signature" not in str(dict(raised.value.context))


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_rather_than_read_into_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong key cannot stream an unbounded body into the console."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_, **__: _Response(b"x" * (MAX_MARKER_BYTES + 1)),
    )
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    with pytest.raises(AppError, match="readable ceiling"):
        await reader.get(VIDEO_KEY)


def test_the_console_derives_the_identity_the_generation_path_mints() -> None:
    """The console and the worker agree on where a result is written.

    Pinned against `application._job_identity` itself rather than against a
    literal, because agreement is the property that matters: a console that
    derived differently would report every successful generation as missing.
    """
    from tkr_cloud_video.application import _job_identity
    from tkr_cloud_video.delivery.subject_requests import identity_for

    principal, request_hash = "runpod-endpoint", "f" * 64

    minted = _job_identity(principal, request_hash)
    assert derive_identity(principal, request_hash) == (
        minted.job_id,
        minted.attempt_id,
    )
    assert derive_identity(principal, request_hash) == identity_for(
        principal, request_hash
    )


@pytest.mark.asyncio
async def test_only_a_job_this_console_derived_is_authorized() -> None:
    """A job id that nothing here derived resolves the same as an absent one."""
    registry = JobRegistry("runpod-endpoint")
    record = registry.register(
        request_hash="b" * 64,
        run_id="run-1",
        submitted_at=SIGNING_INSTANT,
        model_set_id="minimax-h3-t2v-int8-20260809",
        below_trained_envelope=False,
    )
    authorizer = RegisteredJobAuthorizer(registry)

    assert await authorizer.allowed("runpod-endpoint", record.job_id) is True
    assert await authorizer.allowed("runpod-endpoint", "job-" + "0" * 32) is False
    assert await authorizer.allowed("someone-else", record.job_id) is False


@pytest.mark.asyncio
async def test_the_repository_reads_the_marker_at_the_derived_attempt_key() -> None:
    """No listing is needed: the attempt id is derived, so the key is known."""
    registry = JobRegistry("runpod-endpoint")
    record = registry.register(
        request_hash="b" * 64,
        run_id="run-1",
        submitted_at=SIGNING_INSTANT,
        model_set_id="minimax-h3-t2v-int8-20260809",
        below_trained_envelope=False,
    )
    committed = manifest(record.job_id, record.attempt_id)
    reader = FakeObjectReader({record.commit_key: committed.canonical_bytes()})
    repository = AttemptResultRepository(registry, reader)

    assert await repository.state(record.job_id) is ResultState.COMPLETED
    assert await repository.manifest(record.job_id) == committed
    assert reader.reads == [record.commit_key, record.commit_key]


@pytest.mark.asyncio
async def test_an_uncommitted_job_is_in_progress_and_an_unknown_one_absent() -> None:
    """The bucket answers only whether a marker exists."""
    registry = JobRegistry("runpod-endpoint")
    record = registry.register(
        request_hash="b" * 64,
        run_id="run-1",
        submitted_at=SIGNING_INSTANT,
        model_set_id="minimax-h3-t2v-int8-20260809",
        below_trained_envelope=False,
    )
    repository = AttemptResultRepository(registry, FakeObjectReader())

    assert await repository.state(record.job_id) is ResultState.IN_PROGRESS
    assert await repository.state("job-" + "0" * 32) is ResultState.NOT_FOUND
    assert await repository.manifest(record.job_id) is None


@pytest.mark.asyncio
async def test_a_marker_naming_another_job_is_refused() -> None:
    """A marker is trusted for its contents only where it names its own key."""
    registry = JobRegistry("runpod-endpoint")
    record = registry.register(
        request_hash="b" * 64,
        run_id="run-1",
        submitted_at=SIGNING_INSTANT,
        model_set_id="minimax-h3-t2v-int8-20260809",
        below_trained_envelope=False,
    )
    other = manifest("job-" + "9" * 32, "attempt-" + "9" * 32)
    repository = AttemptResultRepository(
        registry, FakeObjectReader({record.commit_key: other.canonical_bytes()})
    )

    with pytest.raises(AppError, match="different job"):
        await repository.manifest(record.job_id)


@pytest.mark.asyncio
async def test_a_marker_that_is_not_a_result_is_refused() -> None:
    """A committed marker either satisfies the result contract or is nothing."""
    registry = JobRegistry("runpod-endpoint")
    record = registry.register(
        request_hash="b" * 64,
        run_id="run-1",
        submitted_at=SIGNING_INSTANT,
        model_set_id="minimax-h3-t2v-int8-20260809",
        below_trained_envelope=False,
    )
    repository = AttemptResultRepository(
        registry, FakeObjectReader({record.commit_key: b'{"schema_version":"9"}'})
    )

    with pytest.raises(AppError, match="result contract"):
        await repository.manifest(record.job_id)


def test_the_registry_is_addressable_by_run_and_by_job() -> None:
    """A run id is what the provider knows; a job id is what the bucket knows."""
    registry = JobRegistry("runpod-endpoint")
    record = registry.register(
        request_hash="b" * 64,
        run_id="run-1",
        submitted_at=SIGNING_INSTANT,
        model_set_id="minimax-h3-t2v-int8-20260809",
        below_trained_envelope=True,
    )

    assert registry.by_run("run-1") == record
    assert registry.by_job(record.job_id) == record
    assert registry.by_run("run-absent") is None
    assert list(registry) == [record]


@pytest.mark.asyncio
async def test_a_transport_failure_reading_a_marker_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset connection is worth retrying; a refused signature is not."""
    monkeypatch.setattr(urllib.request, "urlopen", _raise(OSError("connection reset")))
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    with pytest.raises(AppError) as raised:
        await reader.get(VIDEO_KEY)

    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_a_marker_within_the_ceiling_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary read path returns the object's exact bytes."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_, **__: _Response(b"{}"))
    reader = PresignedObjectReader(build_presigner(), ttl_seconds=60, timeout_seconds=5)

    assert await reader.get(VIDEO_KEY) == b"{}"
