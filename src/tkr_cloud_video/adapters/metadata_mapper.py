"""Rclone metadata-mapper protocol for allowlisted publication metadata."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from typing import Any, TextIO

MAXIMUM_MAPPER_INPUT_CHARACTERS = 65_536
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def allowlisted_metadata(
    payload: Mapping[str, Any], sha256: str
) -> dict[str, dict[str, str]]:
    """Replace all untrusted source metadata with one approved digest.

    Args:
        payload: Rclone's source-object metadata envelope.
        sha256: Operator-reviewed artifact digest.

    Returns:
        A metadata envelope containing only the SHA-256 digest.

    Raises:
        ValueError: If the mapper input is not an object.

    """
    if not isinstance(payload, Mapping):
        raise ValueError("metadata mapper input must be an object")
    if SHA256_PATTERN.fullmatch(sha256) is None:
        raise ValueError("metadata mapper digest must be canonical SHA-256")
    return {"Metadata": {"sha256": sha256}}


def run(input_stream: TextIO, output_stream: TextIO, sha256: str) -> None:
    """Execute one bounded metadata-mapper exchange.

    Args:
        input_stream: JSON protocol input supplied by rclone.
        output_stream: JSON protocol output consumed by rclone.
        sha256: Operator-reviewed artifact digest.

    Raises:
        ValueError: If the input is oversized or malformed.

    """
    content = input_stream.read(MAXIMUM_MAPPER_INPUT_CHARACTERS + 1)
    if len(content) > MAXIMUM_MAPPER_INPUT_CHARACTERS:
        raise ValueError("metadata mapper input exceeds its bound")
    payload = json.loads(content)
    json.dump(
        allowlisted_metadata(payload, sha256),
        output_stream,
        separators=(",", ":"),
    )


def main() -> int:
    """Run the rclone metadata-mapper protocol without diagnostic output."""
    if len(sys.argv) != 2:
        raise ValueError("metadata mapper requires one digest")
    run(sys.stdin, sys.stdout, sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
