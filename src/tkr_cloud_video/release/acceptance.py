"""Executable product acceptance scenario and evidence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tkr_cloud_video.core.context import validate_identifier
from tkr_cloud_video.security.validation import Sha256Digest


class AcceptanceScenario(StrEnum):
    """Required environment scenarios mapped to product success criteria."""

    COLD_START = "cold-start"
    WARM_ZERO_DOWNLOAD = "warm-zero-download"
    GENERATE_COMMIT_DESTROY = "generate-commit-destroy"
    CORRUPT_ARTIFACT = "corrupt-artifact"
    STORAGE_FAILURE = "storage-failure"
    READINESS_FAILURE = "readiness-failure"
    UPLOAD_FAILURE = "upload-failure"
    CREDENTIAL_MISUSE = "credential-misuse"
    CONCURRENT_ISOLATION = "concurrent-isolation"
    DUPLICATE_REQUEST = "duplicate-request"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    CAPACITY_EXHAUSTION = "capacity-exhaustion"


@dataclass(frozen=True, slots=True)
class ScenarioEvidence:
    """Immutable sanitized evidence for one executed scenario."""

    scenario: AcceptanceScenario
    passed: bool
    evidence_digest: str

    def __post_init__(self) -> None:
        """Require immutable evidence identity rather than mutable prose."""
        Sha256Digest(self.evidence_digest)


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    """Release eligibility derived from complete scenario evidence."""

    release_id: str
    evidence: tuple[ScenarioEvidence, ...]

    def __post_init__(self) -> None:
        """Validate the candidate identity at the release boundary."""
        validate_identifier(self.release_id, "release_id")

    @property
    def passed(self) -> bool:
        """Require every scenario exactly once and every result passing."""
        scenarios = [item.scenario for item in self.evidence]
        return (
            set(scenarios) == set(AcceptanceScenario)
            and len(scenarios) == len(set(scenarios))
            and all(item.passed for item in self.evidence)
        )
