from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Callable, Sequence

from ctypes import wintypes


MCP_IMAGE_NAME = "antiagent-mcp.exe"
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260


class UpgradeError(RuntimeError):
    """Raised before pipx can mutate an installation unsafely."""


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


def _windows_processes() -> list[tuple[str, int]]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot_processes = kernel32.CreateToolhelp32Snapshot
    snapshot_processes.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot_processes.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = snapshot_processes(_TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not process_first(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Process32FirstW failed")
        processes: list[tuple[str, int]] = []
        while True:
            processes.append((entry.szExeFile, int(entry.th32ProcessID)))
            if not process_next(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error not in (0, 18):  # ERROR_NO_MORE_FILES
                    raise OSError(error, "Process32NextW failed")
                break
        return processes
    finally:
        close_handle(snapshot)


def ensure_upgrade_is_safe(
    *,
    platform_name: str | None = None,
    scanner: Callable[[], list[tuple[str, int]]] = _windows_processes,
) -> None:
    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        return
    try:
        active = [
            (name, pid)
            for name, pid in scanner()
            if name.lower() == MCP_IMAGE_NAME
        ]
    except Exception as error:
        raise UpgradeError(
            "Unable to verify whether antiagent-mcp.exe is running"
        ) from error
    if active:
        pids = ", ".join(str(pid) for _, pid in active)
        raise UpgradeError(
            "Antiagent MCP is still running (PID "
            f"{pids}). Close every Codex app/CLI/IDE process before upgrading."
        )


def _project_root(source: str | None = None) -> Path:
    root = Path(source).expanduser() if source else Path(__file__).resolve().parent
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise UpgradeError("Antiagent source directory does not exist") from error
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise UpgradeError("Antiagent source directory must contain pyproject.toml")
    try:
        with pyproject.open("rb") as stream:
            metadata = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise UpgradeError(
            "Antiagent source directory has an invalid pyproject.toml"
        ) from error
    project = metadata.get("project")
    if not isinstance(project, dict) or project.get("name") != "antiagent-mcp":
        raise UpgradeError(
            "Antiagent source pyproject.toml must name project antiagent-mcp"
        )
    required = (
        "antiagent_setup.py",
        "agy_server.py",
        "agent_manager.py",
        "response_diagnostics.py",
        "runtime_identity.py",
    )
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise UpgradeError(
            "Antiagent source directory is incomplete; missing " + ", ".join(missing)
        )
    return root


def upgrade(
    root: Path,
    *,
    dry_run: bool = False,
    register: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    scanner: Callable[[], list[tuple[str, int]]] = _windows_processes,
) -> None:
    ensure_upgrade_is_safe(scanner=scanner)
    install_command = [
        sys.executable,
        "-m",
        "pipx",
        "install",
        "--force",
        str(root),
    ]
    register_command = [sys.executable, str(root / "antiagent_setup.py")]
    if dry_run:
        print(subprocess.list2cmdline(install_command))
        if register:
            print(subprocess.list2cmdline(register_command))
        return

    installed = runner(install_command, cwd=str(root), check=False)
    if installed.returncode != 0:
        raise UpgradeError(f"pipx upgrade failed (exit {installed.returncode})")
    if not register:
        return
    registered = runner(register_command, cwd=str(root), check=False)
    if registered.returncode != 0:
        raise UpgradeError(
            f"Antiagent was upgraded but Codex registration failed (exit {registered.returncode})"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely upgrade the pipx Antiagent installation after proving that "
            "no Windows MCP launcher is active."
        )
    )
    parser.add_argument(
        "--source",
        help="Antiagent checkout root; defaults to the directory containing this module",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Upgrade pipx but do not refresh the Codex MCP registration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check safety and print commands without changing pipx or Codex",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _project_root(args.source)
        upgrade(root, dry_run=args.dry_run, register=not args.no_register)
    except UpgradeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if not args.dry_run:
        if args.no_register:
            print(
                "Antiagent upgraded. Codex registration was not changed. "
                "Restart Codex completely before use."
            )
        else:
            print(
                "Antiagent upgraded and registered. "
                "Restart Codex completely before use."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
