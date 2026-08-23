"""Reviewed storage deployment constants and the rule that pins them."""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

B2_S3_ENDPOINT: Final[str] = "https://s3.ca-east-006.backblazeb2.com"
B2_S3_REGION: Final[str] = "ca-east-006"


def validate_canadian_b2_endpoint(endpoint: str, region: str) -> None:
    """Reject any endpoint and region pair outside a reviewed Canadian region.

    The pair is checked together rather than separately because either half
    alone is satisfiable while the transfer still leaves the country: a
    Canadian region name with a foreign host, or a Canadian host addressed
    under another region's signature.

    Args:
        endpoint: The S3-compatible base URL, which must carry no path,
            parameters, query, or fragment that could redirect a signed request.
        region: The region the endpoint's hostname must name.

    Raises:
        ValueError: The pair does not describe one reviewed Canadian region.

    """
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not region.startswith("ca-")
        or parsed.hostname != f"s3.{region}.backblazeb2.com"
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "endpoint must match its reviewed Canadian Backblaze S3 region"
        )
