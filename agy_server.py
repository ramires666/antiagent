"""Small MCP stdio adapter for the OAuth-authenticated Antigravity CLI.

The CLI owns authentication (system keyring and Google browser login).  This
wrapper deliberately does not read .env files or API-key environment values.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, cast

from ctypes import wintypes
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, SkipValidation

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from agent_manager import (
    AgentCapacityError,
    AgentSnapshot,
    AgentStore,
    prepare_state_dir,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("antigravity-cli-mcp")
mcp = MCPServer("Antigravity CLI Executor")

EXECUTION_BOUNDARY_ENV = "ANTIAGENT_EXECUTION_BOUNDARY"

ThinkingLevel = Literal["low", "medium", "high"]
Mode = Literal["plan", "accept-edits"]
PayloadMode = Literal["workspace", "prompt_only", "scoped_files"]
THINKING_LEVELS = ("low", "medium", "high")
MODES = ("plan", "accept-edits")
DEFAULT_TIMEOUT_SECONDS = 840
MAX_TIMEOUT_SECONDS = 3600
MAX_RESULT_CHARS = 30_000
TRUNCATION_MARKER = "\n\n[Antigravity result truncated by MCP wrapper]"
MAX_STDOUT_CHARS = 1_000_000
MAX_STDERR_CHARS = 16_384
MAX_GIT_STATUS_BYTES = 1_000_000
MAX_GIT_STATUS_PATH_LENGTH = 4_096
MAX_GIT_STATUS_PATHS = 10_000
MAX_GIT_ROOT_BYTES = 32_768
MAX_PROBE_STDERR_BYTES = 16_384
MAX_VERSION_BYTES = 128
MAX_PROMPT_CHARS = 24_000
MAX_WINDOWS_COMMAND_LINE_UNITS = 32_767
_LOCK_DIRECTORY: Path | None = None
_SAFE_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_AGENT_ID = re.compile(r"^[0-9a-f]{32}$")
_INPUT_CONVERSATION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE
)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
ProgressCallback = Callable[[str], Awaitable[None]]
ErrorType = Literal[
    "invalid_request",
    "path_not_found",
    "path_outside_allowed_root",
    "workspace_not_git",
    "workspace_not_root",
    "git_trust_denied",
    "git_unavailable",
    "cli_unavailable",
    "workspace_lock_timeout",
    "workspace_lock_unavailable",
    "command_line_too_long",
    "spawn_failed",
    "timeout",
    "output_limit",
    "invalid_json",
    "invalid_payload",
    "invalid_response",
    "cli_error",
    "profile_unreadable",
    "profile_not_writable",
    "network_denied",
    "auth_missing",
    "oauth_timeout",
    "permission_denied",
    "policy_denied",
    "no_content",
    "verification_failed",
    "scope_enforcement_unavailable",
    "review_required",
    "review_state_unavailable",
]
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
AgentStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
AgentManagerErrorType = Literal[
    "invalid_request",
    "path_not_found",
    "path_outside_allowed_root",
    "workspace_not_git",
    "workspace_not_root",
    "git_trust_denied",
    "git_unavailable",
    "cli_unavailable",
    "workspace_lock_timeout",
    "workspace_lock_unavailable",
    "command_line_too_long",
    "spawn_failed",
    "timeout",
    "output_limit",
    "invalid_json",
    "invalid_payload",
    "invalid_response",
    "cli_error",
    "profile_unreadable",
    "profile_not_writable",
    "network_denied",
    "auth_missing",
    "oauth_timeout",
    "permission_denied",
    "policy_denied",
    "no_content",
    "verification_failed",
    "scope_enforcement_unavailable",
    "review_required",
    "review_state_unavailable",
    "agent_not_found",
    "invalid_state",
    "state_unavailable",
    "capacity_reached",
]
WorkspaceDiagnostic = Literal[
    "ready",
    "path_not_found",
    "path_outside_allowed_root",
    "workspace_not_git",
    "workspace_not_root",
    "git_trust_denied",
    "git_unavailable",
]
DoctorErrorType = Literal[
    "invalid_request",
    "boundary_unverified",
    "cli_unavailable",
    "state_unavailable",
    "path_not_found",
    "path_outside_allowed_root",
    "workspace_not_git",
    "workspace_not_root",
    "git_trust_denied",
    "git_unavailable",
]
_TERMINAL_AGENT_STATUSES = ("completed", "failed", "interrupted")
MAX_AGENT_WAIT_SECONDS = 60.0
_AGENT_STORE: AgentStore | None = None
_AGENT_TASKS: dict[str, asyncio.Task[None]] = {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass
class RunInfo:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=_timestamp)
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    finished_at: str | None = None
    duration_seconds: float | None = None

    def finish(self) -> None:
        if self.finished_at is None:
            self.finished_at = _timestamp()
            self.duration_seconds = round(time.monotonic() - self._started_monotonic, 3)


@dataclass
class CliRunResult:
    returncode: int | None
    stdout: str
    timed_out: bool
    command_line_too_long: bool = False
    output_limit: bool = False
    stderr: str = ""

    def __iter__(self):
        yield self.returncode
        yield self.stdout
        yield self.timed_out

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return tuple(self) == other
        if isinstance(other, CliRunResult):
            return self.__dict__ == other.__dict__
        return NotImplemented


class GitPreflight(NamedTuple):
    root: Path | None
    error_type: ErrorType | None


class GitStatusSnapshot(NamedTuple):
    entries: dict[str, object]


class BoundedProbeResult(NamedTuple):
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limit: bool = False


class PostflightInfo(NamedTuple):
    preexisting_dirty: bool | None
    worktree_changed: bool | None
    changed_paths: list[str]
    postflight_complete: bool
    requires_review: bool


class ReviewStateError(RuntimeError):
    def __init__(self, error_type: Literal["review_required", "review_state_unavailable"]):
        super().__init__(error_type)
        self.error_type = error_type


class WorkspaceLockError(RuntimeError):
    """Base class for the per-workspace inter-process lock."""


class WorkspaceLockBusy(WorkspaceLockError):
    """The workspace lock is currently held by another process."""


class WorkspaceLockTimeout(WorkspaceLockError):
    """The workspace lock was not acquired before the deadline."""


def _workspace_lock_path(root: Path) -> Path:
    try:
        canonical = os.path.normcase(os.path.realpath(os.fspath(root)))
        lock_directory = (
            _LOCK_DIRECTORY
            if _LOCK_DIRECTORY is not None
            else prepare_state_dir() / "locks"
        )
        lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if lock_directory.is_symlink() or (
            hasattr(lock_directory, "is_junction") and lock_directory.is_junction()
        ) or not lock_directory.is_dir():
            raise OSError("workspace lock directory is not a real directory")
        if os.name != "nt":
            directory_stat = lock_directory.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise OSError("workspace lock directory is not a directory")
            if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
                raise OSError("workspace lock directory is not private")
        digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()
        return lock_directory / f"{digest}.lock"
    except (AttributeError, OSError, TypeError, ValueError):
        # Do not expose a temporary-directory path through the MCP result.
        raise WorkspaceLockError from None


class WorkspaceLock:
    def __init__(self, root: Path):
        self.path = _workspace_lock_path(root)
        self._fd: int | None = None

    def _try_acquire(self) -> None:
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
        except OSError:
            raise WorkspaceLockError from None
        try:
            path_stat = os.lstat(self.path)
            fd_stat = os.fstat(fd)
            if not stat.S_ISREG(path_stat.st_mode) or (
                path_stat.st_dev,
                path_stat.st_ino,
            ) != (fd_stat.st_dev, fd_stat.st_ino):
                raise WorkspaceLockError
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN):
                        raise WorkspaceLockBusy from exc
                    raise WorkspaceLockError from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise WorkspaceLockError from exc
                    raise WorkspaceLockBusy from exc
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._fd = fd

    async def acquire(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                self._try_acquire()
                return
            except WorkspaceLockBusy:
                if timeout <= 0:
                    raise
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise WorkspaceLockTimeout from None
                await asyncio.sleep(min(0.05, remaining))

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


@asynccontextmanager
async def locked_workspace(root: Path, timeout: float):
    lock = WorkspaceLock(root)
    await lock.acquire(timeout)
    try:
        yield
    finally:
        lock.release()


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class AntigravityCliOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["SUCCESS", "ERROR"]
    result: str
    model: str | None
    thinking_level: ThinkingLevel | None
    mode: Mode | None
    usage: dict[str, int | float]
    conversation_id: str | None
    result_truncated: bool
    error_type: ErrorType | None
    exit_code: int | None
    retryable: bool
    run_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    cli_version: str | None
    metadata_complete: bool
    usage_available: bool
    conversation_id_available: bool
    preexisting_dirty: bool | None
    worktree_changed: bool | None
    changed_paths: list[str]
    postflight_complete: bool
    requires_review: bool
    payload_mode: PayloadMode
    file_scope_enforced: bool
    shell_denied: bool


class AntigravityDoctorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks_passed: bool
    cli_available: bool
    cli_version: str | None
    execution_boundary_declared: bool
    state_writable: bool
    workspace_status: WorkspaceDiagnostic
    auth_probe: Literal["unsupported"]
    network_probe: Literal["not_run"]
    oauth_ready: Literal["unknown"]
    error_type: DoctorErrorType | None


class AgentSnapshotOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    parent_agent_id: str | None
    workspace: str
    thinking_level: str
    mode: str
    status: AgentStatus
    cancel_requested: bool
    conversation_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: float
    output: dict[str, Any] | None
    manager_error: str | None


class AgentOperationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    agent: AgentSnapshotOutput | None
    wait_timed_out: bool
    error_type: AgentManagerErrorType | None


class AgentListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    agents: list[AgentSnapshotOutput]
    error_type: AgentManagerErrorType | None


@dataclass(frozen=True)
class PreparedExecution:
    workspace: Path
    prompt: str
    thinking_level: ThinkingLevel
    mode: Mode
    acknowledge_review: bool
    conversation_id: str | None
    expected_marker: str | None
    payload_mode: PayloadMode


def _timeout_seconds() -> int:
    raw = os.environ.get("ANTIGRAVITY_CLI_TIMEOUT_SECONDS", "")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid timeout setting; using default")
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning("Invalid timeout setting; using default")
        return DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_TIMEOUT_SECONDS)


async def _emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress is None:
        return
    try:
        await asyncio.wait_for(progress(message), timeout=1)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("MCP progress notification failed")


async def _progress_heartbeat(progress: ProgressCallback, run_id: str = "") -> None:
    try:
        while True:
            await asyncio.sleep(12)
            await _emit_progress(
                progress, f"run_id={run_id} state=running"
            )
    except asyncio.CancelledError:
        return


def _model_for(level: str) -> str:
    return f"gemini-3.7-flash-{level}"


def _empty_result(
    status: str,
    result: str,
    level: str | None,
    mode: str | None,
    conversation_id: str | None = None,
    usage: dict[str, int | float] | None = None,
    *,
    error_type: ErrorType | None = None,
    exit_code: int | None = None,
    retryable: bool = False,
    run_info: RunInfo | None = None,
    cli_version: str | None = None,
    metadata_complete: bool = False,
    usage_available: bool = False,
    conversation_id_available: bool = False,
    postflight: PostflightInfo | None = None,
    payload_mode: PayloadMode = "workspace",
) -> dict[str, Any]:
    run = run_info or RunInfo()
    run.finish()
    if len(result) <= MAX_RESULT_CHARS:
        safe_result, truncated = result, False
    elif MAX_RESULT_CHARS <= len(TRUNCATION_MARKER):
        safe_result, truncated = TRUNCATION_MARKER[:MAX_RESULT_CHARS], True
    else:
        available = MAX_RESULT_CHARS - len(TRUNCATION_MARKER)
        head_size = (available + 1) // 2
        tail_size = available - head_size
        tail = result[-tail_size:] if tail_size else ""
        safe_result = result[:head_size] + TRUNCATION_MARKER + tail
        truncated = True
    postflight = postflight or PostflightInfo(None, None, [], False, False)
    return {
        "status": status,
        "result": safe_result,
        "model": _model_for(level) if level in THINKING_LEVELS else None,
        "thinking_level": level if level in THINKING_LEVELS else None,
        "mode": mode if mode in MODES else None,
        "usage": usage or {},
        "conversation_id": conversation_id,
        "result_truncated": truncated,
        "error_type": error_type,
        "exit_code": exit_code,
        "retryable": retryable,
        "run_id": run.run_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": run.duration_seconds,
        "cli_version": cli_version,
        "metadata_complete": metadata_complete,
        "usage_available": usage_available,
        "conversation_id_available": conversation_id_available,
        "preexisting_dirty": postflight.preexisting_dirty,
        "worktree_changed": postflight.worktree_changed,
        "changed_paths": postflight.changed_paths,
        "postflight_complete": postflight.postflight_complete,
        "requires_review": postflight.requires_review,
        "payload_mode": payload_mode,
        "file_scope_enforced": False,
        "shell_denied": False,
    }


def _tool_result(result: dict[str, Any]) -> AntigravityCliOutput:
    if result.get("status") != "ERROR":
        return cast(AntigravityCliOutput, result)
    return cast(
        AntigravityCliOutput,
        CallToolResult(
            content=[TextContent(type="text", text=result["result"])],
            structured_content=result,
            is_error=True,
        ),
    )


def _agent_operation_result(
    agent: AgentSnapshot | None = None,
    *,
    wait_timed_out: bool = False,
    error_type: AgentManagerErrorType | None = None,
    message: str = "",
) -> AgentOperationOutput:
    result = {
        "ok": error_type is None,
        "agent": asdict(agent) if agent is not None else None,
        "wait_timed_out": wait_timed_out,
        "error_type": error_type,
    }
    if error_type is None:
        return cast(AgentOperationOutput, result)
    return cast(
        AgentOperationOutput,
        CallToolResult(
            content=[TextContent(type="text", text=message)],
            structured_content=result,
            is_error=True,
        ),
    )


def _agent_list_result(
    agents: list[AgentSnapshot] | None = None,
    *,
    error_type: AgentManagerErrorType | None = None,
    message: str = "",
) -> AgentListOutput:
    result = {
        "ok": error_type is None,
        "agents": [asdict(agent) for agent in agents or []],
        "error_type": error_type,
    }
    if error_type is None:
        return cast(AgentListOutput, result)
    return cast(
        AgentListOutput,
        CallToolResult(
            content=[TextContent(type="text", text=message)],
            structured_content=result,
            is_error=True,
        ),
    )


def _get_agent_store() -> AgentStore:
    global _AGENT_STORE
    if _AGENT_STORE is None:
        _AGENT_STORE = AgentStore()
    return _AGENT_STORE


def _reconcile_agent_store() -> AgentStore:
    store = _get_agent_store()
    store.reconcile_stale(MAX_TIMEOUT_SECONDS + 60)
    return store


def _valid_agent_id(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_AGENT_ID.fullmatch(value) else None


def _snapshot_in_scope(snapshot: AgentSnapshot) -> bool:
    try:
        return Path(snapshot.workspace).resolve().is_relative_to(Path.cwd().resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_executable(name: str) -> Path | None:
    if os.name != "nt":
        found = shutil.which(name)
        if found:
            path = Path(found)
            if path.is_file():
                return path.resolve()
        return None

    # Do not let Windows search the current directory before PATH entries.
    path_entries = [
        entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry
    ]
    extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    for entry in path_entries:
        directory = Path(entry)
        if not directory.is_absolute():
            continue
        candidates = [directory / name]
        if not Path(name).suffix:
            candidates.extend(
                directory / f"{name}{extension}" for extension in extensions
            )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    return None


def _resolve_cli() -> Path | None:
    configured = os.environ.get("ANTIGRAVITY_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute() and path.is_file():
            return path.resolve()

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        path = Path(local_app_data) / "agy" / "bin" / "agy.exe"
        if path.is_absolute() and path.is_file():
            return path.resolve()

    path = _resolve_executable("agy")
    if path is not None:
        return path
    return None


def _run_bounded_probe(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
) -> BoundedProbeResult:
    """Run a small local probe without retaining unbounded child output."""
    if stdout_limit < 0 or stderr_limit < 0 or timeout_seconds <= 0:
        return BoundedProbeResult(None, b"", b"")

    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": _child_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(argv, **kwargs)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return BoundedProbeResult(None, b"", b"")

    job = _create_windows_job(process.pid)
    streams = (process.stdout, process.stderr)
    limits = (stdout_limit, stderr_limit)
    output = [b"", b""]
    reader_failed = [False, False]
    exceeded = threading.Event()

    def read_stream(index: int) -> None:
        stream = streams[index]
        if stream is None:
            reader_failed[index] = True
            return
        chunks: list[bytes] = []
        size = 0
        try:
            while True:
                remaining = limits[index] - size
                data = stream.read(min(65_536, max(1, remaining + 1)))
                if not data:
                    break
                if len(data) > remaining:
                    exceeded.set()
                    return
                chunks.append(data)
                size += len(data)
            output[index] = b"".join(chunks)
        except (OSError, ValueError):
            reader_failed[index] = True
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=read_stream, args=(index,), daemon=True)
        for index in range(2)
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None and not exceeded.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass

        if exceeded.is_set() or timed_out:
            _kill_process_tree(process)
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass

        for reader in readers:
            reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            _kill_process_tree(process)
            for stream in streams:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for reader in readers:
                reader.join(timeout=0.5)
            reader_failed = [True, True]
    finally:
        _close_windows_job(job)

    output_limit = exceeded.is_set()
    if timed_out or output_limit or any(reader_failed):
        output = [b"", b""]
    return BoundedProbeResult(
        process.returncode,
        output[0],
        output[1],
        timed_out=timed_out,
        output_limit=output_limit,
    )


def _git_preflight(directory: str | Path | None = None) -> GitPreflight:
    try:
        base = Path.cwd().resolve()
        cwd = base if directory is None else Path(directory)
        if not cwd.is_absolute():
            cwd = base / cwd
        cwd = cwd.resolve()
        if directory is not None and not cwd.is_relative_to(base):
            return GitPreflight(None, "path_outside_allowed_root")
        if directory is not None and not cwd.is_dir():
            return GitPreflight(None, "path_not_found")
        git = _resolve_executable("git")
        if git is None:
            return GitPreflight(None, "git_unavailable")
        completed = _run_bounded_probe(
            [str(git), "rev-parse", "--show-toplevel"],
            cwd=cwd,
            timeout_seconds=5,
            stdout_limit=MAX_GIT_ROOT_BYTES,
            stderr_limit=MAX_PROBE_STDERR_BYTES,
        )
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
        return GitPreflight(None, "git_unavailable")

    if completed.timed_out or completed.output_limit or completed.returncode is None:
        return GitPreflight(None, "git_unavailable")
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").lower()
        if "dubious ownership" in stderr or "safe.directory" in stderr:
            return GitPreflight(None, "git_trust_denied")
        return GitPreflight(None, "workspace_not_git")
    try:
        raw_root = completed.stdout.decode("utf-8").strip()
        if not raw_root:
            return GitPreflight(None, "workspace_not_git")
        root = Path(raw_root).resolve()
    except (AttributeError, OSError, ValueError):
        return GitPreflight(None, "workspace_not_git")
    if root != cwd:
        return GitPreflight(None, "workspace_not_root")
    return GitPreflight(cwd, None)


def _git_root(directory: str | Path | None = None) -> Path | None:
    """Compatibility helper returning only a valid repository root."""
    return _git_preflight(directory).root


def _parse_git_status(raw: bytes) -> GitStatusSnapshot | None:
    if raw and not raw.endswith(b"\0"):
        return None
    entries: dict[str, str] = {}
    fields = raw.split(b"\0")
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            return None
        try:
            status = record[:2].decode("ascii")
            path = record[3:].decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not path or len(path) > MAX_GIT_STATUS_PATH_LENGTH:
            return None
        entries[path] = status
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                return None
            try:
                previous_path = fields[index].decode("utf-8")
            except UnicodeDecodeError:
                return None
            index += 1
            if not previous_path or len(previous_path) > MAX_GIT_STATUS_PATH_LENGTH:
                return None
            entries[previous_path] = status
        if len(entries) > MAX_GIT_STATUS_PATHS:
            return None
    return GitStatusSnapshot(entries)


def _git_status_snapshot(workspace: Path) -> GitStatusSnapshot | None:
    """Return bounded porcelain status without exposing command output."""
    git = _resolve_executable("git")
    if git is None:
        return None
    try:
        scratch_directory = prepare_state_dir() / "scratch"
        scratch_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if scratch_directory.is_symlink() or (
            hasattr(scratch_directory, "is_junction")
            and scratch_directory.is_junction()
        ) or not scratch_directory.is_dir():
            return None
        if os.name != "nt":
            directory_stat = scratch_directory.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode):
                return None
            if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
                return None
        # Review state must stay usable when the sandbox cannot access system TEMP.
        with tempfile.TemporaryFile(dir=scratch_directory) as output:
            completed = subprocess.run(
                [str(git), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cwd=str(workspace),
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
                env=_child_environment(),
            )
            if getattr(completed, "returncode", 1) != 0:
                return None
            output.seek(0, os.SEEK_END)
            if output.tell() > MAX_GIT_STATUS_BYTES:
                return None
            output.seek(0)
            raw = output.read()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    snapshot = _parse_git_status(raw)
    if snapshot is None:
        return None
    entries: dict[str, object] = {}
    for path, status in snapshot.entries.items():
        try:
            file_stat = (workspace / path).lstat()
            # ponytail: metadata fingerprints avoid hashing a potentially huge dirty
            # tree; add bounded content hashes only if timestamp-preserving writers
            # become part of the threat model.
            fingerprint = (
                file_stat.st_mode,
                file_stat.st_size,
                file_stat.st_mtime_ns,
                file_stat.st_ctime_ns,
            )
        except OSError:
            fingerprint = None
        entries[path] = (status, fingerprint)
    return GitStatusSnapshot(entries)


def _postflight_info(
    before: GitStatusSnapshot | None,
    after: GitStatusSnapshot | None,
    *,
    execution_failed: bool,
    mode: str,
) -> PostflightInfo:
    if mode != "accept-edits":
        return PostflightInfo(None, False, [], True, False)
    if before is None or after is None:
        return PostflightInfo(
            None if before is None else bool(before.entries),
            None,
            [],
            False,
            True,
        )
    paths = sorted(
        path for path in set(before.entries) | set(after.entries)
        if before.entries.get(path) != after.entries.get(path)
    )
    changed = bool(paths)
    return PostflightInfo(
        bool(before.entries),
        changed,
        paths,
        True,
        mode == "accept-edits" and changed and execution_failed,
    )


def _review_marker_path(workspace: Path) -> Path:
    return _workspace_lock_path(workspace).with_suffix(".review")


def _prepare_review_marker(workspace: Path, acknowledge_review: bool) -> Path:
    try:
        marker = _review_marker_path(workspace)
        if marker.exists():
            if not acknowledge_review:
                raise ReviewStateError("review_required")
            marker.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(marker, flags | no_follow, 0o600)
        try:
            payload = b"review-required\n"
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("review marker write failed")
                view = view[written:]
        finally:
            os.close(fd)
        return marker
    except ReviewStateError:
        raise
    except (OSError, UnicodeError, WorkspaceLockError):
        raise ReviewStateError("review_state_unavailable") from None


def _clear_review_marker(marker: Path) -> bool:
    try:
        marker.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cli_result_failed_for_review(
    cli_result: object, expected_marker: str | None = None
) -> bool:
    if isinstance(cli_result, CliRunResult):
        returncode, stdout, timed_out = (
            cli_result.returncode,
            cli_result.stdout,
            cli_result.timed_out,
        )
        if cli_result.command_line_too_long or cli_result.output_limit:
            return True
    else:
        try:
            returncode, stdout, timed_out = cli_result  # type: ignore[misc]
        except (TypeError, ValueError):
            return True
    if timed_out or returncode != 0:
        return True
    try:
        payload = json.loads(str(stdout).strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        return True
    response = payload.get("response", "")
    return not (
        isinstance(response, str)
        and bool(response.strip())
        and (expected_marker is None or expected_marker in response)
    )


def _probe_cli_version(cli: Path) -> str | None:
    completed = _run_bounded_probe(
        [str(cli), "--version"],
        cwd=cli.parent,
        timeout_seconds=5,
        stdout_limit=MAX_VERSION_BYTES,
        stderr_limit=MAX_PROBE_STDERR_BYTES,
    )
    if (
        completed.returncode != 0
        or completed.timed_out
        or completed.output_limit
    ):
        return None
    try:
        version = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return version if _VERSION_RE.fullmatch(version) else None


def _probe_state_writable() -> bool:
    """Check wrapper state writes without returning or logging its path."""
    try:
        directory = prepare_state_dir()
        with tempfile.TemporaryFile(dir=directory):
            pass
        return True
    except (OSError, TypeError, ValueError):
        return False


def _prompt(task: str, context: str, verification: str) -> str:
    return (
        "You are a coding subagent operating in the current Git repository.\n"
        "Complete the requested task, make the smallest safe changes, and run "
        "the requested verification. Do not disclose credentials or secrets. "
        "Do not use MCP, plugins, subagents, network access, destructive commands, "
        "git commit, or git push.\n\n"
        f"TASK:\n{task}\n\n"
        f"CONTEXT:\n{context}\n\n"
        f"VERIFICATION:\n{verification}"
    )


def _child_environment() -> dict[str, str]:
    """Keep normal CLI/keyring settings, but prevent accidental API-key use."""
    return {
        name: value
        for name, value in os.environ.items()
        if not _SENSITIVE_ENV_NAME.search(name)
        and name != "GOOGLE_APPLICATION_CREDENTIALS"
    }


def _create_windows_job(pid: int) -> wintypes.HANDLE | None:
    if os.name != "nt":
        return None
    job: wintypes.HANDLE | None = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        set_info = kernel32.SetInformationJobObject
        set_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        job = create_job(None, None)
        if not job:
            return None
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not set_info(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            close_handle(job)
            return None
        process_handle = open_process(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process_handle:
            close_handle(job)
            return None
        try:
            if not assign(job, process_handle):
                close_handle(job)
                return None
        finally:
            close_handle(process_handle)
        return job
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        _close_windows_job(job)
        return None


def _close_windows_job(job: wintypes.HANDLE | None) -> None:
    if job is None or os.name != "nt":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(job)
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        pass


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        try:
            system_root = (
                os.environ.get("SYSTEMROOT") or os.environ.get("SystemRoot", "")
            ).strip()
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            if not system_root or not taskkill.is_absolute() or not taskkill.is_file():
                raise OSError("taskkill unavailable")
            completed = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                shell=False,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    if process.returncode is None:
        try:
            process.kill()
        except OSError:
            pass


async def _finish_killed_process(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bool, bytes]],
) -> None:
    """Best-effort reap/close for a child whose tree was forcibly killed."""
    try:
        await asyncio.wait_for(asyncio.shield(communication), timeout=2)
    except Exception:
        if not communication.done():
            communication.cancel()
        try:
            await communication
        except BaseException:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except (asyncio.TimeoutError, OSError):
        pass
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()


async def _read_bounded(
    stream: asyncio.StreamReader | None, limit: int
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    size = 0
    while True:
        data = await stream.read(min(65_536, limit - size + 1))
        if not data:
            return b"".join(chunks), False
        if size + len(data) > limit:
            return b"".join(chunks), True
        chunks.append(data)
        size += len(data)


async def _collect_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bool, bytes]:
    """Drain both pipes with fixed bounds while the process is running."""
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, MAX_STDOUT_CHARS))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, MAX_STDERR_CHARS))
    wait_task = asyncio.create_task(process.wait())
    pending = {stdout_task, stderr_task, wait_task}
    stdout = b""
    stderr = b""
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is wait_task:
                    continue
                data, exceeded = task.result()
                if task is stdout_task:
                    stdout = data
                    if exceeded:
                        return stdout, True, stderr
                else:
                    stderr = data
                    if exceeded:
                        return stdout, True, stderr
            if wait_task in done:
                await wait_task
                stdout, stdout_exceeded = await stdout_task
                stderr, stderr_exceeded = await stderr_task
                return stdout, stdout_exceeded or stderr_exceeded, stderr
        return stdout, False, stderr
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _run_cli(
    argv: list[str], cwd: Path, timeout_seconds: float | None = None
) -> CliRunResult:
    if os.name == "nt":
        command_line = subprocess.list2cmdline(argv)
        if len(command_line.encode("utf-16-le")) // 2 >= MAX_WINDOWS_COMMAND_LINE_UNITS:
            return CliRunResult(None, "", False, command_line_too_long=True)
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": _child_environment(),
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
    except (OSError, ValueError):
        return CliRunResult(None, "", False)

    job = _create_windows_job(process.pid)
    communication = asyncio.create_task(_collect_output(process))
    try:
        # Shield the pipe-draining task: cancelling it on Windows can leave
        # Proactor pipe transports behind after taskkill.
        stdout, exceeded, stderr = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds or _timeout_seconds()
        )
        if exceeded:
            _close_windows_job(job)
            job = None
            await asyncio.to_thread(_kill_process_tree, process)
            await _finish_killed_process(process, communication)
            # A pipe exceeded its bound. Discard both raw streams instead of
            # retaining an incomplete diagnostic that may contain secrets.
            return CliRunResult(process.returncode, "", False, output_limit=True)
        return CliRunResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            False,
            stderr=stderr.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        _close_windows_job(job)
        job = None
        await asyncio.to_thread(_kill_process_tree, process)
        try:
            stdout, _exceeded, stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout=10
            )
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
            await _finish_killed_process(process, communication)
            # Reader failure means no trustworthy bounded diagnostic is
            # available; preserve the typed timeout and keep stderr empty.
            return CliRunResult(process.returncode, "", True)
        await _finish_killed_process(process, communication)
        return CliRunResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            True,
            stderr=stderr.decode("utf-8", errors="replace"),
        )
    except asyncio.CancelledError:
        _close_windows_job(job)
        job = None
        await asyncio.to_thread(_kill_process_tree, process)
        await _finish_killed_process(process, communication)
        raise
    except Exception:
        _close_windows_job(job)
        job = None
        await asyncio.to_thread(_kill_process_tree, process)
        await _finish_killed_process(process, communication)
        return CliRunResult(process.returncode, "", False)
    finally:
        _close_windows_job(job)


def _usage(payload: dict[str, Any]) -> dict[str, int | float]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, int | float] = {}
    for field in _USAGE_FIELDS:
        value = raw.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if finite and value >= 0:
            clean[field] = value
    return clean


def _usage_info(payload: dict[str, Any]) -> tuple[dict[str, int | float], bool]:
    raw = payload.get("usage")
    return _usage(payload), isinstance(raw, dict)


def _conversation_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("conversation_id")
    if isinstance(value, str) and _SAFE_CONVERSATION_ID.fullmatch(value):
        return value
    return None


def _conversation_id_info(payload: dict[str, Any]) -> tuple[str | None, bool]:
    value = payload.get("conversation_id")
    conversation_id = _conversation_id(payload)
    return conversation_id, conversation_id is not None and isinstance(value, str)


def _input_conversation_id(value: object) -> str | None:
    if not isinstance(value, str) or not _INPUT_CONVERSATION_ID.fullmatch(value):
        return None
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value


def _prepare_execution(
    *,
    task: object,
    context: object,
    verification: object,
    working_directory: object,
    thinking_level: object,
    mode: object,
    acknowledge_review: object,
    conversation_id: object,
    expected_marker: object,
    payload_mode: object,
    run_info: RunInfo,
) -> tuple[PreparedExecution | None, dict[str, Any] | None]:
    def invalid(
        message: str,
        level: str | None = cast(str | None, thinking_level),
        selected_mode: str | None = cast(str | None, mode),
    ) -> tuple[None, dict[str, Any]]:
        return None, _empty_result(
            "ERROR", message, level, selected_mode,
            error_type="invalid_request", run_info=run_info,
        )

    if not isinstance(task, str) or not task.strip():
        return invalid("task must be a non-empty string")
    if not isinstance(context, str) or not isinstance(verification, str):
        return invalid("context and verification must be strings")
    if not isinstance(working_directory, str):
        return invalid("working_directory must be an existing Git root")
    if thinking_level not in THINKING_LEVELS:
        return invalid(
            "thinking_level must be low, medium, or high", None,
            cast(str | None, mode),
        )
    if mode not in MODES:
        return invalid(
            "mode must be plan or accept-edits", cast(str, thinking_level), None,
        )
    if payload_mode not in ("workspace", "prompt_only", "scoped_files"):
        return invalid("payload_mode must be workspace, prompt_only, or scoped_files")
    if payload_mode != "workspace":
        return None, _empty_result(
            "ERROR", "Requested payload scope cannot be enforced by this CLI",
            cast(str, thinking_level), cast(str, mode),
            error_type="scope_enforcement_unavailable", run_info=run_info,
            payload_mode=cast(PayloadMode, payload_mode),
        )
    if not isinstance(acknowledge_review, bool):
        return invalid("acknowledge_review must be a boolean")
    if expected_marker is not None and (
        not isinstance(expected_marker, str)
        or not expected_marker
        or len(expected_marker) > 256
    ):
        return invalid("expected_marker must be a non-empty string up to 256 characters")
    normalized_conversation_id: str | None = None
    if conversation_id is not None:
        normalized_conversation_id = _input_conversation_id(conversation_id)
        if normalized_conversation_id is None:
            return invalid("conversation_id must be a UUID")

    prompt = _prompt(task.strip(), context.strip(), verification.strip())
    if len(prompt) > MAX_PROMPT_CHARS:
        return invalid("task context is too large")

    preflight = _git_preflight(working_directory or None)
    workspace = preflight.root
    if workspace is None:
        message = (
            "working_directory must be an existing Git root"
            if working_directory
            else "current working directory must be a Git root"
        )
        return None, _empty_result(
            "ERROR", message, cast(str, thinking_level), cast(str, mode),
            error_type=preflight.error_type or "workspace_not_git",
            run_info=run_info,
        )

    return PreparedExecution(
        workspace=workspace,
        prompt=prompt,
        thinking_level=cast(ThinkingLevel, thinking_level),
        mode=cast(Mode, mode),
        acknowledge_review=acknowledge_review,
        conversation_id=normalized_conversation_id,
        expected_marker=cast(str | None, expected_marker),
        payload_mode=cast(PayloadMode, payload_mode),
    ), None


def _success_result(
    payload: dict[str, Any], level: str, mode: str, *,
    run_info: RunInfo, cli_version: str | None, exit_code: int | None,
    expected_marker: str | None = None,
) -> dict[str, Any]:
    response = payload.get("response", "")
    usage, usage_available = _usage_info(payload)
    conversation_id, conversation_id_available = _conversation_id_info(payload)
    if not isinstance(response, str):
        return _empty_result(
            "ERROR", "Antigravity CLI returned an invalid response", level, mode,
            error_type="invalid_response", run_info=run_info,
            cli_version=cli_version, exit_code=exit_code, usage=usage,
            usage_available=usage_available,
            conversation_id=conversation_id,
            conversation_id_available=conversation_id_available,
        )
    if not response.strip():
        return _empty_result(
            "ERROR", "Antigravity CLI returned no content", level, mode,
            error_type="no_content", run_info=run_info,
            cli_version=cli_version, exit_code=exit_code, usage=usage,
            usage_available=usage_available,
            conversation_id=conversation_id,
            conversation_id_available=conversation_id_available,
        )
    if expected_marker is not None and expected_marker not in response:
        return _empty_result(
            "ERROR", "Antigravity CLI response failed verification", level, mode,
            error_type="verification_failed", run_info=run_info,
            cli_version=cli_version, exit_code=exit_code, usage=usage,
            usage_available=usage_available,
            conversation_id=conversation_id,
            conversation_id_available=conversation_id_available,
        )
    return _empty_result(
        "SUCCESS",
        response,
        level,
        mode,
        conversation_id=conversation_id,
        usage=usage,
        run_info=run_info,
        cli_version=cli_version,
        exit_code=exit_code,
        metadata_complete=(
            "response" in payload
            and usage_available
            and conversation_id_available
            and cli_version is not None
        ),
        usage_available=usage_available,
        conversation_id_available=conversation_id_available,
    )


def _build_argv(
    cli: Path,
    workspace: Path,
    prompt: str,
    thinking_level: str,
    mode: str,
    timeout_seconds: int,
    conversation_id: str | None = None,
) -> list[str]:
    argv = [
        str(cli),
        "-p",
        prompt,
        "--mode",
        mode,
        "--model",
        _model_for(thinking_level),
        "--effort",
        thinking_level,
        "--output-format",
        "json",
        "--print-timeout",
        f"{max(1, timeout_seconds - 5)}s",
        "--sandbox",
        "--disable-slash-commands",
        "--add-dir",
        str(workspace),
    ]
    if conversation_id is not None:
        argv.extend(("--conversation", conversation_id))
    return argv


def _safe_exit_code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_CLI_FAILURE_MESSAGES: dict[ErrorType, str] = {
    "profile_unreadable": "Antigravity profile is not readable by the executor",
    "profile_not_writable": "Antigravity profile state is not writable by the executor",
    "network_denied": "Antigravity network access was denied",
    "auth_missing": "Antigravity authentication is unavailable",
    "oauth_timeout": "Antigravity OAuth did not complete",
    "permission_denied": "Antigravity tool permission was denied",
    "policy_denied": "Antigravity payload was denied by policy",
}


def _classify_cli_failure(
    stderr: str,
    payload: dict[str, Any] | None = None,
) -> ErrorType | None:
    """Return an allowlisted final failure class without exposing diagnostics."""
    parts = [stderr]
    if payload is not None:
        for key in ("error_type", "error", "message", "final_status"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                try:
                    parts.append(json.dumps(value, ensure_ascii=False, default=str))
                except (TypeError, ValueError):
                    pass
    diagnostic = "\n".join(parts).lower()
    auth_succeeded = any(marker in diagnostic for marker in (
        "authenticated via keyring", "oauth: authenticated successfully",
        "keyringauth: loaded token", "streamgeneratecontent", "responseid",
    ))

    # Root-cause order is deliberate. Derived auth/OAuth messages often follow
    # an earlier profile or network denial in the same CLI run.
    if any(marker in diagnostic for marker in (
        "approval gate", "approval-review", "policy_denied", "policy denied",
        "blocked_policy", "unacceptable risk",
    )):
        return "policy_denied"

    profile_marker = any(marker in diagnostic for marker in (
        ".gemini", "antigravity-cli", "summary store", "summary_store",
        "crash reporter", "crash output", "profile directory", "profile access",
        "profile_unreadable",
        "profile unreadable", "profile_not_writable", "profile not writable",
    ))
    if profile_marker and any(marker in diagnostic for marker in (
        "profile_not_writable", "profile not writable", "unable to open database file",
        "failed to setup crash output", "summary store recreate failed",
    )):
        return "profile_not_writable"
    if profile_marker and any(marker in diagnostic for marker in (
        "access is denied", "permission denied", "profile_unreadable",
        "profile unreadable", "cannot read", "failed to resolve geminidir",
    )):
        return "profile_unreadable"

    if any(marker in diagnostic for marker in (
        "network_denied", "network denied", "socket access was forbidden",
        "socket in a way forbidden by its access permissions",
        "connectex:", "network is unreachable",
    )):
        return "network_denied"
    if any(marker in diagnostic for marker in (
        "soft-denying tool confirmation", "permission_denied", "permission denied",
        "tool permission was denied", "denied tool",
    )):
        return "permission_denied"
    if not auth_succeeded and any(marker in diagnostic for marker in (
        "oauth_timeout", "oauth timeout", "auth timed out",
        "triggering interactive oauth",
    )):
        return "oauth_timeout"
    if not auth_succeeded and any(marker in diagnostic for marker in (
        "auth_missing", "auth missing", "you are not logged into antigravity",
        "login required", "authentication required",
    )):
        return "auth_missing"
    return None


def _retryable(error_type: ErrorType | None, mode: str | None) -> bool:
    return error_type == "workspace_lock_timeout" and mode == "plan"


async def execute_with_antigravity_cli(
    *,
    workspace: Path,
    prompt: str,
    thinking_level: ThinkingLevel,
    mode: Mode,
    progress: ProgressCallback | None = None,
    run_info: RunInfo | None = None,
    acknowledge_review: bool = False,
    conversation_id: str | None = None,
    expected_marker: str | None = None,
) -> dict[str, Any]:
    """Execute one authenticated CLI process; also used by the live smoke."""
    run = run_info or RunInfo()
    if conversation_id is not None and _input_conversation_id(conversation_id) is None:
        return _empty_result(
            "ERROR", "conversation_id must be a UUID", thinking_level, mode,
            error_type="invalid_request", run_info=run,
        )
    cli = _resolve_cli()
    if cli is None:
        return _empty_result(
            "ERROR", "Antigravity CLI is not installed or unavailable", thinking_level, mode,
            error_type="cli_unavailable", run_info=run,
        )
    cli_version = await asyncio.to_thread(_probe_cli_version, cli)
    timeout_seconds = _timeout_seconds()
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    argv = _build_argv(
        cli, workspace, prompt, thinking_level, mode, timeout_seconds,
        conversation_id,
    )

    async def run_serialized() -> tuple[object, PostflightInfo]:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise WorkspaceLockTimeout
        async with locked_workspace(workspace, remaining):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise WorkspaceLockTimeout
            await _emit_progress(
                progress, f"run_id={run.run_id} state=running"
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise WorkspaceLockTimeout
            before = _git_status_snapshot(workspace) if mode == "accept-edits" else None
            if mode == "accept-edits" and before is None:
                raise ReviewStateError("review_state_unavailable")
            marker = (
                _prepare_review_marker(workspace, acknowledge_review)
                if mode == "accept-edits"
                else None
            )
            cli_result: object | None = None
            postflight: PostflightInfo | None = None
            execution_failed = True
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise WorkspaceLockTimeout
                cli_result = await _run_cli(argv, workspace, remaining)
                execution_failed = _cli_result_failed_for_review(
                    cli_result, expected_marker
                )
            finally:
                after = _git_status_snapshot(workspace) if mode == "accept-edits" else None
                postflight = _postflight_info(
                    before,
                    after,
                    execution_failed=execution_failed,
                    mode=mode,
                )
                if marker is not None and postflight.postflight_complete and not postflight.requires_review:
                    if not _clear_review_marker(marker):
                        postflight = postflight._replace(
                            postflight_complete=False, requires_review=True
                        )
            assert postflight is not None
            assert cli_result is not None
            return cli_result, postflight

    heartbeat: asyncio.Task[None] | None = None
    try:
        await _emit_progress(progress, f"run_id={run.run_id} state=queued")
        if progress is not None:
            heartbeat = asyncio.create_task(
                _progress_heartbeat(progress, run.run_id)
            )
        # The lock and CLI share one deadline.  _run_cli owns its cancellation
        # cleanup, so a second outer wait_for would race the typed lock error.
        cli_result, postflight = await run_serialized()
        if isinstance(cli_result, CliRunResult):
            returncode = cli_result.returncode
            stdout = cli_result.stdout
            timed_out = cli_result.timed_out
            stderr = cli_result.stderr
            command_line_too_long = cli_result.command_line_too_long
            output_limit = cli_result.output_limit
        else:
            # Keep small test/integration fakes compatible with the old triple.
            returncode, stdout, timed_out = cli_result
            stderr = ""
            command_line_too_long = False
            output_limit = False
    except asyncio.TimeoutError:
        logger.warning("Antigravity CLI timed out")
        return _empty_result(
            "ERROR", "Antigravity CLI timed out", thinking_level, mode,
            error_type="timeout", retryable=False, run_info=run,
            cli_version=cli_version,
        )
    except WorkspaceLockTimeout:
        logger.warning("Workspace lock timed out")
        return _empty_result(
            "ERROR", "Workspace lock timed out", thinking_level, mode,
            error_type="workspace_lock_timeout",
            retryable=_retryable("workspace_lock_timeout", mode), run_info=run,
            cli_version=cli_version,
        )
    except WorkspaceLockError:
        logger.warning("Workspace lock could not be acquired")
        return _empty_result(
            "ERROR", "Workspace lock could not be acquired", thinking_level, mode,
            error_type="workspace_lock_unavailable", run_info=run,
            cli_version=cli_version,
        )
    except ReviewStateError as exc:
        message = (
            "Review of previous edits is required before the next edit"
            if exc.error_type == "review_required"
            else "Git review state could not be recorded"
        )
        return _empty_result(
            "ERROR", message, thinking_level, mode,
            error_type=exc.error_type, retryable=False, run_info=run,
            cli_version=cli_version,
            postflight=PostflightInfo(None, None, [], False, True),
        )
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        run.finish()

    if timed_out:
        classified = _classify_cli_failure(stderr)
        error_type = classified or "timeout"
        message = (
            _CLI_FAILURE_MESSAGES[classified]
            if classified in _CLI_FAILURE_MESSAGES
            else "Antigravity CLI timed out"
        )
        logger.warning("Antigravity CLI failed (%s)", error_type)
        return _empty_result(
            "ERROR", message, thinking_level, mode,
            error_type=error_type, run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            postflight=postflight,
        )
    if returncode is None:
        error_type: ErrorType = (
            "command_line_too_long"
            if command_line_too_long
            else "spawn_failed"
        )
        message = (
            "Antigravity CLI command line is too long"
            if error_type == "command_line_too_long"
            else "Antigravity CLI could not be started"
        )
        return _empty_result(
            "ERROR", message, thinking_level, mode, error_type=error_type,
            run_info=run, cli_version=cli_version, postflight=postflight,
        )
    if len(stdout) > MAX_STDOUT_CHARS or output_limit:
        logger.warning("Antigravity CLI response exceeded the wrapper limit")
        return _empty_result(
            "ERROR", "Antigravity CLI response was too large", thinking_level, mode,
            error_type="output_limit", run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            postflight=postflight,
        )
    if returncode != 0:
        payload_for_classification: dict[str, Any] | None = None
        try:
            parsed_failure = json.loads(stdout.strip())
            if isinstance(parsed_failure, dict):
                payload_for_classification = parsed_failure
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        classified = _classify_cli_failure(stderr, payload_for_classification)
        usage, usage_available = (
            _usage_info(payload_for_classification)
            if payload_for_classification is not None
            else ({}, False)
        )
        error_type = classified or "cli_error"
        message = (
            _CLI_FAILURE_MESSAGES[classified]
            if classified in _CLI_FAILURE_MESSAGES
            else "Antigravity CLI task failed"
        )
        logger.warning("Antigravity CLI task failed (%s)", error_type)
        return _empty_result(
            "ERROR", message, thinking_level, mode,
            error_type=error_type, run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            usage=usage, usage_available=usage_available,
            postflight=postflight,
        )
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Antigravity CLI returned invalid JSON")
        return _empty_result(
            "ERROR", "Antigravity CLI returned invalid JSON", thinking_level, mode,
            error_type="invalid_json", run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            postflight=postflight,
        )
    if not isinstance(payload, dict):
        return _empty_result(
            "ERROR", "Antigravity CLI returned an invalid response", thinking_level, mode,
            error_type="invalid_payload", run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            postflight=postflight,
        )
    if payload.get("status") != "SUCCESS":
        usage, usage_available = _usage_info(payload)
        classified = _classify_cli_failure(stderr, payload)
        error_type = classified or "cli_error"
        message = (
            _CLI_FAILURE_MESSAGES[classified]
            if classified in _CLI_FAILURE_MESSAGES
            else "Antigravity CLI task failed"
        )
        logger.warning("Antigravity CLI task failed (%s)", error_type)
        return _empty_result(
            "ERROR", message, thinking_level, mode,
            error_type=error_type, run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            usage=usage, usage_available=usage_available,
            postflight=postflight,
        )
    return _success_result(
        payload, thinking_level, mode, run_info=run,
        cli_version=cli_version, exit_code=_safe_exit_code(returncode),
        expected_marker=expected_marker,
    ) | {
        "preexisting_dirty": postflight.preexisting_dirty,
        "worktree_changed": postflight.worktree_changed,
        "changed_paths": postflight.changed_paths,
        "postflight_complete": postflight.postflight_complete,
        "requires_review": postflight.requires_review,
    }


async def _run_managed_agent(agent_id: str, prepared: PreparedExecution) -> None:
    store = _get_agent_store()
    execution: asyncio.Task[dict[str, Any]] | None = None
    try:
        snapshot = store.get(agent_id)
        if snapshot is None:
            return
        if snapshot.cancel_requested:
            store.finish(agent_id, "interrupted", manager_error="cancelled")
            return
        snapshot = store.mark_running(agent_id)
        if snapshot is None or snapshot.status != "running":
            return
        execution = asyncio.create_task(
            execute_with_antigravity_cli(
                workspace=prepared.workspace,
                prompt=prepared.prompt,
                thinking_level=prepared.thinking_level,
                mode=prepared.mode,
                acknowledge_review=prepared.acknowledge_review,
                conversation_id=prepared.conversation_id,
                expected_marker=prepared.expected_marker,
            )
        )
        while not execution.done():
            await asyncio.wait({execution}, timeout=0.25)
            if store.cancel_requested(agent_id):
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                store.finish(agent_id, "interrupted", manager_error="cancelled")
                return
        result = await execution
        status: AgentStatus = (
            "completed" if result.get("status") == "SUCCESS" else "failed"
        )
        store.finish(
            agent_id,
            status,
            output=result,
            conversation_id=_input_conversation_id(result.get("conversation_id")),
            manager_error=(
                cast(str | None, result.get("error_type"))
                if status == "failed"
                else None
            ),
        )
    except asyncio.CancelledError:
        if execution is not None:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        try:
            store.finish(agent_id, "interrupted", manager_error="cancelled")
        except Exception:
            logger.warning("Managed agent cancellation state could not be persisted")
    except Exception:
        logger.warning("Managed agent failed before producing a result")
        try:
            store.finish(agent_id, "failed", manager_error="manager_error")
        except Exception:
            logger.warning("Managed agent failure state could not be persisted")
    finally:
        if _AGENT_TASKS.get(agent_id) is asyncio.current_task():
            _AGENT_TASKS.pop(agent_id, None)


def _forget_managed_agent(agent_id: str, task: asyncio.Task[None]) -> None:
    if _AGENT_TASKS.get(agent_id) is task:
        _AGENT_TASKS.pop(agent_id, None)
    if task.cancelled():
        try:
            _get_agent_store().finish(
                agent_id, "interrupted", manager_error="cancelled"
            )
        except Exception:
            logger.warning("Managed agent cancellation state could not be persisted")


def _schedule_managed_agent(
    prepared: PreparedExecution,
    *,
    parent_agent_id: str | None = None,
) -> AgentSnapshot:
    store = _reconcile_agent_store()
    snapshot = store.create(
        workspace=prepared.workspace,
        thinking_level=prepared.thinking_level,
        mode=prepared.mode,
        parent_agent_id=parent_agent_id,
        conversation_id=prepared.conversation_id,
    )
    task = asyncio.create_task(
        _run_managed_agent(snapshot.agent_id, prepared)
    )
    _AGENT_TASKS[snapshot.agent_id] = task
    task.add_done_callback(
        lambda done, agent_id=snapshot.agent_id: _forget_managed_agent(
            agent_id, done
        )
    )
    return snapshot


def _scoped_agent(agent_id: object) -> tuple[AgentSnapshot | None, AgentOperationOutput | None]:
    normalized = _valid_agent_id(agent_id)
    if normalized is None:
        return None, _agent_operation_result(
            error_type="invalid_request",
            message="agent_id must be a 32-character lowercase hexadecimal ID",
        )
    try:
        snapshot = _reconcile_agent_store().get(normalized)
    except Exception:
        return None, _agent_operation_result(
            error_type="state_unavailable", message="Agent state is unavailable"
        )
    if snapshot is None or not _snapshot_in_scope(snapshot):
        return None, _agent_operation_result(
            error_type="agent_not_found", message="Agent was not found"
        )
    return snapshot, None


@mcp.tool()
async def antigravity_agent_spawn(
    task: Annotated[str, SkipValidation],
    context: Annotated[str, SkipValidation] = "",
    verification: Annotated[str, SkipValidation] = "",
    working_directory: Annotated[str, SkipValidation] = "",
    thinking_level: Annotated[ThinkingLevel, SkipValidation] = "medium",
    mode: Annotated[Mode, SkipValidation] = "plan",
    acknowledge_review: Annotated[bool, SkipValidation] = False,
    expected_marker: Annotated[str | None, SkipValidation] = None,
    payload_mode: Annotated[PayloadMode, SkipValidation] = "workspace",
) -> AgentOperationOutput:
    """Start a durable Antigravity task and immediately return its agent ID."""
    prepared, error = _prepare_execution(
        task=task,
        context=context,
        verification=verification,
        working_directory=working_directory,
        thinking_level=thinking_level,
        mode=mode,
        acknowledge_review=acknowledge_review,
        conversation_id=None,
        expected_marker=expected_marker,
        payload_mode=payload_mode,
        run_info=RunInfo(),
    )
    if prepared is None:
        assert error is not None
        return _agent_operation_result(
            error_type=cast(AgentManagerErrorType, error["error_type"]),
            message=cast(str, error["result"]),
        )
    try:
        return _agent_operation_result(_schedule_managed_agent(prepared))
    except AgentCapacityError:
        return _agent_operation_result(
            error_type="capacity_reached", message="Active agent limit reached"
        )
    except Exception:
        return _agent_operation_result(
            error_type="state_unavailable", message="Agent state is unavailable"
        )


@mcp.tool()
async def antigravity_agent_status(
    agent_id: Annotated[str, SkipValidation],
) -> AgentOperationOutput:
    """Return the durable snapshot and terminal result for one agent."""
    snapshot, error = _scoped_agent(agent_id)
    return error or _agent_operation_result(snapshot)


@mcp.tool()
async def antigravity_agent_list(
    status: Annotated[AgentStatus | None, SkipValidation] = None,
    working_directory: Annotated[str, SkipValidation] = "",
    limit: Annotated[int, SkipValidation] = 20,
) -> AgentListOutput:
    """List recent Antigravity agents in one allowed Git workspace."""
    if status is not None and status not in (
        "queued", "running", "completed", "failed", "interrupted"
    ):
        return _agent_list_result(
            error_type="invalid_request", message="Invalid agent status"
        )
    if not isinstance(working_directory, str) or (
        not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100
    ):
        return _agent_list_result(
            error_type="invalid_request",
            message="working_directory must be a string and limit must be 1..100",
        )
    preflight = _git_preflight(working_directory or None)
    if preflight.root is None:
        return _agent_list_result(
            error_type=cast(
                AgentManagerErrorType,
                preflight.error_type or "workspace_not_git",
            ),
            message="working_directory must be an existing Git root",
        )
    try:
        agents = _reconcile_agent_store().list(
            status=cast(AgentStatus | None, status),
            workspace=preflight.root,
            limit=limit,
        )
        return _agent_list_result(agents)
    except Exception:
        return _agent_list_result(
            error_type="state_unavailable", message="Agent state is unavailable"
        )


@mcp.tool()
async def antigravity_agent_wait(
    agent_id: Annotated[str, SkipValidation],
    timeout_seconds: Annotated[float, SkipValidation] = 30.0,
) -> AgentOperationOutput:
    """Wait up to 60 seconds for an agent to reach a terminal state."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or not 0 <= timeout_seconds <= MAX_AGENT_WAIT_SECONDS
    ):
        return _agent_operation_result(
            error_type="invalid_request", message="timeout_seconds must be 0..60"
        )
    snapshot, error = _scoped_agent(agent_id)
    if error is not None:
        return error
    assert snapshot is not None
    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
    while snapshot.status not in _TERMINAL_AGENT_STATUSES:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return _agent_operation_result(snapshot, wait_timed_out=True)
        await asyncio.sleep(min(0.2, remaining))
        snapshot, error = _scoped_agent(agent_id)
        if error is not None:
            return error
        assert snapshot is not None
    return _agent_operation_result(snapshot)


@mcp.tool()
async def antigravity_agent_interrupt(
    agent_id: Annotated[str, SkipValidation],
) -> AgentOperationOutput:
    """Request idempotent cancellation and stop a locally owned agent now."""
    snapshot, error = _scoped_agent(agent_id)
    if error is not None:
        return error
    assert snapshot is not None
    if snapshot.status in _TERMINAL_AGENT_STATUSES:
        return _agent_operation_result(snapshot)
    try:
        store = _get_agent_store()
        snapshot = store.request_cancel(snapshot.agent_id)
        if snapshot is None:
            return _agent_operation_result(
                error_type="agent_not_found", message="Agent was not found"
            )
        task = _AGENT_TASKS.get(snapshot.agent_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        refreshed = store.finish(
            snapshot.agent_id, "interrupted", manager_error="cancelled"
        )
        return _agent_operation_result(refreshed)
    except Exception:
        return _agent_operation_result(
            error_type="state_unavailable", message="Agent state is unavailable"
        )


@mcp.tool()
async def antigravity_agent_followup(
    agent_id: Annotated[str, SkipValidation],
    task: Annotated[str, SkipValidation],
    context: Annotated[str, SkipValidation] = "",
    verification: Annotated[str, SkipValidation] = "",
    thinking_level: Annotated[ThinkingLevel | None, SkipValidation] = None,
    mode: Annotated[Mode, SkipValidation] = "plan",
    acknowledge_review: Annotated[bool, SkipValidation] = False,
    expected_marker: Annotated[str | None, SkipValidation] = None,
    payload_mode: Annotated[PayloadMode, SkipValidation] = "workspace",
) -> AgentOperationOutput:
    """Continue a terminal agent's authenticated Antigravity conversation."""
    parent, error = _scoped_agent(agent_id)
    if error is not None:
        return error
    assert parent is not None
    if parent.status not in _TERMINAL_AGENT_STATUSES or parent.conversation_id is None:
        return _agent_operation_result(
            error_type="invalid_state",
            message="Follow-up requires a terminal agent with a conversation ID",
        )
    prepared, validation_error = _prepare_execution(
        task=task,
        context=context,
        verification=verification,
        working_directory=parent.workspace,
        thinking_level=(
            parent.thinking_level if thinking_level is None else thinking_level
        ),
        mode=mode,
        acknowledge_review=acknowledge_review,
        conversation_id=parent.conversation_id,
        expected_marker=expected_marker,
        payload_mode=payload_mode,
        run_info=RunInfo(),
    )
    if prepared is None:
        assert validation_error is not None
        return _agent_operation_result(
            error_type=cast(
                AgentManagerErrorType, validation_error["error_type"]
            ),
            message=cast(str, validation_error["result"]),
        )
    try:
        return _agent_operation_result(
            _schedule_managed_agent(
                prepared, parent_agent_id=parent.agent_id
            )
        )
    except AgentCapacityError:
        return _agent_operation_result(
            error_type="capacity_reached", message="Active agent limit reached"
        )
    except Exception:
        return _agent_operation_result(
            error_type="state_unavailable", message="Agent state is unavailable"
        )


@mcp.tool()
async def antigravity_cli_execute(
    task: Annotated[str, SkipValidation],
    context: Annotated[str, SkipValidation] = "",
    verification: Annotated[str, SkipValidation] = "",
    working_directory: Annotated[str, SkipValidation] = "",
    thinking_level: Annotated[ThinkingLevel, SkipValidation] = "medium",
    mode: Annotated[Mode, SkipValidation] = "plan",
    acknowledge_review: Annotated[bool, SkipValidation] = False,
    conversation_id: Annotated[str | None, SkipValidation] = None,
    expected_marker: Annotated[str | None, SkipValidation] = None,
    payload_mode: Annotated[PayloadMode, SkipValidation] = "workspace",
    ctx: Context | None = None,
) -> AntigravityCliOutput:
    """Execute one coding task through the locally authenticated agy CLI."""
    run = RunInfo()
    prepared, error = _prepare_execution(
        task=task,
        context=context,
        verification=verification,
        working_directory=working_directory,
        thinking_level=thinking_level,
        mode=mode,
        acknowledge_review=acknowledge_review,
        conversation_id=conversation_id,
        expected_marker=expected_marker,
        payload_mode=payload_mode,
        run_info=run,
    )
    if prepared is None:
        assert error is not None
        return _tool_result(error)

    progress_count = 0
    progress_lock = asyncio.Lock()

    async def report_progress(message: str) -> None:
        nonlocal progress_count
        if ctx is None:
            return
        async with progress_lock:
            progress_count += 1
            await ctx.report_progress(progress_count, None, message)

    result = await execute_with_antigravity_cli(
        workspace=prepared.workspace,
        prompt=prepared.prompt,
        thinking_level=prepared.thinking_level,
        mode=prepared.mode,
        progress=report_progress if ctx is not None else None,
        run_info=run,
        acknowledge_review=prepared.acknowledge_review,
        conversation_id=prepared.conversation_id,
        expected_marker=prepared.expected_marker,
    )
    return _tool_result(result)


@mcp.tool()
async def antigravity_doctor(
    working_directory: Annotated[str, SkipValidation] = "",
) -> AntigravityDoctorOutput:
    """Run local preflight checks without probing OAuth, keyring, or network."""
    if not isinstance(working_directory, str):
        return AntigravityDoctorOutput(
            checks_passed=False,
            cli_available=False,
            cli_version=None,
            execution_boundary_declared=_execution_boundary_declared(),
            state_writable=False,
            workspace_status="path_not_found",
            auth_probe="unsupported",
            network_probe="not_run",
            oauth_ready="unknown",
            error_type="invalid_request",
        )

    preflight = _git_preflight(working_directory or None)
    workspace_status = cast(
        WorkspaceDiagnostic,
        "ready"
        if preflight.root is not None
        else (preflight.error_type or "workspace_not_git"),
    )
    cli = _resolve_cli()
    cli_version = (
        await asyncio.to_thread(_probe_cli_version, cli) if cli is not None else None
    )
    cli_available = cli is not None and cli_version is not None
    state_writable = await asyncio.to_thread(_probe_state_writable)
    boundary_declared = _execution_boundary_declared()

    error_type: DoctorErrorType | None = None
    if not boundary_declared:
        error_type = "boundary_unverified"
    elif not cli_available:
        error_type = "cli_unavailable"
    elif not state_writable:
        error_type = "state_unavailable"
    elif workspace_status != "ready":
        error_type = cast(DoctorErrorType, workspace_status)

    return AntigravityDoctorOutput(
        checks_passed=error_type is None,
        cli_available=cli_available,
        cli_version=cli_version,
        execution_boundary_declared=boundary_declared,
        state_writable=state_writable,
        workspace_status=workspace_status,
        auth_probe="unsupported",
        network_probe="not_run",
        oauth_ready="unknown",
        error_type=error_type,
    )


async def _redact_tool_validation_error(ctx, call_next):
    result = await call_next(ctx)
    if (
        ctx.method == "tools/call"
        and isinstance(result, dict)
        and result.get("isError") is True
        and "structuredContent" not in result
    ):
        return {
            **result,
            "content": [{"type": "text", "text": "Invalid tool arguments"}],
        }
    return result


mcp.middleware.append(_redact_tool_validation_error)


def _execution_boundary_declared() -> bool:
    return os.environ.get(EXECUTION_BOUNDARY_ENV) == "host"


def _check_execution_boundary() -> bool:
    """Check the operator-declared process boundary without exposing its value."""
    if _execution_boundary_declared():
        return True
    logger.warning(
        "Host execution boundary is not declared; OAuth readiness is unverified"
    )
    return False


def main() -> None:
    _check_execution_boundary()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
