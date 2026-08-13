"""Deployment-territory classification for provider placement regions.

The model license grants a worldwide territory minus named exclusions, so a
placement decision needs the country a provider region sits in. Region
identifiers cannot be parsed for that: the second segment is a city in
``CA-MTL-1``, a state in ``US-TX-1``, and a country only in ``EUR-IS-1``. The
mapping is therefore an explicit registry, and a region absent from it is
denied and reported rather than guessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

# The 27 member states, needed because the license excludes the Union itself
# while Iceland and Norway sit outside it.
EUROPEAN_UNION_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)

# MiniMax H3 Community License Agreement (2026-08-02): "Excluded Territories"
# means the European Union, the United Kingdom, the Republic of Korea and the
# United States of America.
LICENSE_EXCLUDED_TERRITORIES: Final[frozenset[str]] = (
    EUROPEAN_UNION_MEMBERS | frozenset({"GB", "KR", "US"})
)

# RunPod region identifier to ISO 3166-1 alpha-2 territory.
DATA_CENTRE_TERRITORIES: Final[Mapping[str, str]] = {
    "CA-MTL-1": "CA",
    "CA-MTL-2": "CA",
    "CA-MTL-3": "CA",
    "CA-MTL-4": "CA",
    "US-CA-2": "US",
    "US-DE-1": "US",
    "US-GA-1": "US",
    "US-GA-2": "US",
    "US-IL-1": "US",
    "US-KS-2": "US",
    "US-KS-3": "US",
    "US-MD-1": "US",
    "US-MO-1": "US",
    "US-MO-2": "US",
    "US-NC-1": "US",
    "US-NC-2": "US",
    "US-NE-1": "US",
    "US-TX-1": "US",
    "US-TX-3": "US",
    "US-TX-4": "US",
    "US-WA-1": "US",
    "EU-CZ-1": "CZ",
    "EU-FR-1": "FR",
    "EU-NL-1": "NL",
    "EU-RO-1": "RO",
    "EU-SE-1": "SE",
    "EUR-IS-1": "IS",
    "EUR-IS-2": "IS",
    "EUR-IS-3": "IS",
    "EUR-IS-4": "IS",
    "EUR-NO-1": "NO",
    "EUR-NO-2": "NO",
    "AP-IN-1": "IN",
    "AP-IN-2": "IN",
    "AP-JP-1": "JP",
    "OC-AU-1": "AU",
}


@dataclass(frozen=True, slots=True)
class PlacementReview:
    """Which observed regions a territory grant permits, and which it cannot."""

    permitted: tuple[str, ...]
    excluded: tuple[str, ...]
    unmapped: tuple[str, ...]

    @property
    def approved(self) -> bool:
        """Return whether every observed region is permitted."""
        return not self.excluded and not self.unmapped


def territory_for(
    data_centre_id: str,
    registry: Mapping[str, str] = DATA_CENTRE_TERRITORIES,
) -> str | None:
    """Return the territory for one region, or None when it is not registered.

    Args:
        data_centre_id: Provider region identifier.
        registry: Injected region-to-territory mapping.

    Returns:
        The ISO alpha-2 territory, or None when the region is unregistered.

    """
    return registry.get(data_centre_id)


def review_placement(
    observed: Iterable[str],
    granted_territories: Iterable[str],
    registry: Mapping[str, str] = DATA_CENTRE_TERRITORIES,
) -> PlacementReview:
    """Classify observed regions against the territories a grant covers.

    The observed regions are supplied by the caller rather than fetched here,
    so the decision stays a pure function of what it is handed and an
    unregistered region is reported instead of silently passing.

    Args:
        observed: Region identifiers the deployment may actually place in.
        granted_territories: Territories the license approval covers.
        registry: Injected region-to-territory mapping.

    Returns:
        The permitted, excluded, and unmapped regions.

    """
    granted = frozenset(granted_territories)
    permitted: list[str] = []
    excluded: list[str] = []
    unmapped: list[str] = []
    for region in observed:
        territory = territory_for(region, registry)
        if territory is None:
            unmapped.append(region)
        elif territory in granted:
            permitted.append(region)
        else:
            excluded.append(region)
    return PlacementReview(
        permitted=tuple(permitted),
        excluded=tuple(excluded),
        unmapped=tuple(unmapped),
    )


def license_permitted_territories(
    registry: Mapping[str, str] = DATA_CENTRE_TERRITORIES,
    excluded: Iterable[str] = LICENSE_EXCLUDED_TERRITORIES,
) -> tuple[str, ...]:
    """Return registered territories the license does not exclude.

    This reports what the license permits; it does not approve anything. A
    territory still requires a recorded approval before it may be used.

    Args:
        registry: Injected region-to-territory mapping.
        excluded: Territories the license excludes.

    Returns:
        Sorted territories present in the registry and outside the exclusions.

    """
    forbidden = frozenset(excluded)
    return tuple(sorted({t for t in registry.values() if t not in forbidden}))
