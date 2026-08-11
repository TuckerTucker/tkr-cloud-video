"""Typed settings models and injected configuration source contract."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.core.errors import ContextValidationError, SettingsValidationError


class LogLevel(StrEnum):
    """Supported structured logging levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class SettingsSource(Protocol):
    """Port for reading unvalidated configuration at an external boundary."""

    def load(self) -> Mapping[str, str]:
        """Return configuration values without interpreting them."""
        ...


class CoreSettings(BaseModel):
    """Provider-neutral application settings that never contain credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    log_level: LogLevel = Field(default=LogLevel.INFO, alias="LOG_LEVEL")
    schema_version: str = Field(default="1", alias="SCHEMA_VERSION")
    environment: str = Field(default="development", alias="ENVIRONMENT")

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        """Accept the single currently supported positive schema version."""
        if value != "1":
            raise ValueError("unsupported schema version")
        return value

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        """Require a lowercase safe identifier for environment names."""
        validate_identifier(value, "environment")
        if value != value.lower():
            raise ValueError("environment must be lowercase")
        return value


def load_core_settings(source: SettingsSource) -> CoreSettings:
    """Load and validate core settings from an injected source.

    Args:
        source: External settings adapter.

    Returns:
        Immutable validated core settings.

    Raises:
        SettingsValidationError: If source data violates the schema.

    """
    try:
        return CoreSettings.model_validate(dict(source.load()))
    except (ValidationError, ContextValidationError) as error:
        raise SettingsValidationError(
            "invalid_settings",
            "Core settings failed schema validation.",
            context={"operation": "load_core_settings"},
            cause=error,
        ) from error
