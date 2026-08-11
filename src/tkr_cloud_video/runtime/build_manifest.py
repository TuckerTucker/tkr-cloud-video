"""Machine-readable immutable worker build identity."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from tkr_cloud_video.security.validation import Sha256Digest


class WorkerBuildManifest(BaseModel):
    """Sanitized build inputs pinned by digest or immutable revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "1"
    image_digest: str
    python_lock_digest: str
    comfyui_revision: str
    custom_node_revisions: dict[str, str]
    tool_versions: dict[str, str]
    built_at: datetime

    @field_validator("image_digest", "python_lock_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        """Accept bare or OCI-prefixed SHA-256 identities."""
        digest = value.removeprefix("sha256:")
        Sha256Digest(digest)
        return value

    @field_validator("built_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        """Require timezone-aware build time."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("built_at must include a timezone")
        return value

    def digest(self) -> Sha256Digest:
        """Return deterministic manifest identity."""
        content = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return Sha256Digest(hashlib.sha256(content).hexdigest())
