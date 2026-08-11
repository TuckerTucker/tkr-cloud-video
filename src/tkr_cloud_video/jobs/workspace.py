"""Server-owned contained per-attempt workspaces."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tkr_cloud_video.jobs.identity import JobIdentity
from tkr_cloud_video.security.validation import RelativeDestination, contained_path


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    """Contained paths whose names come only from server-owned identities."""

    root: Path
    inputs: Path
    outputs: Path
    metadata: Path


class WorkspaceManager:
    """Allocates and removes exact attempt workspaces without directory scans."""

    def __init__(self, root: Path) -> None:
        """Initialize with an existing server-owned workspace root."""
        self._root = root

    def allocate(self, identity: JobIdentity) -> JobWorkspace:
        """Create a private, contained workspace for one attempt."""
        relative = RelativeDestination(f"{identity.job_id}/{identity.attempt_id}")
        root = contained_path(self._root, relative)
        inputs, outputs, metadata = (
            root / name for name in ("inputs", "outputs", "metadata")
        )
        for path in (inputs, outputs, metadata):
            path.mkdir(parents=True, exist_ok=False, mode=0o700)
        return JobWorkspace(root, inputs, outputs, metadata)

    def cleanup(self, workspace: JobWorkspace) -> None:
        """Remove only the allocated attempt workspace."""
        workspace.root.relative_to(self._root.resolve())
        job_root = workspace.root.parent
        shutil.rmtree(workspace.root)
        try:
            job_root.rmdir()
        except OSError:
            # Another attempt owns the remaining job directory content.
            return
