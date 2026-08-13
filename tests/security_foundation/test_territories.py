"""Deployment-territory classification tests."""

from __future__ import annotations

from tkr_cloud_video.security.territories import (
    EUROPEAN_UNION_MEMBERS,
    LICENSE_EXCLUDED_TERRITORIES,
    license_permitted_territories,
    review_placement,
    territory_for,
)

# A registry of one, injected so each case drives an exact population.
SINGLE = {"CA-MTL-1": "CA"}


def test_registered_region_resolves_to_its_territory() -> None:
    """A registered region reports the country it sits in."""
    assert territory_for("CA-MTL-1", SINGLE) == "CA"


def test_unregistered_region_resolves_to_none() -> None:
    """An unknown region is not guessed from its identifier."""
    assert territory_for("XX-ZZ-9", SINGLE) is None


def test_region_identifiers_are_not_parsed_for_territory() -> None:
    """A city or state segment never stands in for a country code."""
    # MTL is a city and TX a state; only the registry knows the territory.
    assert territory_for("CA-MTL-1") == "CA"
    assert territory_for("US-TX-1") == "US"
    assert territory_for("EUR-IS-1") == "IS"


def test_unregistered_region_is_reported_not_permitted() -> None:
    """A region absent from the registry is surfaced rather than passing."""
    review = review_placement(["CA-MTL-1", "XX-ZZ-9"], ["CA"], SINGLE)
    assert review.permitted == ("CA-MTL-1",)
    assert review.unmapped == ("XX-ZZ-9",)
    assert not review.approved


def test_region_outside_the_grant_is_excluded() -> None:
    """A registered region whose territory is ungranted is excluded."""
    registry = {"CA-MTL-1": "CA", "US-NE-1": "US"}
    review = review_placement(["CA-MTL-1", "US-NE-1"], ["CA"], registry)
    assert review.permitted == ("CA-MTL-1",)
    assert review.excluded == ("US-NE-1",)
    assert not review.approved


def test_placement_is_approved_only_when_every_region_is_permitted() -> None:
    """Approval requires no excluded and no unmapped region."""
    review = review_placement(["CA-MTL-1"], ["CA"], SINGLE)
    assert review.approved


def test_license_excludes_the_union_the_uk_korea_and_the_united_states() -> None:
    """The exclusions match the license's named territories."""
    for territory in ("GB", "KR", "US"):
        assert territory in LICENSE_EXCLUDED_TERRITORIES
    for member in EUROPEAN_UNION_MEMBERS:
        assert member in LICENSE_EXCLUDED_TERRITORIES


def test_iceland_and_norway_are_outside_the_union_exclusion() -> None:
    """EEA states that are not Union members are not excluded by membership."""
    assert "IS" not in EUROPEAN_UNION_MEMBERS
    assert "NO" not in EUROPEAN_UNION_MEMBERS
    assert "IS" not in LICENSE_EXCLUDED_TERRITORIES
    assert "NO" not in LICENSE_EXCLUDED_TERRITORIES


def test_netherlands_is_excluded_as_a_union_member() -> None:
    """A Union member is excluded even though its region prefix differs."""
    assert territory_for("EU-NL-1") == "NL"
    assert "NL" in LICENSE_EXCLUDED_TERRITORIES


def test_permitted_territories_exclude_the_named_exclusions() -> None:
    """The permitted set reports registry territories outside the exclusions."""
    registry = {"CA-MTL-1": "CA", "US-NE-1": "US", "EUR-IS-1": "IS"}
    assert license_permitted_territories(registry) == ("CA", "IS")
