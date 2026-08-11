"""Release gate for immutable provenance, license, use, and territory evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.security.provenance import (
    ReleaseAssuranceError,
    ReleaseManifest,
)
from tkr_cloud_video.security.validation import ModelSetId, Sha256Digest


class DeploymentUse(StrEnum):
    """Deployment audience/use categories that require explicit review."""

    PRIVATE = "private"
    INTERNAL = "internal"
    PUBLIC = "public"
    COMMERCIAL = "commercial"


class LicenseApproval(BaseModel):
    """Immutable reviewer evidence bound to exact release inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    reviewer_id: str
    model_set_id: str
    manifest_digest: str
    deployment_use: DeploymentUse
    territories: tuple[str, ...]
    approved_at: datetime
    expires_at: datetime
    constraints: tuple[str, ...] = ()

    @field_validator("approval_id", "reviewer_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        """Validate reviewer-controlled record identities."""
        return validate_identifier(value, "resource_id")

    @field_validator("model_set_id")
    @classmethod
    def validate_model_set(cls, value: str) -> str:
        """Validate the approval's model-set binding."""
        return str(ModelSetId(value))

    @field_validator("manifest_digest")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        """Validate the approval's exact manifest binding."""
        return str(Sha256Digest(value))

    @field_validator("approved_at", "expires_at")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        """Require timezone-aware evidence validity bounds."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamps must include a timezone")
        return value

    @field_validator("territories")
    @classmethod
    def validate_territories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a non-empty unique set of uppercase ISO-like country codes."""
        if not value or len(value) != len(set(value)):
            raise ValueError("approval territories must be non-empty and unique")
        if any(
            len(item) != 2 or not item.isalpha() or not item.isupper() for item in value
        ):
            raise ValueError("territories must use uppercase alpha-2 codes")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        """Require the evidence to expire after approval."""
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")
        return self


@dataclass(frozen=True, slots=True)
class ReleaseAssuranceResult:
    """Safe evidence that a candidate passed all release assurance gates."""

    release_id: str
    approval_id: str
    manifest_digest: Sha256Digest
    evaluated_at: datetime


class ReleaseAssuranceGate:
    """Blocks promotion unless approval matches exact use, territory, and inputs."""

    def __init__(self, clock: Clock) -> None:
        """Initialize the gate with an injected clock."""
        self._clock = clock

    async def evaluate(
        self,
        manifest: ReleaseManifest,
        approval: LicenseApproval | None,
        deployment_use: DeploymentUse,
        territory: str,
    ) -> ReleaseAssuranceResult:
        """Evaluate provenance-bound license evidence for a release target."""
        if approval is None:
            raise ReleaseAssuranceError(
                "approval_missing",
                "Release requires current model license approval.",
                context={"field": "license_approval"},
            )
        now = self._clock.now()
        checks = (
            (approval.model_set_id == manifest.model_set_id, "model_set_id"),
            (approval.manifest_digest == str(manifest.digest()), "manifest_digest"),
            (approval.deployment_use is deployment_use, "deployment_use"),
            (territory in approval.territories, "territory"),
            (approval.approved_at <= now < approval.expires_at, "approval_window"),
        )
        for passed, field in checks:
            if not passed:
                raise ReleaseAssuranceError(
                    "approval_stale",
                    "Release approval is stale or incompatible with the target.",
                    context={"field": field},
                )
        return ReleaseAssuranceResult(
            release_id=manifest.release_id,
            approval_id=approval.approval_id,
            manifest_digest=manifest.digest(),
            evaluated_at=now,
        )
