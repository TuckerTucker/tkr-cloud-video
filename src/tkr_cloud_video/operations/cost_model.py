"""Dated unit-explicit object-storage provider cost model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ProviderPricing:
    """Dated official standard-storage pricing inputs in decimal USD."""

    provider: str
    source_url: str
    source_date: date
    storage_per_gb_month: Decimal
    egress_per_gb: Decimal
    free_storage_gb_month: Decimal
    free_egress_multiple: Decimal | None
    write_per_million: Decimal
    read_per_million: Decimal
    free_write_requests: int
    free_read_requests: int


@dataclass(frozen=True, slots=True)
class MonthlyUsage:
    """Measured monthly storage, transfer, and operation volumes."""

    storage_gb_month: Decimal
    egress_gb: Decimal
    write_requests: int
    read_requests: int

    def __post_init__(self) -> None:
        """Reject nonsensical measurements before calculating a report."""
        values = (self.storage_gb_month, self.egress_gb)
        if any(value < 0 for value in values):
            raise ValueError("usage measurements cannot be negative")
        if self.write_requests < 0 or self.read_requests < 0:
            raise ValueError("request counts cannot be negative")


B2_PRICING = ProviderPricing(
    "Backblaze B2",
    "https://www.backblaze.com/cloud-storage/pricing",
    date(2026, 8, 10),
    Decimal("0.00695"),
    Decimal("0.01"),
    Decimal("10"),
    Decimal("3"),
    Decimal("0"),
    Decimal("0"),
    0,
    0,
)
R2_PRICING = ProviderPricing(
    "Cloudflare R2 Standard",
    "https://developers.cloudflare.com/r2/pricing/",
    date(2026, 8, 10),
    Decimal("0.015"),
    Decimal("0"),
    Decimal("10"),
    None,
    Decimal("4.50"),
    Decimal("0.36"),
    1_000_000,
    10_000_000,
)


def monthly_cost(pricing: ProviderPricing, usage: MonthlyUsage) -> Decimal:
    """Calculate cost with explicit free-tier and B2 fair-use treatment."""
    billable_storage = max(
        Decimal(0), usage.storage_gb_month - pricing.free_storage_gb_month
    )
    free_egress = (
        usage.storage_gb_month * pricing.free_egress_multiple
        if pricing.free_egress_multiple is not None
        else Decimal(0)
    )
    billable_egress = max(Decimal(0), usage.egress_gb - free_egress)
    writes = max(0, usage.write_requests - pricing.free_write_requests)
    reads = max(0, usage.read_requests - pricing.free_read_requests)
    return (
        billable_storage * pricing.storage_per_gb_month
        + billable_egress * pricing.egress_per_gb
        + Decimal(writes) / Decimal(1_000_000) * pricing.write_per_million
        + Decimal(reads) / Decimal(1_000_000) * pricing.read_per_million
    ).quantize(Decimal("0.0001"))
