"""Console configuration, its secrets, and the CLI that serves it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tkr_cloud_video.cli import build_parser, run_console
from tkr_cloud_video.console.settings import (
    ConsoleCredentials,
    credentials_from_environment,
    settings_from_environment,
)

ENVIRONMENT = {
    "TKR_CONSOLE_ENDPOINT_ID": "176tpna3ogl94t",
    "TKR_B2_BUCKET_NAME": "tkr-video-ca",
    "TKR_PRINCIPAL_ID": "runpod-endpoint",
    "RUNPOD_API_KEY": "rpa_key",
    "B2_DELIVERY_KEY_ID": "0026abcdef0123456789abcd",
    "B2_DELIVERY_APPLICATION_KEY": "K002example",
}


def test_settings_come_from_the_environment_it_is_handed() -> None:
    """Nothing reads the ambient process environment implicitly."""
    built = settings_from_environment({**ENVIRONMENT, "TKR_CONSOLE_BIND_PORT": "9100"})

    assert built.endpoint_id == "176tpna3ogl94t"
    assert built.bind_port == 9100
    assert built.b2_s3_region == "ca-east-006"


def test_an_empty_variable_falls_back_rather_than_overriding() -> None:
    """An exported-but-empty variable is absent, not a value."""
    built = settings_from_environment({**ENVIRONMENT, "TKR_PRINCIPAL_ID": ""})

    assert built.principal_id == "runpod-endpoint"


def test_a_storage_pair_outside_a_reviewed_canadian_region_is_refused() -> None:
    """The same rule the rclone location enforces, enforced here too."""
    with pytest.raises(ValidationError, match="Canadian"):
        settings_from_environment(
            {
                **ENVIRONMENT,
                "TKR_B2_S3_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
                "TKR_B2_S3_REGION": "us-west-004",
            }
        )


def test_a_privileged_port_is_refused_and_an_ephemeral_one_is_not() -> None:
    """Zero asks the operating system to choose; below 1024 asks for elevation."""
    assert (
        settings_from_environment(
            {**ENVIRONMENT, "TKR_CONSOLE_BIND_PORT": "0"}
        ).bind_port
        == 0
    )
    with pytest.raises(ValidationError, match="unprivileged"):
        settings_from_environment({**ENVIRONMENT, "TKR_CONSOLE_BIND_PORT": "443"})


def test_a_missing_secret_is_named_and_a_value_never_is() -> None:
    """The operator learns which variable to set, and nothing about the others."""
    with pytest.raises(ValueError, match="B2_DELIVERY_KEY_ID") as raised:
        credentials_from_environment(
            {**ENVIRONMENT, "B2_DELIVERY_KEY_ID": "", "RUNPOD_API_KEY": ""}
        )

    assert "RUNPOD_API_KEY" in str(raised.value)
    assert ENVIRONMENT["B2_DELIVERY_APPLICATION_KEY"] not in str(raised.value)


def test_credentials_are_read_and_never_rendered() -> None:
    """A credential in a traceback or a log line would be the whole failure."""
    credentials = credentials_from_environment(ENVIRONMENT)

    assert credentials.runpod_api_key == "rpa_key"
    for secret in (
        ENVIRONMENT["RUNPOD_API_KEY"],
        ENVIRONMENT["B2_DELIVERY_KEY_ID"],
        ENVIRONMENT["B2_DELIVERY_APPLICATION_KEY"],
    ):
        assert secret not in repr(credentials)
    assert secret not in str(credentials)


def test_an_empty_credential_is_refused_at_construction() -> None:
    """A blank secret fails now rather than at the first provider call."""
    with pytest.raises(ValueError, match="cannot be empty"):
        ConsoleCredentials(
            runpod_api_key="", delivery_key_id="a", delivery_application_key="b"
        )


def test_the_console_command_is_parsed_with_its_port_override() -> None:
    """The console is a first-class command beside doctor and serverless."""
    arguments = build_parser().parse_args(["console", "--port", "9100"])

    assert arguments.command == "console"
    assert arguments.port == 9100


def test_an_incomplete_configuration_exits_rather_than_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A console with no endpoint or no secret must not start at all."""
    monkeypatch.setattr("os.environ", {})

    assert run_console() == 2
    assert "console configuration is incomplete" in capsys.readouterr().err
