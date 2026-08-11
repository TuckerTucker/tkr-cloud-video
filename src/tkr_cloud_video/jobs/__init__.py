"""Validated job intake and deterministic ComfyUI execution."""

from tkr_cloud_video.jobs.contracts import GenerationRequest, parse_generation_request

__all__ = ["GenerationRequest", "parse_generation_request"]
