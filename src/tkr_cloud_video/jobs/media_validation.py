"""Injected media inspection contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Required ffprobe-derived video evidence."""

    duration_seconds: float
    width: int
    height: int
    video_codec: str
    has_video: bool


class MediaInspector(Protocol):
    """ffprobe-compatible media inspection port."""

    def inspect(self, path: Path) -> MediaInfo:
        """Inspect one exact media path without directory discovery."""
        ...
