"""Strict console configuration and the credentials it never renders."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.storage import (
    B2_S3_ENDPOINT,
    B2_S3_REGION,
    validate_canadian_b2_endpoint,
)

RUNPOD_API_BASE: Final[str] = "https://api.runpod.ai/v2"

# The three secrets the console needs, named as the vault names them so a
# launcher can be read against `scripts/fetch_result.sh` without translation.
RUNPOD_API_KEY_VARIABLE: Final[str] = "RUNPOD_API_KEY"
DELIVERY_KEY_ID_VARIABLE: Final[str] = "B2_DELIVERY_KEY_ID"
DELIVERY_APPLICATION_KEY_VARIABLE: Final[str] = "B2_DELIVERY_APPLICATION_KEY"


class ConsoleSettings(BaseModel):
    """Validated non-secret console settings.

    Args:
        endpoint_id: The RunPod Serverless endpoint to submit to.
        principal_id: The principal the endpoint generates as. This must equal
            the worker's ``TKR_PRINCIPAL_ID``, because a job's identity is
            derived from it: configured differently, the console computes keys
            for a job the worker never wrote and every generation reads as
            never committed.
        bucket_name: The delivery bucket committed results are read from.
        signed_link_ttl_seconds: Requested playback link lifetime. The signing
            service caps it again at one hour, so this only narrows.
        read_link_ttl_seconds: Lifetime of the internal link the console signs
            for its own read of a result marker. Seconds, not minutes: it is
            used once, immediately, by this process.

    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    endpoint_id: str
    principal_id: str = "runpod-endpoint"
    bucket_name: str
    b2_s3_endpoint: str = B2_S3_ENDPOINT
    b2_s3_region: str = B2_S3_REGION
    signed_link_ttl_seconds: int = Field(default=900, ge=1, le=3600)
    read_link_ttl_seconds: int = Field(default=60, ge=1, le=300)
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8765, ge=0, le=65535)
    runpod_api_base: str = RUNPOD_API_BASE
    request_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("endpoint_id", "principal_id", "bucket_name")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        """Validate non-secret console identities."""
        return validate_identifier(value, "resource_id")

    @field_validator("bind_host")
    @classmethod
    def validate_loopback(cls, value: str) -> str:
        """Refuse to serve anything but loopback.

        The console holds a RunPod key and a delivery credential and performs
        no authentication of its own, because on loopback the operating system
        has already answered who the caller is. Bound anywhere else that answer
        is gone and the whole design is wrong, so the address is constrained
        here rather than left to whoever writes the launch command.
        """
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("console must bind a loopback address")
        return value

    @field_validator("bind_port")
    @classmethod
    def validate_unprivileged_port(cls, value: int) -> int:
        """Permit an ephemeral or unprivileged port and nothing between.

        Zero is kept because it is how a caller asks the operating system to
        choose, which is what a second console on the same machine needs. The
        privileged range is refused: nothing about a single-operator tool wants
        the elevation that binding below 1024 would require.
        """
        if value != 0 and value < 1024:
            raise ValueError("console port must be 0 or an unprivileged port")
        return value

    @model_validator(mode="after")
    def validate_reviewed_region(self) -> ConsoleSettings:
        """Require the storage pair to name one reviewed Canadian region."""
        validate_identifier(self.b2_s3_region, "resource_id")
        validate_canadian_b2_endpoint(self.b2_s3_endpoint, self.b2_s3_region)
        return self


CONSOLE_ENVIRONMENT_FIELDS: Final[dict[str, str]] = {
    "TKR_CONSOLE_ENDPOINT_ID": "endpoint_id",
    "TKR_PRINCIPAL_ID": "principal_id",
    "TKR_B2_BUCKET_NAME": "bucket_name",
    "TKR_B2_S3_ENDPOINT": "b2_s3_endpoint",
    "TKR_B2_S3_REGION": "b2_s3_region",
    "TKR_CONSOLE_SIGNED_LINK_TTL_SECONDS": "signed_link_ttl_seconds",
    "TKR_CONSOLE_READ_LINK_TTL_SECONDS": "read_link_ttl_seconds",
    "TKR_CONSOLE_BIND_HOST": "bind_host",
    "TKR_CONSOLE_BIND_PORT": "bind_port",
    "TKR_CONSOLE_REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
}


@dataclass(frozen=True, slots=True)
class ConsoleCredentials:
    """In-memory secrets that are never rendered, logged, or serialized.

    Args:
        runpod_api_key: Bearer token for the Serverless run routes.
        delivery_key_id: Delivery-read key identifier.
        delivery_application_key: Delivery-read application key.

    """

    runpod_api_key: str = field(repr=False)
    delivery_key_id: str = field(repr=False)
    delivery_application_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject an empty secret rather than failing at the first call."""
        if not (
            self.runpod_api_key
            and self.delivery_key_id
            and self.delivery_application_key
        ):
            raise ValueError("console credentials cannot be empty")


def settings_from_environment(environment: Mapping[str, str]) -> ConsoleSettings:
    """Build console settings from the process environment.

    Args:
        environment: The variables to read. Passed explicitly so a test never
            depends on the ambient process environment.

    Returns:
        The validated settings.

    """
    values: dict[str, Any] = {}
    for variable, attribute in CONSOLE_ENVIRONMENT_FIELDS.items():
        present = environment.get(variable)
        if present is not None and present != "":
            values[attribute] = present
    return ConsoleSettings(**values)


def credentials_from_environment(
    environment: Mapping[str, str],
) -> ConsoleCredentials:
    """Read the three console secrets, naming any that is absent.

    Args:
        environment: The variables to read.

    Returns:
        The credentials.

    Raises:
        ValueError: A required secret is absent or empty. The message names the
            variables, never a value.

    """
    variables = (
        RUNPOD_API_KEY_VARIABLE,
        DELIVERY_KEY_ID_VARIABLE,
        DELIVERY_APPLICATION_KEY_VARIABLE,
    )
    missing = [name for name in variables if not environment.get(name)]
    if missing:
        raise ValueError(f"console credentials are not set: {', '.join(missing)}")
    return ConsoleCredentials(
        runpod_api_key=environment[RUNPOD_API_KEY_VARIABLE],
        delivery_key_id=environment[DELIVERY_KEY_ID_VARIABLE],
        delivery_application_key=environment[DELIVERY_APPLICATION_KEY_VARIABLE],
    )
