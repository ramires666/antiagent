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
import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, cast

from ctypes import wintypes
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, SkipValidation

from mcp.server import MCPServer
from mcp.server.mcpserver import Context


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("antigravity-cli-mcp")
mcp = MCPServer("Antigravity CLI Executor")

ThinkingLevel = Literal["low", "medium", "high"]
Mode = Literal["plan", "accept-edits"]
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
MAX_PROMPT_CHARS = 24_000
MAX_WINDOWS_COMMAND_LINE_UNITS = 32_767
_LOCK_DIRECTORY = Path(tempfile.gettempdir()) / "antiagent-workspace-locks"
_SAFE_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
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
    "review_required",
    "review_state_unavailable",
]
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


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
        _LOCK_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            directory_stat = _LOCK_DIRECTORY.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise OSError("workspace lock directory is not a directory")
            if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
                raise OSError("workspace lock directory is not private")
        digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()
        return _LOCK_DIRECTORY / f"{digest}.lock"
    except (AttributeError, OSError, TypeError, ValueError):
        # Do not expose a temporary-directory path through the MCP result.
        raise WorkspaceLockError from None


class WorkspaceLock:
    def __init__(self, root: Path):
        self.path = _workspace_lock_path(root)
        self._fd: int | None = None

    def _try_acquire(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise WorkspaceLockError from None
        try:
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
        completed = subprocess.run(
            [str(git), "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
            env=_child_environment(),
        )
    except subprocess.CalledProcessError as exc:
        stderr = str(getattr(exc, "stderr", "") or "").lower()
        if "dubious ownership" in stderr or "safe.directory" in stderr:
            return GitPreflight(None, "git_trust_denied")
        return GitPreflight(None, "workspace_not_git")
    except subprocess.TimeoutExpired:
        return GitPreflight(None, "git_unavailable")
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
        return GitPreflight(None, "git_unavailable")

    if getattr(completed, "returncode", 0) != 0:
        stderr = str(getattr(completed, "stderr", "") or "").lower()
        if "dubious ownership" in stderr or "safe.directory" in stderr:
            return GitPreflight(None, "git_trust_denied")
        return GitPreflight(None, "workspace_not_git")
    try:
        raw_root = completed.stdout.strip()
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
        with tempfile.TemporaryFile() as output:
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


def _cli_result_failed_for_review(cli_result: object) -> bool:
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
    return not (
        isinstance(payload, dict)
        and payload.get("status") == "SUCCESS"
        and isinstance(payload.get("response", ""), str)
    )


def _probe_cli_version(cli: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(cli), "--version"],
            cwd=str(cli.parent),
            env=_child_environment(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    version = completed.stdout.strip()
    return version if _VERSION_RE.fullmatch(version) else None


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
                capture_output=True,
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
        stdout, exceeded, _stderr = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds or _timeout_seconds()
        )
        if exceeded:
            _close_windows_job(job)
            job = None
            await asyncio.to_thread(_kill_process_tree, process)
            await _finish_killed_process(process, communication)
            return CliRunResult(process.returncode, "", False, output_limit=True)
        return CliRunResult(
            process.returncode, stdout.decode("utf-8", errors="replace"), False
        )
    except asyncio.TimeoutError:
        _close_windows_job(job)
        job = None
        await asyncio.to_thread(_kill_process_tree, process)
        try:
            stdout, _exceeded, _stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout=10
            )
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
            await _finish_killed_process(process, communication)
            return CliRunResult(process.returncode, "", True)
        await _finish_killed_process(process, communication)
        return CliRunResult(
            process.returncode, stdout.decode("utf-8", errors="replace"), True
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


def _success_result(
    payload: dict[str, Any], level: str, mode: str, *,
    run_info: RunInfo, cli_version: str | None, exit_code: int | None,
) -> dict[str, Any]:
    response = payload.get("response", "")
    if not isinstance(response, str):
        return _empty_result(
            "ERROR", "Antigravity CLI returned an invalid response", level, mode,
            error_type="invalid_response", run_info=run_info,
            cli_version=cli_version, exit_code=exit_code,
        )
    usage, usage_available = _usage_info(payload)
    conversation_id, conversation_id_available = _conversation_id_info(payload)
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
    return argv


def _safe_exit_code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
) -> dict[str, Any]:
    """Execute one authenticated CLI process; also used by the live smoke."""
    run = run_info or RunInfo()
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
        cli, workspace, prompt, thinking_level, mode, timeout_seconds
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
                execution_failed = _cli_result_failed_for_review(cli_result)
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
            command_line_too_long = cli_result.command_line_too_long
            output_limit = cli_result.output_limit
        else:
            # Keep small test/integration fakes compatible with the old triple.
            returncode, stdout, timed_out = cli_result
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
        logger.warning("Antigravity CLI timed out")
        return _empty_result(
            "ERROR", "Antigravity CLI timed out", thinking_level, mode,
            error_type="timeout", run_info=run, cli_version=cli_version,
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
    if returncode != 0 or payload.get("status") != "SUCCESS":
        logger.warning("Antigravity CLI task failed")
        return _empty_result(
            "ERROR", "Antigravity CLI task failed", thinking_level, mode,
            error_type="cli_error", run_info=run, cli_version=cli_version,
            exit_code=_safe_exit_code(returncode),
            postflight=postflight,
        )
    return _success_result(
        payload, thinking_level, mode, run_info=run,
        cli_version=cli_version, exit_code=_safe_exit_code(returncode),
    ) | {
        "preexisting_dirty": postflight.preexisting_dirty,
        "worktree_changed": postflight.worktree_changed,
        "changed_paths": postflight.changed_paths,
        "postflight_complete": postflight.postflight_complete,
        "requires_review": postflight.requires_review,
    }


@mcp.tool()
async def antigravity_cli_execute(
    task: Annotated[str, SkipValidation],
    context: Annotated[str, SkipValidation] = "",
    verification: Annotated[str, SkipValidation] = "",
    working_directory: Annotated[str, SkipValidation] = "",
    thinking_level: Annotated[ThinkingLevel, SkipValidation] = "medium",
    mode: Annotated[Mode, SkipValidation] = "plan",
    acknowledge_review: Annotated[bool, SkipValidation] = False,
    ctx: Context | None = None,
) -> AntigravityCliOutput:
    """Execute one coding task through the locally authenticated agy CLI."""
    run = RunInfo()
    if not isinstance(task, str) or not task.strip():
        return _tool_result(
            _empty_result(
                "ERROR", "task must be a non-empty string", thinking_level, mode,
                error_type="invalid_request", run_info=run,
            )
        )
    if not isinstance(context, str) or not isinstance(verification, str):
        return _tool_result(
            _empty_result(
                "ERROR", "context and verification must be strings", thinking_level, mode,
                error_type="invalid_request", run_info=run,
            )
        )
    if not isinstance(working_directory, str):
        return _tool_result(
            _empty_result(
                "ERROR", "working_directory must be an existing Git root", thinking_level, mode,
                error_type="invalid_request", run_info=run,
            )
        )
    if thinking_level not in THINKING_LEVELS:
        return _tool_result(
            _empty_result(
                "ERROR", "thinking_level must be low, medium, or high", None, mode,
                error_type="invalid_request", run_info=run,
            )
        )
    if mode not in MODES:
        return _tool_result(
            _empty_result(
                "ERROR", "mode must be plan or accept-edits", thinking_level, None,
                error_type="invalid_request", run_info=run,
            )
        )
    if not isinstance(acknowledge_review, bool):
        return _tool_result(
            _empty_result(
                "ERROR", "acknowledge_review must be a boolean", thinking_level, mode,
                error_type="invalid_request", run_info=run,
            )
        )

    prompt = _prompt(task.strip(), context.strip(), verification.strip())
    if len(prompt) > MAX_PROMPT_CHARS:
        return _tool_result(
            _empty_result(
                "ERROR", "task context is too large", thinking_level, mode,
                error_type="invalid_request", run_info=run,
            )
        )

    preflight = _git_preflight(working_directory or None)
    workspace = preflight.root
    if workspace is None:
        message = (
            "working_directory must be an existing Git root"
            if working_directory
            else "current working directory must be a Git root"
        )
        return _tool_result(
            _empty_result(
                "ERROR", message, thinking_level, mode,
                error_type=preflight.error_type or "workspace_not_git", run_info=run,
            )
        )

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
        workspace=workspace,
        prompt=prompt,
        thinking_level=thinking_level,
        mode=mode,
        progress=report_progress if ctx is not None else None,
        run_info=run,
        acknowledge_review=acknowledge_review,
    )
    return _tool_result(result)


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
