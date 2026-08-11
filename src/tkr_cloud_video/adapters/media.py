"""Exact ffprobe media inspection adapters."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.jobs.media_validation import MediaInfo


class FfprobeMediaInspector:
    """Inspect one caller-selected path with fixed ffprobe arguments."""

    def __init__(
        self,
        executable: str = "/usr/bin/ffprobe",
        timeout_seconds: float = 30,
        maximum_output_bytes: int = 1024 * 1024,
    ) -> None:
        """Initialize fixed executable and safety bounds."""
        if not executable.startswith("/") or timeout_seconds <= 0:
            raise ValueError("ffprobe configuration is invalid")
        if maximum_output_bytes < 1:
            raise ValueError("ffprobe output bound must be positive")
        self._executable = executable
        self._timeout = timeout_seconds
        self._maximum_output = maximum_output_bytes

    def inspect(self, path: Path) -> MediaInfo:
        """Return validated video evidence without directory discovery."""
        payload = self._probe(path)
        streams = payload.get("streams", [])
        file_format = payload.get("format", {})
        if not isinstance(streams, list) or not isinstance(file_format, dict):
            raise _invalid_media()
        video = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            None,
        )
        if video is None:
            return MediaInfo(0, 0, 0, "", False)
        duration = _float_value(video.get("duration") or file_format.get("duration"))
        width, height = video.get("width"), video.get("height")
        codec = video.get("codec_name")
        if (
            not isinstance(width, int)
            or not isinstance(height, int)
            or not isinstance(codec, str)
        ):
            raise _invalid_media()
        return MediaInfo(duration, width, height, codec, True)

    def inspect_input(self, path: Path) -> tuple[str, int, int]:
        """Return an allowlisted MIME type and dimensions for staged input."""
        payload = self._probe(path)
        streams = payload.get("streams", [])
        file_format = payload.get("format", {})
        if not isinstance(streams, list) or not isinstance(file_format, dict):
            raise _invalid_media()
        video = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            None,
        )
        if video is None:
            raise _invalid_media()
        width, height = video.get("width"), video.get("height")
        codec, format_name = video.get("codec_name"), file_format.get("format_name", "")
        if not isinstance(width, int) or not isinstance(height, int):
            raise _invalid_media()
        media_type = _media_type(codec, format_name)
        return media_type, width, height

    def _probe(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise AppError("media_missing", "Media file does not exist.")
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and flags.
                (
                    self._executable,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ),
                check=False,
                capture_output=True,
                timeout=self._timeout,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AppError(
                "ffprobe_failed",
                "Media inspection failed.",
                retryable=isinstance(error, subprocess.TimeoutExpired),
                cause=error,
            ) from error
        if result.returncode != 0 or len(result.stdout) > self._maximum_output:
            raise _invalid_media()
        try:
            value = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AppError(
                "ffprobe_response_invalid",
                "Media inspection returned invalid metadata.",
                cause=error,
            ) from error
        if not isinstance(value, dict):
            raise _invalid_media()
        return value


class FfprobeInputInspector:
    """InputMediaInspector view over the shared ffprobe parser."""

    def __init__(self, inspector: FfprobeMediaInspector) -> None:
        """Initialize with an explicitly shared media inspector."""
        self._inspector = inspector

    def inspect(self, path: Path) -> tuple[str, int, int]:
        """Return staged-input MIME and dimensions."""
        return self._inspector.inspect_input(path)


def _float_value(value: object) -> float:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _invalid_media()
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise _invalid_media() from error


def _media_type(codec: object, format_name: object) -> str:
    if codec == "mjpeg":
        return "image/jpeg"
    if codec == "png":
        return "image/png"
    if isinstance(format_name, str) and "mp4" in format_name.split(","):
        return "video/mp4"
    raise _invalid_media()


def _invalid_media() -> AppError:
    return AppError("media_invalid", "Media inspection did not prove valid media.")
