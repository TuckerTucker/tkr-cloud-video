"""Authenticated safe health response policy."""

from __future__ import annotations

from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.runtime.lifecycle import WorkerLifecycle


def health_response(
    lifecycle: WorkerLifecycle,
    release_id: str,
    supplied_token: str,
    expected_token: str,
) -> dict[str, str | bool]:
    """Return safe status only to an authenticated probe."""
    if not supplied_token or supplied_token != expected_token:
        raise AppError("health_unauthorized", "Health probe is unauthorized.")
    return {
        "release_id": release_id,
        "state": lifecycle.state.value,
        "ready": lifecycle.ready,
        "live": lifecycle.live,
    }
