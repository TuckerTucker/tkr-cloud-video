"""Dependency composition and offline project verification."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog
from pydantic import BaseModel, ConfigDict, Field

from tkr_cloud_video.core.clock import Clock
from tkr_cloud_video.core.logging import EventSink, configure_logging
from tkr_cloud_video.core.settings import (
    CoreSettings,
    SettingsSource,
    load_core_settings,
)
from tkr_cloud_video.prompt_authoring.composition import grammar_report

BOUNDARY_DIRECTORIES: Final[tuple[str, ...]] = (
    "_tkr_kit",
    "docs/assurance",
    "docs/briefs",
    "docs/models",
    "docs/patterns",
    "docs/published",
    "docs/runbooks",
    "src/tkr_cloud_video",
    "tests",
)


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Immutable bundle of shared dependencies owned by a composition root."""

    settings: CoreSettings
    clock: Clock
    event_sink: EventSink
    logger: structlog.typing.FilteringBoundLogger


class DoctorResult(BaseModel):
    """Stable safe result from the read-only diagnostic command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str
    context: str
    python: str
    lock_digest: str | None
    core_contracts: bool
    boundary_directories: dict[str, bool]
    prompt_grammar: dict[str, object] = Field(default_factory=dict)
    outcome: str
    errors: tuple[str, ...] = Field(default_factory=tuple)


def build_runtime(
    settings_source: SettingsSource,
    clock: Clock,
    event_sink: EventSink,
) -> RuntimeServices:
    """Build shared runtime services exclusively from injected adapters."""
    settings = load_core_settings(settings_source)
    logger = configure_logging(event_sink, settings.log_level)
    return RuntimeServices(
        settings=settings,
        clock=clock,
        event_sink=event_sink,
        logger=logger,
    )


def find_repository_root(start: Path | None = None) -> Path:
    """Find the contained checkout root from an injected or package path."""
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "_tkr_kit"
        ).is_dir():
            return candidate
    return current


def run_doctor(repository_root: Path | None = None) -> DoctorResult:
    """Validate repository wiring or the installed runtime without provider access."""
    root = find_repository_root(repository_root)
    if repository_root is None and not _is_repository_root(root):
        return run_runtime_doctor(Path.cwd())

    errors: list[str] = []
    if sys.version_info < (3, 11):  # noqa: UP036 - doctor verifies project metadata.
        errors.append("unsupported_python")

    package_root = root / "src" / "tkr_cloud_video"
    core_ok = all(
        path.is_file()
        for path in (
            package_root / "__init__.py",
            package_root / "py.typed",
            package_root / "core" / "__init__.py",
        )
    )
    if not core_ok:
        errors.append("core_contract_missing")

    boundary_status = {
        relative: _is_contained_directory(root, relative)
        for relative in BOUNDARY_DIRECTORIES
    }
    errors.extend(
        f"missing_boundary:{relative}"
        for relative, present in boundary_status.items()
        if not present
    )

    lock_path = root / "uv.lock"
    lock_digest = (
        hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if lock_path.is_file()
        else None
    )
    if lock_digest is None:
        errors.append("lock_missing")

    grammar = grammar_report()
    if not grammar["digest_verified"]:
        errors.append("prompt_grammar_digest_mismatch")

    return DoctorResult(
        package="tkr-cloud-video",
        context="repository",
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        lock_digest=lock_digest,
        core_contracts=core_ok,
        boundary_directories=boundary_status,
        prompt_grammar=grammar,
        outcome="succeeded" if not errors else "failed",
        errors=tuple(errors),
    )


def run_runtime_doctor(
    runtime_root: Path,
    package_root: Path | None = None,
) -> DoctorResult:
    """Validate an installed worker package and its embedded dependency lock."""
    errors: list[str] = []
    if sys.version_info < (3, 11):  # noqa: UP036 - doctor verifies runtime metadata.
        errors.append("unsupported_python")

    installed_package = (package_root or Path(__file__).resolve().parent).resolve()
    core_ok = all(
        path.is_file()
        for path in (
            installed_package / "__init__.py",
            installed_package / "py.typed",
            installed_package / "core" / "__init__.py",
        )
    )
    if not core_ok:
        errors.append("core_contract_missing")

    lock_path = runtime_root.resolve() / "uv.lock"
    lock_digest = (
        hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if lock_path.is_file()
        else None
    )
    if lock_digest is None:
        errors.append("lock_missing")

    return DoctorResult(
        package="tkr-cloud-video",
        context="runtime",
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        lock_digest=lock_digest,
        core_contracts=core_ok,
        boundary_directories={},
        outcome="succeeded" if not errors else "failed",
        errors=tuple(errors),
    )


def _is_repository_root(root: Path) -> bool:
    """Return whether a path contains the complete development checkout markers."""
    return (root / "pyproject.toml").is_file() and (root / "_tkr_kit").is_dir()


def _is_contained_directory(root: Path, relative: str) -> bool:
    """Return whether a relative path resolves to a directory below root."""
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return candidate.is_dir()
