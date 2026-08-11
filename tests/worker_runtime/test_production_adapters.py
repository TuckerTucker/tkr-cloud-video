"""Contract tests for concrete rclone, ComfyUI, ffprobe, and runtime adapters."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from tests.conftest import CapturingEventSink

from tkr_cloud_video.adapters.comfy import (
    ComfyApiClient,
    JsonTransport,
    PollingComfyApi,
    UrllibJsonTransport,
)
from tkr_cloud_video.adapters.media import FfprobeInputInspector, FfprobeMediaInspector
from tkr_cloud_video.adapters.metadata_mapper import (
    MAXIMUM_MAPPER_INPUT_CHARACTERS,
    allowlisted_metadata,
)
from tkr_cloud_video.adapters.metadata_mapper import (
    run as run_metadata_mapper,
)
from tkr_cloud_video.adapters.process import (
    CommandExecutor,
    CommandResult,
    SubprocessCommandExecutor,
)
from tkr_cloud_video.adapters.rclone import (
    ArtifactRcloneStore,
    RcloneB2Client,
    RcloneBlobDownloader,
    RcloneCredentials,
    RcloneLocation,
    ResultRcloneStore,
)
from tkr_cloud_video.adapters.runtime import (
    AsyncioWaiter,
    ComfyProcessHandle,
    ComfyProcessRunner,
    EnvironmentSecretResolver,
    LocalPlatformProbe,
)
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.comfy_client import PromptState
from tkr_cloud_video.release.serverless import compose_serverless_deployment
from tkr_cloud_video.runtime.config import load_worker_settings
from tkr_cloud_video.runtime.lifecycle import WorkerState
from tkr_cloud_video.security.process_secrets import SecretReference
from tkr_cloud_video.worker import compose_worker_application


@dataclass
class RecordingExecutor(CommandExecutor):
    """Command fake with explicit response queue and environment evidence."""

    responses: list[CommandResult | AppError]
    calls: list[tuple[tuple[str, ...], dict[str, str], bytes | None]] = field(
        default_factory=list
    )

    async def run(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float = 300,
    ) -> CommandResult:
        """Record exact process boundaries and return the next result."""
        assert timeout_seconds > 0
        self.calls.append((arguments, environment, stdin))
        response = self.responses.pop(0)
        if isinstance(response, AppError):
            raise response
        return response


@dataclass
class Transport(JsonTransport):
    """Path-keyed ComfyUI JSON transport fake."""

    responses: dict[tuple[str, str], object]
    calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> object:
        """Return deterministic response and capture submitted workflow data."""
        self.calls.append((method, path, payload))
        return self.responses[(method, path)]


def metadata(digest: str = "a" * 64) -> CommandResult:
    """Build an rclone lsjson stat response with immutable metadata."""
    return CommandResult(
        json.dumps(
            {
                "Size": 4,
                "ID": "provider-version-1",
                "Metadata": {"sha256": digest},
            }
        ).encode(),
        b"",
    )


def missing() -> AppError:
    """Build the sanitized command failure used for an absent object."""
    return AppError(
        "adapter_command_failed",
        "Provider adapter command failed.",
        cause=RuntimeError("object not found"),
    )


@pytest.mark.asyncio
async def test_rclone_upload_is_immutable_verified_and_secret_isolated() -> None:
    """B2 writes use stdin, SHA metadata, exact prefix, and no secret arguments."""
    executor = RecordingExecutor([missing(), CommandResult(b"", b""), metadata()])
    credentials = RcloneCredentials("key-id-marker", "application-key-marker")
    client = RcloneB2Client(
        executor,
        credentials,
        RcloneLocation("tkr", "private-bucket", "models/"),
    )
    store = ArtifactRcloneStore(client)

    result = await store.put("blobs/object", b"data", "a" * 64)

    assert result.version_id == "provider-version-1"
    assert executor.calls[1][2] == b"data"
    command_text = repr([call[0] for call in executor.calls])
    assert "models/blobs/object" in command_text
    assert "--immutable" in command_text
    assert "sha256=" + "a" * 64 in command_text
    assert "--metadata" in executor.calls[1][0]
    assert executor.calls[1][1]["RCLONE_CONFIG_TKR_TYPE"] == "s3"
    assert executor.calls[1][1]["RCLONE_CONFIG_TKR_PROVIDER"] == "Other"
    assert executor.calls[1][1]["RCLONE_CONFIG_TKR_NO_CHECK_BUCKET"] == "true"
    assert "key-id-marker" not in command_text
    assert "application-key-marker" not in command_text
    assert "key-id-marker" not in repr(credentials)
    assert "application-key-marker" not in repr(credentials)


@pytest.mark.asyncio
async def test_rclone_normalizes_remote_name_for_environment_configuration() -> None:
    """The target and environment use the same shell-safe remote section."""
    executor = RecordingExecutor([metadata()])
    client = RcloneB2Client(
        executor,
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr-publisher", "bucket", "models/"),
    )

    assert await client.head("blobs/object") is not None

    arguments, environment, _ = executor.calls[0]
    assert "tkr_publisher:bucket/models/blobs/object" in arguments
    assert "RCLONE_CONFIG_TKR_PUBLISHER_TYPE" in environment


@pytest.mark.asyncio
async def test_rclone_url_upload_uses_seekable_isolated_http_remote() -> None:
    """Remote publication uses ranged HTTPS input with bounded retries."""
    executor = RecordingExecutor([missing(), CommandResult(b"", b""), metadata()])
    client = RcloneB2Client(
        executor,
        RcloneCredentials("key-id-marker", "application-key-marker"),
        RcloneLocation("tkr-publisher", "bucket", "models/"),
    )
    source_url = (
        "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/"
        "0123456789abcdef0123456789abcdef01234567/model.safetensors"
    )

    result = await client.put_url("blobs/object", source_url, "a" * 64, 4)

    assert result.sha256 == "a" * 64
    arguments, environment, _ = executor.calls[1]
    assert arguments[1] == "copyto"
    assert (
        "tkr_publisher_source:Comfy-Org/MiniMax-H3/resolve/"
        "0123456789abcdef0123456789abcdef01234567/model.safetensors"
    ) in arguments
    assert source_url not in arguments
    assert "--retries" in arguments
    assert "--metadata-mapper" in arguments
    assert environment["RCLONE_CONFIG_TKR_PUBLISHER_SOURCE_TYPE"] == "http"
    assert (
        environment["RCLONE_CONFIG_TKR_PUBLISHER_SOURCE_URL"]
        == "https://huggingface.co"
    )
    assert "key-id-marker" not in repr(arguments)
    assert "application-key-marker" not in repr(arguments)


def test_metadata_mapper_discards_every_source_field() -> None:
    """Only later operator-controlled metadata may reach object storage."""
    payload = {
        "Metadata": {
            "content-disposition": "unsafe upstream value",
            "authorization": "must-not-propagate",
        },
        "Remote": "model.safetensors",
    }

    digest = "a" * 64
    assert allowlisted_metadata(payload, digest) == {"Metadata": {"sha256": digest}}

    output = StringIO()
    run_metadata_mapper(StringIO(json.dumps(payload)), output, digest)
    assert json.loads(output.getvalue()) == {"Metadata": {"sha256": digest}}

    with pytest.raises(ValueError, match="must be an object"):
        allowlisted_metadata([], digest)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="canonical"):
        allowlisted_metadata(payload, "not-a-digest")
    with pytest.raises(ValueError, match="exceeds"):
        run_metadata_mapper(
            StringIO("x" * (MAXIMUM_MAPPER_INPUT_CHARACTERS + 1)),
            StringIO(),
            digest,
        )


@pytest.mark.asyncio
async def test_rclone_missing_and_metadata_failure_are_distinct() -> None:
    """Absence returns None while unverifiable existing content fails closed."""
    missing_client = RcloneB2Client(
        RecordingExecutor([missing()]),
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr", "bucket", "models/"),
    )
    assert await missing_client.head("blobs/object") is None

    virtual_directory_client = RcloneB2Client(
        RecordingExecutor(
            [
                CommandResult(
                    json.dumps(
                        {
                            "Path": "",
                            "Name": "",
                            "Size": -1,
                            "MimeType": "inode/directory",
                            "IsDir": True,
                        }
                    ).encode(),
                    b"",
                )
            ]
        ),
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr", "bucket", "models/"),
    )
    assert await virtual_directory_client.head("blobs/object") is None

    with pytest.raises(ValueError, match="Canadian Backblaze S3 region"):
        RcloneLocation(
            "tkr",
            "bucket",
            "models/",
            "https://untrusted.example.com",
            "ca-east-006",
        )
    with pytest.raises(ValueError, match="Canadian Backblaze S3 region"):
        RcloneLocation(
            "tkr",
            "bucket",
            "models/",
            "https://s3.us-west-004.backblazeb2.com",
            "us-west-004",
        )
    with pytest.raises(ValueError, match="Canadian Backblaze S3 region"):
        RcloneLocation(
            "tkr",
            "bucket",
            "models/",
            "https://s3.ca-east-006.backblazeb2.com",
            "ca-west-001",
        )

    invalid_client = RcloneB2Client(
        RecordingExecutor(
            [CommandResult(json.dumps({"Size": 4, "Metadata": {}}).encode(), b"")]
        ),
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr", "bucket", "models/"),
    )
    with pytest.raises(AppError, match="SHA-256"):
        await invalid_client.head("blobs/object")


@pytest.mark.asyncio
async def test_rclone_read_download_probe_and_store_views(tmp_path: Path) -> None:
    """Concrete views preserve ArtifactStore, ResultStore, and downloader contracts."""
    executor = RecordingExecutor(
        [
            CommandResult(b"[]", b""),
            metadata(),
            CommandResult(b"data", b""),
            metadata(),
            CommandResult(b"result", b""),
            CommandResult(b"", b""),
            missing(),
        ]
    )
    client = RcloneB2Client(
        executor,
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr", "bucket", "models/"),
    )

    assert await client.probe() is True
    assert (await ArtifactRcloneStore(client).head("blobs/object")) is not None
    assert await ArtifactRcloneStore(client).get("blobs/object") == b"data"
    assert (await ResultRcloneStore(client).head("outputs/object")) is not None
    assert await ResultRcloneStore(client).get("outputs/object") == b"result"
    destination = tmp_path / "partial"
    await RcloneBlobDownloader(client).download("blobs/object", destination)
    assert "--config" in executor.calls[-1][0]
    assert await ResultRcloneStore(client).get("missing") is None


@pytest.mark.asyncio
async def test_rclone_rejects_existing_put_invalid_json_and_destination_drift(
    tmp_path: Path,
) -> None:
    """Unverifiable provider state and pre-existing partials fail closed."""
    client = RcloneB2Client(
        RecordingExecutor([metadata()]),
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr", "bucket", "models/"),
    )
    with pytest.raises(AppError, match="already exists"):
        await client.put("object", b"data", "a" * 64)

    invalid = RcloneB2Client(
        RecordingExecutor([CommandResult(b"not-json", b"")]),
        RcloneCredentials("key", "application"),
        RcloneLocation("tkr", "bucket", "models/"),
    )
    with pytest.raises(AppError, match="invalid metadata"):
        await invalid.head("object")
    destination = tmp_path / "existing"
    destination.write_text("existing")
    with pytest.raises(ValueError, match="must not exist"):
        await invalid.download("object", destination)


@pytest.mark.asyncio
async def test_comfy_adapter_proves_inventory_and_prompt_owned_output() -> None:
    """One local API adapter supports readiness, submission, and exact history."""
    transport = Transport(
        {
            ("GET", "/system_stats"): {"system": {}},
            ("GET", "/object_info"): {
                "H3Sampler": {"input": {"required": {"model": [["h3.safetensors"]]}}}
            },
            ("POST", "/prompt"): {"prompt_id": "prompt-1"},
            ("GET", "/history/prompt-1"): {
                "prompt-1": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "10": {
                            "videos": [
                                {
                                    "filename": "video.mp4",
                                    "subfolder": "job-1",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
            },
            ("POST", "/queue"): {},
        }
    )
    client = ComfyApiClient(transport)

    assert await client.healthy() is True
    assert await client.node_types() == frozenset({"H3Sampler"})
    assert await client.model_names() == frozenset({"h3.safetensors"})
    assert await client.submit({"10": {}}, "client-1") == "prompt-1"
    status = await client.status("prompt-1")
    assert status.state is PromptState.SUCCEEDED
    assert status.outputs == ("job-1/video.mp4",)
    await client.cancel("prompt-1")
    assert transport.calls[-1][2] == {"delete": ["prompt-1"]}


@pytest.mark.asyncio
async def test_comfy_adapter_rejects_cross_workspace_output() -> None:
    """Provider history cannot smuggle an escaping filesystem path inward."""
    transport = Transport(
        {
            ("GET", "/history/prompt-1"): {
                "prompt-1": {
                    "status": {"completed": True},
                    "outputs": {
                        "1": {
                            "videos": [
                                {
                                    "filename": "escape.mp4",
                                    "subfolder": "../other",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
            }
        }
    )
    with pytest.raises(AppError):
        await ComfyApiClient(transport).status("prompt-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({}, PromptState.RUNNING),
        ({"status": {"status_str": "error", "messages": []}}, PromptState.FAILED),
        ({"status": {"status_str": "cancelled"}}, PromptState.CANCELLED),
        ({"status": {"status_str": "running"}}, PromptState.RUNNING),
    ],
)
async def test_comfy_status_classification(
    status: dict[str, object], expected: PromptState
) -> None:
    """Each provider status maps to one stable domain state."""
    response = {} if not status else {"prompt-1": status}
    client = ComfyApiClient(Transport({("GET", "/history/prompt-1"): response}))
    assert (await client.status("prompt-1")).state is expected


@pytest.mark.asyncio
async def test_polling_comfy_api_recovers_retryable_startup_error() -> None:
    """Comfy startup polling absorbs transient connection refusal only."""

    class Client:
        calls = 0

        async def healthy(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise AppError("unavailable", "Unavailable.", retryable=True)
            return True

        async def node_types(self) -> frozenset[str]:
            return frozenset({"Node"})

        async def model_names(self) -> frozenset[str]:
            return frozenset({"Model"})

    client = Client()
    polling = PollingComfyApi(client, 1, 0.001)  # type: ignore[arg-type]
    assert await polling.healthy() is True
    assert await polling.node_types() == frozenset({"Node"})
    assert await polling.model_names() == frozenset({"Model"})


@pytest.mark.asyncio
async def test_urllib_transport_bounds_scheme_json_and_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback transport accepts bounded JSON and safely classifies failures."""

    class Response:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return self.content

    with pytest.raises(ValueError, match="loopback"):
        UrllibJsonTransport("https://public.invalid")
    transport = UrllibJsonTransport("http://127.0.0.1:8188", maximum_response_bytes=8)
    monkeypatch.setattr(
        "tkr_cloud_video.adapters.comfy.urlopen",
        lambda *_args, **_kwargs: Response(b'{"ok":1}'),
    )
    assert await transport.request("GET", "/health") == {"ok": 1}

    monkeypatch.setattr(
        "tkr_cloud_video.adapters.comfy.urlopen",
        lambda *_args, **_kwargs: Response(b"invalid"),
    )
    with pytest.raises(AppError, match="invalid JSON"):
        await transport.request("GET", "/health")
    monkeypatch.setattr(
        "tkr_cloud_video.adapters.comfy.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("unavailable")),
    )
    with pytest.raises(AppError, match="request failed"):
        await transport.request("GET", "/health")
    with pytest.raises(ValueError, match="normalized"):
        await transport.request("GET", "/../escape")


def test_ffprobe_adapters_parse_exact_video_and_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed ffprobe adapter supplies both result and staged-input contracts."""
    path = tmp_path / "media.bin"
    path.write_bytes(b"media")
    response = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "png",
                "width": 1280,
                "height": 720,
                "duration": "2.5",
            }
        ],
        "format": {"format_name": "image2", "duration": "2.5"},
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            (), 0, json.dumps(response).encode(), b""
        ),
    )
    inspector = FfprobeMediaInspector()

    assert inspector.inspect(path).duration_seconds == 2.5
    assert FfprobeInputInspector(inspector).inspect(path) == (
        "image/png",
        1280,
        720,
    )


def test_ffprobe_classifies_missing_nonvideo_mp4_and_process_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Media inspection distinguishes supported inputs from invalid provider output."""
    inspector = FfprobeMediaInspector()
    with pytest.raises(AppError, match="does not exist"):
        inspector.inspect(tmp_path / "missing")
    path = tmp_path / "media"
    path.write_bytes(b"media")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            (), 0, json.dumps({"streams": [], "format": {}}).encode(), b""
        ),
    )
    assert inspector.inspect(path).has_video is False

    mp4 = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 10, "height": 10}
        ],
        "format": {"format_name": "mov,mp4,m4a", "duration": "1"},
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            (), 0, json.dumps(mp4).encode(), b""
        ),
    )
    assert FfprobeInputInspector(inspector).inspect(path)[0] == "video/mp4"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess((), 1, b"", b"failure"),
    )
    with pytest.raises(AppError, match="valid media"):
        inspector.inspect(path)


@pytest.mark.asyncio
async def test_runtime_secrets_and_storage_probe_are_explicit(tmp_path: Path) -> None:
    """Runtime adapters resolve one named secret and probe one injected namespace."""
    resolver = EnvironmentSecretResolver({"model-key": "secret-marker"})
    assert resolver.resolve(SecretReference("B2_MODEL_KEY_ID", "model-key")) == (
        "secret-marker"
    )
    assert "secret-marker" not in repr(resolver)

    class Storage:
        async def probe(self) -> bool:
            return True

    probe = LocalPlatformProbe(tmp_path, Storage())
    assert probe.available_disk_bytes() > 0
    assert await probe.storage_authorized() is True

    with pytest.raises(AppError, match="unavailable"):
        EnvironmentSecretResolver({}).resolve(
            SecretReference("B2_MODEL_KEY_ID", "missing")
        )
    await AsyncioWaiter().wait(0)
    with pytest.raises(ValueError):
        await AsyncioWaiter().wait(-1)


@pytest.mark.asyncio
async def test_shell_free_process_executor_success_failure_and_output_bound() -> None:
    """Production command execution never invokes a shell and sanitizes failures."""
    executor = SubprocessCommandExecutor(maximum_output_bytes=4)
    result = await executor.run(("/usr/bin/printf", "ok"), {})
    assert result.stdout == b"ok"
    with pytest.raises(AppError, match="command failed"):
        await executor.run(("/usr/bin/false",), {})
    with pytest.raises(AppError, match="safety bound"):
        await executor.run(("/usr/bin/printf", "12345"), {})
    with pytest.raises(ValueError, match="absolute"):
        await executor.run(("printf", "bad"), {})


@pytest.mark.asyncio
async def test_comfy_process_runner_rejects_secrets_and_terminates_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher binds loopback and its handle terminates only its child."""

    class Process:
        returncode: int | None = None
        terminated = False
        stdout = None
        stderr = None

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    process = Process()

    async def create(*arguments: str, **kwargs: object) -> Process:
        assert "127.0.0.1" in arguments
        assert "--user-directory" in arguments
        assert str(tmp_path / "inputs" / ".comfy-user") in arguments
        assert "--temp-directory" in arguments
        assert str(tmp_path / "inputs" / ".comfy-temp") in arguments
        assert kwargs["env"] == {"PATH": "/usr/bin"}
        return process

    monkeypatch.setattr(
        "tkr_cloud_video.adapters.runtime.asyncio.create_subprocess_exec", create
    )
    main = tmp_path / "main.py"
    main.write_text("# pinned\n")
    runner = ComfyProcessRunner(
        "/usr/bin/python3", main, tmp_path / "inputs", tmp_path / "outputs"
    )
    with pytest.raises(AppError, match="forbidden secret"):
        await runner.start({"API_TOKEN": "marker"})
    handle = await runner.start({"PATH": "/usr/bin"})
    assert isinstance(handle, ComfyProcessHandle)
    await handle.terminate(1)
    assert process.terminated is True


def test_worker_composition_loads_allowlisted_environment_and_isolates_comfy(
    tmp_path: Path,
) -> None:
    """Deployable composition consumes named secrets but excludes them from ComfyUI."""
    roots = {
        name: tmp_path / name
        for name in ("cache", "models", "workspaces", "outputs", "comfy")
    }
    for path in roots.values():
        path.mkdir()
    (roots["comfy"] / "main.py").write_text("# pinned synthetic entrypoint\n")
    environment = {
        "TKR_RELEASE_ID": "release-1",
        "TKR_WORKER_ID": "worker-1",
        "TKR_MODEL_SET_ID": "models-1",
        "TKR_MANIFEST_DIGEST": "a" * 64,
        "TKR_B2_BUCKET_NAME": "private-bucket",
        "TKR_CACHE_ROOT": str(roots["cache"]),
        "TKR_MODEL_ROOT": str(roots["models"]),
        "TKR_WORKSPACE_ROOT": str(roots["workspaces"]),
        "TKR_OUTPUT_ROOT": str(roots["outputs"]),
        "TKR_COMFYUI_ROOT": str(roots["comfy"]),
        "B2_MODEL_KEY_ID": "key-id-marker",
        "B2_MODEL_APPLICATION_KEY": "application-key-marker",
        "UNRELATED_SECRET": "must-not-propagate",
    }

    application = compose_worker_application(environment, CapturingEventSink())

    assert application.settings == load_worker_settings(environment)
    serialized_environment = repr(application.comfy_environment)
    assert "key-id-marker" not in serialized_environment
    assert "application-key-marker" not in serialized_environment
    assert "must-not-propagate" not in serialized_environment
    assert application.services.lifecycle.ready is False


@pytest.mark.asyncio
async def test_serverless_composition_starts_once_and_rejects_invalid_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RunPod composition owns all three storage roles and one readiness gate."""
    roots = {
        name: tmp_path / name
        for name in ("cache", "models", "workspaces", "outputs", "comfy")
    }
    for path in roots.values():
        path.mkdir()
    (roots["comfy"] / "main.py").write_text("# pinned synthetic entrypoint\n")
    environment = {
        "TKR_RELEASE_ID": "release-1",
        "TKR_WORKER_ID": "worker-1",
        "TKR_MODEL_SET_ID": "models-1",
        "TKR_MANIFEST_DIGEST": "a" * 64,
        "TKR_B2_BUCKET_NAME": "private-bucket",
        "TKR_CACHE_ROOT": str(roots["cache"]),
        "TKR_MODEL_ROOT": str(roots["models"]),
        "TKR_WORKSPACE_ROOT": str(roots["workspaces"]),
        "TKR_OUTPUT_ROOT": str(roots["outputs"]),
        "TKR_COMFYUI_ROOT": str(roots["comfy"]),
        "B2_MODEL_KEY_ID": "model-id",
        "B2_MODEL_APPLICATION_KEY": "model-key",
        "B2_INPUT_KEY_ID": "input-id",
        "B2_INPUT_APPLICATION_KEY": "input-key",
        "B2_OUTPUT_KEY_ID": "output-id",
        "B2_OUTPUT_APPLICATION_KEY": "output-key",
    }
    deployment = compose_serverless_deployment(environment, CapturingEventSink())
    starts = 0

    async def start(_environment: dict[str, str]) -> None:
        nonlocal starts
        starts += 1
        deployment.worker.services.lifecycle.state = WorkerState.READY

    monkeypatch.setattr(deployment.worker.services.supervisor, "start", start)

    response = await deployment.handle({})
    assert response["error_code"] == "invalid_envelope"
    assert starts == 0
    assert await deployment.ensure_started() is True
    assert starts == 1
    serialized = repr(deployment.worker.comfy_environment)
    assert not any(
        marker in serialized for marker in environment.values() if "-key" in marker
    )
