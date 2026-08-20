"""Small MCP stdio adapter for the OAuth-authenticated Antigravity CLI.

The CLI owns authentication (system keyring and Google browser login).  This
wrapper deliberately does not read .env files or API-key environment values.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from ctypes import wintypes

from mcp.server import MCPServer


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
MAX_STDOUT_CHARS = 1_000_000
MAX_STDERR_CHARS = 16_384
MAX_PROMPT_CHARS = 24_000
EXECUTION_LOCK = asyncio.Lock()
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


def _model_for(level: str) -> str:
    return f"gemini-3.7-flash-{level}"


def _empty_result(
    status: str,
    result: str,
    level: str | None,
    mode: str | None,
    conversation_id: str | None = None,
    usage: dict[str, int | float] | None = None,
) -> dict[str, Any]:
    truncated = len(result) > MAX_RESULT_CHARS
    return {
        "status": status,
        "result": result[:MAX_RESULT_CHARS],
        "model": _model_for(level) if level in THINKING_LEVELS else None,
        "thinking_level": level if level in THINKING_LEVELS else None,
        "mode": mode if mode in MODES else None,
        "usage": usage or {},
        "conversation_id": conversation_id,
        "result_truncated": truncated,
    }


def _resolve_cli() -> Path | None:
    configured = os.environ.get("ANTIGRAVITY_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()

    found = shutil.which("agy")
    if found:
        path = Path(found)
        if path.is_file():
            return path.resolve()

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        path = Path(local_app_data) / "agy" / "bin" / "agy.exe"
        if path.is_file():
            return path.resolve()
    return None


def _git_root() -> Path | None:
    """Return cwd only when cwd itself is the repository's top-level root."""
    cwd = Path.cwd().resolve()
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        root = Path(completed.stdout.strip()).resolve()
    except (OSError, ValueError):
        return None
    return cwd if root == cwd else None


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
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
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
        except ProcessLookupError:
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
    argv: list[str], cwd: Path, timeout_seconds: int | None = None
) -> tuple[int | None, str, bool]:
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
        return None, "", False

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
            return process.returncode, "", False
        return process.returncode, stdout.decode("utf-8", errors="replace"), False
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
            except ProcessLookupError:
                pass
            await _finish_killed_process(process, communication)
            return process.returncode, "", True
        await _finish_killed_process(process, communication)
        return process.returncode, stdout.decode("utf-8", errors="replace"), True
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
        return process.returncode, "", False
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
        if finite:
            clean[field] = value
    return clean


def _conversation_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("conversation_id")
    if isinstance(value, str) and _SAFE_CONVERSATION_ID.fullmatch(value):
        return value
    return None


def _success_result(payload: dict[str, Any], level: str, mode: str) -> dict[str, Any]:
    response = payload.get("response", "")
    if not isinstance(response, str):
        return _empty_result(
            "ERROR", "Antigravity CLI returned an invalid response", level, mode
        )
    return _empty_result(
        "SUCCESS",
        response,
        level,
        mode,
        conversation_id=_conversation_id(payload),
        usage=_usage(payload),
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


async def execute_with_antigravity_cli(
    *, workspace: Path, prompt: str, thinking_level: ThinkingLevel, mode: Mode
) -> dict[str, Any]:
    """Execute one authenticated CLI process; also used by the live smoke."""
    cli = _resolve_cli()
    if cli is None:
        return _empty_result(
            "ERROR", "Antigravity CLI is not installed or unavailable", thinking_level, mode
        )
    timeout_seconds = _timeout_seconds()
    argv = _build_argv(
        cli, workspace, prompt, thinking_level, mode, timeout_seconds
    )

    async def run_serialized() -> tuple[int | None, str, bool]:
        async with EXECUTION_LOCK:
            return await _run_cli(argv, workspace, timeout_seconds)

    try:
        returncode, stdout, timed_out = await asyncio.wait_for(
            run_serialized(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.warning("Antigravity CLI timed out")
        return _empty_result("ERROR", "Antigravity CLI timed out", thinking_level, mode)

    if timed_out:
        logger.warning("Antigravity CLI timed out")
        return _empty_result("ERROR", "Antigravity CLI timed out", thinking_level, mode)
    if returncode is None:
        return _empty_result(
            "ERROR", "Antigravity CLI could not be started", thinking_level, mode
        )
    if len(stdout) > MAX_STDOUT_CHARS:
        logger.warning("Antigravity CLI response exceeded the wrapper limit")
        return _empty_result(
            "ERROR", "Antigravity CLI response was too large", thinking_level, mode
        )
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Antigravity CLI returned invalid JSON")
        return _empty_result(
            "ERROR", "Antigravity CLI returned invalid JSON", thinking_level, mode
        )
    if not isinstance(payload, dict):
        return _empty_result(
            "ERROR", "Antigravity CLI returned an invalid response", thinking_level, mode
        )
    if returncode != 0 or payload.get("status") != "SUCCESS":
        logger.warning("Antigravity CLI task failed")
        return _empty_result("ERROR", "Antigravity CLI task failed", thinking_level, mode)
    return _success_result(payload, thinking_level, mode)


@mcp.tool()
async def antigravity_cli_execute(
    task: str,
    context: str = "",
    verification: str = "",
    thinking_level: ThinkingLevel = "medium",
    mode: Mode = "accept-edits",
) -> dict[str, Any]:
    """Execute one coding task through the locally authenticated agy CLI."""
    if not isinstance(task, str) or not task.strip():
        return _empty_result("ERROR", "task must be a non-empty string", thinking_level, mode)
    if not isinstance(context, str) or not isinstance(verification, str):
        return _empty_result("ERROR", "context and verification must be strings", thinking_level, mode)
    if thinking_level not in THINKING_LEVELS:
        return _empty_result("ERROR", "thinking_level must be low, medium, or high", None, mode)
    if mode not in MODES:
        return _empty_result("ERROR", "mode must be plan or accept-edits", thinking_level, None)

    prompt = _prompt(task.strip(), context.strip(), verification.strip())
    if len(prompt) > MAX_PROMPT_CHARS:
        return _empty_result(
            "ERROR", "task context is too large", thinking_level, mode
        )

    workspace = _git_root()
    if workspace is None:
        return _empty_result("ERROR", "current working directory must be a Git root", thinking_level, mode)
    return await execute_with_antigravity_cli(
        workspace=workspace,
        prompt=prompt,
        thinking_level=thinking_level,
        mode=mode,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
