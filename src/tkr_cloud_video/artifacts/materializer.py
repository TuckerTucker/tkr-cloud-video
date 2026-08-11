"""Safe atomic ComfyUI model-link materialization."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from tkr_cloud_video.security.validation import RelativeDestination, contained_path


class ModelMaterializer:
    """Creates only managed symlinks beneath an assigned model root."""

    def __init__(self, model_root: Path) -> None:
        """Initialize with a server-owned existing model root."""
        self._model_root = model_root

    def materialize(self, blob: Path, destination: RelativeDestination) -> Path:
        """Atomically install an idempotent symlink to a verified cache blob."""
        target = contained_path(self._model_root, destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.is_symlink() and target.resolve() == blob.resolve():
            return target
        if target.exists() and not target.is_symlink():
            raise FileExistsError("model destination is not a managed symlink")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.link")
        try:
            temporary.symlink_to(blob.resolve())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target
