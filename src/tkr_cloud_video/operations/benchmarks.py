"""Reproducible cold/warm worker benchmark evidence."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkRun(BaseModel):
    """Measured immutable workload result for one cache condition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    region: str
    manifest_digest: str
    source_date: date
    cold: bool
    startup_seconds: float = Field(ge=0)
    hydration_bytes: int = Field(ge=0)
    generation_seconds: float = Field(ge=0)
    upload_bytes: int = Field(ge=0)
    request_count: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    errors: int = Field(ge=0)


class BenchmarkComparison(BaseModel):
    """Comparable cold and warm measurements for one exact workload."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cold: BenchmarkRun
    warm: BenchmarkRun

    def warm_downloads_zero(self) -> bool:
        """Return the explicit warm-cache acceptance result."""
        return self.warm.hydration_bytes == 0
