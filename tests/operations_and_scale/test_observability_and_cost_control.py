"""Bounded telemetry, alert, benchmark, and cost-control tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tkr_cloud_video.core.context import OperationContext
from tkr_cloud_video.core.logging import configure_logging
from tkr_cloud_video.operations.alerts import AlertRule
from tkr_cloud_video.operations.benchmarks import BenchmarkComparison, BenchmarkRun
from tkr_cloud_video.operations.cost_model import (
    B2_PRICING,
    R2_PRICING,
    MonthlyUsage,
    monthly_cost,
)
from tkr_cloud_video.operations.events import (
    EventName,
    MetricName,
    Outcome,
    Phase,
    Severity,
)
from tkr_cloud_video.operations.history import OperationalEventHistory
from tkr_cloud_video.operations.logging import OperationalEventEmitter
from tkr_cloud_video.operations.metrics import InMemoryMetrics, TelemetryContractError


def operation_context(correlation_id: str = "correlation-1") -> OperationContext:
    """Build a complete deterministic telemetry context."""
    return OperationContext(
        release_id="release-1",
        worker_id="worker-1",
        correlation_id=correlation_id,
        job_id="job-1",
        attempt_id="attempt-1",
        deadline=datetime(2026, 8, 10, tzinfo=UTC),
    )


def benchmark(*, cold: bool, hydration_bytes: int) -> BenchmarkRun:
    """Build one reproducible workload measurement."""
    return BenchmarkRun(
        run_id="run-cold" if cold else "run-warm",
        region="ca-west-1",
        manifest_digest="a" * 64,
        source_date=date(2026, 8, 10),
        cold=cold,
        startup_seconds=120 if cold else 8,
        hydration_bytes=hydration_bytes,
        generation_seconds=60,
        upload_bytes=1024,
        request_count=4,
        cache_hits=0 if cold else 4,
        errors=0,
    )


def test_correlated_history_preserves_ordered_terminal_transition() -> None:
    """A correlation query reconstructs state changes through termination."""
    history = OperationalEventHistory(capacity=10)
    emitter = OperationalEventEmitter(configure_logging(history))
    context = operation_context()

    emitter.emit(
        EventName.GENERATION,
        context,
        Phase.GENERATION,
        Outcome.STARTED,
    )
    emitter.emit(
        EventName.GENERATION,
        context,
        Phase.GENERATION,
        Outcome.FAILED,
        Severity.CRITICAL,
        duration_ms=5.5,
        error_code="generation_failed",
    )

    events = history.for_correlation("correlation-1")
    assert [event.outcome for event in events] == ["started", "failed"]
    assert events[-1].error_code == "generation_failed"
    assert events[-1].level == "ERROR"
    assert events[-1].safe_context == {"phase": "generation"}


def test_event_history_is_bounded_and_rejects_invalid_capacity() -> None:
    """Retention evicts oldest evidence and cannot be configured unbounded."""
    with pytest.raises(ValueError):
        OperationalEventHistory(capacity=0)

    history = OperationalEventHistory(capacity=1)
    emitter = OperationalEventEmitter(configure_logging(history))
    emitter.emit(
        EventName.BOOT, operation_context("first"), Phase.BOOT, Outcome.STARTED
    )
    emitter.emit(
        EventName.BOOT, operation_context("second"), Phase.BOOT, Outcome.SUCCEEDED
    )

    assert history.for_correlation("first") == ()
    assert len(history.for_correlation("second")) == 1


def test_metrics_enforce_names_labels_values_and_non_negative_samples() -> None:
    """Only the product metric vocabulary and bounded labels are recorded."""
    metrics = InMemoryMetrics()
    labels = {"release_id": "release-1", "phase": "hydration"}
    metrics.increment(MetricName.CACHE_HITS_TOTAL, labels)
    metrics.observe(MetricName.HYDRATION_SECONDS, labels, 1.25)

    assert sum(metrics.counters.values()) == 1
    assert metrics.observations[0][-1] == 1.25
    with pytest.raises(TelemetryContractError):
        metrics.increment(MetricName.ERRORS_TOTAL, {"job_id": "job-1"})
    with pytest.raises(TelemetryContractError):
        metrics.increment(MetricName.ERRORS_TOTAL, {"provider": "x" * 129})
    with pytest.raises(ValueError):
        metrics.increment(MetricName.ERRORS_TOTAL, {}, -1)
    with pytest.raises(ValueError):
        metrics.observe(MetricName.GENERATION_SECONDS, {}, -0.1)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((1.0, 1.0, 1.0), True),
        ((1.0, 0.0, 1.0), False),
        ((1.0, 1.0), False),
    ],
)
def test_alert_requires_consecutive_breaches(
    values: tuple[float, ...], expected: bool
) -> None:
    """A transient recovery remains measurable without causing a noisy page."""
    rule = AlertRule(
        name="checksum-spike",
        metric=MetricName.ERRORS_TOTAL.value,
        threshold=1,
        evaluation_periods=3,
        severity=Severity.CRITICAL,
        phase=Phase.HYDRATION,
        runbook="docs/runbooks/checksum-failure.md",
    )

    assert rule.triggered(values) is expected


def test_alert_rejects_missing_evidence_window_or_external_runbook() -> None:
    """Alert configuration cannot bypass consecutive evidence or procedures."""
    with pytest.raises(ValueError):
        AlertRule("bad", "x", 1, 0, Severity.WARNING, Phase.BOOT, "docs/runbooks/x")
    with pytest.raises(ValueError):
        AlertRule("bad", "x", 1, 1, Severity.WARNING, Phase.BOOT, "https://x")


def test_benchmark_requires_reproducibility_fields_and_zero_warm_downloads() -> None:
    """Cold/warm comparisons carry exact workload identity and byte evidence."""
    comparison = BenchmarkComparison(
        cold=benchmark(cold=True, hydration_bytes=4096),
        warm=benchmark(cold=False, hydration_bytes=0),
    )
    assert comparison.warm_downloads_zero() is True

    payload = benchmark(cold=True, hydration_bytes=1).model_dump()
    del payload["region"]
    with pytest.raises(ValidationError):
        BenchmarkRun.model_validate(payload)


def test_dated_provider_costs_compare_the_same_measured_workload() -> None:
    """B2 and R2 costs use explicit dated decimal units and free tiers."""
    usage = MonthlyUsage(Decimal("1000"), Decimal("4000"), 2_000_000, 20_000_000)

    assert monthly_cost(B2_PRICING, usage) == Decimal("16.8805")
    assert monthly_cost(R2_PRICING, usage) == Decimal("22.9500")
    assert B2_PRICING.source_date == date(2026, 8, 10)
    assert R2_PRICING.source_date == date(2026, 8, 10)
    assert B2_PRICING.source_url.startswith("https://")
    assert R2_PRICING.source_url.startswith("https://")


def test_cost_model_rejects_negative_measurements() -> None:
    """Invalid measurements cannot produce plausible-looking cost evidence."""
    with pytest.raises(ValueError):
        MonthlyUsage(Decimal("-1"), Decimal("0"), 0, 0)
