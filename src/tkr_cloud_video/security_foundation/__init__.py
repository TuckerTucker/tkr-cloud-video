"""Security capability composition roots."""

from tkr_cloud_video.security_foundation.composition import (
    SecurityDependencies,
    SecurityServices,
    compose_security_foundation,
)

__all__ = [
    "SecurityDependencies",
    "SecurityServices",
    "compose_security_foundation",
]
