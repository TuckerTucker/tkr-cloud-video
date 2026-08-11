"""Boundary value, containment, provenance, and release-gate tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import FakeClock
from tkr_cloud_video.security.provenance import (
    ArtifactKind,
    ProvenanceArtifact,
    ReleaseAssuranceError,
    ReleaseManifest,
)
from tkr_cloud_video.security.release_gate import (
    DeploymentUse,
    LicenseApproval,
    ReleaseAssuranceGate,
)
from tkr_cloud_video.security.validation import (
    BoundaryValidationError,
    JobId,
    ModelSetId,
    ObjectKey,
    PathContainmentError,
    RelativeDestination,
    Sha256Digest,
    contained_path,
)

DIGEST = "a" * 64


@pytest.mark.parametrize(
    "constructor",
    [JobId, ModelSetId],
)
@pytest.mark.parametrize("value", ["", "../escape", "bad value", "line\nbreak"])
def test_identifiers_reject_unsafe_values(constructor: type[JobId], value: str) -> None:
    """Externally derived identifiers fail before any adapter receives them."""
    with pytest.raises(BoundaryValidationError):
        constructor(value)


@pytest.mark.parametrize(
    "value",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "../" + "a" * 64],
)
def test_digest_requires_exact_canonical_sha256(value: str) -> None:
    """Digest separators, case variants, and wrong lengths are invalid."""
    with pytest.raises(BoundaryValidationError):
        Sha256Digest(value)
    assert str(Sha256Digest(DIGEST)) == DIGEST


@pytest.mark.parametrize(
    "constructor",
    [ObjectKey, RelativeDestination],
)
@pytest.mark.parametrize(
    "value", ["", "/absolute", "../escape", "a/../b", "a//b", "a\\b", "a/"]
)
def test_path_like_values_reject_ambiguous_syntax(
    constructor: type[ObjectKey], value: str
) -> None:
    """Remote and local relative paths share strict normalization rules."""
    with pytest.raises(BoundaryValidationError):
        constructor(value)


def test_contained_path_accepts_nested_destination(tmp_path: Path) -> None:
    """A canonical nested destination stays beneath its assigned root."""
    root = tmp_path / "workspace"
    root.mkdir()

    result = contained_path(root, RelativeDestination("job-1/output.mp4"))

    assert result == root / "job-1" / "output.mp4"
    assert not result.exists()


def test_contained_path_rejects_symlink_escape(tmp_path: Path) -> None:
    """Existing parent symlinks cannot redirect a future write outside the root."""
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathContainmentError):
        contained_path(root, RelativeDestination("linked/output.mp4"))


def artifact(kind: ArtifactKind, sequence: int) -> ProvenanceArtifact:
    """Build deterministic complete provenance evidence."""
    return ProvenanceArtifact(
        name=f"component-{sequence}",
        kind=kind,
        identity=f"immutable-identity-{sequence}",
        sha256=hex(sequence)[2:].rjust(64, "0"),
        verification_command=f"verify component-{sequence}",
    )


def manifest() -> ReleaseManifest:
    """Build a complete deterministic release manifest."""
    return ReleaseManifest(
        release_id="release-1",
        model_set_id="minimax-h3-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        artifacts=tuple(
            artifact(kind, sequence)
            for sequence, kind in enumerate(ArtifactKind, start=1)
        ),
    )


def approval(
    release: ReleaseManifest,
    *,
    deployment_use: DeploymentUse = DeploymentUse.COMMERCIAL,
    territories: tuple[str, ...] = ("CA",),
    model_set_id: str | None = None,
) -> LicenseApproval:
    """Build current evidence bound to a manifest and release target."""
    return LicenseApproval(
        approval_id="approval-1",
        reviewer_id="reviewer-1",
        model_set_id=model_set_id or release.model_set_id,
        manifest_digest=str(release.digest()),
        deployment_use=deployment_use,
        territories=territories,
        approved_at=datetime(2025, 12, 1, tzinfo=UTC),
        expires_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def test_manifest_serialization_digest_and_version_identity_are_deterministic() -> None:
    """Canonical bytes bind every artifact without exposing sensitive config."""
    release = manifest()

    assert release.canonical_bytes() == manifest().canonical_bytes()
    assert release.digest() == manifest().digest()
    identity = release.version_identity()
    assert identity["release_id"] == "release-1"
    assert set(identity) == {"release_id", "model_set_id", "manifest_digest"}
    assert json.loads(release.canonical_bytes())["schema_version"] == "1"


def test_manifest_requires_every_artifact_kind_and_unique_names() -> None:
    """Incomplete or ambiguous provenance cannot form a release identity."""
    complete = list(manifest().artifacts)
    with pytest.raises(ValidationError):
        ReleaseManifest(
            release_id="release-1",
            model_set_id="model-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            artifacts=tuple(complete[:-1]),
        )
    complete[-1] = complete[0].model_copy(update={"kind": ArtifactKind.MODEL})
    with pytest.raises(ValidationError):
        ReleaseManifest(
            release_id="release-1",
            model_set_id="model-1",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            artifacts=tuple(complete),
        )


def test_manifest_verification_names_drifting_component() -> None:
    """Missing or mismatched observed digests block promotion."""
    release = manifest()
    observed = {item.name: Sha256Digest(item.sha256) for item in release.artifacts}
    release.verify(observed)
    observed[release.artifacts[0].name] = Sha256Digest("f" * 64)

    with pytest.raises(ReleaseAssuranceError) as captured:
        release.verify(observed)
    assert captured.value.context["resource_id"] == release.artifacts[0].name


@pytest.mark.asyncio
async def test_release_gate_accepts_current_exact_approval() -> None:
    """Current matching use, territory, model, and manifest evidence passes."""
    release = manifest()
    gate = ReleaseAssuranceGate(FakeClock())

    result = await gate.evaluate(
        release, approval(release), DeploymentUse.COMMERCIAL, "CA"
    )

    assert result.release_id == release.release_id
    assert result.manifest_digest == release.digest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_factory", "use", "territory", "expected_code"),
    [
        (lambda _release: None, DeploymentUse.PUBLIC, "CA", "approval_missing"),
        (
            lambda release: approval(release, model_set_id="different-model"),
            DeploymentUse.COMMERCIAL,
            "CA",
            "approval_stale",
        ),
        (
            lambda release: approval(release, deployment_use=DeploymentUse.PRIVATE),
            DeploymentUse.COMMERCIAL,
            "CA",
            "approval_stale",
        ),
        (
            lambda release: approval(release, territories=("US",)),
            DeploymentUse.COMMERCIAL,
            "CA",
            "approval_stale",
        ),
    ],
)
async def test_release_gate_blocks_missing_or_stale_evidence(
    approval_factory: object,
    use: DeploymentUse,
    territory: str,
    expected_code: str,
) -> None:
    """A changed model, use, or territory requires renewed approval."""
    release = manifest()
    candidate = approval_factory(release)  # type: ignore[operator]
    gate = ReleaseAssuranceGate(FakeClock())

    with pytest.raises(ReleaseAssuranceError) as captured:
        await gate.evaluate(release, candidate, use, territory)
    assert captured.value.code == expected_code


def test_license_approval_rejects_invalid_window_and_territory() -> None:
    """Ambiguous evidence time windows and territory codes fail at the boundary."""
    release = manifest()
    base = approval(release)

    with pytest.raises(ValidationError):
        LicenseApproval.model_validate(
            {
                **base.model_dump(),
                "expires_at": base.approved_at - timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError):
        LicenseApproval.model_validate({**base.model_dump(), "territories": ["can"]})
