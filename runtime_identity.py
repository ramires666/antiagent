"""Immutable, injectable runtime identity and drift detection helpers.

The module intentionally uses filesystem metadata instead of hashing executable
contents.  This keeps identity capture bounded regardless of binary size while
still detecting path changes, replacements, and ordinary in-place updates.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable, Literal, TypeAlias


DEFAULT_PACKAGE_NAME = "antiagent-mcp"
DEFAULT_SCHEMA_REVISION = "2"

CliState: TypeAlias = Literal["ready", "missing", "unreadable"]
DriftReason: TypeAlias = Literal[
    "mcp_process_drift",
    "package_version_drift",
    "schema_revision_drift",
    "cli_availability_drift",
    "cli_path_drift",
    "cli_binary_drift",
    "cli_version_drift",
]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


_MCP_PROCESS_STARTED_AT = _utc_timestamp()


@dataclass(frozen=True)
class BinaryIdentity:
    """Bounded stat-based identity for one resolved executable."""

    device: int
    inode: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "BinaryIdentity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            size_bytes=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
        )


@dataclass(frozen=True)
class RuntimeIdentity:
    """One immutable observation of the MCP process and selected CLI."""

    package_name: str
    package_version: str | None
    schema_revision: str
    mcp_process_pid: int
    mcp_process_started_at: str
    cli_state: CliState
    cli_executable: str | None
    cli_binary_identity: BinaryIdentity | None
    cli_version: str | None


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Comparison of an immutable process baseline with a fresh observation."""

    baseline: RuntimeIdentity
    observed: RuntimeIdentity
    drift_reasons: tuple[DriftReason, ...]

    @property
    def stale(self) -> bool:
        return bool(self.drift_reasons)

    @property
    def error_type(self) -> Literal["stale_runtime_snapshot"] | None:
        return "stale_runtime_snapshot" if self.stale else None

    @property
    def safe_reason(self) -> str | None:
        """Return categorical diagnostics without paths, versions, or env data."""
        return ",".join(self.drift_reasons) if self.drift_reasons else None


def installed_package_version(package_name: str = DEFAULT_PACKAGE_NAME) -> str | None:
    """Return installed distribution metadata without inventing a fallback."""
    try:
        value = metadata.version(package_name)
    except (metadata.PackageNotFoundError, OSError, TypeError, ValueError):
        return None
    value = value.strip()
    return value or None


def _default_package_version_probe() -> str | None:
    return installed_package_version()


def _default_pid_probe() -> int:
    return os.getpid()


def _default_started_at_probe() -> str:
    # This is the module-load marker for the current MCP process.  Callers may
    # inject a platform-specific OS process start-time probe when available.
    return _MCP_PROCESS_STARTED_AT


def _default_stat_probe(path: Path) -> os.stat_result:
    return path.stat()


@dataclass(frozen=True)
class RuntimeProbes:
    """All external observations needed by :func:`capture_runtime_identity`."""

    resolve_cli: Callable[[], str | os.PathLike[str] | None]
    probe_cli_version: Callable[[Path], str | None]
    probe_package_version: Callable[[], str | None] = _default_package_version_probe
    probe_mcp_pid: Callable[[], int] = _default_pid_probe
    probe_mcp_started_at: Callable[[], str] = _default_started_at_probe
    probe_cli_stat: Callable[[Path], os.stat_result] = _default_stat_probe


def _clean_optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def capture_runtime_identity(
    probes: RuntimeProbes,
    *,
    schema_revision: str = DEFAULT_SCHEMA_REVISION,
    package_name: str = DEFAULT_PACKAGE_NAME,
) -> RuntimeIdentity:
    """Capture one bounded identity, converting probe failures to safe states."""
    if not isinstance(schema_revision, str) or not schema_revision.strip():
        raise ValueError("schema_revision must be a non-empty string")
    if not isinstance(package_name, str) or not package_name.strip():
        raise ValueError("package_name must be a non-empty string")

    try:
        pid = probes.probe_mcp_pid()
    except Exception:
        pid = os.getpid()
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        pid = os.getpid()

    try:
        started_at = probes.probe_mcp_started_at()
    except Exception:
        started_at = _MCP_PROCESS_STARTED_AT
    started_at = _clean_optional_text(started_at) or _MCP_PROCESS_STARTED_AT

    try:
        package_version = _clean_optional_text(probes.probe_package_version())
    except Exception:
        package_version = None

    cli_state: CliState = "missing"
    cli_executable: str | None = None
    binary_identity: BinaryIdentity | None = None
    cli_version: str | None = None

    try:
        candidate = probes.resolve_cli()
    except Exception:
        candidate = None
        cli_state = "unreadable"

    if candidate is not None:
        try:
            resolved = Path(candidate).expanduser().resolve(strict=True)
            if not resolved.is_file():
                raise FileNotFoundError
            cli_executable = str(resolved)
            binary_identity = BinaryIdentity.from_stat(probes.probe_cli_stat(resolved))
            cli_state = "ready"
        except FileNotFoundError:
            cli_state = "missing"
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            cli_state = "unreadable"

    if cli_state == "ready" and cli_executable is not None:
        try:
            cli_version = _clean_optional_text(
                probes.probe_cli_version(Path(cli_executable))
            )
        except Exception:
            cli_version = None

    return RuntimeIdentity(
        package_name=package_name.strip(),
        package_version=package_version,
        schema_revision=schema_revision.strip(),
        mcp_process_pid=pid,
        mcp_process_started_at=started_at,
        cli_state=cli_state,
        cli_executable=cli_executable,
        cli_binary_identity=binary_identity,
        cli_version=cli_version,
    )


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


def compare_runtime_identities(
    baseline: RuntimeIdentity, observed: RuntimeIdentity
) -> RuntimeSnapshot:
    """Compare pre/post identities and return ordered, categorical drift."""
    reasons: list[DriftReason] = []
    if (
        baseline.mcp_process_pid != observed.mcp_process_pid
        or baseline.mcp_process_started_at != observed.mcp_process_started_at
    ):
        reasons.append("mcp_process_drift")
    if (
        baseline.package_name != observed.package_name
        or baseline.package_version != observed.package_version
    ):
        reasons.append("package_version_drift")
    if baseline.schema_revision != observed.schema_revision:
        reasons.append("schema_revision_drift")

    if baseline.cli_state != observed.cli_state:
        reasons.append("cli_availability_drift")
    elif baseline.cli_state == "ready":
        assert baseline.cli_executable is not None
        assert observed.cli_executable is not None
        if not _same_path(baseline.cli_executable, observed.cli_executable):
            reasons.append("cli_path_drift")
        else:
            if baseline.cli_binary_identity != observed.cli_binary_identity:
                reasons.append("cli_binary_drift")
            if baseline.cli_version != observed.cli_version:
                reasons.append("cli_version_drift")

    return RuntimeSnapshot(baseline, observed, tuple(reasons))


class RuntimeSnapshotGuard:
    """Thread-safe, first-observation-wins process snapshot guard."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._baseline: RuntimeIdentity | None = None

    def observe(
        self, identity_factory: Callable[[], RuntimeIdentity]
    ) -> RuntimeSnapshot:
        """Capture under the guard lock and compare with the process baseline."""
        with self._lock:
            observed = identity_factory()
            if not isinstance(observed, RuntimeIdentity):
                raise TypeError("identity_factory must return RuntimeIdentity")
            if self._baseline is None:
                self._baseline = observed
            return compare_runtime_identities(self._baseline, observed)

    def observe_identity(self, identity: RuntimeIdentity) -> RuntimeSnapshot:
        return self.observe(lambda: identity)

    def baseline(self) -> RuntimeIdentity | None:
        with self._lock:
            return self._baseline

    def _reset_for_tests(self) -> None:
        """Clear the baseline; intentionally private outside unit tests."""
        with self._lock:
            self._baseline = None


_PROCESS_RUNTIME_GUARD = RuntimeSnapshotGuard()


def guard_process_runtime(
    identity_factory: Callable[[], RuntimeIdentity],
) -> RuntimeSnapshot:
    """Compare an observation with the process-wide first identity."""
    return _PROCESS_RUNTIME_GUARD.observe(identity_factory)


def process_runtime_baseline() -> RuntimeIdentity | None:
    return _PROCESS_RUNTIME_GUARD.baseline()


def _reset_process_runtime_guard_for_tests() -> None:
    _PROCESS_RUNTIME_GUARD._reset_for_tests()
