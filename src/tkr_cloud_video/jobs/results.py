"""Exact prompt-owned local result validation and metadata."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.media_validation import MediaInfo, MediaInspector
from tkr_cloud_video.security.validation import Sha256Digest


@dataclass(frozen=True, slots=True)
class LocalResult:
    """Upload-ready exact local result and reproducibility identity."""

    job_id: str
    attempt_id: str
    media_path: Path
    size_bytes: int
    sha256: Sha256Digest
    media: MediaInfo
    workflow_digest: Sha256Digest
    model_set_id: str
    request_hash: Sha256Digest


class ResultValidator:
    """Validates only output paths attributed by the current prompt history."""

    def __init__(self, inspector: MediaInspector) -> None:
        """Initialize with an injected ffprobe-like inspector."""
        self._inspector = inspector

    def validate(
        self,
        output_root: Path,
        prompt_outputs: tuple[str, ...],
        *,
        job_id: str,
        attempt_id: str,
        workflow_digest: Sha256Digest,
        model_set_id: str,
        request_hash: Sha256Digest,
    ) -> LocalResult:
        """Require exactly one contained valid video owned by this prompt."""
        if len(prompt_outputs) != 1:
            raise AppError(
                "result_set_invalid", "Prompt must produce exactly one video."
            )
        root = output_root.resolve()
        candidate = (root / prompt_outputs[0]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise AppError(
                "result_path_escape", "Prompt output escapes its root.", cause=error
            ) from error
        if not candidate.is_file():
            raise AppError("result_missing", "Prompt output video is missing.")
        media = self._inspector.inspect(candidate)
        if (
            not media.has_video
            or media.duration_seconds <= 0
            or media.width < 1
            or media.height < 1
        ):
            raise AppError(
                "result_media_invalid", "Prompt output is not a valid video."
            )
        content = candidate.read_bytes()
        return LocalResult(
            job_id,
            attempt_id,
            candidate,
            len(content),
            Sha256Digest(hashlib.sha256(content).hexdigest()),
            media,
            workflow_digest,
            model_set_id,
            request_hash,
        )
